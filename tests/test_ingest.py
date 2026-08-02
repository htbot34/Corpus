"""Ingestion tests, including both documented provider regressions."""

from __future__ import annotations

from datetime import datetime, timezone
from itertools import pairwise

from fake_provider import FakeProvider, load, tweet_ts

from corpus.budget import Budget
from corpus.cache import Cache
from corpus.x.client import XClient
from corpus.x.ingest import ingest_timeline

# The fixture corpus runs 2024-01-08 .. 2024-08-16 and contains a deliberate
# 160-day silence in the middle. Anchor "now" just past the last post so the
# window walk has to cross that gap.
NOW = datetime(2024, 9, 1, tzinfo=timezone.utc)
SINCE = datetime(2023, 12, 1, tzinfo=timezone.utc)
# Crossing the 160-day hiatus at 30-day windows needs a tolerance above 5.
GAP_TOLERANT = 8


def build(provider: FakeProvider, tmp_path, limit: float = 10.0) -> XClient:
    cache = Cache(path=tmp_path / "c.db")
    return XClient(provider, cache, Budget(limit=limit, cache=cache))


def test_ingests_full_corpus(tmp_path):
    provider = FakeProvider()
    client = build(provider, tmp_path)
    tweets, stats = ingest_timeline(
        client,
        "testsubject",
        since=SINCE,
        until=NOW,
        window_days=30,
        empty_window_tolerance=GAP_TOLERANT,
        log=lambda _: None,
    )
    assert len(tweets) == len(load("tweets.json"))
    assert stats.dedupe_rate < 0.25
    assert stats.unique == len(tweets)


def test_long_hiatus_truncates_when_the_tolerance_is_set_low(tmp_path):
    """The documented tradeoff of failure mode 2, still true at a low tolerance.

    A lying empty window is indistinguishable from a genuinely silent one, so a
    160-day hiatus at 30-day windows and tolerance 3 ends the walk. The probe is
    disabled here to isolate the tolerance rule itself. It must be recorded in
    stop_reason, not silently swallowed — the report surfaces it as a caveat.
    """
    provider = FakeProvider()
    client = build(provider, tmp_path)
    tweets, stats = ingest_timeline(
        client,
        "testsubject",
        since=SINCE,
        until=NOW,
        window_days=30,
        empty_window_tolerance=3,
        probe_enabled=False,
        log=lambda _: None,
    )
    assert "consecutive empty windows" in stats.stop_reason
    assert 0 < len(tweets) < len(load("tweets.json")), "the recent side is still collected"
    assert stats.empty_window_ranges
    # Phase 2.3: a truncated run must say so loudly enough for the report.
    assert stats.stopped_on_tolerance
    assert stats.last_date_reached


def test_default_tolerance_crosses_the_160_day_hiatus(tmp_path):
    """Phase 2.3: the old default of 3 truncated the corpus on a real silence.

    Founders and writers go quiet for a quarter routinely, and 30-day windows at
    tolerance 3 gives up after 90 days. The fixture's deliberate 160-day gap is
    exactly that case, and at the new defaults the whole history is collected.
    """
    provider = FakeProvider()
    client = build(provider, tmp_path)
    tweets, stats = ingest_timeline(
        client, "testsubject", since=SINCE, until=NOW, log=lambda _: None
    )
    assert len(tweets) == len(load("tweets.json")), (
        f"lost {len(load('tweets.json')) - len(tweets)} posts to the hiatus"
    )
    assert not stats.stopped_on_tolerance


def test_uses_since_time_not_since(tmp_path):
    """The date-level operators are no longer honoured by the index."""
    provider = FakeProvider()
    client = build(provider, tmp_path)
    ingest_timeline(
        client, "testsubject", since=SINCE, until=NOW, window_days=30, log=lambda _: None
    )
    searches = [c for c in provider.calls if c.startswith("search:")]
    assert searches
    for call in searches:
        assert "since_time:" in call and "until_time:" in call
        assert " since:" not in call and " until:" not in call


def test_survives_duplicate_cursor_pages(tmp_path):
    """Failure mode 1: same tweets returned again under a fresh cursor.

    A naive cursor loop never terminates here. We must break out, record it,
    and still finish with the full corpus.
    """
    provider = FakeProvider(page_size=3, duplicate_cursor_windows=4)
    client = build(provider, tmp_path)
    tweets, stats = ingest_timeline(
        client,
        "testsubject",
        since=SINCE,
        until=NOW,
        window_days=45,
        max_pages=20,
        empty_window_tolerance=GAP_TOLERANT,
        log=lambda _: None,
    )
    assert stats.cursor_repeat_breaks > 0, "the repeat guard should have fired"
    assert stats.pages < 200, "must not spin on a repeating cursor"
    assert len(tweets) > 10


def test_empty_window_is_not_end_of_history(tmp_path):
    """Failure mode 2: a window lies about being empty.

    Tolerance must carry us past it, and the gap must be logged rather than
    silently swallowed.
    """
    provider = FakeProvider(lying_empty_windows=2)
    client = build(provider, tmp_path)
    logged: list[str] = []
    tweets, stats = ingest_timeline(
        client,
        "testsubject",
        since=SINCE,
        until=NOW,
        window_days=30,
        empty_window_tolerance=GAP_TOLERANT,
        log=logged.append,
    )
    assert stats.empty_windows >= 2
    assert stats.empty_window_ranges, "every empty window must be recorded"
    assert any("[empty]" in line for line in logged), "empty windows must be logged"
    # We kept going and still collected the later material.
    assert len(tweets) > 5


