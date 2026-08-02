"""Phase 3.1: structured logging.

The `log: Callable[[str], None] = print` pattern made the pipeline modules
trivially testable and everything else impossible: unfilterable, untimestamped,
and uncorrelatable across a run that costs money and takes minutes.

The callable interface survives — it is what keeps ingest/hydrate/synthesize
easy to test, and threading a Logger through every signature would be a much
larger change than the problem justifies. What changed is what sits behind it.

Offline.
"""

from __future__ import annotations

import io
import json

import pytest

from corpus.logging_setup import JSON, LOG_FORMATS, TEXT, RunLogger


def make(**kwargs) -> tuple[RunLogger, io.StringIO]:
    stream = io.StringIO()
    return RunLogger(kwargs.pop("run_id", "abc123def456"), stream=stream, **kwargs), stream


# -- required fields --------------------------------------------------------


def test_every_json_record_carries_run_id_phase_and_elapsed() -> None:
    log, stream = make(log_format=JSON)
    log.info("ingesting")
    record = json.loads(stream.getvalue().strip())
    assert record["run_id"] == "abc123def456"
    assert record["phase"]
    assert isinstance(record["elapsed"], float)
    assert record["message"] == "ingesting"
    assert record["level"] == "INFO"
    assert record["ts"]


def test_run_id_ties_a_log_line_to_the_spend_table(cache) -> None:
    """The correlation that makes a run auditable after the fact."""
    from corpus.budget import Budget

    budget = Budget(limit=1.0, cache=cache)
    log, stream = make(run_id=budget.run_id, log_format=JSON)
    log.info("charged")
    budget.charge_x_tweets("advanced_search", 10)

    logged = json.loads(stream.getvalue().strip())["run_id"]
    assert logged == budget.run_id
    assert cache.spend_log()[0]["run_id"] == logged


def test_phase_scope_labels_records() -> None:
    log, stream = make(log_format=JSON)
    with log.phase_scope("ingest"):
        log.info("window 1")
    with log.phase_scope("reduce"):
        log.info("reducing")
    phases = [json.loads(line)["phase"] for line in stream.getvalue().strip().splitlines()]
    assert "ingest" in phases and "reduce" in phases


def test_phase_scope_restores_the_previous_phase() -> None:
    log, _ = make()
    log.context.phase = "ingest"
    with log.phase_scope("hydrate"):
        assert log.phase == "hydrate"
    assert log.phase == "ingest"


def test_elapsed_increases() -> None:
    import time

    log, stream = make(log_format=JSON)
    log.info("first")
    time.sleep(0.01)
    log.info("second")
    first, second = (json.loads(line)["elapsed"] for line in stream.getvalue().strip().splitlines())
    assert second > first


# -- formats ----------------------------------------------------------------


def test_text_is_the_default() -> None:
    log, stream = make()
    log.info("hello")
    assert stream.getvalue().strip() == "hello"


def test_text_preserves_progress_lines_verbatim() -> None:
    """The per-window lines are the most useful thing the tool prints."""
    log, stream = make()
    line = "  2024-01-08..2024-02-07: +18 new (total 42, $0.0031)"
    log.sink("ingest")(line)
    assert stream.getvalue().rstrip("\n") == line


def test_verbose_adds_phase_and_elapsed() -> None:
    log, stream = make(verbose=True)
    with log.phase_scope("ingest"):
        log.info("window 1")
    out = stream.getvalue()
    assert "ingest" in out and "s " in out and "window 1" in out


def test_json_is_one_object_per_line() -> None:
    log, stream = make(log_format=JSON)
    for i in range(3):
        log.info(f"line {i}")
    lines = stream.getvalue().strip().splitlines()
    assert len(lines) == 3
    assert all(json.loads(line)["message"].startswith("line") for line in lines)


def test_json_carries_extra_fields() -> None:
    log, stream = make(log_format=JSON)
    log.info("charged", cost=0.0031, endpoint="advanced_search")
    record = json.loads(stream.getvalue().strip())
    assert record["cost"] == 0.0031
    assert record["endpoint"] == "advanced_search"


