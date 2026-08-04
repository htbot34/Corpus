"""Phase 4 verification, driven through the actual CLI.

Items 3, 4, and 5 of the verification list, run end to end rather than against
the internals:

  3. `--budget 0.05` on a large target stops before the reduce call and still
     writes corpus.json and signals.json.
  4. A deliberately malformed timestamp survives: the run continues, skips the
     document, and reports the count.
  5. `corpus resynth` costs nothing in X spend.

Item 2 (a full live run against a real account) needs a network route to
api.twitterapi.io, which this environment denies. Everything here is offline:
the provider is a fixture-backed fake and the model client is a stub.
"""

from __future__ import annotations

import json
from functools import partial
from pathlib import Path
from typing import Any

import pytest
from fake_anthropic import FakeAnthropic
from fake_provider import FakeProvider, load
from fake_search import SilentSearchProvider
from typer.testing import CliRunner

from corpus import cli as cli_module
from corpus.cache import Cache
from corpus.cli import app
from corpus.x.providers import ProviderError

runner = CliRunner()


@pytest.fixture()
def wired(monkeypatch, tmp_path: Path):
    """Point the CLI at a fake provider and a stub model, in a scratch cache."""
    monkeypatch.setenv("CORPUS_CACHE_DB", str(tmp_path / "cache.db"))
    monkeypatch.setenv("X_API_KEY", "not-used-by-the-fake")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "not-used-by-the-stub")

    provider = FakeProvider()
    monkeypatch.setattr(cli_module, "get_provider", lambda **_: provider)

    anthropic = FakeAnthropic()
    real_synthesize = cli_module.synthesize
    monkeypatch.setattr(cli_module, "synthesize", partial(real_synthesize, client=anthropic))
    # cli_module resets this global per run; make sure a previous test's logger
    # cannot leak into this one.
    monkeypatch.setattr(cli_module, "_ACTIVE_LOGGER", None)
    # Search runs by default; these tests are not about it, so it answers
    # with nothing and bills nothing rather than reaching a vendor.
    monkeypatch.setattr(cli_module, "get_search_provider", lambda **_: SilentSearchProvider())
    return {"provider": provider, "anthropic": anthropic, "out": tmp_path / "out"}


# The fixture corpus is dated 2024-01..2024-08 and the CLI always walks back
# from *now*, so at the shipped defaults the walk correctly gives up on a
# two-year silence long before reaching the fixtures (8 empty 30-day windows
# plus a 365-day probe reaches ~605 days back; the fixtures are ~730). Widening
# the window and the tolerance here is about making the fixture era reachable,
# not about the defaults being wrong — test_hiatus.py covers those directly.
REACH_FIXTURES = ["--window-days", "60", "--empty-window-tolerance", "30"]


def invoke(args: list[str]):
    return runner.invoke(app, args, catch_exceptions=False)


def outputs(base: Path, handle: str = "testsubject") -> Path:
    """The single dated directory a run produced."""
    days = sorted((base / handle).iterdir())
    assert days, f"no output directory under {base / handle}"
    return days[-1]


# ==========================================================================
# 4.3  A tripped budget stops before reduce and preserves what was paid for
# ==========================================================================


def test_tripped_budget_stops_before_reduce_and_preserves_data(wired) -> None:
    result = invoke(
        [
            "run",
            "--x",
            "testsubject",
            "--budget",
            "0.05",
            "--out",
            str(wired["out"]),
            "--yes",
            "--max-posts",
            "5000",
            *REACH_FIXTURES,
        ]
    )
    assert result.exit_code == 0, result.output

    directory = outputs(wired["out"])
    corpus_json = directory / "corpus.json"
    signals_json = directory / "signals.json"
    assert corpus_json.exists(), "corpus.json must survive a budget stop"
    assert signals_json.exists(), "signals.json must survive a budget stop"

    documents = json.loads(corpus_json.read_text())
    assert documents, "the corpus is empty — paid data was thrown away"
    assert json.loads(signals_json.read_text())["total_documents"] == len(documents)


