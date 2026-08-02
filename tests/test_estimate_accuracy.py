"""Phase 3.6: an estimator nobody checks is decoration.

--dry-run prints a cost estimate. Nothing ever compared it to what a run
actually cost, so the estimate could be systematically wrong for months without
anyone noticing — and a systematically low estimate is exactly what makes a
budget stop arrive as a surprise.

Offline.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from corpus.budget import (
    Budget,
    estimate_anthropic_cost,
    estimate_x_cost,
)
from corpus.cache import Cache
from corpus.cli import app


# -- persistence ------------------------------------------------------------


def test_estimates_are_recorded(cache: Cache) -> None:
    cache.log_estimate("run1", "paulg", "total", 1.50, 1.80, 3000, 2900)
    rows = cache.estimate_log()
    assert len(rows) == 1
    assert rows[0]["handle"] == "paulg"
    assert rows[0]["estimated"] == pytest.approx(1.50)
    assert rows[0]["actual"] == pytest.approx(1.80)
    assert rows[0]["posts_actual"] == 2900


def test_estimates_are_newest_first(cache: Cache) -> None:
    for i in range(3):
        cache.log_estimate(f"run{i}", "paulg", "total", 1.0, float(i))
    actuals = [row["actual"] for row in cache.estimate_log()]
    assert actuals == [2.0, 1.0, 0.0]


def test_estimate_table_survives_an_existing_database(tmp_path: Path) -> None:
    """The schema is created with IF NOT EXISTS; an old cache must upgrade."""
    first = Cache(path=tmp_path / "c.db")
    first.put("x", "tweet:1", {"a": 1})
    first.close()
    second = Cache(path=tmp_path / "c.db")
    second.log_estimate("run1", "paulg", "total", 1.0, 1.0)
    assert len(second.estimate_log()) == 1
    second.close()


# -- the accuracy report ----------------------------------------------------


def run_cli(args: list[str], db: Path):
    runner = CliRunner()
    import os

    env = {**os.environ, "CORPUS_CACHE_DB": str(db)}
    return runner.invoke(app, args, env=env)


def test_accuracy_reports_nothing_gracefully(tmp_path: Path) -> None:
    result = run_cli(["budget", "accuracy"], tmp_path / "c.db")
    assert result.exit_code == 0
    assert "no estimates recorded yet" in result.output


def test_accuracy_reports_signed_error(tmp_path: Path) -> None:
    """Direction matters: consistently low is a different bug from noisy."""
    db = tmp_path / "c.db"
    cache = Cache(path=db)
    cache.log_estimate("r1", "paulg", "total", 1.00, 1.50)  # +50%
    cache.log_estimate("r2", "pmarca", "total", 2.00, 1.00)  # -50%
    cache.close()

    result = run_cli(["budget", "accuracy"], db)
    assert result.exit_code == 0
    assert "+50%" in result.output
    assert "-50%" in result.output
    assert "mean error" in result.output
    assert "mean |error|" in result.output


def test_accuracy_flags_a_badly_wrong_estimator(tmp_path: Path) -> None:
    db = tmp_path / "c.db"
    cache = Cache(path=db)
    for i in range(4):
        cache.log_estimate(f"r{i}", "paulg", "total", 1.00, 2.00)  # consistently 100% low
    cache.close()

    result = run_cli(["budget", "accuracy"], db)
    assert "off by more than 30%" in result.output
    assert "estimate_x_cost" in result.output, "the report must say where to look"
    assert "low" in result.output


def test_accuracy_does_not_flag_a_good_estimator(tmp_path: Path) -> None:
    db = tmp_path / "c.db"
    cache = Cache(path=db)
    for i in range(4):
        cache.log_estimate(f"r{i}", "paulg", "total", 1.00, 1.05)
    cache.close()

    result = run_cli(["budget", "accuracy"], db)
    assert "off by more than 30%" not in result.output


def test_accuracy_shows_posts_estimated_versus_actual(tmp_path: Path) -> None:
    """Cost error and volume error are different failures; show both."""
    db = tmp_path / "c.db"
    cache = Cache(path=db)
    cache.log_estimate("r1", "paulg", "total", 1.0, 1.0, 3000, 412)
    cache.close()

    result = run_cli(["budget", "accuracy"], db)
    assert "412/3000" in result.output


def test_command_is_registered() -> None:
    from corpus.cli import budget_app

    names = {c.name or c.callback.__name__ for c in budget_app.registered_commands}
    assert "accuracy" in names


# -- the estimators themselves ---------------------------------------------


def test_x_estimate_scales_with_posts() -> None:
    assert estimate_x_cost(6000) > estimate_x_cost(3000)


def test_anthropic_estimate_scales_with_posts() -> None:
    assert estimate_anthropic_cost(6000) > estimate_anthropic_cost(3000)


def test_documented_worked_example_still_holds() -> None:
    """README claims ~$1.83 total for 3,000 posts. Keep the doc honest."""
    total = estimate_x_cost(3000) + estimate_anthropic_cost(3000)
    assert 1.0 < total < 3.0, f"3,000-post estimate is now ${total:.2f}"


def test_this_attempt_excludes_carried_spend(cache: Cache) -> None:
    """Accuracy compares an estimate to what THIS run cost, not the resumed sum."""
    budget = Budget(limit=10.0, cache=cache)
    budget.prior_spend = 5.0
    budget.charge("x", "search", 1, 0.25)
    assert budget.this_attempt == pytest.approx(0.25)
    assert budget.total == pytest.approx(5.25)
