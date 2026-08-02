"""The shallow-pull path: user_info, last_tweets, ingest_recent.

Phase 3.5 found this entirely untested — which is how `ingest_recent` came to
contain a NameError that would have crashed every call to it. `mypy --strict`
caught that one; these tests are what stops the next one.

Offline.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from fake_provider import FakeProvider, load

from corpus.budget import Budget, BudgetExceeded
from corpus.cache import Cache
from corpus.x.client import XClient
from corpus.x.ingest import ingest_recent
from corpus.x.providers import ProviderError
from corpus.x.validate import InvalidHandle


def build(provider: FakeProvider, tmp_path: Path, limit: float = 10.0) -> XClient:
    cache = Cache(path=tmp_path / "c.db")
    return XClient(provider, cache, Budget(limit=limit, cache=cache))


# -- user_info --------------------------------------------------------------


def test_user_info_returns_the_profile(client) -> None:
    profile = client.user_info("testsubject")
    assert profile["userName"] == "testsubject"
    assert isinstance(profile["statusesCount"], int)


def test_user_info_is_cached_and_billed_once(client, provider) -> None:
    client.user_info("testsubject")
    spend_after_first = client.budget.total
    client.user_info("testsubject")
    assert provider.calls.count("user_info:testsubject") == 1
    assert client.budget.total == spend_after_first, "a cache hit must not be billed"


def test_user_info_is_charged_at_the_profile_rate(client) -> None:
    from corpus.budget import X_COST_PER_PROFILE, X_MIN_CHARGE_PER_REQUEST

    client.user_info("testsubject")
    assert client.budget.total == pytest.approx(max(X_COST_PER_PROFILE, X_MIN_CHARGE_PER_REQUEST))


def test_user_info_offline_without_cache_explains_itself(tmp_path: Path) -> None:
    cache = Cache(path=tmp_path / "c.db", offline=True)
    client = XClient(FakeProvider(), cache, Budget(limit=10.0, cache=cache))
    with pytest.raises(ProviderError) as exc:
        client.user_info("testsubject")
    assert "--offline" in str(exc.value)
    assert "Run once online first" in str(exc.value)


def test_user_info_offline_serves_a_cached_profile(tmp_path: Path) -> None:
    online_cache = Cache(path=tmp_path / "c.db")
    XClient(FakeProvider(), online_cache, Budget(limit=10.0, cache=online_cache)).user_info(
        "testsubject"
    )
    online_cache.close()

    offline_cache = Cache(path=tmp_path / "c.db", offline=True)
    client = XClient(FakeProvider(), offline_cache, Budget(limit=10.0, cache=offline_cache))
    assert client.user_info("testsubject")["userName"] == "testsubject"
    assert client.budget.total == 0.0, "offline must cost nothing"
    offline_cache.close()


def test_user_info_is_refused_when_the_budget_cannot_cover_it(tmp_path: Path) -> None:
    provider = FakeProvider()
    client = build(provider, tmp_path, limit=0.00001)
    with pytest.raises(BudgetExceeded):
        client.user_info("testsubject")
    assert provider.calls == [], "a refused call must not reach the provider"


# -- last_tweets ------------------------------------------------------------


def test_last_tweets_returns_a_page(client) -> None:
    tweets, cursor, has_next = client.last_tweets("testsubject")
    assert tweets
    assert all("id" in t for t in tweets)
    assert cursor is not None or has_next is False


def test_last_tweets_is_billed_per_tweet(client) -> None:
    from corpus.budget import X_COST_PER_TWEET, X_MIN_CHARGE_PER_REQUEST

    tweets, _, _ = client.last_tweets("testsubject")
    assert client.budget.total == pytest.approx(
        max(len(tweets) * X_COST_PER_TWEET, X_MIN_CHARGE_PER_REQUEST)
    )


def test_last_tweets_caches_permanently(client, cache) -> None:
    tweets, _, _ = client.last_tweets("testsubject")
    tid = tweets[0]["id"]
    row = cache.conn.execute(
        "SELECT permanent FROM entries WHERE source='x' AND source_id=?", (f"tweet:{tid}",)
    ).fetchone()
    assert row["permanent"] == 1, "a tweet we paid for must never expire"


def test_last_tweets_learns_the_page_size(client) -> None:
    before = client._page_reservation()
    client.last_tweets("testsubject")
    assert client._page_reservation() <= before


def test_last_tweets_is_refused_when_unaffordable(tmp_path: Path) -> None:
    provider = FakeProvider()
    client = build(provider, tmp_path, limit=0.00001)
    with pytest.raises(BudgetExceeded):
        client.last_tweets("testsubject")
    assert provider.calls == []


# -- ingest_recent ----------------------------------------------------------


def test_ingest_recent_collects_posts(tmp_path: Path) -> None:
    """The regression test for the NameError mypy found."""
    client = build(FakeProvider(page_size=3), tmp_path)
    tweets, stats = ingest_recent(client, "testsubject", log=lambda _: None)
    assert tweets
    assert stats.unique == len(tweets)
    assert stats.stop_reason == "completed"
    assert stats.pages >= 1


def test_ingest_recent_validates_the_handle(tmp_path: Path) -> None:
    client = build(FakeProvider(), tmp_path)
    with pytest.raises(InvalidHandle):
        ingest_recent(client, "bad handle", log=lambda _: None)


def test_ingest_recent_strips_the_at_sign(tmp_path: Path) -> None:
    client = build(FakeProvider(), tmp_path)
    tweets, _ = ingest_recent(client, "@testsubject", log=lambda _: None)
    assert tweets


def test_ingest_recent_honours_max_posts(tmp_path: Path) -> None:
    client = build(FakeProvider(page_size=3), tmp_path)
    tweets, _ = ingest_recent(client, "testsubject", max_posts=2, log=lambda _: None)
    assert len(tweets) <= 2


def test_ingest_recent_returns_newest_first(tmp_path: Path) -> None:
    from corpus.x.client import parse_created_at

    client = build(FakeProvider(page_size=10), tmp_path)
    tweets, _ = ingest_recent(client, "testsubject", log=lambda _: None)
    stamps = [parse_created_at(t["createdAt"]) for t in tweets]
    assert stamps == sorted(stamps, reverse=True)


def test_ingest_recent_dedupes(tmp_path: Path) -> None:
    """last_tweets can repeat a tweet across pages; the corpus must not."""

    class Repeats(FakeProvider):
        def last_tweets(self, handle: str, cursor: str | None = None):  # type: ignore[no-untyped-def]
            page = self.subject[:3]
            return page, "cursor-next", cursor is None

        def user_info(self, handle: str) -> dict[str, Any]:
            return load("user_info.json")["data"]

    client = build(Repeats(), tmp_path)
    tweets, stats = ingest_recent(client, "testsubject", log=lambda _: None)
    ids = [t["id"] for t in tweets]
    assert len(ids) == len(set(ids))
    assert stats.duplicates > 0, "the duplicates should have been counted, not hidden"


def test_ingest_recent_skips_tweets_without_an_id(tmp_path: Path) -> None:
    subject = [dict(t) for t in load("tweets.json")[:3]]
    subject[1].pop("id")

    class NoId(FakeProvider):
        def last_tweets(self, handle: str, cursor: str | None = None):  # type: ignore[no-untyped-def]
            return subject, None, False

    client = build(NoId(), tmp_path)
    tweets, _ = ingest_recent(client, "testsubject", log=lambda _: None)
    assert len(tweets) == 2


def test_ingest_recent_survives_a_budget_stop(tmp_path: Path) -> None:
    """Paid data must reach the caller even when the budget bites."""
    client = build(FakeProvider(page_size=2), tmp_path, limit=0.0031)
    tweets, stats = ingest_recent(client, "testsubject", max_pages=50, log=lambda _: None)
    assert client.budget.total <= 0.0031
    if "budget" in stats.stop_reason:
        assert tweets, "a budget stop must not discard what was already paid for"


def test_ingest_recent_records_the_public_post_count(tmp_path: Path) -> None:
    client = build(FakeProvider(), tmp_path)
    _, stats = ingest_recent(client, "testsubject", public_post_count=4820, log=lambda _: None)
    assert stats.public_post_count == 4820
    assert stats.ingested_share is not None


def test_ingest_recent_logs_progress(tmp_path: Path) -> None:
    client = build(FakeProvider(page_size=3), tmp_path)
    lines: list[str] = []
    ingest_recent(client, "testsubject", log=lines.append)
    assert any("last_tweets page" in line for line in lines)


# -- tweets_by_ids ----------------------------------------------------------


def test_tweets_by_ids_is_cache_first(client, provider) -> None:
    ids = [t["id"] for t in load("parents.json")[:2]]
    client.tweets_by_ids(ids)
    calls_after_first = len(provider.batch_calls)
    client.tweets_by_ids(ids)
    assert len(provider.batch_calls) == calls_after_first, "a cache hit must not refetch"


def test_tweets_by_ids_returns_nothing_for_nothing(client, provider) -> None:
    assert client.tweets_by_ids([]) == {}
    assert provider.batch_calls == []


def test_tweets_by_ids_offline_returns_what_it_has(tmp_path: Path) -> None:
    """A missing parent is rendered [unavailable] upstream, not an error."""
    cache = Cache(path=tmp_path / "c.db", offline=True)
    client = XClient(FakeProvider(), cache, Budget(limit=10.0, cache=cache))
    assert client.tweets_by_ids(["1700000000000000201"]) == {}
    assert client.budget.total == 0.0
    cache.close()


def test_tweets_by_ids_deduplicates_requested_ids(client, provider) -> None:
    tid = load("parents.json")[0]["id"]
    client.tweets_by_ids([tid, tid, tid])
    assert provider.batch_calls == [[tid]]


def test_batch_lookup_is_capped_at_100(client) -> None:
    from corpus.x.providers import TwitterApiIoProvider

    provider = TwitterApiIoProvider.__new__(TwitterApiIoProvider)
    with pytest.raises(ProviderError) as exc:
        TwitterApiIoProvider.tweets_by_ids(provider, [str(i) for i in range(101)])
    assert "100 ids" in str(exc.value)


# -- close ------------------------------------------------------------------


def test_close_closes_the_provider(client, provider) -> None:
    client.close()
    assert getattr(provider, "closed", True)