def test_a_tiny_budget_never_reaches_the_reduce_call(wired) -> None:
    """The money guarantee: reduce is the single most expensive call."""
    invoke(
        [
            "run",
            "--x",
            "testsubject",
            "--budget",
            "0.0005",
            "--out",
            str(wired["out"]),
            "--yes",
            *REACH_FIXTURES,
        ]
    )
    from corpus.synthesize import REDUCE_MODEL

    reduce_calls = [c for c in wired["anthropic"].calls if c.get("model") == REDUCE_MODEL]
    assert reduce_calls == [], "reduce ran despite an exhausted budget"


def test_the_report_is_written_even_when_synthesis_never_runs(wired) -> None:
    """report.md is a record of what was paid for, not a synthesis artifact."""
    invoke(
        [
            "run",
            "--x",
            "testsubject",
            "--budget",
            "5",
            "--out",
            str(wired["out"]),
            "--yes",
            "--skip-synthesis",
            *REACH_FIXTURES,
        ]
    )
    report = (outputs(wired["out"]) / "report.md").read_text()
    assert "Coverage and caveats" in report
    assert "No synthesis" in report


# ==========================================================================
# 4.4  A malformed timestamp does not kill the run
# ==========================================================================


def corrupt_one(index: int = 3) -> list[dict[str, Any]]:
    tweets = [dict(t) for t in load("tweets.json")]
    tweets[index] = {**tweets[index], "createdAt": "not-a-timestamp"}
    return tweets


def test_a_malformed_timestamp_is_skipped_counted_and_reported(monkeypatch, wired) -> None:
    monkeypatch.setattr(cli_module, "get_provider", lambda **_: FakeProvider(subject=corrupt_one()))

    result = invoke(
        [
            "run",
            "--x",
            "testsubject",
            "--budget",
            "5",
            "--out",
            str(wired["out"]),
            "--yes",
            "--skip-synthesis",
            *REACH_FIXTURES,
        ]
    )
    assert result.exit_code == 0, result.output

    directory = outputs(wired["out"])
    signals = json.loads((directory / "signals.json").read_text())

    # 1. the run survived, and lost exactly the one bad document. Compared
    #    against the *ingested* count, not the document count: hydration
    #    collapses the fixture's 4-part thread into one document, so the two
    #    numbers legitimately differ.
    assert signals["ingest"]["unique"] == len(load("tweets.json")) - 1
    assert json.loads((directory / "corpus.json").read_text())

    # 2. the count is recorded where a reader will find it
    errors = signals["ingest"]["timestamp_errors"]
    assert errors >= 1, "the skipped document was not counted"
    assert signals["ingest"]["timestamp_error_samples"], "no sample of what failed"

    # 3. and stated in the report
    report = (directory / "report.md").read_text()
    assert "timestamp could not be parsed" in report
    assert "not-a-timestamp" in report


def test_every_timestamp_malformed_still_terminates(monkeypatch, wired) -> None:
    """The 2.6 failure mode: a stalled walk that pays per page forever."""
    tweets = [{**t, "createdAt": "garbage"} for t in load("tweets.json")]
    monkeypatch.setattr(cli_module, "get_provider", lambda **_: FakeProvider(subject=tweets))

    result = invoke(
        [
            "run",
            "--x",
            "testsubject",
            "--budget",
            "5",
            "--out",
            str(wired["out"]),
            "--yes",
            "--skip-synthesis",
            *REACH_FIXTURES,
        ]
    )
    # No posts means nothing to synthesize, which is exit 1 — but it terminated
    # promptly rather than walking backwards a second at a time.
    assert result.exit_code == 1
    assert "no posts ingested" in result.output.lower()


# ==========================================================================
# 4.5  resynth costs nothing in X spend
# ==========================================================================


