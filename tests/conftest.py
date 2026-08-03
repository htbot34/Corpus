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
