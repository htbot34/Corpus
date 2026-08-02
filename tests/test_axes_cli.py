"""Configurable axes, the render-only path, and the old-schema migration error."""

from __future__ import annotations

import json

import pytest
from fake_anthropic import FakeAnthropic
from fake_provider import load
from typer.testing import CliRunner

from corpus.axes import AxisError, load_axes, select_axes
from corpus.cli import app, load_synthesis
from corpus.models import Document, Synthesis
from corpus.x.hydrate import hydrate
from corpus.x.signals import compute_signals

runner = CliRunner()


# -- axis configuration -----------------------------------------------------


def test_default_axes_come_from_profiles_yaml():
    names = [a.name for a in load_axes()]
    assert "politics_and_ideology" in names
    assert "defense_intel_natsec" in names
    assert "epistemics" in names


def test_no_selection_means_every_axis():
    assert select_axes(None) == load_axes()
    assert select_axes("  ") == load_axes()


def test_selection_restricts_and_preserves_the_requested_order():
    chosen = select_axes("defense_intel_natsec,politics_and_ideology")
    assert [a.name for a in chosen] == ["defense_intel_natsec", "politics_and_ideology"]


def test_unknown_axis_is_an_error_naming_the_valid_ones():
    """A typo must not silently produce a report that looks complete."""
    with pytest.raises(AxisError) as exc:
        select_axes("politics_and_ideology,defence_intel")
    assert "defence_intel" in str(exc.value)
    assert "defense_intel_natsec" in str(exc.value)


def test_duplicate_selection_is_collapsed():
    assert len(select_axes("epistemics,epistemics")) == 1


def test_every_axis_carries_a_probe_for_the_prompt():
    for spec in load_axes():
        assert spec.probe.strip(), f"{spec.name} has no probe"
        assert spec.name in spec.as_prompt_line()


def test_bad_profiles_file_fails_loudly(tmp_path):
    path = tmp_path / "profiles.yaml"
    path.write_text("axes: []\n", encoding="utf-8")
    with pytest.raises(AxisError):
        load_axes(path)


def test_cli_rejects_an_unknown_axis_before_spending_anything():
    result = runner.invoke(app, ["run", "--x", "someone", "--axes", "nonsense", "--dry-run"])
    assert result.exit_code == 2
    assert "unknown axis" in result.output


# -- render-only ------------------------------------------------------------


def _seed(directory, client):
    docs, _ = hydrate(client, load("tweets.json"), "testsubject", log=lambda _: None)
    signals = compute_signals(docs)
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "corpus.json").write_text(
        json.dumps([d.model_dump() for d in docs], default=str), encoding="utf-8"
    )
    (directory / "signals.json").write_text(json.dumps(signals, default=str), encoding="utf-8")
    return docs


def test_render_only_rebuilds_the_report_with_zero_api_calls(tmp_path, client):
    directory = tmp_path / "out" / "testsubject" / "2026-08-02"
    docs = _seed(directory, client)
    marker = " ".join(f"[id: {d.source_id}]" for d in docs[:2])
    synthesis = Synthesis.model_validate(
        json.loads(FakeAnthropic()._default_reduce({"messages": [{"c": marker}]}))
    )
    (directory / "synthesis.json").write_text(
        json.dumps(synthesis.model_dump(), default=str), encoding="utf-8"
    )

    result = runner.invoke(app, ["resynth", str(directory), "--render-only"])
    assert result.exit_code == 0, result.output
    assert "$0.0000 spent" in result.output
    report = (directory / "report.md").read_text(encoding="utf-8")
    assert "## The generating model" in report
    assert "no API calls" in report


def test_render_only_without_a_synthesis_says_what_to_run(tmp_path, client):
    directory = tmp_path / "out" / "testsubject" / "2026-08-02"
    _seed(directory, client)
    result = runner.invoke(app, ["resynth", str(directory), "--render-only"])
    assert result.exit_code == 2
    assert "does not exist" in result.output
    assert "corpus resynth" in result.output


# -- old-schema migration ---------------------------------------------------

LEGACY = {
    "summary": "One. Two. Three.",
    "themes": [{"name": "hiring", "evidence_ids": ["1"]}],
    "positions": [],
    "performance_gap": {"posts_most_about": "hiring"},
    "hooks": [{"opener": "You said..."}],
    "reading_diet": [],
    "coverage": {"date_range": "", "total_documents": 3, "confidence": "medium"},
}


def test_old_schema_synthesis_gets_a_migration_error_not_a_stack_trace(tmp_path):
    path = tmp_path / "synthesis.json"
    path.write_text(json.dumps(LEGACY), encoding="utf-8")
    with pytest.raises(ValueError) as exc:
        load_synthesis(path)
    message = str(exc.value)
    assert "pre-cognition schema" in message
    assert "themes" in message and "hooks" in message
    assert "corpus resynth" in message
    assert "no X spend" in message


def test_render_only_on_an_old_corpus_directory_explains_the_fix(tmp_path, client):
    """The paid corpus on disk must not produce a traceback."""
    directory = tmp_path / "out" / "testsubject" / "2026-08-02"
    _seed(directory, client)
    (directory / "synthesis.json").write_text(json.dumps(LEGACY), encoding="utf-8")
    result = runner.invoke(app, ["resynth", str(directory), "--render-only"])
    assert result.exit_code == 2
    assert "pre-cognition schema" in result.output


def test_resynth_itself_still_works_against_an_old_corpus(tmp_path, client, monkeypatch):
    """corpus.json is unchanged by the redesign, so re-synthesizing an old
    directory must need no migration at all."""
    import corpus.cli as cli_module

    directory = tmp_path / "out" / "testsubject" / "2026-08-02"
    _seed(directory, client)
    (directory / "synthesis.json").write_text(json.dumps(LEGACY), encoding="utf-8")

    real_synthesize = cli_module.synthesize

    async def stubbed(*args, **kwargs):
        kwargs["client"] = FakeAnthropic()
        return await real_synthesize(*args, **kwargs)

    monkeypatch.setattr(cli_module, "synthesize", stubbed)
    result = runner.invoke(app, ["resynth", str(directory)])
    assert result.exit_code == 0, result.output

    migrated = load_synthesis(directory / "synthesis.json")
    assert migrated.core_model
    assert "## The generating model" in (directory / "report.md").read_text()


def test_a_current_synthesis_loads_without_complaint(tmp_path):
    path = tmp_path / "synthesis.json"
    raw = FakeAnthropic()._default_reduce({"messages": [{"c": "[id: 1]"}]})
    path.write_text(raw, encoding="utf-8")
    assert load_synthesis(path).core_model


def test_documents_round_trip_unchanged(client):
    """The Document schema did not move, which is why old corpora survive."""
    docs, _ = hydrate(client, load("tweets.json"), "testsubject", log=lambda _: None)
    again = [Document.model_validate(json.loads(d.model_dump_json())) for d in docs]
    assert [d.source_id for d in again] == [d.source_id for d in docs]
