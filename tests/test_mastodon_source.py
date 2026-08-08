"""The Mastodon adapter, against fixtures written to the documented shape.

Fixture provenance: `mastodon_*.json` are SYNTHETIC — the repo's offline rule
means no live capture exists. The load-bearing cases:

* a status with `visibility: private` is skipped and counted — the scope
  boundary is enforced per document, never worked around;
* `in_reply_to_id` is hydrated one request at a time, capped and cached
  permanently, and an unreadable parent becomes `[unavailable]`;
* consecutive self-replies stitch into one thread document;
* `content` is HTML and must come out as text with its links kept.

Everything runs from a pre-seeded cache; `http_client` is replaced with
something that fails the test if it is ever constructed.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from corpus.cache import Cache
from corpus.sources import base as base_module
from corpus.sources.mastodon import PER_PAGE, MastodonSource, parse_account

FIXTURES = Path(__file__).parent / "fixtures"
ACCT = "@testsubject@mastodon.example"
HOST = "mastodon.example"


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
        raise AssertionError("the mastodon adapter opened an HTTP client during a test")

    monkeypatch.setattr(base_module, "http_client", forbidden)


def seed(cache: Cache) -> None:
    statuses = fixture("mastodon_statuses.json")
    cache.put(
        "mastodon",
        f"https://{HOST}/api/v1/accounts/lookup?acct=testsubject",
        fixture("mastodon_lookup.json"),
    )
    cache.put(
        "mastodon",
        f"https://{HOST}/api/v1/accounts/1099/statuses?limit={PER_PAGE}&exclude_reblogs=true",
        statuses,
    )
    cache.put(
        "mastodon",
        f"https://{HOST}/api/v1/statuses/201",
        fixture("mastodon_parent.json"),
        permanent=True,
    )
    # Self-reply parents resolve to statuses already in the feed.
    by_id = {s["id"]: s for s in statuses}
    cache.put("mastodon", f"https://{HOST}/api/v1/statuses/101", by_id["101"], permanent=True)
    cache.put("mastodon", f"https://{HOST}/api/v1/statuses/103", by_id["103"], permanent=True)


def read(cache: Cache, target: str = ACCT, **kwargs):
    return MastodonSource().fetch_with_stats(
        target, author_handle="testsubject", cache=cache, log=lambda _: None, **kwargs
    )


@pytest.mark.parametrize(
    "given",
    [
        "testsubject@mastodon.example",
        ACCT,
        "https://mastodon.example/@testsubject",
        "https://mastodon.example/@testsubject/",
        "https://mastodon.example/users/testsubject",
    ],
)
def test_account_parsing(given: str) -> None:
    assert parse_account(given) == ("testsubject", HOST)


def test_posts_and_replies_ingested_with_context(cache: Cache) -> None:
    seed(cache)
    docs, stats = read(cache)
    reply = next(d for d in docs if d.source_id == "102")
    assert reply.kind == "reply"
    assert reply.context == "Floating point is simpler, just use that."
    assert reply.context_author == "someoneelse"
    assert reply.context_url == "https://mastodon.example/@someoneelse/201"
    assert reply.engagement["likes"] == 6
    assert stats.parents_hydrated == 3  # 201, plus the two self-reply parents


def test_consecutive_self_replies_stitch_into_one_thread(cache: Cache) -> None:
    seed(cache)
    docs, stats = read(cache)
    threads = [d for d in docs if d.part_count > 1]
    assert len(threads) == 1
    thread = threads[0]
    assert thread.kind == "thread"
    assert thread.part_count == 3
    assert thread.url == "https://mastodon.example/@testsubject/101"
    assert thread.body.index("Thread root") < thread.body.index("Part two")
    assert thread.body.index("Part two") < thread.body.index("Part three")
    assert thread.outbound_links == ["https://example.com/gate-design"]
    assert stats.threads_stitched == 1


def test_a_private_status_is_never_read(cache: Cache) -> None:
    seed(cache)
    docs, stats = read(cache)
    assert all("follower-only" not in d.body for d in docs)
    assert stats.non_public_skipped == 1


def test_an_empty_status_is_skipped(cache: Cache) -> None:
    seed(cache)
    docs, _ = read(cache)
    assert "106" not in {d.source_id for d in docs}


def test_an_unavailable_parent_is_kept_as_unavailable(cache: Cache) -> None:
    seed(cache)
    cache.offline = True  # parents 201/101/103 are permanent, so they survive
    # Re-seed only the page + lookup, drop nothing: instead run a fresh cache
    # where the parent lookup misses.
    c2 = Cache(path=cache.path.parent / "cache2.db")
    try:
        statuses = fixture("mastodon_statuses.json")
        c2.put(
            "mastodon",
            f"https://{HOST}/api/v1/accounts/lookup?acct=testsubject",
            fixture("mastodon_lookup.json"),
        )
        c2.put(
            "mastodon",
            f"https://{HOST}/api/v1/accounts/1099/statuses?limit={PER_PAGE}&exclude_reblogs=true",
            statuses,
        )
        c2.offline = True
        docs, stats = read(c2)
    finally:
        c2.close()
    reply = next(d for d in docs if d.source_id == "102")
    assert reply.context == "[unavailable]"
    assert stats.parents_unavailable >= 1


def test_an_unknown_account_is_a_result_not_a_crash(cache: Cache) -> None:
    cache.put(
        "mastodon",
        f"https://{HOST}/api/v1/accounts/lookup?acct=ghost",
        {"error": "Record not found"},
    )
    cache.offline = True
    docs, stats = read(cache, target="@ghost@mastodon.example")
    assert docs == []
    assert any("no account" in note for note in stats.notes)


def test_an_unparseable_target_says_what_it_wanted(cache: Cache) -> None:
    docs, stats = read(cache, target="not-an-account")
    assert docs == []
    assert any("@user@host" in note for note in stats.notes)