def test_unknown_format_is_rejected() -> None:
    with pytest.raises(ValueError):
        RunLogger("abc", log_format="yaml")


def test_the_documented_formats_are_the_supported_ones() -> None:
    assert set(LOG_FORMATS) == {TEXT, JSON}


# -- levels -----------------------------------------------------------------


def test_warnings_are_marked_in_text_mode() -> None:
    log, stream = make()
    log.warning("budget exhausted")
    assert "WARNING" in stream.getvalue()


def test_quiet_suppresses_progress() -> None:
    log, stream = make(quiet=True)
    log.info("  window 1: +18 new")
    assert stream.getvalue() == ""


def test_quiet_never_hides_a_warning_or_an_error() -> None:
    """A quiet flag that hides a budget stop is not quiet, it is broken."""
    log, stream = make(quiet=True)
    log.warning("the budget was exhausted; results are partial")
    log.error("synthesis failed")
    out = stream.getvalue()
    assert "budget was exhausted" in out
    assert "synthesis failed" in out


def test_verbose_shows_debug() -> None:
    log, stream = make(verbose=True)
    log.debug("cursor advanced")
    assert "cursor advanced" in stream.getvalue()


def test_debug_is_hidden_by_default() -> None:
    log, stream = make()
    log.debug("cursor advanced")
    assert stream.getvalue() == ""


# -- isolation --------------------------------------------------------------


def test_logger_does_not_propagate_to_root() -> None:
    """Inheriting a library's root handler is how duplicate lines happen."""
    log, _ = make()
    assert log.logger.propagate is False


def test_two_runs_do_not_share_handlers() -> None:
    first, first_stream = make(run_id="run-one")
    second, second_stream = make(run_id="run-two")
    first.info("only in first")
    assert "only in first" in first_stream.getvalue()
    assert second_stream.getvalue() == ""
    first.close()
    second.close()


def test_close_releases_handlers() -> None:
    log, _ = make()
    assert log.logger.handlers
    log.close()
    assert not log.logger.handlers


# -- the pipeline interface -------------------------------------------------


def test_sink_is_the_callable_the_pipeline_expects() -> None:
    """ingest/hydrate/synthesize take log(str); that contract is unchanged."""
    log, stream = make(log_format=JSON)
    sink = log.sink("ingest")
    sink("  +18 new")
    sink()  # a bare blank line, as the pipeline emits between sections
    records = [json.loads(line) for line in stream.getvalue().strip().splitlines()]
    assert records[0]["message"] == "  +18 new"
    assert records[0]["phase"] == "ingest"
    assert len(records) == 2


def test_ingestion_logs_flow_through_the_sink(tmp_path) -> None:
    from datetime import datetime, timezone

    from fake_provider import FakeProvider

    from corpus.budget import Budget
    from corpus.cache import Cache
    from corpus.x.client import XClient
    from corpus.x.ingest import ingest_timeline

    log, stream = make(log_format=JSON)
    cache = Cache(path=tmp_path / "c.db")
    client = XClient(FakeProvider(), cache, Budget(limit=10.0, cache=cache))

    ingest_timeline(
        client,
        "testsubject",
        since=datetime(2023, 12, 1, tzinfo=timezone.utc),
        until=datetime(2024, 9, 1, tzinfo=timezone.utc),
        log=log.sink("ingest"),
    )

    records = [json.loads(line) for line in stream.getvalue().strip().splitlines()]
    assert records, "ingestion produced no log records"
    assert all(r["phase"] == "ingest" for r in records)
    assert all(r["run_id"] == "abc123def456" for r in records)
    assert any("new" in r["message"] for r in records), "window progress lines are missing"
    cache.close()


def test_cli_exposes_the_flags() -> None:
    import inspect

    from corpus.cli import run

    params = inspect.signature(run).parameters
    for flag in ("log_format", "verbose", "quiet"):
        assert flag in params, f"--{flag.replace('_', '-')} is missing"
