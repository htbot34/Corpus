"""The Exa search provider, entirely offline.

Fixture provenance: `exa_search_response.json` and
`exa_find_similar_response.json` are SYNTHETIC — written to Exa's documented
request and response shapes, never captured from the wire, in the same sense
as `web_search_response_with_citations.json` (see docs/wire-contract.md).
This repo's bug history is four separate documented-shape-versus-real-shape
failures, so these tests prove the code is self-consistent and defensive;
only a live capture can prove the shapes.

The load-bearing cases the fixtures carry: a complete hit, an empty `text`,
a null `title`, a malformed `publishedDate`, a duplicate URL, a hit with no
URL at all, and a response with and without `costDollars`.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from corpus.budget import (
    EXA_ASSUMED_PAGES_PER_SEARCH,
    EXA_COST_PER_QUERY,
    SEARCH_COST_BY_PROVIDER,
    SEARCH_COST_PER_QUERY,
    Budget,
    BudgetExceeded,
)
from corpus.cache import Cache
from corpus.search.capture import SearchCapture
from corpus.search.client import SearchClient
from corpus.search.providers import (
    EXA_FIND_SIMILAR_ENDPOINT,
    EXA_SEARCH_ENDPOINT,
    EXA_SNIPPET_MAX_CHARS,
    AnthropicSearchProvider,
    ExaSearchProvider,
    SearchError,
    SearchResult,
    SearchUsage,
    exa_cost_dollars,
)
from corpus.search.scoring import PEOPLE_SEARCH_HOSTS
from corpus.search.verify import RESULTS_PER_QUERY

FIXTURES = Path(__file__).parent / "fixtures"


def fixture(name: str) -> Any:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def search_payload() -> Any:
    return fixture("exa_search_response.json")


def similar_payload() -> Any:
    return fixture("exa_find_similar_response.json")


class StubResponse:
    def __init__(self, payload: Any, status_code: int = 200) -> None:
        self.payload = payload
        self.status_code = status_code

    def json(self) -> Any:
        if isinstance(self.payload, Exception):
            raise self.payload
        return self.payload


class StubHttp:
    """Records every POST and hands back the scripted responses in order."""

    def __init__(self, *responses: Any) -> None:
        self.responses = list(responses)
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.closed = False

    def post(self, url: str, json: Any = None) -> Any:
        self.calls.append((url, json))
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response

    def close(self) -> None:
        self.closed = True


def provider_for(*responses: Any, **kwargs: Any) -> tuple[ExaSearchProvider, StubHttp]:
    stub = StubHttp(
        *(r if isinstance(r, (StubResponse, Exception)) else StubResponse(r) for r in responses)
    )
    return ExaSearchProvider(api_key="k", client=stub, **kwargs), stub


@pytest.fixture()
def cache(tmp_path: Path) -> Any:
    c = Cache(path=tmp_path / "cache.db")
    yield c
    c.close()


# -- mapping the documented shape -------------------------------------------


def test_a_documented_hit_maps_to_searchresult() -> None:
    provider, _ = provider_for(search_payload())
    results = provider.search("Jane Smith Acme Corp", 8)

    first = results[0]
    assert first.url == "https://janesmith.com/essays/measurement"
    assert first.title == "What we talk about when we talk about measurement"
    assert "measuring a thing" in first.snippet
    assert first.published_at is not None and first.published_at.year == 2025
    # The whole hit survives in raw, so a wire-shape surprise stays inspectable.
    assert first.raw["author"] == "Jane Smith"
    assert first.raw["score"] == 0.31


def test_an_empty_text_is_an_empty_snippet_not_a_dropped_result() -> None:
    provider, _ = provider_for(search_payload())
    results = provider.search("q", 8)
    by_url = {r.url: r for r in results}
    empty = by_url["https://conf.example/talks/jane-smith"]
    assert empty.snippet == ""


def test_a_missing_title_maps_to_an_empty_string() -> None:
    provider, _ = provider_for(search_payload())
    by_url = {r.url: r for r in provider.search("q", 8)}
    assert by_url["https://untitled.example/post/44"].title == ""


def test_a_malformed_date_is_left_unknown_never_guessed() -> None:
    provider, _ = provider_for(search_payload())
    by_url = {r.url: r for r in provider.search("q", 8)}
    assert by_url["https://badly-dated.example/note"].published_at is None


def test_a_duplicate_url_and_a_missing_url_are_both_dropped() -> None:
    provider, _ = provider_for(search_payload())
    results = provider.search("q", 8)
    urls = [r.url for r in results]
    assert len(urls) == len(set(urls))
    # 6 hits in the fixture: one duplicate, one with no url.
    assert len(results) == 4


def test_the_snippet_is_clipped_at_the_documented_ceiling() -> None:
    payload = search_payload()
    payload["results"][0]["text"] = "word " * 3_000
    provider, _ = provider_for(payload)
    first = provider.search("q", 8)[0]
    assert len(first.snippet) <= EXA_SNIPPET_MAX_CHARS
    # ...and the untruncated extract is not smuggled in through raw parsing
    # errors: the clip is a clip, not a failure.
    assert provider.last_usage.errors == []
    # The cap also holds inside `raw`, which lands in the permanent cache —
    # marked, so nobody mistakes the clip for what the wire sent. Everything
    # else in the hit survives verbatim.
    assert len(first.raw["text"]) <= EXA_SNIPPET_MAX_CHARS
    assert first.raw["_text_clipped"] is True
    assert first.raw["score"] == 0.31


def test_an_unclipped_hit_carries_no_clip_marker() -> None:
    provider, _ = provider_for(search_payload())
    first = provider.search("q", 8)[0]
    assert "_text_clipped" not in first.raw
    assert first.raw["text"] == first.snippet


def test_the_limit_caps_what_comes_back() -> None:
    provider, _ = provider_for(search_payload())
    assert len(provider.search("q", 2)) == 2


def test_a_response_without_a_results_list_degrades_with_an_error() -> None:
    provider, _ = provider_for({"requestId": "synthetic", "unexpected": True})
    results = provider.search("q", 8)
    assert results == []
    assert any("results" in e for e in provider.last_usage.errors)


# -- the request shape -------------------------------------------------------


def test_the_request_carries_the_documented_body() -> None:
    provider, stub = provider_for(search_payload())
    provider.search("Jane Smith Acme Corp", RESULTS_PER_QUERY)

    endpoint, body = stub.calls[0]
    assert endpoint == EXA_SEARCH_ENDPOINT
    assert body["query"] == "Jane Smith Acme Corp"
    assert body["numResults"] == RESULTS_PER_QUERY
    assert body["contents"] == {"text": True}


def test_exclude_domains_carries_the_people_search_list() -> None:
    """The API-level filter recovers the result slots a rejected hit would
    have wasted. It is an optimisation, never the guarantee — scoring.py's
    post-hoc rejection is the guarantee, and it is untouched."""
    provider, stub = provider_for(search_payload(), similar_payload())
    provider.search("q", 8)
    provider.find_similar("https://janesmith.com", 8)

    for _, body in stub.calls:
        assert body["excludeDomains"] == list(PEOPLE_SEARCH_HOSTS)


def test_find_similar_posts_the_url_to_its_own_endpoint_and_maps_the_same_way() -> None:
    provider, stub = provider_for(similar_payload())
    results = provider.find_similar("https://janesmith.com", 8)

    endpoint, body = stub.calls[0]
    assert endpoint == EXA_FIND_SIMILAR_ENDPOINT
    assert body["url"] == "https://janesmith.com"
    assert body["numResults"] == 8
    assert body["contents"] == {"text": True}

    assert [r.url for r in results] == [
        "https://blog.example/notes-on-rubrics",
        "https://another.example/essay",
    ]
    assert results[0].published_at is not None
    assert results[1].published_at is None  # null publishedDate stays unknown


# -- failure modes -----------------------------------------------------------


def test_an_http_error_raises_searcherror_not_results() -> None:
    provider, _ = provider_for(StubResponse({"nope": True}, status_code=500))
    with pytest.raises(SearchError) as caught:
        provider.search("q", 8)
    assert "500" in str(caught.value)


def test_a_transport_failure_raises_searcherror() -> None:
    provider, _ = provider_for(ConnectionError("the wire went away"))
    with pytest.raises(SearchError):
        provider.search("q", 8)


def test_unreadable_json_raises_searcherror() -> None:
    provider, _ = provider_for(StubResponse(ValueError("not json")))
    with pytest.raises(SearchError):
        provider.search("q", 8)


def test_the_key_refusal_happens_at_construction(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("EXA_API_KEY", raising=False)
    with pytest.raises(SearchError) as caught:
        ExaSearchProvider()
    assert "EXA_API_KEY" in str(caught.value)


def test_no_client_is_built_until_a_search_actually_runs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("EXA_API_KEY", "not-a-real-key")
    provider = ExaSearchProvider()
    provider.close()


# -- money -------------------------------------------------------------------


def test_the_rate_table_and_the_provider_attributes_agree() -> None:
    """budget.py's name->rate table (for the estimator, which must not
    construct a provider) and the class attributes (for the client, which
    holds one) are the same fact written twice. This is the pin."""
    assert SEARCH_COST_BY_PROVIDER["exa"] == ExaSearchProvider.cost_per_search
    assert SEARCH_COST_BY_PROVIDER["anthropic_search"] == AnthropicSearchProvider.cost_per_search
    assert AnthropicSearchProvider.cost_per_search == SEARCH_COST_PER_QUERY


def test_the_worst_case_page_assumption_covers_what_phase_2_asks_for() -> None:
    """budget.py cannot import verify without a cycle, so the assumption that
    one Exa search bills text for every result is pinned here instead: the
    reservation must never assume fewer pages than verify actually requests."""
    assert EXA_ASSUMED_PAGES_PER_SEARCH >= RESULTS_PER_QUERY


def test_an_exa_search_bills_the_exa_rate_not_anthropics(cache: Cache) -> None:
    payload = search_payload()
    del payload["costDollars"]  # force the documented-rate path
    provider, _ = provider_for(payload)
    budget = Budget(limit=10.0)
    client = SearchClient(provider, cache, budget, log=lambda _m: None)

    client.search("q", 8)

    assert budget.total_for("search") == pytest.approx(EXA_COST_PER_QUERY)
    assert budget.total_for("search") != pytest.approx(SEARCH_COST_PER_QUERY)


def test_a_stated_costdollars_beats_the_price_list(cache: Cache) -> None:
    provider, _ = provider_for(search_payload())
    budget = Budget(limit=10.0)
    client = SearchClient(provider, cache, budget, log=lambda _m: None)

    client.search("q", 8)

    assert budget.total_for("search") == pytest.approx(0.011)


def test_a_garbage_costdollars_falls_back_to_the_documented_rate() -> None:
    assert exa_cost_dollars({"costDollars": {"total": "not a number"}}) is None
    assert exa_cost_dollars({"costDollars": {"total": -1}}) is None
    assert exa_cost_dollars({"costDollars": {"total": float("inf")}}) is None
    assert exa_cost_dollars({"costDollars": {"total": True}}) is None
    assert exa_cost_dollars({}) is None
    assert exa_cost_dollars({"costDollars": {"total": 0.011}}) == pytest.approx(0.011)


def test_an_absurd_costdollars_cannot_replace_the_price_list() -> None:
    """The obvious unit surprise from an UNVERIFIED wire shape: cents where
    dollars belong. One such value must not bill 1,000x the documented rate,
    exhaust the budget, and write fictitious spend into the ledger."""
    from corpus.search.providers import EXA_COST_SANITY_MULTIPLE

    ceiling = EXA_COST_PER_QUERY * EXA_COST_SANITY_MULTIPLE
    assert exa_cost_dollars({"costDollars": {"total": 13}}) is None
    assert exa_cost_dollars({"costDollars": {"total": ceiling * 1.01}}) is None
    assert exa_cost_dollars({"costDollars": {"total": ceiling}}) == pytest.approx(ceiling)


def test_an_errored_empty_search_is_never_cached_as_a_finding(cache: Cache) -> None:
    """A 200 whose body the parser cannot read must not become "this person
    has no web presence" on every future run: the first live run is the test
    of these UNVERIFIED shapes, and a shape surprise there would otherwise
    permanently poison the cache for every query it executed."""
    provider, stub = provider_for({"requestId": "synthetic", "unexpected": True}, search_payload())
    budget = Budget(limit=10.0)
    client = SearchClient(provider, cache, budget, log=lambda _m: None)

    assert client.search("q", 8) == []
    # Billed — the search happened and Exa will invoice it — but not cached.
    assert budget.total_for("search") > 0

    # The provider answers properly on the next run; the query is retried
    # rather than served an empty lie from the cache.
    results = client.search("q", 8)
    assert len(results) == 4
    assert len(stub.calls) == 2


def test_an_empty_search_with_no_errors_is_a_finding_and_is_cached(cache: Cache) -> None:
    provider, stub = provider_for({"requestId": "synthetic", "results": []})
    client = SearchClient(provider, cache, Budget(limit=10.0), log=lambda _m: None)

    assert client.search("q", 8) == []
    assert client.search("q", 8) == []
    assert len(stub.calls) == 1  # the second answer came from the cache
    assert client.cached_searches == 1


def test_the_reservation_refuses_before_the_call_is_made(cache: Cache) -> None:
    """Budget.reserve() is the hard stop for Exa exactly as it is for
    Anthropic: it raises BEFORE the provider is asked, so a refused search
    costs nothing and touches no wire."""
    provider, stub = provider_for(search_payload())
    budget = Budget(limit=0.001)
    client = SearchClient(provider, cache, budget, log=lambda _m: None)

    with pytest.raises(BudgetExceeded):
        client.search("q", 8)
    assert stub.calls == []
    assert budget.total == 0.0


def test_find_similar_is_reserved_and_billed_like_a_search(cache: Cache) -> None:
    provider, _ = provider_for(similar_payload())
    budget = Budget(limit=10.0)
    client = SearchClient(provider, cache, budget, log=lambda _m: None)

    client.find_similar("https://janesmith.com", 8)

    # No costDollars in this fixture, so the documented rate is billed.
    assert budget.total_for("search") == pytest.approx(EXA_COST_PER_QUERY)


def test_find_similar_results_are_cached_by_seed_url(cache: Cache) -> None:
    provider, stub = provider_for(similar_payload())
    budget = Budget(limit=10.0)
    client = SearchClient(provider, cache, budget, log=lambda _m: None)

    first = client.find_similar("https://janesmith.com", 8)
    again = client.find_similar("https://janesmith.com", 8)

    assert [r.url for r in again] == [r.url for r in first]
    assert len(stub.calls) == 1
    assert client.cached_searches == 1


# -- the capability flag -----------------------------------------------------


class PlainProvider:
    """A provider from before find_similar existed: no flag, no method."""

    name = "plain"

    def __init__(self) -> None:
        self.last_usage = SearchUsage()
        self.searches: list[str] = []

    def search(self, query: str, limit: int) -> list[SearchResult]:
        self.searches.append(query)
        self.last_usage = SearchUsage(searches=1)
        return []

    def close(self) -> None:
        return None


class FlagWithoutMethodProvider(PlainProvider):
    """Declares the flag but forgot the method. Must degrade, not break."""

    name = "confused"
    supports_find_similar = True


def test_a_provider_without_the_capability_is_never_asked(cache: Cache) -> None:
    provider = PlainProvider()
    budget = Budget(limit=10.0)
    client = SearchClient(provider, cache, budget, log=lambda _m: None)

    assert client.finds_similar is False
    assert client.find_similar("https://janesmith.com", 8) == []
    assert budget.charges == []
    assert client.searches_run == 0
    assert client.errors == []


def test_a_flag_without_a_method_is_not_broken_by_its_absence(cache: Cache) -> None:
    client = SearchClient(
        FlagWithoutMethodProvider(), cache, Budget(limit=10.0), log=lambda _m: None
    )
    assert client.finds_similar is False
    assert client.find_similar("https://janesmith.com", 8) == []


def test_exa_declares_the_capability(cache: Cache) -> None:
    provider, _ = provider_for(similar_payload())
    client = SearchClient(provider, cache, Budget(limit=10.0), log=lambda _m: None)
    assert client.finds_similar is True


# -- capture -----------------------------------------------------------------


def test_capture_records_the_raw_exa_payload(tmp_path: Path) -> None:
    capture = SearchCapture(tmp_path)
    provider, _ = provider_for(search_payload(), capture=capture)

    provider.search("Jane Smith Acme Corp", 8)

    files = list(tmp_path.glob("*.json"))
    assert len(files) == 1
    written = json.loads(files[0].read_text())
    assert written["provider"] == "exa"
    assert written["raw_message"]["requestId"] == "synthetic-request-0001"
    assert written["usage"]["searches"] == 1
