"""The two passes, and the phase that runs them.

Every test is offline, and the fixture proves it rather than asserting it: the
HTTP client raises if it is ever constructed, and the search provider is a
fake that records what it was asked. A test that quietly acquired a network
dependency would fail loudly here, which is the same guard the Phase 1
discovery tests put on `http_client`.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from corpus import discovery as discovery_module
from corpus.budget import SEARCH_COST_PER_QUERY, Budget, BudgetExceeded
from corpus.cache import Cache
from corpus.identity import IdentityCard
from corpus.search.client import SearchClient
from corpus.search.pagefacts import extract_facts, facts_from_snippet
from corpus.search.providers import SearchResult, SearchUsage
from corpus.search.scoring import score_candidate
from corpus.search.verify import (
    COMMON_NAME_CONFLICT_HOSTS,
    RESULTS_PER_QUERY,
    detect_common_name,
    search_for_sources,
)

HAIKU = "claude-haiku-4-5-20251001"


class FakeSearchProvider:
    """A search vendor that never leaves the process."""

    name = "fake"

    def __init__(self, results: dict[str, list[SearchResult]] | None = None) -> None:
        self.results = results or {}
        self.queries: list[str] = []
        self.model = HAIKU
        self.last_usage = SearchUsage(model=HAIKU)
        self.closed = False

    def search(self, query: str, limit: int) -> list[SearchResult]:
        self.queries.append(query)
        self.last_usage = SearchUsage(searches=1, input_tokens=1_000, output_tokens=20, model=HAIKU)
        return list(self.results.get(query, []))[:limit]

    def close(self) -> None:
        self.closed = True


class EverythingProvider(FakeSearchProvider):
    """Returns the same hits for whatever it is asked, so a test can talk
    about candidates without caring which query found them."""

    def __init__(self, hits: list[SearchResult]) -> None:
        super().__init__()
        self.hits = hits

    def search(self, query: str, limit: int) -> list[SearchResult]:
        self.queries.append(query)
        self.last_usage = SearchUsage(searches=1, input_tokens=1_000, output_tokens=20, model=HAIKU)
        return self.hits[:limit] if len(self.queries) == 1 else []


class BatchProvider(FakeSearchProvider):
    """Hands each query the next batch of hits.

    `RESULTS_PER_QUERY` caps what one search returns, so a test that needs more
    candidates than that has to spread them over several queries — which is
    also how a real run accumulates them.
    """

    def __init__(self, batches: list[list[SearchResult]]) -> None:
        super().__init__()
        self.batches = list(batches)

    def search(self, query: str, limit: int) -> list[SearchResult]:
        self.queries.append(query)
        self.last_usage = SearchUsage(searches=1, input_tokens=1_000, output_tokens=20, model=HAIKU)
        batch = self.batches.pop(0) if self.batches else []
        return batch[:limit]


@pytest.fixture()
def cache(tmp_path: Path) -> Any:
    c = Cache(path=tmp_path / "cache.db")
    yield c
    c.close()


@pytest.fixture(autouse=True)
def no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """Verification is offline in tests, and the test fails loudly if it is not."""

    def forbidden(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("the verification pass opened an HTTP client during a test")

    monkeypatch.setattr(discovery_module, "http_client", forbidden)


def card(**kwargs: object) -> IdentityCard:
    base: dict[str, object] = {
        "key": "jane",
        "name": "Jane Smith",
        "employer": "Acme Corp",
        "anchors": {"github": "jsmith"},
    }
    base.update(kwargs)
    return IdentityCard(**base)  # type: ignore[arg-type]


def seed(cache: Cache, url: str, body: str) -> None:
    cache.put("discovery", f"get:{url}", body)


def client_for(provider: FakeSearchProvider, cache: Cache, limit: float = 10.0) -> SearchClient:
    return SearchClient(provider, cache, Budget(limit=limit), log=lambda _m: None)


def hit(url: str, snippet: str = "Jane Smith", title: str = "") -> SearchResult:
    return SearchResult(url=url, title=title, snippet=snippet)


GOOD_PAGE = (
    '<html><head><meta name="author" content="Jane Smith"></head>'
    "<body><p>Notes on rubrics, written at Acme Corp.</p>"
    '<a href="https://github.com/jsmith">code</a></body></html>'
)


# -- the two passes ---------------------------------------------------------


def test_a_verified_match_is_ingested(cache: Cache) -> None:
    provider = EverythingProvider([hit("https://thinking.example/rubrics")])
    seed(cache, "https://thinking.example/rubrics", GOOD_PAGE)

    result = search_for_sources(card(), cache, client_for(provider, cache), log=lambda _m: None)

    assert [c.url for c in result.candidates] == ["https://thinking.example/rubrics"]
    found = result.candidates[0]
    assert found.verified
    assert found.ingestible
    assert found.score is not None and found.score.attribution == "corroborated"


def test_a_snippet_alone_can_never_ingest_anything(cache: Cache) -> None:
    """The page is never fetched, so the strong signals are never checked.

    A snippet that reads like a perfect match is still a snippet: 150
    characters chosen by a model, on a page nobody has read.
    """
    provider = EverythingProvider(
        [
            hit(
                "https://thinking.example/rubrics",
                snippet="By Jane Smith of Acme Corp, linking to github.com/jsmith",
            )
        ]
    )
    # Nothing seeded, and the HTTP client raises, so the fetch fails.
    result = search_for_sources(card(), cache, client_for(provider, cache), log=lambda _m: None)

    assert result.candidates == []
    assert [c.url for c in result.held] == ["https://thinking.example/rubrics"]
    assert not result.held[0].verified


def test_a_page_that_stops_matching_once_fetched_is_held(cache: Cache) -> None:
    """The whole reason for the second pass. The snippet says one thing; the
    page says the person works somewhere else."""
    url = "https://taxblog.example/post"
    provider = EverythingProvider([hit(url, snippet="Jane Smith, Acme Corp")])
    seed(
        cache,
        url,
        '<html><head><meta name="author" content="Jane Smith"></head><body><p>'
        "Jane Smith is a partner at Beta Industries.</p>"
        '<a href="https://github.com/jsmith">g</a></body></html>',
    )

    result = search_for_sources(card(), cache, client_for(provider, cache), log=lambda _m: None)

    assert result.candidates == []
    held = result.held[0]
    assert held.verified, "it was fetched — that is how the contradiction was found"
    assert held.score is not None
    assert "different_employer" in {n.name for n in held.score.negatives}


def test_a_data_broker_is_rejected_without_spending_a_request(cache: Cache) -> None:
    """No page is fetched, which matters twice over: it saves a request, and
    the request would have been to a people-search site."""
    provider = EverythingProvider([hit("https://rocketreach.co/jane-smith_1234")])

    result = search_for_sources(card(), cache, client_for(provider, cache), log=lambda _m: None)

    assert [c.url for c in result.rejected] == ["https://rocketreach.co/jane-smith_1234"]
    assert result.fetches == 0
    assert result.rejected[0].skipped == "rejected before fetching"


def test_a_page_whose_snippet_says_nothing_is_still_fetched(cache: Cache) -> None:
    """The regression this file exists to prevent a second time.

    An earlier version refused to fetch a candidate whose snippet carried
    neither the name nor a signal — and silently dropped correct sources,
    because a snippet is 150 characters a model chose to quote and a page can
    be unmistakably theirs while its snippet mentions none of it. The query
    already connected the result to the target.
    """
    url = "https://thinking.example/rubrics"
    provider = EverythingProvider(
        [hit(url, snippet="Rubrics beat interviews.", title="On rubrics")]
    )
    seed(cache, url, GOOD_PAGE)

    result = search_for_sources(card(), cache, client_for(provider, cache), log=lambda _m: None)

    assert [c.url for c in result.candidates] == [url], (
        "a corroborated page was dropped because its snippet was uninformative"
    )
    assert result.candidates[0].verified


def test_the_fetch_cap_bounds_the_verification_pass(cache: Cache) -> None:
    """What actually limits the work: a cap on requests, not a guess about
    snippets. Nothing is seeded here, so every candidate needs a real request
    and the cap is what stops the third one."""
    provider = EverythingProvider(
        [hit(f"https://site{i}.example/p", snippet="Jane Smith") for i in range(5)]
    )

    result = search_for_sources(
        card(), cache, client_for(provider, cache), max_fetches=2, log=lambda _m: None
    )

    assert result.fetches == 2
    assert any("fetch cap" in e for e in result.errors)
    assert len(result.held) == 5, "everything unread is held, never quietly dropped"
    assert all(not c.verified for c in result.held)


def test_a_cached_page_does_not_count_against_the_fetch_cap(cache: Cache) -> None:
    """A cache hit costs nothing, so it is not what the cap is protecting."""
    provider = EverythingProvider(
        [hit(f"https://site{i}.example/p", snippet="Jane Smith") for i in range(5)]
    )
    for i in range(5):
        seed(cache, f"https://site{i}.example/p", "<html><body><p>hello</p></body></html>")

    result = search_for_sources(
        card(), cache, client_for(provider, cache), max_fetches=2, log=lambda _m: None
    )

    assert result.fetches == 0
    assert result.verified_count == 5


def test_a_source_phase_one_already_found_is_corroboration_not_a_new_source(
    cache: Cache,
) -> None:
    url = "https://janesmith.com/feed"
    provider = EverythingProvider([hit(url)])

    result = search_for_sources(
        card(),
        cache,
        client_for(provider, cache),
        known_urls={url},
        log=lambda _m: None,
    )

    assert result.candidates == []
    assert result.held == []
    assert any("already known" in s for s in result.identity_signals)


def test_a_page_about_them_is_kept_apart_from_the_corpus(cache: Cache) -> None:
    url = "https://magazine.example/features/jane"
    provider = EverythingProvider([hit(url)])
    seed(
        cache,
        url,
        '<html><head><meta name="author" content="Bob Reporter"></head>'
        "<body><p>Jane Smith, of Acme Corp, met me at a cafe.</p></body></html>",
    )

    result = search_for_sources(card(), cache, client_for(provider, cache), log=lambda _m: None)

    assert [c.url for c in result.context] == [url]
    assert result.candidates == []
    assert not any(c.ingestible for c in result.everything)


# -- billing ----------------------------------------------------------------


def test_every_search_is_billed_for_what_it_actually_did(cache: Cache) -> None:
    provider = FakeSearchProvider()
    budget = Budget(limit=10.0)
    client = SearchClient(provider, cache, budget, log=lambda _m: None)

    client.search("a query", 5)

    assert budget.total_for("search") == pytest.approx(SEARCH_COST_PER_QUERY)
    assert budget.total_for("anthropic") > 0
    assert client.searches_run == 1


def test_a_repeated_query_costs_nothing(cache: Cache) -> None:
    """Iterating on the scoring must not cost a dollar a lap."""
    provider = FakeSearchProvider({"a query": [hit("https://x.example/1")]})
    budget = Budget(limit=10.0)
    client = SearchClient(provider, cache, budget, log=lambda _m: None)

    first = client.search("a query", 5)
    spent = budget.total
    second = client.search("a query", 5)

    assert [r.url for r in first] == [r.url for r in second]
    assert provider.queries == ["a query"], "the second call reached the provider"
    assert budget.total == spent
    assert client.cached_searches == 1


def test_a_search_that_cannot_be_reserved_is_refused_before_it_is_made(cache: Cache) -> None:
    provider = FakeSearchProvider()
    budget = Budget(limit=0.001)
    client = SearchClient(provider, cache, budget, log=lambda _m: None)

    with pytest.raises(BudgetExceeded):
        client.search("a query", 5)

    assert provider.queries == [], "the call was made anyway"


def test_offline_never_reaches_the_provider(cache: Cache, tmp_path: Path) -> None:
    offline = Cache(path=tmp_path / "cache.db", offline=True)
    provider = FakeSearchProvider()
    client = SearchClient(provider, offline, Budget(limit=10.0), log=lambda _m: None)

    assert client.search("a query", 5) == []
    assert provider.queries == []
    assert any("offline" in e for e in client.errors)
    offline.close()


# -- the census is never taken before the pages are read ---------------------


def test_a_run_that_scores_candidates_reads_at_least_one_page(cache: Cache) -> None:
    """The regression that made a live run meaningless, stated as an invariant.

    On 2026-08-03 a verification run scored 50 candidates and fetched zero
    pages: a gate between the two passes stopped the phase on snippet evidence
    alone. Every candidate was held for "never fetched", which reads exactly
    like a scorer whose threshold is too strict, and the census that was
    supposed to answer whether the threshold is calibrated could not answer
    anything.

    So: if any candidate survived rejection and a page was readable, a page was
    read. A phase that scores candidates without reading one has failed,
    whatever its outcome mix looks like.
    """
    urls = [f"https://site{i}.example/page" for i in range(RESULTS_PER_QUERY)]
    provider = EverythingProvider([hit(url, snippet="") for url in urls])
    for url in urls:
        seed(cache, url, "<html><body><p>A post by Jane Smith.</p></body></html>")

    result = search_for_sources(card(), cache, client_for(provider, cache), log=lambda _m: None)

    assert result.results_seen == len(urls)
    assert result.reads_attempted == len(urls)
    assert result.verified_count == len(urls)
    assert not result.unread, "candidates were scored without a single page being read"


def test_empty_snippets_do_not_stop_the_phase_before_it_reads_anything(cache: Cache) -> None:
    """The live shape, reproduced: `anthropic_search` returns no snippet text.

    Its results carry `url`, `title`, `page_age` and an opaque blob; the
    snippet is built from model citations, and the search system prompt tells
    the model to write nothing but "done", so there are none. Every candidate
    therefore scores as "the name and nothing else" — which must not be read as
    a name collision, because it is a statement about the vendor.
    """
    own = "https://janesmith.example/essay"
    strangers = [f"https://site{i}.example/page" for i in range(8)]
    provider = BatchProvider(
        [
            [hit(url, snippet="", title="Jane Smith") for url in strangers],
            [hit(own, snippet="", title="On rubrics — Jane Smith")],
        ]
    )
    seed(cache, own, GOOD_PAGE)
    for url in strangers:
        # The title carried the name; the page turns out to be somebody else's.
        # Only the fetch can tell those two apart.
        seed(cache, url, "<html><body><p>Notes from John Doe.</p></body></html>")

    result = search_for_sources(card(), cache, client_for(provider, cache), log=lambda _m: None)

    assert [c.url for c in result.candidates] == [own], (
        "the one page that corroborates on its own content was never read"
    )
    assert result.verified_count == len(strangers) + 1
    assert not result.common_name, (
        "eight snippet-less results are a fact about the search vendor, not about the name"
    )
    assert not any("collision" in n for n in result.notes), (
        "the card names an employer, so the check ran; it simply found no conflict"
    )


# -- common names -----------------------------------------------------------


def fetched_score(url: str, body: str, **card_kwargs: object) -> Any:
    """Score a page the way the verification pass does — from its body."""
    return score_candidate(extract_facts(body, url), card(**card_kwargs))


NAME_ONLY_PAGE = "<html><body><p>A post by John Smith.</p></body></html>"


def test_fame_does_not_look_like_ambiguity() -> None:
    """The Simon Willison misfire, pinned from the passing side.

    Forty distinct domains all matching the name, none contradicting the
    card: that is reach, not several people sharing a name. The old
    domain-count trigger fired here and silently held every candidate —
    hardest exactly where search works best.
    """
    scores = [
        fetched_score(
            f"https://site{i}.example/page",
            NAME_ONLY_PAGE,
            name="John Smith",
        )
        for i in range(40)
    ]
    common, conflicts = detect_common_name(scores)

    assert all(s.name_present for s in scores)
    assert not common
    assert conflicts == []


def test_two_pages_at_different_employers_is_ambiguity() -> None:
    """Two independent pages putting the same name at two different
    employers is a collision, and the finding names the conflict."""
    scores = [
        fetched_score(
            "https://taxblog.example/post",
            "<html><body><p>John Smith is a partner at Beta Industries.</p></body></html>",
            name="John Smith",
        ),
        fetched_score(
            "https://clinic.example/staff",
            "<html><body><p>John Smith works at Gamma Labs on dermatology.</p></body></html>",
            name="John Smith",
        ),
    ]
    common, conflicts = detect_common_name(scores)

    assert common
    assert len(conflicts) == COMMON_NAME_CONFLICT_HOSTS
    joined = " ".join(conflicts)
    assert "Beta Industries" in joined and "Gamma Labs" in joined, (
        "the finding must name the actual conflict, not just declare one"
    )
    assert "taxblog.example" in joined and "clinic.example" in joined


def test_a_name_that_looks_common_only_because_nothing_was_read_is_not_one() -> None:
    """Snippet scores carry no page, so they can neither confirm nor
    contradict anything. Counting them is how a run once concluded that a
    researcher's own arXiv, ACM, OpenReview and Semantic Scholar profiles
    were four different people."""
    scores = [
        score_candidate(
            facts_from_snippet(
                f"https://site{i}.example/page", "John Smith", "John Smith of Beta Industries"
            ),
            card(name="John Smith"),
        )
        for i in range(16)
    ]
    common, conflicts = detect_common_name(scores)

    assert not common
    assert conflicts == [], "an unread page is not evidence of anything, in either direction"


def test_conflicts_on_one_host_are_one_conflicting_source() -> None:
    """Ten contradicting pages on one site is one publication being wrong
    (or covering someone else) — not independent corroboration of a
    collision."""
    scores = [
        fetched_score(
            f"https://news.example/{i}",
            "<html><body><p>John Smith is a partner at Beta Industries.</p></body></html>",
            name="John Smith",
        )
        for i in range(10)
    ]
    common, conflicts = detect_common_name(scores)

    assert not common
    assert conflicts == []


def test_a_card_with_nothing_to_contradict_declines_rather_than_counts(cache: Cache) -> None:
    """No employer, no role, no location: contradiction cannot be
    established, and the check must not fall back to counting domains. It
    declines, and says so."""
    urls = [f"https://site{i}.example/page" for i in range(RESULTS_PER_QUERY)]
    provider = EverythingProvider([hit(url, snippet="John Smith wrote this.") for url in urls])
    for url in urls:
        seed(cache, url, NAME_ONLY_PAGE)
    subject = IdentityCard(key="john", name="John Smith", anchors={"github": "jsmith"})

    result = search_for_sources(subject, cache, client_for(provider, cache), log=lambda _m: None)

    assert not result.common_name, "domain count alone must never trigger the refusal"
    assert any("collision check did not run" in n for n in result.notes)


CONFLICT_PAGES = {
    "https://taxblog.example/post": (
        "<html><body><p>John Smith is a partner at Beta Industries.</p></body></html>"
    ),
    "https://clinic.example/staff": (
        "<html><body><p>John Smith works at Gamma Labs on dermatology.</p></body></html>"
    ),
    "https://blog.example/notes": (
        "<html><body><p>A post by John Smith about rubrics at Acme Corp.</p></body></html>"
    ),
}


def test_a_real_collision_ingests_nothing_and_names_the_conflict(cache: Cache) -> None:
    """The refusal is made on evidence, the evidence is kept, and the note
    says what was actually found rather than "the name is too common"."""
    provider = EverythingProvider([hit(url, snippet="John Smith") for url in CONFLICT_PAGES])
    for url, body in CONFLICT_PAGES.items():
        seed(cache, url, body)
    subject = IdentityCard(
        key="john", name="John Smith", employer="Acme Corp", anchors={"github": "jsmith"}
    )

    result = search_for_sources(subject, cache, client_for(provider, cache), log=lambda _m: None)

    assert result.common_name
    assert result.candidates == [], "nothing is ingested once the name is known to be ambiguous"
    assert len(result.held) == result.results_seen == len(CONFLICT_PAGES), (
        "every candidate seen must be held for a human, not silently dropped"
    )
    assert all(c.verified for c in result.held), (
        "the pages that proved the collision are kept, so the next run need not refetch them"
    )
    conflict_note = next(n for n in result.notes if "conflicting identity facts" in n)
    assert "Beta Industries" in conflict_note and "Gamma Labs" in conflict_note


def test_the_collision_stop_is_logged_and_not_only_recorded(cache: Cache) -> None:
    """It was silent in `verify_search_contract.py` for a whole live run."""
    provider = EverythingProvider([hit(url, snippet="John Smith") for url in CONFLICT_PAGES])
    for url, body in CONFLICT_PAGES.items():
        seed(cache, url, body)
    subject = IdentityCard(
        key="john", name="John Smith", employer="Acme Corp", anchors={"github": "jsmith"}
    )
    logged: list[str] = []

    search_for_sources(subject, cache, client_for(provider, cache), log=logged.append)

    assert any("conflicting identity facts" in line for line in logged)


def test_a_run_that_could_not_read_a_page_says_so_in_its_own_notes(cache: Cache) -> None:
    """`unread` is the flag anything reporting on the phase has to check first,
    so the phase states it rather than leaving it to be inferred."""
    provider = EverythingProvider(
        [hit(f"https://site{i}.example/p", snippet="Jane Smith") for i in range(3)]
    )

    # Nothing seeded and the HTTP client raises, so every fetch fails.
    result = search_for_sources(card(), cache, client_for(provider, cache), log=lambda _m: None)

    assert result.reads_attempted == 3
    assert result.verified_count == 0
    assert result.unread
    assert any("fetch failure" in n for n in result.notes)


# -- the phase's own guards -------------------------------------------------


def test_a_card_with_nothing_to_search_on_runs_no_queries(cache: Cache) -> None:
    provider = FakeSearchProvider()
    subject = IdentityCard(key="x", name="", anchors={"site": "https://a.example"})

    result = search_for_sources(subject, cache, client_for(provider, cache), log=lambda _m: None)

    assert provider.queries == []
    assert result.queries == []
    assert any("no searchable query" in n for n in result.notes)


def test_the_search_cap_is_honoured(cache: Cache) -> None:
    provider = FakeSearchProvider()

    search_for_sources(
        card(role="VP Engineering"),
        cache,
        client_for(provider, cache),
        max_searches=2,
        log=lambda _m: None,
    )

    assert len(provider.queries) == 2


def test_the_result_serializes_everything_a_reader_would_need(cache: Cache) -> None:
    import json

    provider = EverythingProvider([hit("https://thinking.example/rubrics")])
    seed(cache, "https://thinking.example/rubrics", GOOD_PAGE)

    payload = search_for_sources(
        card(), cache, client_for(provider, cache), log=lambda _m: None
    ).as_dict()

    assert payload["searches"] >= 1
    assert payload["verified"] >= 1
    assert payload["candidates"][0]["attribution"] == "corroborated"
    assert "signals" in payload["candidates"][0]
    json.dumps(payload)  # it lands in discovery.json, so it has to serialize


# -- find_similar ------------------------------------------------------------


class SimilarProvider(FakeSearchProvider):
    """A vendor with the optional capability, never leaving the process."""

    supports_find_similar = True

    def __init__(
        self,
        results: dict[str, list[SearchResult]] | None = None,
        similar: dict[str, list[SearchResult]] | None = None,
    ) -> None:
        super().__init__(results)
        self.similar = similar or {}
        self.similar_calls: list[str] = []

    def find_similar(self, url: str, limit: int) -> list[SearchResult]:
        self.similar_calls.append(url)
        self.last_usage = SearchUsage(searches=1, input_tokens=0, output_tokens=0)
        return list(self.similar.get(url, []))[:limit]


def test_find_similar_results_join_the_candidate_pool(cache: Cache) -> None:
    """Seeded from the anchors that are the target's own writing, recorded as
    queries with a why, and verified exactly like every keyword hit."""
    provider = SimilarProvider(
        similar={"https://janesmith.com": [hit("https://thinking.example/rubrics")]}
    )
    seed(cache, "https://thinking.example/rubrics", GOOD_PAGE)

    result = search_for_sources(
        card(anchors={"github": "jsmith", "site": "https://janesmith.com"}),
        cache,
        client_for(provider, cache),
        log=lambda _m: None,
    )

    assert provider.similar_calls == ["https://janesmith.com"]
    assert [c.url for c in result.candidates] == ["https://thinking.example/rubrics"]
    assert result.candidates[0].verified
    assert result.candidates[0].query == "more like https://janesmith.com"
    similar_queries = [q for q in result.queries if q.text.startswith("more like ")]
    assert len(similar_queries) == 1 and similar_queries[0].why


def test_a_provider_without_the_capability_is_never_asked_for_similar(cache: Cache) -> None:
    """anthropic_search has no find_similar, and its absence must cost nothing:
    no call, no charge, no error, no phantom query in the record."""
    provider = EverythingProvider([hit("https://thinking.example/rubrics")])
    seed(cache, "https://thinking.example/rubrics", GOOD_PAGE)

    result = search_for_sources(
        card(anchors={"github": "jsmith", "site": "https://janesmith.com"}),
        cache,
        client_for(provider, cache),
        log=lambda _m: None,
    )

    assert not any(q.text.startswith("more like ") for q in result.queries)
    assert result.errors == []
    assert [c.url for c in result.candidates] == ["https://thinking.example/rubrics"]


def test_a_similar_hit_is_a_lead_not_evidence(cache: Cache) -> None:
    """find_similar starts from a page that IS the target's, which is exactly
    why its results must not inherit that trust: neighbours of their writing
    are other people's writing until a fetched page says otherwise."""
    provider = SimilarProvider(
        similar={"https://janesmith.com": [hit("https://stranger.example/post")]}
    )
    # The page fetches fine but attaches nobody's identity to itself.
    seed(cache, "https://stranger.example/post", "<html><body><p>An essay.</p></body></html>")

    result = search_for_sources(
        card(anchors={"github": "jsmith", "site": "https://janesmith.com"}),
        cache,
        client_for(provider, cache),
        log=lambda _m: None,
    )

    assert result.candidates == []
    assert [c.url for c in result.held] == ["https://stranger.example/post"]


def test_a_rich_extract_snippet_still_cannot_corroborate(cache: Cache) -> None:
    """The Exa temptation, pinned. An Exa snippet is a real page extract and
    can carry every signal the scorer knows — name, employer, role, handles —
    and it is still 1,000 characters of unread page. Snippets RANK the fetch
    order; only a score against a FETCHED page can promote. If this test
    breaks, someone has built the gate that silently destroyed coverage once
    already (see verify.py's module docstring)."""
    rich = (
        "By Jane Smith. Jane Smith is VP Engineering at Acme Corp, based in "
        "Seattle. Follow @jsmith on GitHub (github.com/jsmith). "
    ) * 8  # Exa-extract sized, not citation-fragment sized
    provider = EverythingProvider([hit("https://unfetchable.example/essay", snippet=rich)])
    # Nothing seeded and the HTTP client raises, so the page is never read.

    result = search_for_sources(card(), cache, client_for(provider, cache), log=lambda _m: None)

    assert result.candidates == [], "a snippet promoted a candidate no one fetched"
    assert [c.url for c in result.held] == ["https://unfetchable.example/essay"]
    held = result.held[0]
    assert not held.verified
    assert held.score is not None and not held.score.fetched
    assert "snippet" in held.skipped
