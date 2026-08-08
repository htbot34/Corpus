"""The Reddit adapter, against fixtures written to the documented shape.

Fixture provenance: `reddit_comments.json` / `reddit_submitted.json` are
SYNTHETIC — the repo's offline rule means no live capture exists. The
load-bearing cases:

* a comment keeps its submission title as context, inline, at no extra cost;
* a child whose author is not the subject is never ingested, however it got
  into the listing;
* `created_utc` is a float epoch, not an ISO string;
* an empty listing page ends the walk.

Everything runs from a pre-seeded cache; `http_client` is replaced with
something that fails the test if it is ever constructed.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from corpus.cache import Cache
from corpus.sources import base as base_module
from corpus.sources.reddit import PER_PAGE, SITE, RedditSource, _username_of

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
        raise AssertionError("the reddit adapter opened an HTTP client during a test")

    monkeypatch.setattr(base_module, "http_client", forbidden)


def seed(cache: Cache, comments, submitted) -> None:
    cache.put("reddit", f"{SITE}/user/{USER}/comments.json?limit={PER_PAGE}", comments)
    cache.put("reddit", f"{SITE}/user/{USER}/submitted.json?limit={PER_PAGE}", submitted)


def read(cache: Cache, **kwargs):
    return RedditSource().fetch_with_stats(
        USER, author_handle=USER, cache=cache, log=lambda _: None, **kwargs
    )


@pytest.mark.parametrize(
    "given",
    [
        USER,
        f"u/{USER}",
        f"https://www.reddit.com/user/{USER}",
        f"https://www.reddit.com/user/{USER}/",
        f"https://old.reddit.com/u/{USER}",
    ],
)
def test_username_normalization(given: str) -> None:
    assert _username_of(given) == USER


def test_comments_and_submissions_ingested(cache: Cache) -> None:
    seed(cache, fixture("reddit_comments.json"), fixture("reddit_submitted.json"))
    docs, stats = read(cache)
    by_id = {d.source_id: d for d in docs}

    comment = by_id["reddit-t1_c1"]
    assert comment.kind == "reply"
    assert comment.context == "Fixed point or floating point for audio?"
    assert (
        comment.context_url
        == "https://www.reddit.com/r/dsp/comments/abc123/fixed_point_or_floating/"
    )
    assert comment.url == "https://www.reddit.com/r/dsp/comments/abc123/fixed_point_or_floating/c1/"
    assert comment.engagement == {"score": 17}
    assert comment.published_at.year == 2024

    self_post = by_id["reddit-t3_s1"]
    assert self_post.kind == "original"
    assert "A gate that keeps its threshold" in self_post.body
    assert "floating point in the interrupt path" in self_post.body
    assert self_post.outbound_links == []  # a self-post's url is itself

    link_post = by_id["reddit-t3_s2"]
    assert link_post.outbound_links == ["https://example.com/gate-design"]

    assert stats.comments == 1
    assert stats.submissions == 2


def test_a_child_by_another_author_is_never_ingested(cache: Cache) -> None:
    seed(cache, fixture("reddit_comments.json"), fixture("reddit_submitted.json"))
    docs, _ = read(cache)
    assert all("never be attributed" not in d.body for d in docs)


def test_an_empty_comment_body_is_skipped(cache: Cache) -> None:
    seed(cache, fixture("reddit_comments.json"), fixture("reddit_submitted.json"))
    docs, _ = read(cache)
    assert "reddit-t1_c3" not in {d.source_id for d in docs}


def test_pagination_follows_after_until_null(cache: Cache) -> None:
    page1 = {
        "kind": "Listing",
        "data": {
            "after": "t1_next",
            "children": [
                {
                    "kind": "t1",
                    "data": {
                        "id": "p1",
                        "name": "t1_p1",
                        "author": USER,
                        "body": "page one body, long enough to stand on its own",
                        "created_utc": 1710000000.0,
                        "permalink": "/r/x/comments/a/t/p1/",
                        "link_title": "thread",
                        "subreddit": "x",
                    },
                }
            ],
        },
    }
    page2 = {
        "kind": "Listing",
        "data": {
            "after": None,
            "children": [
                {
                    "kind": "t1",
                    "data": {
                        "id": "p2",
                        "name": "t1_p2",
                        "author": USER,
                        "body": "page two body, long enough to stand on its own",
                        "created_utc": 1709000000.0,
                        "permalink": "/r/x/comments/a/t/p2/",
                        "link_title": "thread",
                        "subreddit": "x",
                    },
                }
            ],
        },
    }
    empty_submitted = {"kind": "Listing", "data": {"after": None, "children": []}}
    cache.put("reddit", f"{SITE}/user/{USER}/comments.json?limit={PER_PAGE}", page1)
    cache.put("reddit", f"{SITE}/user/{USER}/comments.json?limit={PER_PAGE}&after=t1_next", page2)
    cache.put("reddit", f"{SITE}/user/{USER}/submitted.json?limit={PER_PAGE}", empty_submitted)
    docs, stats = read(cache)
    assert {d.source_id for d in docs} == {"reddit-t1_p1", "reddit-t1_p2"}
    assert stats.comment_pages == 2


def test_a_403_style_failure_degrades_to_nothing_with_notes(cache: Cache) -> None:
    cache.offline = True  # nothing seeded: every read is a recorded miss
    docs, stats = read(cache)
    assert docs == []
    assert any("--offline" in note for note in stats.notes)
