"""The Bluesky adapter, against a fixture written to the documented API shape.

Fixture provenance: `bluesky_author_feed.json` is SYNTHETIC — the repo's
offline rule means no live capture exists. The load-bearing cases are the
same classes of failure the GitHub capture taught, transposed:

* a repost item carries someone else's `post` — it must be skipped, not
  attributed;
* a `#blockedPost` parent has no record — the reply is kept with
  `[unavailable]` context, the X pipeline's rule;
* consecutive self-replies stitch into one thread document;
* an item whose author is not the subject is never ingested, however it got
  into the feed.

Everything runs from a pre-seeded cache; `http_client` is replaced with
something that fails the test if it is ever constructed.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from corpus.cache import Cache
from corpus.sources import base as base_module
from corpus.sources.bluesky import API, PER_PAGE, BlueskySource, _handle_of

FIXTURES = Path(__file__).parent / "fixtures"
HANDLE = "testsubject.bsky.social"


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
        raise AssertionError("the bluesky adapter opened an HTTP client during a test")

    monkeypatch.setattr(base_module, "http_client", forbidden)


def feed_url(handle: str, cursor: str | None = None) -> str:
    url = (
        f"{API}/app.bsky.feed.getAuthorFeed?actor={handle}"
        f"&limit={PER_PAGE}&filter=posts_with_replies"
    )
    return f"{url}&cursor={cursor}" if cursor else url


def seed(cache: Cache, feed) -> None:
    cache.put("bluesky", feed_url(HANDLE), feed)
    cache.put("bluesky", feed_url(HANDLE, "cursor-page-2"), {"feed": []})


def read(cache: Cache, **kwargs):
    return BlueskySource().fetch_with_stats(
        HANDLE, author_handle=HANDLE, cache=cache, log=lambda _: None, **kwargs
    )


# -- handle normalization ---------------------------------------------------


@pytest.mark.parametrize(
    "given",
    [
        HANDLE,
        f"@{HANDLE}",
        f"https://bsky.app/profile/{HANDLE}",
        f"https://bsky.app/profile/{HANDLE}/",
        f"  {HANDLE.upper()}  ",
    ],
)
def test_handle_normalization(given: str) -> None:
    assert _handle_of(given) == HANDLE


# -- the happy path -----------------------------------------------------------


def test_posts_and_replies_ingested_with_context(cache: Cache) -> None:
    seed(cache, fixture("bluesky_author_feed.json"))
    docs, stats = read(cache)
    by_id = {d.source_id: d for d in docs}

    reply = by_id["at://did:plc:subj/app.bsky.feed.post/reply1"]
    assert reply.kind == "reply"
    # The inline parent view is the hydration the X pipeline pays for.
    assert reply.context == "Floating point is simpler, just use that."
    assert reply.context_author == "someoneelse.bsky.social"
    assert reply.context_url == "https://bsky.app/profile/someoneelse.bsky.social/post/abc"
    assert reply.engagement == {"likes": 18, "replies": 2, "reposts": 1}

    original = by_id["at://did:plc:subj/app.bsky.feed.post/post1"]
    assert original.kind == "original"
    assert original.outbound_links == ["https://example.com/gate-design"]
    assert stats.posts >= 1 and stats.replies >= 1


def test_consecutive_self_replies_stitch_into_one_thread(cache: Cache) -> None:
    seed(cache, fixture("bluesky_author_feed.json"))
    docs, stats = read(cache)
    threads = [d for d in docs if d.part_count > 1]
    assert len(threads) == 1
    thread = threads[0]
    assert thread.kind == "thread"
    assert thread.part_count == 3
    assert thread.url == "https://bsky.app/profile/testsubject.bsky.social/post/root1"
    assert thread.body.index("Thread root") < thread.body.index("Part two")
    assert thread.body.index("Part two") < thread.body.index("Part three")
    # Engagement comes from the root only; later parts always under-count.
    assert thread.engagement["likes"] == 12
    assert stats.threads_stitched == 1
    # The parts are gone as standalone documents.
    assert "at://did:plc:subj/app.bsky.feed.post/self1" not in {d.source_id for d in docs}


def test_a_repost_is_never_attributed_to_the_subject(cache: Cache) -> None:
    seed(cache, fixture("bluesky_author_feed.json"))
    docs, stats = read(cache)
    assert all("Someone else's words" not in d.body for d in docs)
    assert stats.reposts_skipped == 1


def test_a_blocked_parent_is_unavailable_not_dropped(cache: Cache) -> None:
    seed(cache, fixture("bluesky_author_feed.json"))
    docs, stats = read(cache)
    reply = next(d for d in docs if d.source_id.endswith("/reply2"))
    assert reply.kind == "reply"
    assert reply.context == "[unavailable]"
    assert stats.unavailable_parents == 1


def test_an_item_by_another_author_is_never_ingested(cache: Cache) -> None:
    seed(cache, fixture("bluesky_author_feed.json"))
    docs, _ = read(cache)
    assert all("never be attributed" not in d.body for d in docs)


# -- degradation ---------------------------------------------------------------


def test_an_empty_feed_is_a_result_not_a_crash(cache: Cache) -> None:
    seed(cache, {"feed": []})
    docs, stats = read(cache)
    assert docs == []
    assert stats.posts == 0


def test_a_fetch_failure_degrades_to_nothing_with_notes(cache: Cache) -> None:
    # Nothing seeded; the reader is offline, so every get is a recorded miss.
    cache.offline = True
    docs, stats = read(cache)
    assert docs == []
    assert any("--offline" in note for note in stats.notes)


def test_malformed_items_are_skipped_not_fatal(cache: Cache) -> None:
    seed(
        cache,
        {
            "feed": [
                {"post": "not a dict"},
                {"post": {"author": {"handle": HANDLE}, "record": {"text": ""}, "uri": "at://x"}},
                {"post": {"author": {"handle": "wrong.bsky.social"}, "record": {"text": "x"}}},
                "not even a dict",
            ]
        },
    )
    docs, _ = read(cache)
    assert docs == []


def test_date_range_is_reported_from_real_dates(cache: Cache) -> None:
    seed(cache, fixture("bluesky_author_feed.json"))
    docs, stats = read(cache)
    assert stats.earliest == "2024-05-01"
    assert stats.latest == "2024-05-04"
    assert all(not d.date_unknown for d in docs)
