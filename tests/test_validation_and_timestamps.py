"""Phase 2.2 (handle validation) and Phase 2.6 (silent timestamp fallback).

Both are the same class of bug: an input the code accepts, silently reinterprets,
and then reports a confident wrong answer about. Neither raised anything.

Offline.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from corpus.x.client import (
    TimestampParseError,
    normalize_tweet,
    parse_created_at,
)
from corpus.x.ingest import IngestCorruption, ingest_timeline
from corpus.x.validate import InvalidHandle, validate_handle
from fake_provider import FakeProvider, load

NOW = datetime(2024, 9, 1, tzinfo=timezone.utc)
SINCE = datetime(2023, 12, 1, tzinfo=timezone.utc)
GAP_TOLERANT = 8


# ==========================================================================
# 2.2 handle validation
# ==========================================================================


@pytest.mark.parametrize(
    "raw", ["paulg", "@paulg", "  paulg  ", "a", "A_1", "_underscore_", "x" * 15]
)
def test_valid_handles_are_accepted(raw: str) -> None:
    assert validate_handle(raw) == raw.strip().lstrip("@")


@pytest.mark.parametrize(
    "raw",
    [
        "paul g",                      # whitespace starts a new search term
        "paulg since_time:0",          # injects an operator
        "paulg OR from:someoneelse",   # changes the query entirely
        'paulg"',                      # opens a phrase match
        "paulg -filter:replies",       # negation
        "(paulg)",
        "paul*",
        "x" * 16,                      # over X's 15-char limit
        "",
        "@",
        "paul.g",
        "paul-g",
        "email@example.com",
    ],
)
def test_query_altering_handles_are_rejected(raw: str) -> None:
    with pytest.raises(InvalidHandle):
        validate_handle(raw)


def test_error_names_the_offending_input() -> None:
    with pytest.raises(InvalidHandle) as exc:
        validate_handle("paulg since_time:0")
    message = str(exc.value)
    assert "paulg since_time:0" in message, "the error must quote what was given"
    assert "--x" in message, "and name the flag it came from"
    assert "search query" in message, "and say why it matters"


def test_error_explains_operator_characters() -> None:
    with pytest.raises(InvalidHandle) as exc:
        validate_handle("paul:g")
    assert "search operator" in str(exc.value)


def test_length_error_is_specific() -> None:
    with pytest.raises(InvalidHandle) as exc:
        validate_handle("a" * 30)
    assert "at most 15" in str(exc.value) and "30" in str(exc.value)


def test_ingest_validates_even_when_called_directly(client) -> None:
    """ingest_timeline is public; it cannot rely on the CLI having checked."""
    with pytest.raises(InvalidHandle):
        ingest_timeline(client, "bad handle", since=SINCE, until=NOW, log=lambda _: None)


def test_injection_never_reaches_the_provider(client, provider) -> None:
    with pytest.raises(InvalidHandle):
        ingest_timeline(
            client, "x OR from:victim", since=SINCE, until=NOW, log=lambda _: None
        )
    assert provider.calls == []


# ==========================================================================
# 2.6 timestamps
# ==========================================================================


def test_the_format_the_live_api_actually_returns() -> None:
    """Confirmed 2026-07-31: ISO 8601, six-digit microseconds, Z suffix."""
    assert parse_created_at("2010-08-27T20:13:59.000000Z") == datetime(
        2010, 8, 27, 20, 13, 59, tzinfo=timezone.utc
    )


def test_the_legacy_format_still_parses() -> None:
    assert parse_created_at("Mon Mar 03 12:00:00 +0000 2014") == datetime(
        2014, 3, 3, 12, 0, tzinfo=timezone.utc
    )


@pytest.mark.parametrize(
    "value",
    [
        "2024-01-08T15:30:00+00:00",
        "2024-01-08 15:30:00+0000",
        "2024-01-08T15:30:00Z",
        1704727800,
        1704727800.5,
    ],
)
def test_other_accepted_shapes(value: object) -> None:
    assert parse_created_at(value).tzinfo is not None


@pytest.mark.parametrize(
    "value",
    ["", "   ", None, "not a date", "2024-13-45T99:99:99Z", [], {}, True, "0000-00-00"],
)
def test_unparseable_values_raise_instead_of_returning_now(value: object) -> None:
    """The bug: this used to return datetime.now(), silently."""
    with pytest.raises(TimestampParseError):
        parse_created_at(value)


def test_the_exception_carries_the_offending_value() -> None:
    with pytest.raises(TimestampParseError) as exc:
        parse_created_at("garbage")
    assert exc.value.value == "garbage"
    assert "garbage" in str(exc.value)


def test_naive_timestamps_are_treated_as_utc() -> None:
    """Assuming local time would shift every post by the runner's offset."""
    assert parse_created_at("2024-01-08T15:30:00") == datetime(
        2024, 1, 8, 15, 30, tzinfo=timezone.utc
    )


def test_normalize_tweet_propagates_the_error() -> None:
    with pytest.raises(TimestampParseError):
        normalize_tweet({"id": "1", "text": "hi", "createdAt": "garbage"})


