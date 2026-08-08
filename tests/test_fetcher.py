"""The fetch layer: failure accounting, the retry ladder, and honest identity.

Offline throughout, and with no fixture files: every response is a scripted
in-process stub handed to the Fetcher through the `http_client` seam, sleeps
land in a list instead of a clock, and the per-host throttle runs against a
fake monotonic clock.

The failure accounting is the deliverable here. A reference run lost 56% of
its candidate pages and the breakdown behind that number had never been
recorded — `errors` is prose, and prose cannot be counted.
"""

from __future__ import annotations

import ssl
from pathlib import Path
from typing import Any

import httpx
import pytest

from corpus import discovery as discovery_module
from corpus.cache import Cache
from corpus.discovery import (
    BROWSER_ACCEPT,
    FETCH_FAILURE_CATEGORIES,
    FETCH_RETRIES,
    PER_HOST_INTERVAL_SECONDS,
    Fetcher,
    categorize_fetch_exception,
    categorize_fetch_status,
    parse_retry_after,
)
from corpus.sources.base import USER_AGENT


class FakeResponse:
    def __init__(
        self,
        status_code: int = 200,
        text: str = "<html><body>a page</body></html>",
        headers: dict[str, str] | None = None,
    ) -> None:
        self.status_code = status_code
        self.text = text
        self.headers = headers or {}


class FakeClient:
    """Hands back the scripted responses (or raises the scripted exceptions)
    in order, recording every request it saw."""

    def __init__(self, script: list[Any]) -> None:
        self.script = list(script)
        self.requests: list[tuple[str, dict[str, str]]] = []

    def get(self, url: str, headers: dict[str, str] | None = None) -> Any:
        self.requests.append((url, dict(headers or {})))
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    def close(self) -> None:
        return None


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture()
def cache(tmp_path: Path) -> Any:
    c = Cache(path=tmp_path / "cache.db")
    yield c
    c.close()


def make_fetcher(
    monkeypatch: pytest.MonkeyPatch,
    cache: Cache,
    script: list[Any],
    **kwargs: Any,
) -> tuple[Fetcher, FakeClient, list[float], FakeClock]:
    client = FakeClient(script)
    monkeypatch.setattr(discovery_module, "http_client", lambda *a, **k: client)
    clock = FakeClock()
    sleeps: list[float] = []

    def sleep(seconds: float) -> None:
        sleeps.append(seconds)
        clock.now += seconds

    fetcher = Fetcher(cache, 10, lambda _m: None, sleep=sleep, clock=clock, **kwargs)
    return fetcher, client, sleeps, clock


# -- the accounting ----------------------------------------------------------


def test_a_clean_fetch_counts_no_failures(monkeypatch, cache) -> None:
    fetcher, _, sleeps, _ = make_fetcher(monkeypatch, cache, [FakeResponse()])
    body = fetcher.get("https://a.example/page")
    assert body is not None and "a page" in body
    assert fetcher.failures == {}
    assert sleeps == []


@pytest.mark.parametrize(
    ("script", "category"),
    [
        ([FakeResponse(403), FakeResponse(403)], "http_403"),
        ([FakeResponse(404)], "http_404"),
        ([FakeResponse(410)], "other"),
        ([FakeResponse(429)] * (FETCH_RETRIES + 1), "http_429"),
        ([FakeResponse(500)] * (FETCH_RETRIES + 1), "http_5xx"),
        ([FakeResponse(503)] * (FETCH_RETRIES + 1), "http_5xx"),
        ([httpx.ReadTimeout("slow")], "timeout"),
        ([httpx.TooManyRedirects("loop")], "redirect_loop"),
        ([httpx.ConnectError("[Errno -2] Name or service not known")], "dns"),
        ([httpx.ConnectError("connection refused")], "other"),
    ],
)
def test_each_lost_read_lands_in_exactly_one_category(monkeypatch, cache, script, category) -> None:
    fetcher, _, _, _ = make_fetcher(monkeypatch, cache, script)
    assert fetcher.get("https://a.example/page") is None
    assert fetcher.failures == {category: 1}
    assert category in FETCH_FAILURE_CATEGORIES
    assert len(fetcher.errors) == 1  # prose too, but once — counted is the point


