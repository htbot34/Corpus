"""Phase 2.3: the hiatus truncation, and the probe that replaces the guess.

The old default — window_days=30, empty_window_tolerance=3 — gave up after 90
days of silence and discarded everything older, silently enough that the run
still looked successful. For the actual targets (founders, writers) a quarter of
silence is ordinary.

Two changes: the default tolerance is 8, and on reaching it the walk spends one
more call sweeping twelve months before concluding history has ended. That turns
"probably the end" into evidence for about $0.0002.

Offline.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

import pytest
from fake_provider import FakeProvider

from corpus.budget import Budget
from corpus.cache import Cache
from corpus.x.client import XClient
from corpus.x.ingest import (
    DEFAULT_EMPTY_WINDOW_TOLERANCE,
    HIATUS_PROBE_DAYS,
    IngestStats,
    ingest_timeline,
)

NOW = datetime(2024, 9, 1, tzinfo=timezone.utc)
SINCE = datetime(2023, 12, 1, tzinfo=timezone.utc)


def build(provider: FakeProvider, tmp_path, limit: float = 10.0) -> XClient:
    cache = Cache(path=tmp_path / "c.db")
    return XClient(provider, cache, Budget(limit=limit, cache=cache))


def _ts(dt: datetime) -> str:
    return dt.strftime("%a %b %d %H:%M:%S %z %Y")


def corpus_with_gap(gap_days: int, *, n_recent: int = 6, n_old: int = 6) -> list[dict[str, Any]]:
    """Posts on both sides of a silence of `gap_days`."""
    tweets: list[dict[str, Any]] = []
    for i in range(n_recent):
        when = NOW - timedelta(days=3 + i * 4)
        tweets.append(_tweet(f"9{i:03d}", when))
    oldest_recent = NOW - timedelta(days=3 + (n_recent - 1) * 4)
    for i in range(n_old):
        when = oldest_recent - timedelta(days=gap_days + i * 4)
        tweets.append(_tweet(f"1{i:03d}", when))
    return tweets


def _tweet(tid: str, when: datetime) -> dict[str, Any]:
    return {
        "id": tid,
        "text": f"post {tid}",
        "createdAt": _ts(when),
        "isReply": False,
        "author": {"userName": "testsubject", "id": "1"},
        "entities": {},
    }


# -- defaults ---------------------------------------------------------------


def test_default_tolerance_is_eight():
    """3 x 30 days = a 90-day ceiling on silence, which is too tight."""
    assert DEFAULT_EMPTY_WINDOW_TOLERANCE == 8


def test_signature_default_matches_the_constant():
    import inspect

    sig = inspect.signature(ingest_timeline)
    assert sig.parameters["empty_window_tolerance"].default == DEFAULT_EMPTY_WINDOW_TOLERANCE


def test_probe_window_is_twelve_months():
    assert HIATUS_PROBE_DAYS == 365


# -- the probe --------------------------------------------------------------


def test_probe_crosses_a_silence_longer_than_the_tolerance(tmp_path):
    """A 200-day gap defeats even tolerance 8 at 30-day windows. The probe wins."""
    tweets = corpus_with_gap(200)
    client = build(FakeProvider(subject=tweets, page_size=50), tmp_path)

    got, stats = ingest_timeline(
        client,
        "testsubject",
        since=datetime(2022, 1, 1, tzinfo=timezone.utc),
        until=NOW,
        empty_window_tolerance=3,  # deliberately tight, to force the probe
        log=lambda _: None,
    )

    assert stats.hiatus_probes >= 1, "the probe never ran"
    assert stats.hiatus_probes_productive >= 1, "the probe ran but found nothing"
    assert len(got) == len(tweets), f"lost {len(tweets) - len(got)} posts across the gap"
    assert stats.probe_recovered_ranges
    # The walk does eventually stop on the tolerance rule — at the true end of
    # history, below the oldest post, with a probe confirming nothing older.
    # That is a different claim from "we gave up", and the report must not
    # conflate them.
    assert stats.probe_confirmed_end


def test_probe_is_cheap(tmp_path):
    """One extra call, not a second walk."""
    tweets = corpus_with_gap(200)
    client = build(FakeProvider(subject=tweets, page_size=50), tmp_path)

    _, stats = ingest_timeline(
        client,
        "testsubject",
        since=datetime(2022, 1, 1, tzinfo=timezone.utc),
        until=NOW,
        empty_window_tolerance=3,
        log=lambda _: None,
    )
    assert stats.hiatus_probes <= 3, "probing should be rare, not routine"
    assert client.budget.total < 0.01


def test_probe_finding_nothing_stops_with_confidence(tmp_path):
    """When history really has ended, the probe converts a guess into evidence."""
    tweets = [_tweet(f"9{i:03d}", NOW - timedelta(days=3 + i * 4)) for i in range(6)]
    client = build(FakeProvider(subject=tweets, page_size=50), tmp_path)

    _, stats = ingest_timeline(
        client,
        "testsubject",
        since=datetime(2020, 1, 1, tzinfo=timezone.utc),
        until=NOW,
        empty_window_tolerance=3,
        log=lambda _: None,
    )

    assert stats.hiatus_probes >= 1
    assert stats.hiatus_probes_productive == 0
    assert stats.stopped_on_tolerance
    assert stats.probe_confirmed_end, "a probe that found nothing is evidence"
    assert "probe" in stats.stop_reason
    assert "found nothing" in stats.stop_reason


def test_probe_can_be_disabled(tmp_path):
    tweets = corpus_with_gap(200)
    client = build(FakeProvider(subject=tweets, page_size=50), tmp_path)
    _, stats = ingest_timeline(
        client,
        "testsubject",
        since=datetime(2022, 1, 1, tzinfo=timezone.utc),
        until=NOW,
        empty_window_tolerance=3,
        probe_enabled=False,
        log=lambda _: None,
    )
    assert stats.hiatus_probes == 0
    assert stats.stopped_on_tolerance
    assert not stats.probe_confirmed_end, "no probe ran, so nothing was confirmed"


def test_probe_does_not_rewalk_ground_it_already_covered(tmp_path):
    """Resuming below the newest post the probe found, not at the probe ceiling."""
    tweets = corpus_with_gap(200)
    client = build(FakeProvider(subject=tweets, page_size=50), tmp_path)
    _, stats = ingest_timeline(
        client,
        "testsubject",
        since=datetime(2022, 1, 1, tzinfo=timezone.utc),
        until=NOW,
        empty_window_tolerance=3,
        log=lambda _: None,
    )
    assert stats.dedupe_rate < 0.5, f"dedupe rate {stats.dedupe_rate:.0%} — re-walking"


def test_probe_respects_the_since_floor(tmp_path):
    """A probe must never reach below --since; that data was not asked for."""
    tweets = corpus_with_gap(200)
    floor = NOW - timedelta(days=120)
    client = build(FakeProvider(subject=tweets, page_size=50), tmp_path)
    got, _ = ingest_timeline(
        client,
        "testsubject",
        since=floor,
        until=NOW,
        empty_window_tolerance=3,
        log=lambda _: None,
    )
    from corpus.x.client import parse_created_at

    assert all(parse_created_at(t["createdAt"]) >= floor for t in got)


def test_probe_survives_a_bad_timestamp(tmp_path):
    """2.6 and 2.3 interact: the probe parses timestamps too."""
    tweets = corpus_with_gap(200)
    tweets[-1] = {**tweets[-1], "createdAt": "not-a-date"}
    client = build(FakeProvider(subject=tweets, page_size=50), tmp_path)
    got, stats = ingest_timeline(
        client,
        "testsubject",
        since=datetime(2022, 1, 1, tzinfo=timezone.utc),
        until=NOW,
        empty_window_tolerance=3,
        log=lambda _: None,
    )
    assert stats.timestamp_errors >= 1
    assert len(got) == len(tweets) - 1


# -- surfacing --------------------------------------------------------------


def test_ingested_share_is_computed():
    stats = IngestStats(unique=400, public_post_count=53_901)
    assert stats.ingested_share == pytest.approx(0.0074, abs=0.0005)
    assert stats.as_dict()["public_post_count"] == 53_901


def test_ingested_share_is_none_without_a_profile_count():
    assert IngestStats(unique=400).ingested_share is None


def test_truncation_is_stated_prominently_in_the_report():
    from corpus.render import render_report

    report = render_report(
        handle="testsubject",
        synthesis=None,
        docs=[],
        signals={"date_range": "2024"},
        budget_lines=["Total: $0.00"],
        run_meta={
            "ingest": {
                "stopped_on_tolerance": True,
                "last_date_reached": "2021-06-04",
                "unique": 400,
                "public_post_count": 53_901,
                "ingested_share": 0.0074,
                "stop_reason": "8 consecutive empty windows",
            }
        },
    )
    assert "HISTORY MAY BE INCOMPLETE" in report
    assert "2021-06-04" in report, "the report must name the last date reached"
    assert "400 of 53,901" in report or ("400" in report and "53,901" in report)


def test_full_coverage_is_not_flagged_as_truncated():
    from corpus.render import render_report

    report = render_report(
        handle="testsubject",
        synthesis=None,
        docs=[],
        signals={},
        budget_lines=[],
        run_meta={"ingest": {"stopped_on_tolerance": False, "stop_reason": "completed"}},
    )
    assert "HISTORY MAY BE INCOMPLETE" not in report


def test_probe_confirmed_end_reads_as_evidence_not_a_warning():
    """Crying wolf on a confirmed-complete history devalues the real warning."""
    from corpus.render import render_report

    report = render_report(
        handle="testsubject",
        synthesis=None,
        docs=[],
        signals={},
        budget_lines=[],
        run_meta={
            "ingest": {
                "stopped_on_tolerance": True,
                "probe_confirmed_end": True,
                "last_date_reached": "2009-04-01",
                "stop_reason": "8 consecutive empty windows, and a 365-day probe found nothing",
            }
        },
    )
    assert "HISTORY MAY BE INCOMPLETE" not in report
    assert "most likely the full public history" in report
    assert "2009-04-01" in report
