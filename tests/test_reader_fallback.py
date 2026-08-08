"""The reader-service fallback: off by default, marked when used, refused
where it must be.

Offline throughout. The Jina provider is driven through an injected stub
client (the conftest guard forbids the real one), and the Fetcher-level tests
use a fake reader that never leaves the process.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from corpus import discovery as discovery_module
from corpus.budget import Budget
from corpus.cache import Cache
from corpus.discovery import Fetcher
from corpus.identity import IdentityCard
from corpus.reader import (
    JINA_READER_BASE,
    JinaReaderProvider,
    ReaderError,
    get_reader,
    reader_fallback_enabled,
)
from corpus.search.client import SearchClient
from corpus.search.providers import SearchResult, SearchUsage
from corpus.search.verify import search_for_sources
from tests.test_fetcher import FakeClient, FakeResponse


class FakeReader:
    """A reader service that never leaves the process."""

    name = "fake-reader"

    def __init__(self, pages: dict[str, str] | None = None) -> None:
        self.pages = pages or {}
        self.calls: list[str] = []

    def read(self, url: str) -> str | None:
        self.calls.append(url)
        return self.pages.get(url)

    def close(self) -> None:
        return None


class OneShotProvider:
    """Returns the given hits for the first query, nothing after."""

    name = "oneshot"

    def __init__(self, hits: list[SearchResult]) -> None:
        self.hits = hits
        self.queries: list[str] = []
        self.last_usage = SearchUsage()

    def search(self, query: str, limit: int) -> list[SearchResult]:
        self.queries.append(query)
        self.last_usage = SearchUsage()
        return self.hits[:limit] if len(self.queries) == 1 else []

    def close(self) -> None:
        return None


@pytest.fixture()
def cache(tmp_path: Path) -> Any:
    c = Cache(path=tmp_path / "cache.db")
    yield c
    c.close()


def make_fetcher(
    monkeypatch: pytest.MonkeyPatch, cache: Cache, script: list[Any], **kwargs: Any
) -> tuple[Fetcher, FakeClient]:
    client = FakeClient(script)
    monkeypatch.setattr(discovery_module, "http_client", lambda *a, **k: client)
    fetcher = Fetcher(
        cache, 10, lambda _m: None, sleep=lambda _s: None, clock=lambda: 0.0, **kwargs
    )
    return fetcher, client


# -- the switch ---------------------------------------------------------------


def test_the_fallback_is_off_by_default(monkeypatch) -> None:
    monkeypatch.delenv("CORPUS_READER_FALLBACK", raising=False)
    assert reader_fallback_enabled(False) is False
    assert reader_fallback_enabled(True) is True
    monkeypatch.setenv("CORPUS_READER_FALLBACK", "1")
    assert reader_fallback_enabled(False) is True
    monkeypatch.setenv("CORPUS_READER_FALLBACK", "0")
    assert reader_fallback_enabled(False) is False


def test_without_a_reader_a_403_is_simply_lost(monkeypatch, cache) -> None:
    fetcher, _ = make_fetcher(monkeypatch, cache, [FakeResponse(403), FakeResponse(403)])
    assert fetcher.get("https://blocked.example/essay") is None
    assert fetcher.reader_attempts == 0
    assert fetcher.via_reader == set()


def test_get_reader_names_the_known_ones_on_a_typo(monkeypatch) -> None:
    monkeypatch.delenv("READER_PROVIDER", raising=False)
    with pytest.raises(ReaderError) as caught:
        get_reader("nope")
    assert "jina" in str(caught.value)
    assert isinstance(get_reader(), JinaReaderProvider)


# -- the Jina provider --------------------------------------------------------


def test_jina_reads_through_its_prefix_url(cache) -> None:
    client = FakeClient([FakeResponse(text="# Extracted\n\nBy Jane Smith. The essay.")])
    reader = JinaReaderProvider(api_key="", client=client)

    text = reader.read("https://blocked.example/essay")

    assert text is not None and "By Jane Smith" in text
    assert client.requests[0][0] == f"{JINA_READER_BASE}https://blocked.example/essay"


def test_jina_refuses_a_people_search_host_without_a_request() -> None:
    client = FakeClient([])
    reader = JinaReaderProvider(api_key="", client=client)

    assert reader.read("https://rocketreach.co/jane-smith_1234") is None
    assert client.requests == []
    assert any("people-search" in e for e in reader.errors)


def test_jina_records_an_http_refusal_and_returns_nothing() -> None:
    reader = JinaReaderProvider(api_key="", client=FakeClient([FakeResponse(451)]))
    assert reader.read("https://blocked.example/essay") is None
    assert any("451" in e for e in reader.errors)


def test_jina_headers_carry_identity_and_the_optional_key() -> None:
    keyless = JinaReaderProvider(api_key="", client=FakeClient([]))
    assert "Authorization" not in keyless.headers()
    assert "corpus/" in keyless.headers()["User-Agent"]

    keyed = JinaReaderProvider(api_key="k-123", client=FakeClient([]))
    assert keyed.headers()["Authorization"] == "Bearer k-123"


# -- the Fetcher integration --------------------------------------------------


def test_a_final_403_falls_back_to_the_reader_and_is_marked(monkeypatch, cache) -> None:
    url = "https://blocked.example/essay"
    reader = FakeReader({url: "By Jane Smith\n\nThe essay, extracted."})
    fetcher, _ = make_fetcher(
        monkeypatch, cache, [FakeResponse(403), FakeResponse(403)], reader=reader
    )

    body = fetcher.get(url)

    assert body is not None and "extracted" in body
    assert reader.calls == [url]
    assert fetcher.via_reader == {url}
    assert fetcher.reader_attempts == 1 and fetcher.reader_recoveries == 1
    # The 403 is still counted where reads are lost — the fallback recovered
    # the text, not the fetch — and the reader's work is counted apart.
    assert fetcher.failures == {"http_403": 1}
    # Cached under the reader's own key, never the direct-fetch key.
    assert cache.get("discovery", f"get:reader:{url}") is not None
    assert cache.get("discovery", f"get:{url}") is None


def test_reader_cache_hits_stay_marked_and_reader_off_runs_never_see_them(
    monkeypatch, cache
) -> None:
    url = "https://blocked.example/essay"
    reader = FakeReader({url: "Extracted text."})
    first, _ = make_fetcher(
        monkeypatch, cache, [FakeResponse(403), FakeResponse(403)], reader=reader
    )
    assert first.get(url) is not None

    # Same cache, reader still on: served from the reader key, still marked.
    second, _ = make_fetcher(monkeypatch, cache, [], reader=FakeReader())
    assert second.get(url) is not None
    assert second.via_reader == {url}
    assert second.cached == 1

    # Same cache, reader OFF: the extracted text is invisible, the fetch is
    # attempted again, and the page is honestly lost.
    third, client = make_fetcher(monkeypatch, cache, [FakeResponse(403), FakeResponse(403)])
    assert third.get(url) is None
    assert len(client.requests) == 2


def test_the_reader_is_never_consulted_for_a_blocked_url(monkeypatch, cache) -> None:
    url = "https://blocked.example/essay"
    reader = FakeReader({url: "Extracted."})
    fetcher, _ = make_fetcher(
        monkeypatch,
        cache,
        [FakeResponse(403), FakeResponse(403)],
        reader=reader,
        reader_blocked=lambda u: True,
    )

    assert fetcher.get(url) is None
    assert reader.calls == []
    assert fetcher.reader_attempts == 0
    assert any("excludes" in e for e in fetcher.errors)


def test_the_reader_is_only_for_403s(monkeypatch, cache) -> None:
    """A 404 is a page that does not exist; a timeout is a host that did not
    answer. Neither is a block a reader should be routing around."""
    url = "https://gone.example/essay"
    reader = FakeReader({url: "Extracted."})
    fetcher, _ = make_fetcher(monkeypatch, cache, [FakeResponse(404)], reader=reader)

    assert fetcher.get(url) is None
    assert reader.calls == []


def test_a_reader_that_finds_nothing_is_counted_and_not_recovered(monkeypatch, cache) -> None:
    url = "https://blocked.example/essay"
    fetcher, _ = make_fetcher(
        monkeypatch, cache, [FakeResponse(403), FakeResponse(403)], reader=FakeReader()
    )

    assert fetcher.get(url) is None
    assert fetcher.reader_attempts == 1 and fetcher.reader_recoveries == 0
    assert any("returned nothing" in e for e in fetcher.errors)


# -- through the verification pass --------------------------------------------


def card(**kwargs: object) -> IdentityCard:
    base: dict[str, object] = {
        "key": "jane",
        "name": "Jane Smith",
        "employer": "Acme Corp",
        "anchors": {"github": "jsmith"},
    }
    base.update(kwargs)
    return IdentityCard(**base)  # type: ignore[arg-type]


def run_phase(
    monkeypatch,
    cache: Cache,
    hits: list[SearchResult],
    script: list[Any],
    reader: FakeReader | None,
    the_card: IdentityCard | None = None,
) -> Any:
    client = FakeClient(script)
    monkeypatch.setattr(discovery_module, "http_client", lambda *a, **k: client)
    provider = OneShotProvider(hits)
    return search_for_sources(
        the_card or card(),
        cache,
        SearchClient(provider, cache, Budget(limit=10.0), log=lambda _m: None),
        reader=reader,
        log=lambda _m: None,
    )


def test_a_reader_recovered_page_is_scored_marked_and_disclosed(monkeypatch, cache) -> None:
    """The page 403s directly, the reader recovers extracted text, and the
    deterministic scorer judges that text like any fetched page — but the
    candidate, its serialized form, and its ingest basis all say where the
    body came from, because extracted text carries weaker authorship signals
    and the report must not pretend otherwise."""
    url = "https://blocked.example/essay"
    # What survives extraction is prose, not markup — no meta tags, no link
    # elements — so the corroborating signals here are the prose ones: the
    # name with the employer and the role, which is exactly the thinner
    # evidence the disclosure below is about.
    extracted = (
        "Notes on rubrics\n\nJane Smith is VP Engineering at Acme Corp. The notes "
        "argue rubrics beat interviews, at enough length for the argument to breathe."
    )
    result = run_phase(
        monkeypatch,
        cache,
        [SearchResult(url=url, snippet="Jane Smith")],
        [FakeResponse(403), FakeResponse(403)],
        FakeReader({url: extracted}),
        the_card=card(role="VP Engineering"),
    )

    assert result.reader_attempts == 1 and result.reader_recoveries == 1
    assert [c.url for c in result.candidates] == [url]
    found = result.candidates[0]
    assert found.verified
    assert found.fetched_via_reader
    assert found.as_dict()["fetched_via_reader"] is True
    assert "reader service" in found.ingest_basis
    assert "weaker" in found.ingest_basis
    assert result.as_dict()["reader_attempts"] == 1


def test_a_directly_fetched_page_carries_no_reader_disclosure(monkeypatch, cache) -> None:
    url = "https://open.example/essay"
    page = (
        '<html><head><meta name="author" content="Jane Smith"></head>'
        "<body><p>Notes, written at Acme Corp.</p></body></html>"
    )
    result = run_phase(
        monkeypatch,
        cache,
        [SearchResult(url=url, snippet="Jane Smith")],
        [FakeResponse(text=page)],
        FakeReader(),
    )

    assert [c.url for c in result.candidates] == [url]
    assert not result.candidates[0].fetched_via_reader
    assert "reader service" not in result.candidates[0].ingest_basis
    assert "fetched_via_reader" not in result.candidates[0].as_dict()


def test_an_excluded_url_never_reaches_the_reader_through_verification(monkeypatch, cache) -> None:
    """Excluded URLs are rejected on the URL alone, before any fetch — so the
    reader cannot be a way around the card's own exclusions."""
    url = "https://excluded.example/essay"
    reader = FakeReader({url: "Extracted."})
    result = run_phase(
        monkeypatch,
        cache,
        [SearchResult(url=url, snippet="Jane Smith")],
        [],
        reader,
        the_card=card(exclude=[url]),
    )

    assert reader.calls == []
    assert [c.url for c in result.rejected] == [url]
    assert result.reader_attempts == 0