def test_a_tls_failure_is_found_anywhere_in_the_exception_chain() -> None:
    try:
        raise httpx.ConnectError("wrapped") from ssl.SSLError("bad certificate")
    except httpx.ConnectError as exc:
        assert categorize_fetch_exception(exc) == "tls"


def test_status_categories() -> None:
    assert categorize_fetch_status(403) == "http_403"
    assert categorize_fetch_status(404) == "http_404"
    assert categorize_fetch_status(429) == "http_429"
    assert categorize_fetch_status(500) == "http_5xx"
    assert categorize_fetch_status(503) == "http_5xx"
    assert categorize_fetch_status(401) == "other"


def test_an_oversized_body_is_too_large_not_a_page(monkeypatch, cache) -> None:
    monkeypatch.setattr(discovery_module, "MAX_FETCH_BODY_BYTES", 16)
    fetcher, _, _, _ = make_fetcher(monkeypatch, cache, [FakeResponse(text="x" * 64)])
    assert fetcher.get("https://a.example/tarball") is None
    assert fetcher.failures == {"too_large": 1}


# -- retries -----------------------------------------------------------------


def test_a_transient_429_is_retried_and_the_page_is_not_lost(monkeypatch, cache) -> None:
    """The whole point: there were previously ZERO retries, so one transient
    rate-limit answer permanently lost a page."""
    fetcher, client, sleeps, _ = make_fetcher(
        monkeypatch,
        cache,
        [FakeResponse(429, headers={"Retry-After": "2"}), FakeResponse()],
    )
    body = fetcher.get("https://a.example/page")
    assert body is not None
    assert fetcher.failures == {}
    assert 2.0 in sleeps  # Retry-After honoured verbatim
    assert len(client.requests) == 2


def test_a_5xx_backs_off_exponentially_with_jitter(monkeypatch, cache) -> None:
    fetcher, client, sleeps, _ = make_fetcher(
        monkeypatch, cache, [FakeResponse(500)] * (FETCH_RETRIES + 1)
    )
    assert fetcher.get("https://a.example/page") is None
    assert fetcher.failures == {"http_5xx": 1}
    assert len(client.requests) == FETCH_RETRIES + 1
    backoffs = [s for s in sleeps if s >= 1.0]  # throttle waits are < 1.0
    assert len(backoffs) == FETCH_RETRIES
    assert 1.0 <= backoffs[0] <= 2.0  # base * 2^0 + jitter
    assert 2.0 <= backoffs[1] <= 3.0  # base * 2^1 + jitter


def test_a_retry_after_past_the_cap_means_the_host_said_no(monkeypatch, cache) -> None:
    fetcher, client, sleeps, _ = make_fetcher(
        monkeypatch, cache, [FakeResponse(429, headers={"Retry-After": "3600"})]
    )
    assert fetcher.get("https://a.example/page") is None
    assert fetcher.failures == {"http_429": 1}
    assert len(client.requests) == 1
    assert sleeps == []  # a run must not sleep an hour for one page


def test_retry_after_parsing_is_defensive() -> None:
    assert parse_retry_after("30") == 30.0
    assert parse_retry_after(" 2.5 ") == 2.5
    assert parse_retry_after("Wed, 21 Oct 2026 07:28:00 GMT") is None
    assert parse_retry_after("-5") is None
    assert parse_retry_after("") is None
    assert parse_retry_after(None) is None


def test_a_404_is_an_answer_not_weather(monkeypatch, cache) -> None:
    """403 and 404 are not retried (beyond the one Accept probe): the host
    answered. Exactly one request goes out for a 404."""
    fetcher, client, _, _ = make_fetcher(monkeypatch, cache, [FakeResponse(404)])
    assert fetcher.get("https://a.example/gone") is None
    assert len(client.requests) == 1


# -- the 403 Accept probe -----------------------------------------------------


def test_a_403_earns_one_probe_with_a_browser_accept_header(monkeypatch, cache) -> None:
    """Some hosts reject on Accept rather than identity. Same User-Agent on
    the probe — identity is never disguised — only the Accept changes."""
    fetcher, client, _, _ = make_fetcher(monkeypatch, cache, [FakeResponse(403), FakeResponse()])
    body = fetcher.get("https://a.example/page")
    assert body is not None
    assert fetcher.failures == {}
    first_headers = client.requests[0][1]
    probe_headers = client.requests[1][1]
    assert probe_headers.get("Accept") == BROWSER_ACCEPT
    assert "Accept" not in first_headers or first_headers["Accept"] != BROWSER_ACCEPT


