"""Phase 3.2: resume instead of restarting.

A 10,000-post run that dies at post 8,000 should not start over. The permanent
cache makes re-*fetching* cheap — the tweets are on disk — but it does not make
re-*walking* free: the window loop reissues every search request, and search
pages are billed per tweet returned whether or not we already have them. The
cache is keyed by tweet id; a search response is not a tweet id.

The most expensive avoidable mistake in the tool is re-paying the whole map
stage to retry a reduce that failed validation, so completed map slices are
checkpointed to disk the moment they land.

Offline.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from corpus.budget import Budget
from corpus.cache import Cache
from corpus.manifest import MANIFEST_NAME, MANIFEST_VERSION, RunManifest
from corpus.models import Document
from corpus.synthesize import MAP_MODEL, REDUCE_MODEL, run_map, synthesize
from corpus.x.client import XClient
from corpus.x.ingest import ingest_timeline
from corpus.x.signals import compute_signals
from fake_anthropic import FakeAnthropic
from fake_provider import FakeProvider, load

NOW = datetime(2024, 9, 1, tzinfo=timezone.utc)
SINCE = datetime(2023, 12, 1, tzinfo=timezone.utc)


# -- the manifest itself ----------------------------------------------------


def test_roundtrip(tmp_path: Path) -> None:
    m = RunManifest(handle="paulg", run_id="abc", until_ts=1700000000)
    m.record_slice(0, {"topics": []})
    m.save(tmp_path)
    loaded = RunManifest.load(tmp_path)
    assert loaded is not None
    assert loaded.handle == "paulg"
    assert loaded.until_ts == 1700000000
    assert loaded.slice_result(0) == {"topics": []}


def test_save_is_atomic(tmp_path: Path) -> None:
    """A half-written manifest would be read on the next attempt and believed."""
    m = RunManifest(handle="paulg")
    m.save(tmp_path)
    # No temp files left behind, and exactly one manifest.
    assert [p.name for p in tmp_path.iterdir()] == [MANIFEST_NAME]


def test_missing_manifest_returns_none(tmp_path: Path) -> None:
    assert RunManifest.load(tmp_path) is None


def test_corrupt_manifest_returns_none_rather_than_raising(tmp_path: Path) -> None:
    """'I cannot tell where we were' means start over, not refuse to run."""
    (tmp_path / MANIFEST_NAME).write_text("{not json", encoding="utf-8")
    assert RunManifest.load(tmp_path) is None


def test_future_version_is_ignored(tmp_path: Path) -> None:
    (tmp_path / MANIFEST_NAME).write_text(
        json.dumps({"version": MANIFEST_VERSION + 1, "handle": "paulg"}), encoding="utf-8"
    )
    assert RunManifest.load(tmp_path) is None


def test_unknown_fields_are_dropped_not_fatal(tmp_path: Path) -> None:
    (tmp_path / MANIFEST_NAME).write_text(
        json.dumps({"version": MANIFEST_VERSION, "handle": "paulg", "invented": 1}),
        encoding="utf-8",
    )
    loaded = RunManifest.load(tmp_path)
    assert loaded is not None and loaded.handle == "paulg"


def test_describe_says_what_is_being_skipped() -> None:
    m = RunManifest(handle="paulg", ingest_complete=True, raw_tweet_ids=["1", "2"])
    m.record_slice(0, {})
    m.map_total = 4
    m.prior_spend = 1.25
    text = m.describe()
    assert "ingest complete" in text and "1/4 map slices" in text and "1.25" in text


# -- ingestion resume -------------------------------------------------------


def build(provider: FakeProvider, tmp_path: Path, limit: float = 10.0) -> XClient:
    cache = Cache(path=tmp_path / "c.db")
    return XClient(provider, cache, Budget(limit=limit, cache=cache))


def test_ingestion_checkpoints_every_window(tmp_path: Path) -> None:
    """Checkpointing only on success would never produce a file when needed."""
    client = build(FakeProvider(), tmp_path)
    checkpoints: list[int] = []

    ingest_timeline(
        client,
        "testsubject",
        since=SINCE,
        until=NOW,
        on_progress=lambda frontier, seen, stats: checkpoints.append(frontier),
        log=lambda _: None,
    )
    assert len(checkpoints) > 1, "no intermediate checkpoints"
    assert checkpoints == sorted(checkpoints, reverse=True), "the frontier must move backwards"


def test_resuming_from_a_frontier_does_not_rewalk(tmp_path: Path) -> None:
    """The saving: fewer search pages, which is what the money is spent on."""
    full_client = build(FakeProvider(), tmp_path / "a")
    (tmp_path / "a").mkdir(exist_ok=True)
    full, full_stats = ingest_timeline(
        full_client, "testsubject", since=SINCE, until=NOW, log=lambda _: None
    )

    # Walk once, stopping halfway, then resume from where it stopped.
    (tmp_path / "b").mkdir(exist_ok=True)
    partial_client = build(FakeProvider(), tmp_path / "b")
    seen_at_halt: dict[str, dict] = {}
    frontier_at_halt: list[int] = []

    def halt_after_three(frontier: int, seen: dict, stats) -> None:
        if stats.windows >= 3 and not frontier_at_halt:
            frontier_at_halt.append(frontier)
            seen_at_halt.update(seen)

    partial, _ = ingest_timeline(
        partial_client,
        "testsubject",
        since=SINCE,
        until=NOW,
        on_progress=halt_after_three,
        log=lambda _: None,
    )

    resumed_client = build(FakeProvider(), tmp_path / "c")
    (tmp_path / "c").mkdir(exist_ok=True)
    resumed, resumed_stats = ingest_timeline(
        resumed_client,
        "testsubject",
        since=SINCE,
        until=NOW,
        resume_until_ts=frontier_at_halt[0],
        resume_seen=seen_at_halt,
        log=lambda _: None,
    )

    assert len(resumed) == len(full), "resuming must not lose posts"
    assert resumed_stats.pages < full_stats.pages, (
        f"resumed walk issued {resumed_stats.pages} pages vs {full_stats.pages} "
        "for a full walk — nothing was saved"
    )


def test_resume_carries_previously_seen_posts(tmp_path: Path) -> None:
    client = build(FakeProvider(), tmp_path)
    prior = {t["id"]: t for t in load("tweets.json")[:5]}
    got, stats = ingest_timeline(
        client,
        "testsubject",
        since=SINCE,
        until=NOW,
        resume_seen=prior,
        log=lambda _: None,
    )
    assert {t["id"] for t in got} >= set(prior)


# -- map slice reuse --------------------------------------------------------


def docs(n: int = 400) -> list[Document]:
    from datetime import timedelta

    start = datetime(2022, 1, 1, tzinfo=timezone.utc)
    return [
        Document(
            source="x",
            source_id=str(1000 + i),
            url=f"https://x.com/s/status/{1000 + i}",
            author_handle="s",
            published_at=start + timedelta(hours=8 * i),
            kind="original",
            body=f"Post {i}. " + ("Hiring selects for interview skill, not the job. " * 8),
            engagement={"likes": i},
        )
        for i in range(n)
    ]


def test_completed_slices_are_not_re_paid_for(cache) -> None:
    """The headline saving: a reduce failure must not re-run the map stage."""
    corpus = docs()
    signals = compute_signals(corpus)

    first_budget = Budget(limit=100.0, cache=cache)
    first_client = FakeAnthropic()
    captured: dict[int, dict] = {}
    asyncio.run(
        synthesize(
            corpus,
            signals,
            first_budget,
            client=first_client,
            on_slice=lambda i, payload: captured.__setitem__(i, payload),
            log=lambda _: None,
        )
    )
    map_calls_first = sum(1 for c in first_client.calls if c.get("model") == MAP_MODEL)
    assert map_calls_first > 1 and captured

    second_budget = Budget(limit=100.0, cache=cache)
    second_client = FakeAnthropic()
    asyncio.run(
        synthesize(
            corpus,
            signals,
            second_budget,
            client=second_client,
            completed_slices=captured,
            log=lambda _: None,
        )
    )
    map_calls_second = sum(1 for c in second_client.calls if c.get("model") == MAP_MODEL)

    assert map_calls_second == 0, (
        f"re-ran {map_calls_second} map slices that were already paid for"
    )
    assert any(c.get("model") == REDUCE_MODEL for c in second_client.calls), (
        "the reduce still has to run"
    )


def test_slices_are_checkpointed_as_they_land(cache) -> None:
    """A slice only avoids being paid for twice if it reaches disk first."""
    corpus = docs(200)
    recorded: list[int] = []
    asyncio.run(
        run_map(
            FakeAnthropic(),
            [corpus[:100], corpus[100:]],
            Budget(limit=100.0, cache=cache),
            on_slice=lambda i, payload: recorded.append(i),
            log=lambda _: None,
        )
    )
    assert sorted(recorded) == [0, 1]


def test_partial_slice_reuse(cache) -> None:
    """Half done: run only what is missing."""
    corpus = docs(200)
    chunks = [corpus[:100], corpus[100:]]
    client = FakeAnthropic()
    outputs, failures = asyncio.run(
        run_map(
            client,
            chunks,
            Budget(limit=100.0, cache=cache),
            completed={0: {"topics": [], "claims": [], "entities": [],
                           "argumentative_moves": [], "highest_signal_document_ids": [],
                           "span": "prior", "document_ids": []}},
            log=lambda _: None,
        )
    )
    assert sum(1 for c in client.calls if c.get("model") == MAP_MODEL) == 1
    assert len(outputs) == 2
    assert any(o.get("span") == "prior" for o in outputs)


# -- budget across attempts -------------------------------------------------


def test_prior_spend_counts_against_the_limit(cache) -> None:
    """--budget 10 resumed three times must not be a $30 run."""
    budget = Budget(limit=1.00, cache=cache)
    budget.prior_spend = 0.90
    assert budget.total == pytest.approx(0.90)
    assert budget.this_attempt == 0.0
    budget.charge("x", "search", 1, 0.05)
    assert budget.total == pytest.approx(0.95)
    assert budget.this_attempt == pytest.approx(0.05)
    from corpus.budget import BudgetExceeded

    with pytest.raises(BudgetExceeded):
        budget.reserve(0.20, "reduce")


def test_manifest_records_spend_for_the_next_attempt(tmp_path: Path) -> None:
    m = RunManifest(handle="paulg", prior_spend=2.5)
    m.save(tmp_path)
    assert RunManifest.load(tmp_path).prior_spend == pytest.approx(2.5)


def test_cli_exposes_resume() -> None:
    import inspect

    from corpus.cli import run

    assert "resume" in inspect.signature(run).parameters