def test_resynth_spends_nothing_on_x(wired, monkeypatch) -> None:
    invoke(
        [
            "run",
            "--x",
            "testsubject",
            "--budget",
            "5",
            "--out",
            str(wired["out"]),
            "--yes",
            *REACH_FIXTURES,
        ]
    )
    directory = outputs(wired["out"])
    assert (directory / "corpus.json").exists()

    # A provider that fails loudly if resynth touches it at all.
    class Forbidden(FakeProvider):
        def user_info(self, handle: str) -> dict[str, Any]:
            raise AssertionError("resynth fetched a profile")

        def advanced_search(self, query: str, cursor: str | None = None):  # type: ignore[no-untyped-def]
            raise AssertionError("resynth searched")

        def last_tweets(self, handle: str, cursor: str | None = None):  # type: ignore[no-untyped-def]
            raise AssertionError("resynth fetched a timeline")

        def tweets_by_ids(self, ids: list[str]) -> list[dict[str, Any]]:
            raise AssertionError("resynth hydrated")

    monkeypatch.setattr(cli_module, "get_provider", lambda **_: Forbidden())

    from corpus.budget import Budget

    seen: list[Budget] = []
    real_budget = cli_module.Budget

    def record(*args: Any, **kwargs: Any) -> Budget:
        budget = real_budget(*args, **kwargs)
        seen.append(budget)
        return budget

    monkeypatch.setattr(cli_module, "Budget", record)

    result = invoke(["resynth", str(directory)])
    assert result.exit_code == 0, result.output
    assert seen, "no budget was constructed"
    assert seen[-1].total_for("x") == 0.0, "resynth spent money on X data"


def test_resynth_rewrites_the_report(wired) -> None:
    invoke(
        [
            "run",
            "--x",
            "testsubject",
            "--budget",
            "5",
            "--out",
            str(wired["out"]),
            "--yes",
            *REACH_FIXTURES,
        ]
    )
    directory = outputs(wired["out"])
    (directory / "report.md").write_text("stale", encoding="utf-8")
    invoke(["resynth", str(directory)])
    assert (directory / "report.md").read_text() != "stale"


def test_resynth_refuses_a_directory_without_a_corpus(tmp_path: Path) -> None:
    result = runner.invoke(app, ["resynth", str(tmp_path)])
    assert result.exit_code == 2
    assert "corpus.json" in result.output


# ==========================================================================
# The run manifest lands where --resume expects it
# ==========================================================================


def test_a_run_leaves_a_resumable_manifest(wired) -> None:
    invoke(
        [
            "run",
            "--x",
            "testsubject",
            "--budget",
            "5",
            "--out",
            str(wired["out"]),
            "--yes",
            "--skip-synthesis",
            *REACH_FIXTURES,
        ]
    )
    from corpus.manifest import RunManifest

    manifest = RunManifest.load(outputs(wired["out"]))
    assert manifest is not None
    assert manifest.handle == "testsubject"
    assert manifest.ingest_complete
    assert manifest.raw_tweet_ids
    assert manifest.prior_spend > 0


def test_resume_rejects_a_manifest_for_another_handle(wired, tmp_path: Path) -> None:
    from corpus.manifest import RunManifest

    directory = tmp_path / "someone-elses"
    directory.mkdir()
    RunManifest(handle="someoneelse", run_id="x").save(directory)

    result = invoke(
        [
            "run",
            "--x",
            "testsubject",
            "--budget",
            "5",
            "--out",
            str(wired["out"]),
            "--yes",
            "--resume",
            str(directory),
            *REACH_FIXTURES,
        ]
    )
    assert result.exit_code == 2
    assert "someoneelse" in result.output


# ==========================================================================
# Handle validation at the CLI boundary
# ==========================================================================


@pytest.mark.parametrize("handle", ["bad handle", "x OR from:victim", "a" * 20])
def test_invalid_handles_are_rejected_before_anything_is_fetched(handle: str, wired) -> None:
    result = invoke(["run", "--x", handle, "--out", str(wired["out"]), "--yes"])
    assert result.exit_code == 2
    assert wired["provider"].calls == [], "an invalid handle reached the provider"


