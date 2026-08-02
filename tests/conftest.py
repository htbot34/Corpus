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