def test_stops_after_tolerance_exceeded(tmp_path):
    provider = FakeProvider(lying_empty_windows=99)
    client = build(provider, tmp_path)
    tweets, stats = ingest_timeline(
        client,
        "testsubject",
        since=SINCE,
        until=NOW,
        window_days=30,
        empty_window_tolerance=3,
        log=lambda _: None,
    )
    assert tweets == []
    assert "consecutive empty windows" in stats.stop_reason
    assert stats.max_consecutive_empty == 3


def test_max_posts_stops_the_walk(tmp_path):
    provider = FakeProvider(page_size=2)
    client = build(provider, tmp_path)
    tweets, stats = ingest_timeline(
        client,
        "testsubject",
        since=SINCE,
        until=NOW,
        max_posts=6,
        empty_window_tolerance=GAP_TOLERANT,
        log=lambda _: None,
    )
    assert len(tweets) == 6
    assert "max-posts" in stats.stop_reason


def test_windows_do_not_overlap(tmp_path):
    provider = FakeProvider()
    client = build(provider, tmp_path)
    ingest_timeline(
        client,
        "testsubject",
        since=SINCE,
        until=NOW,
        window_days=30,
        empty_window_tolerance=GAP_TOLERANT,
        log=lambda _: None,
    )
    windows = provider._windows_seen
    for (s1, u1), (s2, u2) in pairwise(windows):
        assert u2 <= s1, f"windows overlap: {(s1, u1)} then {(s2, u2)}"
        assert u2 < u1, "the walk must always move backwards"


def test_truncated_window_re_covers_rather_than_skipping(tmp_path):
    """When a window is cut short we may re-read, but must never leave a hole.

    A repeating cursor abandons a window mid-way. The unexplored region below
    the oldest tweet we saw has to be revisited, even at the cost of duplicate
    reads — a gap in the history is worse than a few cents.
    """
    provider = FakeProvider(page_size=2, duplicate_cursor_windows=3)
    client = build(provider, tmp_path)
    tweets, stats = ingest_timeline(
        client,
        "testsubject",
        since=SINCE,
        until=NOW,
        window_days=30,
        empty_window_tolerance=GAP_TOLERANT,
        log=lambda _: None,
    )
    assert stats.cursor_repeat_breaks > 0
    ids = {t["id"] for t in tweets}
    # The thread's later parts sit below the first page of their window; if a
    # truncated window skipped ahead they would be missing entirely.
    assert "1700000000000000104" in ids


def test_budget_stop_preserves_partial_results(tmp_path):
    provider = FakeProvider(page_size=2)
    client = build(provider, tmp_path, limit=0.003)
    tweets, stats = ingest_timeline(
        client,
        "testsubject",
        since=SINCE,
        until=NOW,
        empty_window_tolerance=GAP_TOLERANT,
        log=lambda _: None,
    )
    assert "budget" in stats.stop_reason
    assert tweets, "paid data must survive a budget stop"


def test_budget_is_never_exceeded_during_ingestion(tmp_path):
    """Phase 2.1: the stop is pre-flight, so the limit is a ceiling not a trigger.

    Before reservations, ingestion charged after each page returned and raised
    only once the total had already crossed the limit — so the limit was the
    point at which we noticed, not the point at which we stopped.
    """
    provider = FakeProvider(page_size=2)
    client = build(provider, tmp_path, limit=0.003)
    tweets, stats = ingest_timeline(
        client,
        "testsubject",
        since=SINCE,
        until=NOW,
        empty_window_tolerance=GAP_TOLERANT,
        log=lambda _: None,
    )
    assert client.budget.total <= 0.003, f"spent ${client.budget.total:.5f} against a $0.003 budget"
    assert tweets and "budget" in stats.stop_reason


def test_budget_too_small_for_one_call_refuses_before_spending(tmp_path):
    """A budget below one worst-case page buys nothing, and says so."""
    provider = FakeProvider(page_size=2)
    client = build(provider, tmp_path, limit=0.0006)
    tweets, stats = ingest_timeline(
        client, "testsubject", since=SINCE, until=NOW, log=lambda _: None
    )
    assert tweets == []
    assert client.budget.total == 0.0, "refused calls must not be billed"
    assert "pre-flight" in stats.stop_reason
    # The message has to tell the user what to do about it.
    assert "--budget-mode advisory" in stats.stop_reason


def test_advisory_mode_allows_what_strict_refuses(tmp_path):
    """--budget-mode advisory preserves the original charge-then-notice behaviour."""
    cache = Cache(path=tmp_path / "c.db")
    budget = Budget(limit=0.0006, cache=cache, mode="advisory")
    client = XClient(FakeProvider(page_size=2), cache, budget)
    tweets, _ = ingest_timeline(client, "testsubject", since=SINCE, until=NOW, log=lambda _: None)
    assert tweets, "advisory mode must not refuse the call"
    assert budget.refusals, "but it must still record that the budget was blown"


def test_results_are_newest_first(tmp_path):
    provider = FakeProvider()
    client = build(provider, tmp_path)
    tweets, _ = ingest_timeline(
        client,
        "testsubject",
        since=SINCE,
        until=NOW,
        empty_window_tolerance=GAP_TOLERANT,
        log=lambda _: None,
    )
    stamps = [tweet_ts(t) for t in tweets]
    assert stamps == sorted(stamps, reverse=True)