def test_log_format_json_emits_parseable_lines(wired) -> None:
    result = invoke(
        [
            "run",
            "--x",
            "testsubject",
            "--budget",
            "5",
            "--out",
            str(wired["out"]),
            "--yes",
            "--skip-synthesis",
            "--log-format",
            "json",
            *REACH_FIXTURES,
        ]
    )
    assert result.exit_code == 0
    lines = [line for line in result.output.splitlines() if line.startswith("{")]
    assert lines, "no JSON records emitted"
    records = [json.loads(line) for line in lines]
    assert all("run_id" in r and "phase" in r and "elapsed" in r for r in records)


def test_an_unknown_log_format_is_rejected(wired) -> None:
    result = invoke(
        ["run", "--x", "testsubject", "--out", str(wired["out"]), "--log-format", "yaml"]
    )
    assert result.exit_code == 2


# ==========================================================================
# A dying X provider degrades the run; it does not end it
# ==========================================================================

RATE_LIMIT_ERROR = (
    "/twitter/tweet/advanced_search failed after 5 attempts: 429 "
    '{"error":"Too Many Requests","message":"For free-tier users, the QPS '
    'limit is one request every 5 seconds."}'
)

FEED_URL = "https://blog.example.com/feed"
FEED_XML = """<?xml version="1.0"?>
<rss version="2.0"><channel><title>Blog</title>
  <item>
    <title>On rubrics</title>
    <link>https://blog.example.com/rubrics</link>
    <pubDate>Tue, 12 Mar 2024 10:00:00 +0000</pubDate>
    <description>An argument, at length, about rubrics and why they beat
    interviews for hiring engineers, with the failure modes of each spelled
    out and weighed against the other.</description>
    <guid>https://blog.example.com/rubrics</guid>
  </item>
  <item>
    <title>On evals</title>
    <link>https://blog.example.com/evals</link>
    <pubDate>Wed, 12 Jun 2024 10:00:00 +0000</pubDate>
    <description>Ground truth is the hard part of every evaluation, and most
    teams discover that only after the harness is built and trusted.</description>
    <guid>https://blog.example.com/evals</guid>
  </item>
</channel></rss>"""


class RateLimitedProvider(FakeProvider):
    """The live simonw failure: the profile read works, then every timeline
    request dies on the provider's free-tier QPS limit."""

    def advanced_search(self, query: str, cursor: str | None = None):  # type: ignore[no-untyped-def]
        raise ProviderError(RATE_LIMIT_ERROR)

    def last_tweets(self, handle: str, cursor: str | None = None):  # type: ignore[no-untyped-def]
        raise ProviderError(RATE_LIMIT_ERROR)


def test_a_dead_x_provider_degrades_the_run_instead_of_killing_it(wired, monkeypatch) -> None:
    """The load-bearing constraint, proven at the CLI: adapters never raise to
    the CLI — they degrade, and the run reports it. On the live run this
    reproduces, the crash discarded 215 free documents from 36 non-X sources.
    """
    monkeypatch.setattr(cli_module, "get_provider", lambda **_: RateLimitedProvider())
    seed = Cache()  # CORPUS_CACHE_DB points at the wired scratch db
    seed.put("rss", f"rss:{FEED_URL}", FEED_XML)
    seed.close()

    result = invoke(
        [
            "run",
            "--x",
            "testsubject",
            "--rss",
            FEED_URL,
            "--out",
            str(wired["out"]),
            "--yes",
            *REACH_FIXTURES,
        ]
    )
    assert result.exit_code == 0, result.output

    directory = outputs(wired["out"])
    documents = json.loads((directory / "corpus.json").read_text())
    assert documents, "the non-X documents were discarded along with the failed source"
    assert all(d["source"] != "x" for d in documents)

    signals = json.loads((directory / "signals.json").read_text())
    assert signals["ingest"]["x_status"] == "failed"
    assert "Too Many Requests" in signals["ingest"]["x_failure"]

    assert (directory / "synthesis.json").exists(), (
        "the run must still synthesize from the non-X documents"
    )
    report = (directory / "report.md").read_text()
    assert "The X source failed" in report, "the coverage block must say what was lost"

    manifest = json.loads((directory / "run.json").read_text())
    assert manifest["ingest_complete"] is False, "a later run must resume the X walk"
