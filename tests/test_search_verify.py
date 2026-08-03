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
from corpus.search.pagefacts import facts_from_snippet
from corpus.search.providers import SearchResult, SearchUsage
from corpus.search.scoring import score_candidate
from corpus.search.verify import (
    COMMON_NAME_DOMAIN_THRESHOLD,
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


# -- common names -----------------------------------------------------------


def test_many_unrelated_domains_matching_the_name_stops_the_phase() -> None:
    """A tool that guesses on "John Smith" is a liability. Past the threshold
    it must stop and say so rather than pick."""
    scores = [
        score_candidate(
            facts_from_snippet(f"https://site{i}.example/page", "", "A post by John Smith."),
            card(name="John Smith", employer="", anchors={}),
        )
        for i in range(COMMON_NAME_DOMAIN_THRESHOLD)
    ]
    common, domains = detect_common_name(scores)

    assert all(s.name_present and s.independent_count == 0 for s in scores)
    assert common
    assert domains == COMMON_NAME_DOMAIN_THRESHOLD


def test_many_pages_on_one_domain_is_not_a_common_name() -> None:
    """Ten pages on one news site is one publication writing about one person.
    Counting results rather than domains would call that a name collision."""
    scores = [
        score_candidate(
            facts_from_snippet(f"https://news.example/{i}", "", "A post by John Smith."),
            card(name="John Smith", employer="", anchors={}),
        )
        for i in range(COMMON_NAME_DOMAIN_THRESHOLD * 2)
    ]
    common, domains = detect_common_name(scores)

    assert not common
    assert domains == 1


def test_a_common_name_run_ingests_nothing_and_names_what_would_help(cache: Cache) -> None:
    hits = [
        hit(f"https://site{i}.example/page", snippet="John Smith wrote this.")
        for i in range(COMMON_NAME_DOMAIN_THRESHOLD + 2)
    ]
    provider = EverythingProvider(hits)
    subject = IdentityCard(key="john", name="John Smith", anchors={"github": "jsmith"})

    result = search_for_sources(subject, cache, client_for(provider, cache), log=lambda _m: None)

    assert result.common_name
    assert result.candidates == []
    assert result.fetches == 0, "nothing is fetched once the name is known to be ambiguous"
    assert len(result.held) == result.results_seen >= COMMON_NAME_DOMAIN_THRESHOLD, (
        "every candidate seen must be held for a human, not silently dropped"
    )
    assert result.disambiguators[:2] == ["employer", "location"]
    assert any("common name" in n for n in result.notes)


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
