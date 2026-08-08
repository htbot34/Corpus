"""Phase 3.6: an estimator nobody checks is decoration.

--dry-run prints a cost estimate. Nothing ever compared it to what a run
actually cost, so the estimate could be systematically wrong for months without
anyone noticing — and a systematically low estimate is exactly what makes a
budget stop arrive as a surprise.

Offline.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest
from typer.testing import CliRunner

from corpus.budget import (
    Budget,
    estimate_anthropic_cost,
    estimate_anthropic_split,
    estimate_x_cost,
    model_rates,
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
    """README claims ~$1.34 total for 3,000 posts. Keep the doc honest."""
    total = estimate_x_cost(3000) + estimate_anthropic_cost(3000)
    assert 1.0 < total < 3.0, f"3,000-post estimate is now ${total:.2f}"


# -- the reduce-model default ------------------------------------------------


def test_run_and_resynth_default_the_reduce_model_to_sonnet() -> None:
    """Both commands inherit REDUCE_MODEL, and the flag stays: --reduce-model
    claude-opus-5 must remain a complete one-flag revert."""
    import inspect

    from corpus import cli
    from corpus.synthesize import REDUCE_MODEL

    assert REDUCE_MODEL == "claude-sonnet-5"
    for command in (cli.run, cli.resynth):
        option = inspect.signature(command).parameters["reduce_model"].default
        assert option.default == "claude-sonnet-5", command.__name__
        assert "--reduce-model" in option.param_decls, command.__name__


def test_estimate_uses_the_configured_reduce_models_rate() -> None:
    """The dry-run estimate must move with --reduce-model, and the default must
    price the reduce phase at Sonnet's rate from model_rates(), not an assumed
    one."""
    map_default, reduce_default = estimate_anthropic_split(1000)
    map_opus, reduce_opus = estimate_anthropic_split(1000, reduce_model="claude-opus-5")
    assert map_default == pytest.approx(map_opus), "the reduce model must not move map"
    assert reduce_default < reduce_opus

    in_rate, out_rate = model_rates("claude-sonnet-5")
    reduce_in = 4 * 1_500 + 3_000  # 4 map chunks at 1,000 posts, plus signals.json
    expected = reduce_in * in_rate / 1_000_000 + 10_000 * out_rate / 1_000_000
    assert reduce_default == pytest.approx(expected)


def test_sonnet_intro_window_pricing_survives_the_default_change() -> None:
    assert model_rates("claude-sonnet-5", date(2026, 8, 15)) == (2.00, 10.00)
    assert model_rates("claude-sonnet-5", date(2026, 9, 1)) == (3.00, 15.00)


def test_this_attempt_excludes_carried_spend(cache: Cache) -> None:
    """Accuracy compares an estimate to what THIS run cost, not the resumed sum."""
    budget = Budget(limit=10.0, cache=cache)
    budget.prior_spend = 5.0
    budget.charge("x", "search", 1, 0.25)
    assert budget.this_attempt == pytest.approx(0.25)
    assert budget.total == pytest.approx(5.25)


# -- per-provider search rates ----------------------------------------------


def test_the_search_estimate_uses_the_configured_providers_rate() -> None:
    """An estimator quoting Anthropic's rate for an Exa search is the
    estimator lying. Exa's rate has no token half — the provider runs no
    model — but it prices in the page contents every search carries back."""
    from corpus.budget import (
        EXA_COST_PER_QUERY,
        SEARCH_COST_PER_QUERY,
        estimate_search_phase,
    )

    exa = estimate_search_phase(3, provider="exa")
    anthropic = estimate_search_phase(3)

    assert exa == pytest.approx(3 * EXA_COST_PER_QUERY)
    assert anthropic > 3 * SEARCH_COST_PER_QUERY  # fee plus haiku tokens
    assert exa != pytest.approx(anthropic)


def test_an_unknown_provider_cannot_be_estimated() -> None:
    """Billing against a rate nobody wrote down is refused, same as
    model_rates refuses a model with no price on file."""
    from corpus.budget import estimate_search_phase

    with pytest.raises(KeyError):
        estimate_search_phase(3, provider="nope")