def test_a_second_403_is_final_and_counted_once(monkeypatch, cache) -> None:
    fetcher, client, _, _ = make_fetcher(monkeypatch, cache, [FakeResponse(403), FakeResponse(403)])
    assert fetcher.get("https://a.example/page") is None
    assert fetcher.failures == {"http_403": 1}
    assert len(client.requests) == 2  # the original and the one probe, never more


# -- the per-host throttle ----------------------------------------------------


def test_requests_to_one_host_are_spaced(monkeypatch, cache) -> None:
    fetcher, _, sleeps, _ = make_fetcher(monkeypatch, cache, [FakeResponse(), FakeResponse()])
    fetcher.get("https://a.example/one")
    fetcher.get("https://a.example/two")
    assert len(sleeps) == 1
    assert 0 < sleeps[0] <= PER_HOST_INTERVAL_SECONDS


def test_different_hosts_are_not_throttled_against_each_other(monkeypatch, cache) -> None:
    fetcher, _, sleeps, _ = make_fetcher(monkeypatch, cache, [FakeResponse(), FakeResponse()])
    fetcher.get("https://a.example/one")
    fetcher.get("https://b.example/one")
    assert sleeps == []


def test_a_host_visited_long_ago_is_not_throttled(monkeypatch, cache) -> None:
    fetcher, _, sleeps, clock = make_fetcher(monkeypatch, cache, [FakeResponse(), FakeResponse()])
    fetcher.get("https://a.example/one")
    clock.now += PER_HOST_INTERVAL_SECONDS * 3
    fetcher.get("https://a.example/two")
    assert sleeps == []


# -- identity ------------------------------------------------------------------


def test_the_user_agent_names_the_tool_and_a_contact_url() -> None:
    """Some hosts block generic clients and allow identified ones, and
    identifying ourselves is the honest thing to do either way."""
    assert USER_AGENT.startswith("corpus/")
    assert "+https://github.com/htbot34/Corpus" in USER_AGENT


# -- the counts flow out -------------------------------------------------------


def test_the_counts_reach_the_search_phase_result(monkeypatch, cache) -> None:
    from fake_search import SilentSearchProvider

    from corpus.budget import Budget
    from corpus.identity import IdentityCard
    from corpus.search.client import SearchClient
    from corpus.search.providers import SearchResult
    from corpus.search.verify import search_for_sources

    provider = SilentSearchProvider(
        results={'"Jane Smith" "Acme Corp"': [SearchResult(url="https://blocked.example/essay")]}
    )
    client = FakeClient([FakeResponse(403), FakeResponse(403)])
    monkeypatch.setattr(discovery_module, "http_client", lambda *a, **k: client)
    card = IdentityCard(key="jane", name="Jane Smith", employer="Acme Corp")

    result = search_for_sources(
        card,
        cache,
        SearchClient(provider, cache, Budget(limit=10.0), log=lambda _m: None),
        log=lambda _m: None,
    )

    assert result.fetch_failures == {"http_403": 1}
    assert result.as_dict()["fetch_failures"] == {"http_403": 1}


def test_the_counts_reach_the_coverage_block() -> None:
    from corpus.render import _fetch_loss_lines

    run_meta = {
        "discovery": {"fetch_failures": {"http_404": 2}},
        "search": {
            "fetch_failures": {"http_403": 9, "timeout": 2, "dns": 1},
            "reader_attempts": 4,
            "reader_recoveries": 3,
        },
    }
    lines = _fetch_loss_lines(run_meta)

    phase1 = next(line for line in lines if "phase 1" in line)
    assert "http_404 × 2" in phase1
    phase2 = next(line for line in lines if "phase 2" in line)
    assert "12 page read(s)" in phase2
    assert phase2.index("http_403 × 9") < phase2.index("timeout × 2")  # biggest loss first
    reader = next(line for line in lines if "reader-service fallback" in line)
    assert "3 of 4" in reader
    assert "weaker" in reader


def test_no_losses_means_no_lines() -> None:
    from corpus.render import _fetch_loss_lines

    assert _fetch_loss_lines({}) == []
    assert _fetch_loss_lines({"discovery": {"fetch_failures": {}}, "search": {}}) == []
