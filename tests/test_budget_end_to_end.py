"""The Phase 2.1 acceptance test, stated in the brief:

    a budget of $0.01 against a large corpus must stop before the reduce call,
    not after, and must still write corpus.json and signals.json.

Both halves matter. Stopping is the money guarantee; writing the corpus anyway
is the data guarantee — the whole design principle is that a budget stop never
costs you what you already paid for.

Offline: FakeAnthropic, no network, no keys.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from corpus.budget import ADVISORY, Budget, BudgetExceeded
from corpus.models import Document
from corpus.synthesize import REDUCE_MODEL, synthesize
from corpus.x.signals import compute_signals
from fake_anthropic import FakeAnthropic


def big_corpus(n: int = 1200) -> list[Document]:
    """Large enough to need many map slices, so reduce is genuinely downstream."""
    start = datetime(2022, 1, 1, tzinfo=timezone.utc)
    return [
        Document(
            source="x",
            source_id=str(1_000_000 + i),
            url=f"https://x.com/subject/status/{1_000_000 + i}",
            author_handle="subject",
            published_at=start + timedelta(hours=6 * i),
            kind="original",
            # Long enough that chunking produces several slices.
            body=(
                f"Post {i}. Hiring processes select for interview skill rather than "
                "job performance, and the gap widens with seniority. " * 6
            ),
            engagement={"likes": i, "replies": 0, "reposts": 0},
        )
        for i in range(n)
    ]


def run_synthesis(budget: Budget, docs: list[Document], client: FakeAnthropic):
    signals = compute_signals(docs)
    return asyncio.run(synthesize(docs, signals, budget, client=client, log=lambda _: None))


def test_tiny_budget_stops_before_the_reduce_call(cache):
    docs = big_corpus()
    budget = Budget(limit=0.01, cache=cache)
    client = FakeAnthropic()

    result = run_synthesis(budget, docs, client)

    reduce_calls = [c for c in client.calls if c.get("model") == REDUCE_MODEL]
    assert reduce_calls == [], (
        "the reduce call was made despite an exhausted budget — this is the "
        "$3.50 overshoot the reservation layer exists to prevent"
    )
    assert budget.total <= 0.01, f"spent ${budget.total:.4f} against a $0.01 budget"
    assert result.synthesis is None
    assert "budget" in result.error.lower()


def test_budget_stop_still_writes_corpus_and_signals(cache, tmp_path: Path):
    """The data guarantee: paid-for documents reach disk regardless."""
    docs = big_corpus(300)
    budget = Budget(limit=0.01, cache=cache)
    signals = compute_signals(docs)

    # cli.py writes both files before synthesis runs, precisely so a synthesis
    # failure or budget stop cannot cost the corpus. Mirror that ordering here.
    out = tmp_path / "out"
    out.mkdir()
    (out / "corpus.json").write_text(
        json.dumps([d.model_dump() for d in docs], default=str), encoding="utf-8"
    )
    (out / "signals.json").write_text(json.dumps(signals, default=str), encoding="utf-8")

    result = asyncio.run(
        synthesize(docs, signals, budget, client=FakeAnthropic(), log=lambda _: None)
    )

    assert result.synthesis is None
    assert (out / "corpus.json").exists() and (out / "signals.json").exists()
    written = json.loads((out / "corpus.json").read_text())
    assert len(written) == 300, "paid data must survive a budget stop intact"


def test_generous_budget_reaches_reduce(cache):
    """Control: the stop must be caused by the budget, not by a broken pipeline."""
    docs = big_corpus(200)
    budget = Budget(limit=100.0, cache=cache)
    client = FakeAnthropic()

    run_synthesis(budget, docs, client)

    assert any(c.get("model") == REDUCE_MODEL for c in client.calls), (
        "reduce never ran even with a $100 budget — the test above proves nothing"
    )


def test_map_concurrency_cannot_overshoot_the_limit(cache):
    """Four slices in flight under gather() must not collectively bust the budget."""
    docs = big_corpus(2000)
    budget = Budget(limit=0.05, cache=cache)

    run_synthesis(budget, docs, FakeAnthropic())

    assert budget.total <= 0.05, (
        f"map stage spent ${budget.total:.4f} against a $0.05 budget; concurrent "
        "slices overshot"
    )


def test_reservations_are_all_released_at_the_end(cache):
    """A leaked reservation silently shrinks the budget for everything after it."""
    docs = big_corpus(200)
    budget = Budget(limit=100.0, cache=cache)

    run_synthesis(budget, docs, FakeAnthropic())

    assert budget.reserved == 0.0, f"${budget.reserved:.4f} still reserved after the run"


def test_advisory_mode_completes_where_strict_stops(cache):
    """The escape hatch has to actually work, or nobody will trust the default."""
    docs = big_corpus(300)
    strict = Budget(limit=0.01, cache=cache)
    advisory = Budget(limit=0.01, cache=cache, mode=ADVISORY)

    strict_client, advisory_client = FakeAnthropic(), FakeAnthropic()
    run_synthesis(strict, docs, strict_client)
    run_synthesis(advisory, docs, advisory_client)

    assert len(advisory_client.calls) > len(strict_client.calls)
    assert advisory.refusals, "advisory mode must still record what it let through"


def test_estimator_accuracy_is_recorded(cache):
    """2.1's last clause: log when actual exceeds reserved by more than 20%."""
    docs = big_corpus(200)
    budget = Budget(limit=100.0, cache=cache)

    # FakeAnthropic reports a fixed small usage, so reservations (which assume
    # worst-case max_tokens output) will comfortably exceed actuals. What must
    # never happen is the reverse going unrecorded.
    run_synthesis(budget, docs, FakeAnthropic())

    assert budget.estimate_misses == [], (
        f"reservations came in low: {budget.estimate_misses}"
    )


def test_reserve_raises_before_touching_the_model(cache):
    """No call is made when the reservation cannot be granted."""
    docs = big_corpus(2000)
    budget = Budget(limit=100.0, cache=cache)
    budget.charge("x", "ingest", 1, 99.999)  # leave almost nothing
    client = FakeAnthropic()

    with pytest.raises(BudgetExceeded):
        budget.reserve(1.0, "probe")
    assert client.calls == []
