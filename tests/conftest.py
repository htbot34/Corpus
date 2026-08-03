from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from fake_provider import FakeProvider

from corpus.budget import Budget
from corpus.cache import Cache
from corpus.x.client import XClient


@pytest.fixture(autouse=True)
def cache_db_in_tmp(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """No test may touch the real ~/.corpus/cache.db.

    `default_db_path()` falls through to the developer's own cache, so any
    code path that builds a `Cache()` without an explicit path lands in it —
    and the resynth CLI does exactly that, which meant every full test run
    appended two rows to the real `spend` table ('map slice 1' and 'reduce
    attempt 1'). The guard below blocks a live search client and nothing
    else, so it never noticed.

    Redirecting CORPUS_CACHE_DB for every test closes the whole class rather
    than the one instance: a test that forgets to pass a cache path gets an
    empty per-test database, not the user's ledger. Tests that point the env
    var somewhere deliberate still win — their monkeypatch runs after this
    one.
    """
    monkeypatch.setenv("CORPUS_CACHE_DB", str(tmp_path / "suite-cache.db"))


@pytest.fixture(autouse=True)
def no_live_search_client(monkeypatch: pytest.MonkeyPatch) -> None:
    """No test may construct a real Anthropic client for search.

    The same guard the discovery tests put on `http_client`, applied suite-wide
    because search runs by default: without it, any CLI test that reaches the
    search phase makes a real HTTPS call to api.anthropic.com and passes
    anyway, because a failed search is deliberately non-fatal. The suite would
    still be green, still be "offline", and quietly depend on the network.

    A test that wants search behaviour supplies its own fake provider, which
    never touches this method.
    """
    from corpus.search.providers import AnthropicSearchProvider

    def forbidden(self: AnthropicSearchProvider) -> object:
        # A provider handed an explicit `client=` is a test driving a stub, and
        # is exactly what this fixture wants tests to do. Only the branch that
        # would reach for the SDK is refused.
        if self._client is None:
            raise AssertionError(
                "a test built a live Anthropic client for search — pass client= "
                "or patch cli.get_search_provider with a fake instead"
            )
        return self._client

    monkeypatch.setattr(AnthropicSearchProvider, "_ensure_client", forbidden)


@pytest.fixture()
def cache(tmp_path: Path) -> Cache:
    c = Cache(path=tmp_path / "cache.db")
    yield c
    c.close()


@pytest.fixture()
def budget(cache: Cache) -> Budget:
    return Budget(limit=10.0, cache=cache)


@pytest.fixture()
def provider() -> FakeProvider:
    return FakeProvider()


@pytest.fixture()
def client(provider: FakeProvider, cache: Cache, budget: Budget) -> XClient:
    return XClient(provider, cache, budget)
