"""The Hacker News adapter, against fixtures written to the documented shape.

Fixture provenance: `hn_search.json` / `hn_parent_item.json` are SYNTHETIC —
the repo's offline rule means no live capture exists. The load-bearing cases:

* a comment on another comment is unreadable without its parent, so the
  parent is hydrated from the items endpoint (cached permanently — old HN
  items never change);
* a comment on the story itself needs no hydration — the story title is its
  context;
* a hit whose author is not the subject is never ingested, however it got
  into the result set;
* comment_text is HTML and must come out as text with its links kept.

Everything runs from a pre-seeded cache; `http_client` is replaced with
something that fails the test if it is ever constructed.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from corpus.cache import Cache
from corpus.sources import base as base_module
from corpus.sources.hackernews import (
    API,
    MAX_PARENT_LOOKUPS,
    PER_PAGE,
    HackerNewsSource,
    _username_of,
)

FIXTURES = Path(__file__).parent / "fixtures"
USER = "testsubject"


def fixture(name: str):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


@pytest.fixture()
def cache(tmp_path: Path) -> Cache:
    c = Cache(path=tmp_path / "cache.db")
    yield c
    c.close()


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("the hn adapter opened an HTTP client during a test")

    monkeypatch.setattr(base_module, "http_client", forbidden)


def search_url(page: int = 0) -> str:
    return f"{API}/search_by_date?tags=author_{USER}&hitsPerPage={PER_PAGE}&page={page}"


def seed(cache: Cache, search, *, parent=True) -> None:
    cache.put("hn", search_url(0), search)
    if parent:
        cache.put("hn", f"{API}/items/4002", fixture("hn_parent_item.json"), permanent=True)


def read(cache: Cache, **kwargs):
    return HackerNewsSource().fetch_with_stats(
        USER, author_handle=USER, cache=cache, log=lambda _: None, **kwargs
    )


@pytest.mark.parametrize(
    "given",
    [USER, f"https://news.ycombinator.com/user?id={USER}", f"user?id={USER}"],
)
def test_username_normalization(given: str) -> None:
    assert _username_of(given) == USER


def test_stories_and_comments_ingested(cache: Cache) -> None:
    seed(cache, fixture("hn_search.json"))
    docs, stats = read(cache)
    kinds = {d.source_id: d.kind for d in docs}
    assert kinds["hn-3900"] == "original"
    assert kinds["hn-4003"] == "reply"
    assert kinds["hn-4004"] == "reply"
    assert stats.stories == 1
    assert stats.comments == 2
    story = next(d for d in docs if d.source_id == "hn-3900")
    assert "Show HN" in story.body
    assert story.outbound_links == ["https://example.com/gate-design"]
    assert story.engagement == {"points": 42}


def test_a_reply_to_a_comment_hydrates_its_parent(cache: Cache) -> None:
    seed(cache, fixture("hn_search.json"))
    docs, stats = read(cache)
    reply = next(d for d in docs if d.source_id == "hn-4004")
    assert reply.context == (
        "Floating point is simpler, just use that. Nobody measures this stuff anyway."
    )
    assert reply.context_author == "someoneelse"
    assert reply.context_url == "https://news.ycombinator.com/item?id=4002"
    assert stats.parents_hydrated == 1


def test_a_reply_to_the_story_uses_the_title_as_context(cache: Cache) -> None:
    seed(cache, fixture("hn_search.json"))
    docs, stats = read(cache)
    reply = next(d for d in docs if d.source_id == "hn-4003")
    assert reply.context == "Ask HN: Fixed point or floating point for audio?"
    assert reply.outbound_links == ["https://example.com/gate-design"]
    # The story title was inline; only the comment-parent lookup was spent.
    assert stats.parent_lookups == 1


def test_an_unavailable_parent_is_kept_as_unavailable(cache: Cache) -> None:
    seed(cache, fixture("hn_search.json"), parent=False)
    cache.offline = True  # seeded search, no parent: the lookup misses
    docs, stats = read(cache)
    reply = next(d for d in docs if d.source_id == "hn-4004")
    assert reply.context == "[unavailable]"
    assert stats.parents_unavailable == 1


def test_a_hit_by_another_author_is_never_ingested(cache: Cache) -> None:
    seed(cache, fixture("hn_search.json"))
    docs, _ = read(cache)
    assert all("never be attributed" not in d.body for d in docs)


def test_an_empty_comment_is_skipped(cache: Cache) -> None:
    seed(cache, fixture("hn_search.json"))
    docs, _ = read(cache)
    assert "hn-3877" not in {d.source_id for d in docs}


def test_the_parent_lookup_cap_holds(cache: Cache) -> None:
    hits = [
        {
            "objectID": str(5000 + i),
            "created_at": "2024-03-10T14:00:00.000Z",
            "author": USER,
            "comment_text": f"<p>answer {i}</p>",
            "story_title": "A thread",
            "story_url": "",
            "story_id": 4000,
            "parent_id": 9000 + i,
            "_tags": ["comment", f"author_{USER}", "story_4000"],
        }
        for i in range(MAX_PARENT_LOOKUPS + 5)
    ]
    seed(cache, {"nbHits": len(hits), "page": 0, "nbPages": 1, "hits": hits}, parent=False)
    docs, stats = read(cache, limit=1000)
    assert stats.parent_lookups == MAX_PARENT_LOOKUPS
    assert stats.parent_lookups_capped == 5
    # Capped replies fall back to the story title, never to nothing-at-all.
    assert all(d.context == "A thread" for d in docs[-5:])


def test_an_empty_result_is_a_result_not_a_crash(cache: Cache) -> None:
    seed(cache, {"nbHits": 0, "page": 0, "nbPages": 0, "hits": []}, parent=False)
    docs, stats = read(cache)
    assert docs == []
    assert stats.comments == 0