def test_normalize_tweet_accepts_a_fallback_for_hydrated_parents() -> None:
    """A parent's date is not load-bearing; losing its text would be."""
    when = datetime(2024, 1, 1, tzinfo=timezone.utc)
    doc = normalize_tweet(
        {"id": "1", "text": "parent text", "createdAt": "garbage"},
        timestamp_fallback=when,
    )
    assert doc.body == "parent text"
    assert doc.published_at == when


# -- the run must survive one bad document ---------------------------------


def _corrupt_one(subject: list[dict], index: int = 3) -> list[dict]:
    tweets = [dict(t) for t in subject]
    tweets[index] = {**tweets[index], "createdAt": "not-a-timestamp"}
    return tweets


def test_ingestion_skips_the_bad_document_and_keeps_going(tmp_path) -> None:
    from corpus.budget import Budget
    from corpus.cache import Cache
    from corpus.x.client import XClient

    subject = load("tweets.json")
    cache = Cache(path=tmp_path / "c.db")
    client = XClient(
        FakeProvider(subject=_corrupt_one(subject)), cache, Budget(limit=10.0, cache=cache)
    )
    tweets, stats = ingest_timeline(
        client,
        "testsubject",
        since=SINCE,
        until=NOW,
        empty_window_tolerance=GAP_TOLERANT,
        log=lambda _: None,
    )

    assert stats.timestamp_errors >= 1
    assert stats.timestamp_error_samples
    assert len(tweets) == len(subject) - 1, "exactly one document should be lost"
    assert "budget" not in stats.stop_reason, "the run must not have been cut short"


def test_a_bad_timestamp_cannot_stall_the_walk(tmp_path) -> None:
    """The real damage: window_earliest = now, so the walk advances 1s/window.

    With every timestamp unparseable the walk must terminate quickly rather than
    inching backwards a second at a time while paying full price per page.
    """
    from corpus.budget import Budget
    from corpus.cache import Cache
    from corpus.x.client import XClient

    subject = [
        {**t, "createdAt": "not-a-timestamp"} for t in load("tweets.json")
    ]
    cache = Cache(path=tmp_path / "c.db")
    budget = Budget(limit=10.0, cache=cache)
    client = XClient(FakeProvider(subject=subject), cache, budget)

    tweets, stats = ingest_timeline(
        client,
        "testsubject",
        since=SINCE,
        until=NOW,
        empty_window_tolerance=3,
        log=lambda _: None,
    )

    assert tweets == []
    assert stats.timestamp_errors > 0
    # The guard that matters: a bounded number of windows, not thousands.
    assert stats.windows < 20, f"walked {stats.windows} windows on garbage timestamps"
    assert budget.total < 0.05


def test_hydrate_drops_unparseable_documents_and_counts_them(client) -> None:
    from corpus.x.hydrate import hydrate

    subject = _corrupt_one(load("tweets.json"))
    docs, stats = hydrate(client, subject, "testsubject", log=lambda _: None)
    assert stats.timestamp_errors == 1
    assert all(d.published_at.year > 2005 for d in docs)


def test_impossible_window_result_fails_loudly(tmp_path) -> None:
    """A tweet newer than the window ceiling means until_time: was not honoured."""
    from corpus.budget import Budget
    from corpus.cache import Cache
    from corpus.x.client import XClient

    class IgnoresUntilTime(FakeProvider):
        def advanced_search(self, query: str, cursor: str | None = None):
            # Return a tweet dated far in the future regardless of the window.
            return (
                [
                    {
                        "id": "999",
                        "text": "from the future",
                        "createdAt": "Wed Jan 01 00:00:00 +0000 2031",
                        "author": {"userName": "testsubject"},
                    }
                ],
                None,
                False,
            )

    cache = Cache(path=tmp_path / "c.db")
    client = XClient(IgnoresUntilTime(), cache, Budget(limit=10.0, cache=cache))
    with pytest.raises(IngestCorruption) as exc:
        ingest_timeline(
            client, "testsubject", since=SINCE, until=NOW, log=lambda _: None
        )
    assert "until_time" in str(exc.value)


# -- surfacing -------------------------------------------------------------


def test_timestamp_errors_reach_signals_json() -> None:
    from corpus.x.ingest import IngestStats

    stats = IngestStats(timestamp_errors=4, timestamp_error_samples=["1: 'bad'"])
    assert stats.as_dict()["timestamp_errors"] == 4
    assert stats.as_dict()["timestamp_error_samples"] == ["1: 'bad'"]


def test_timestamp_errors_reach_the_report() -> None:
    from corpus.render import render_report

    report = render_report(
        handle="testsubject",
        synthesis=None,
        docs=[],
        signals={"date_range": "2024"},
        budget_lines=["Total: $0.00"],
        run_meta={
            "ingest": {
                "timestamp_errors": 3,
                "timestamp_error_samples": ["17: 'not-a-date'"],
            },
            "hydration": {"timestamp_errors": 1},
        },
    )
    assert "4 document(s) were skipped" in report
    assert "not-a-date" in report
