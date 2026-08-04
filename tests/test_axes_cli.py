"""Configurable axes, the render-only path, and the old-schema migration error."""

from __future__ import annotations

import json

import pytest
from fake_anthropic import FakeAnthropic
from fake_provider import load
from typer.testing import CliRunner

from corpus.axes import AxisError, load_axes, select_axes
from corpus.cache import Cache
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
    # Assert on the belief itself rather than the section header: the header
    # varies with the corpus tier, and this test is about --render-only.
    assert "Process quality is measurable" in report
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
    assert "Process quality is measurable" in (directory / "report.md").read_text()

    # This test drives the real CLI, which opens `Cache()` with no path and
    # logs real spend rows — the two rows every full run used to append to
    # the developer's ~/.corpus/cache.db. They must land in the suite's
    # per-test scratch database (conftest.cache_db_in_tmp), where we can see
    # them, and nowhere else.
    ledger = Cache()
    try:
        notes = [row["note"] for row in ledger.spend_log()]
    finally:
        ledger.close()
    assert any(n.startswith("map slice 1") for n in notes)
    assert any(n.startswith("reduce attempt 1") for n in notes)


def _stub_synthesize(monkeypatch, anthropic):
    import corpus.cli as cli_module

    real_synthesize = cli_module.synthesize

    async def stubbed(*args, **kwargs):
        kwargs["client"] = anthropic
        return await real_synthesize(*args, **kwargs)

    monkeypatch.setattr(cli_module, "synthesize", stubbed)


def _map_calls(anthropic) -> int:
    from corpus.synthesize import MAP_MODEL

    return sum(1 for c in anthropic.calls if c.get("model") == MAP_MODEL)


def test_a_second_resynth_reuses_map_slices_and_never_calls_the_map_model(
    tmp_path, client, monkeypatch
):
    """Budget stop, raise budget, re-run is the most common workflow this
    tool has. The simonw-nox run paid $0.61 for 15 map slices and then $0.63
    to run the same 15 again, because completed slices were persisted by
    `run` but never read back by `resynth`."""
    directory = tmp_path / "out" / "testsubject" / "2026-08-02"
    _seed(directory, client)
    anthropic = FakeAnthropic()
    _stub_synthesize(monkeypatch, anthropic)

    first = runner.invoke(app, ["resynth", str(directory)])
    assert first.exit_code == 0, first.output
    paid_once = _map_calls(anthropic)
    assert paid_once > 0, "the first invocation must actually run the map stage"
    assert (directory / "run.json").exists(), "completed slices were not persisted"

    second = runner.invoke(app, ["resynth", str(directory)])
    assert second.exit_code == 0, second.output
    assert _map_calls(anthropic) == paid_once, "the second invocation re-paid the map stage"
    assert "reusing" in second.output and "map slice(s)" in second.output


def test_a_changed_corpus_invalidates_saved_slices_instead_of_reusing_them(
    tmp_path, client, monkeypatch
):
    """A saved slice belongs to the documents it covered. When the corpus
    changes, reusing it by index would attach an old extraction to different
    documents — worse than re-paying."""
    directory = tmp_path / "out" / "testsubject" / "2026-08-02"
    docs = _seed(directory, client)
    anthropic = FakeAnthropic()
    _stub_synthesize(monkeypatch, anthropic)

    first = runner.invoke(app, ["resynth", str(directory)])
    assert first.exit_code == 0, first.output
    paid_once = _map_calls(anthropic)

    # Drop a document: the chunking no longer matches the saved slices.
    (directory / "corpus.json").write_text(
        json.dumps([d.model_dump() for d in docs[:-1]], default=str), encoding="utf-8"
    )
    second = runner.invoke(app, ["resynth", str(directory)])
    assert second.exit_code == 0, second.output
    assert _map_calls(anthropic) > paid_once, "stale slices were reused against a changed corpus"
    assert "no longer match the chunking" in second.output


def test_the_highlights_cap_is_configurable_and_printed_before_reduce(
    tmp_path, client, monkeypatch
):
    """The reduce input is the biggest cost lever in the tool — ~60% of a
    real run's spend — so the chosen cap and the token estimate print before
    the call, not on the invoice."""
    directory = tmp_path / "out" / "testsubject" / "2026-08-02"
    _seed(directory, client)
    anthropic = FakeAnthropic()
    _stub_synthesize(monkeypatch, anthropic)

    result = runner.invoke(app, ["resynth", str(directory), "--highlights", "2"])
    assert result.exit_code == 0, result.output
    assert "--highlights 2" in result.output
    assert (
        "reduce input: ~" in result.output and "tokens estimated before the call" in result.output
    )


def test_select_highlights_honours_the_cap(client):
    from corpus.synthesize import select_highlights

    docs, _ = hydrate(client, load("tweets.json"), "testsubject", log=lambda _: None)
    ids = [d.source_id for d in docs]
    outputs = [{"highest_signal_document_ids": ids}]
    assert len(select_highlights(docs, outputs, cap=2)) == 2
    assert len(select_highlights(docs, outputs)) == min(60, len(ids))


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
