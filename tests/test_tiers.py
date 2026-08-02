"""Corpus-size tiers: what each one suppresses, and that it is enforced.

The prompt asks the model to respect the tier. These tests are about the half
that does not depend on the model complying — a thin corpus that returns a
confident inference must have it deleted, not merely discouraged.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone

import pytest
from fake_anthropic import GOOD_CHAIN, FakeAnthropic
from fake_provider import load

from corpus.axes import AxisSpec
from corpus.budget import Budget
from corpus.models import BlindSpot, Document, EvolutionEntry, Synthesis
from corpus.render import render_report
from corpus.synthesize import (
    build_reduce_system,
    enforce_signal_counts,
    prune_unsourced,
    synthesize,
)
from corpus.tiers import (
    MODERATE,
    RICH,
    RICH_FROM,
    THIN,
    THIN_BELOW,
    classify_corpus,
    prompt_block,
)
from corpus.x.hydrate import hydrate
from corpus.x.signals import compute_signals

AXES = [
    AxisSpec("institutions_and_authority", "Institutions", "probe"),
    AxisSpec("epistemics", "Epistemics", "probe"),
    AxisSpec("defense_intel_natsec", "Defense", "probe"),
]

IDS = {"111", "222", "333", "444"}


def _doc(doc_id: str, body: str, **kw) -> Document:
    return Document(
        source="x",
        source_id=doc_id,
        url=f"https://x.com/a/status/{doc_id}",
        author_handle="a",
        published_at=datetime(2024, 1, 1, tzinfo=timezone.utc),
        kind=kw.pop("kind", "original"),
        body=body,
        engagement={"likes": 1},
        **kw,
    )


def fresh(ids: str = "[id: 111] [id: 222]") -> Synthesis:
    return Synthesis.model_validate(
        json.loads(FakeAnthropic()._default_reduce({"messages": [{"c": ids}]}))
    )


def axis(synthesis: Synthesis, name: str):
    return next(a for a in synthesis.axes if a.axis == name)


# -- classification ---------------------------------------------------------


@pytest.mark.parametrize(
    ("count", "expected"),
    [
        (0, "thin"),
        (1, "thin"),
        (THIN_BELOW - 1, "thin"),
        (THIN_BELOW, "moderate"),
        (RICH_FROM - 1, "moderate"),
        (RICH_FROM, "rich"),
        (10_000, "rich"),
    ],
)
def test_boundaries(count, expected):
    assert classify_corpus(count).name == expected


def test_the_count_travels_with_the_tier():
    """The report quotes the number, so it has to be on the object."""
    assert classify_corpus(37).document_count == 37


def test_each_tier_permits_strictly_more_than_the_one_below():
    """A monotonicity check: a bigger corpus must never be more restricted."""
    thin, moderate, rich = classify_corpus(5), classify_corpus(90), classify_corpus(900)
    assert (thin.allow_inference, moderate.allow_inference, rich.allow_inference) == (
        False,
        True,
        True,
    )
    assert thin.min_chain_documents <= moderate.min_chain_documents
    assert rich.min_chain_documents <= moderate.min_chain_documents
    assert not thin.allow_blind_spots and moderate.allow_blind_spots and rich.allow_blind_spots
    assert not thin.allow_evolution and moderate.allow_evolution and rich.allow_evolution
    assert thin.forced_confidence == "low"
    assert moderate.forced_confidence is None and rich.forced_confidence is None


# -- thin: inference is switched off ---------------------------------------


def test_thin_suppresses_every_inference_but_keeps_the_stated_half():
    synthesis = fresh()
    notes = prune_unsourced(synthesis, IDS, AXES, classify_corpus(12))

    institutions = axis(synthesis, "institutions_and_authority")
    assert institutions.inferred == ""
    assert institutions.reasoning == ""
    assert institutions.confidence == "low"
    # The sourced half is untouched — it was never the thing in doubt.
    assert institutions.stated == "Credentials are a lazy proxy for competence."
    assert institutions.evidence_ids
    assert institutions.signal == "strong"
    assert any("inference suppressed" in n and "thin" in n for n in notes)


def test_thin_empties_blind_spots_even_when_well_sourced():
    """A blind spot is inference all the way down; there is no half to keep."""
    synthesis = fresh()
    synthesis.reasoning.blind_spots = [
        BlindSpot(pattern="Well-sourced pattern", basis=GOOD_CHAIN, evidence_ids=["111", "222"])
    ]
    notes = prune_unsourced(synthesis, IDS, AXES, classify_corpus(12))
    assert synthesis.reasoning.blind_spots == []
    assert any("cannot establish a pattern" in n for n in notes)


def test_thin_empties_evolution_even_when_well_sourced():
    synthesis = fresh()
    synthesis.evolution = [
        EvolutionEntry(topic="t", earlier="a", later="b", evidence_ids=["111", "222"])
    ]
    notes = prune_unsourced(synthesis, IDS, AXES, classify_corpus(12))
    assert synthesis.evolution == []
    assert any("before from an after" in n for n in notes)


def test_thin_forces_coverage_confidence_low():
    synthesis = fresh()
    synthesis.coverage.confidence = "high"
    notes = enforce_signal_counts(synthesis, {}, 12, classify_corpus(12))
    assert synthesis.coverage.confidence == "low"
    assert any("confidence forced" in n for n in notes)


def test_thin_keeps_the_beliefs_and_clears_their_structure():
    """The cut runs through `core_model`, not around it.

    A belief traced to real posts is a sourced claim and survives. Where it
    sits relative to the other beliefs — `role` and `generates` — is an
    inference about structure, and a thin corpus cannot support it.
    """
    synthesis = fresh()
    belief = synthesis.core_model[0]
    assert belief.role == "load_bearing" and belief.generates, "fixture must have structure"

    notes = prune_unsourced(synthesis, IDS, AXES, classify_corpus(12))

    assert synthesis.core_model, "the sourced half must survive a thin corpus"
    kept = synthesis.core_model[0]
    assert kept.belief.startswith("Process quality is measurable")
    assert kept.evidence_ids, "the evidence goes with the belief"
    assert kept.role == "unclassified"
    assert kept.generates == []
    assert any("structure cleared" in n for n in notes)


def test_unclassified_is_not_held_lightly():
    """ "Held lightly" asserts they voice the view but do not defend it, which is
    itself a claim about the belief. A thin corpus does not know that either, so
    forcing it would trade one confabulation for another."""
    synthesis = fresh()
    prune_unsourced(synthesis, IDS, AXES, classify_corpus(12))
    assert synthesis.core_model[0].role != "held_lightly"


def test_the_model_cannot_emit_unclassified_itself():
    """It is absent from the wire enum, so grammar-constrained decoding cannot
    produce it. The value means "code cleared this" and nothing else."""
    from corpus.synthesize import REDUCE_SCHEMA

    enum = REDUCE_SCHEMA["properties"]["core_model"]["items"]["properties"]["role"]["enum"]
    assert enum == ["load_bearing", "derived", "held_lightly"]
    assert "unclassified" not in enum


def test_an_unclassified_role_from_the_model_is_not_honoured():
    """Only reachable on the prompt-guided fallback path, where the grammar is
    not enforcing the enum. It must not become a way to opt out of a decision
    the corpus is big enough to support."""
    synthesis = fresh()
    synthesis.core_model[0].role = "unclassified"
    notes = prune_unsourced(synthesis, IDS, AXES, classify_corpus(900))
    assert synthesis.core_model[0].role == "derived"
    assert any("not the model's to assign" in n for n in notes)


def test_moderate_and_rich_keep_the_belief_structure():
    for count in (90, 900):
        synthesis = fresh()
        prune_unsourced(synthesis, IDS, AXES, classify_corpus(count))
        belief = synthesis.core_model[0]
        assert belief.role == "load_bearing", count
        assert belief.generates, count


# -- moderate: three sources per chain -------------------------------------


def test_moderate_drops_an_inference_sourced_to_fewer_than_three():
    synthesis = fresh()
    target = axis(synthesis, "institutions_and_authority")
    target.evidence_ids = ["111", "222"]  # two: one short
    notes = prune_unsourced(synthesis, IDS, AXES, classify_corpus(90))
    assert target.inferred == ""
    assert target.reasoning == ""
    assert target.stated, "only the inference should go"
    assert any("requires 3" in n for n in notes)


def test_moderate_keeps_an_inference_sourced_to_three():
    synthesis = fresh()
    target = axis(synthesis, "institutions_and_authority")
    target.evidence_ids = ["111", "222", "333"]
    prune_unsourced(synthesis, IDS, AXES, classify_corpus(90))
    assert target.inferred
    assert target.reasoning == GOOD_CHAIN


def test_moderate_applies_the_same_floor_to_blind_spots():
    synthesis = fresh()
    synthesis.reasoning.blind_spots = [
        BlindSpot(pattern="Thinly sourced", basis=GOOD_CHAIN, evidence_ids=["111"]),
        BlindSpot(pattern="Well sourced", basis=GOOD_CHAIN, evidence_ids=["111", "222", "333"]),
    ]
    prune_unsourced(synthesis, IDS, AXES, classify_corpus(90))
    assert [s.pattern for s in synthesis.reasoning.blind_spots] == ["Well sourced"]


def test_moderate_still_allows_evolution_and_does_not_force_confidence():
    synthesis = fresh()
    prune_unsourced(synthesis, IDS, AXES, classify_corpus(90))
    assert synthesis.evolution, "moderate must not empty evolution"
    notes = enforce_signal_counts(synthesis, {}, 90, classify_corpus(90))
    assert not any("confidence forced" in n for n in notes)


# -- rich: unchanged --------------------------------------------------------


def test_rich_keeps_a_single_source_inference():
    """The rich tier is the behaviour the report was designed around; a chain
    resting on one distinctive document is admissible there."""
    synthesis = fresh()
    target = axis(synthesis, "institutions_and_authority")
    target.evidence_ids = ["111"]
    prune_unsourced(synthesis, IDS, AXES, classify_corpus(900))
    assert target.inferred
    assert target.reasoning == GOOD_CHAIN


def test_rich_still_drops_a_hand_waving_chain():
    """Size relaxes the source floor, never the chain requirement."""
    synthesis = fresh()
    epistemics = axis(synthesis, "epistemics")  # reasoning is "it follows"
    prune_unsourced(synthesis, IDS, AXES, classify_corpus(900))
    assert epistemics.inferred == ""
    assert epistemics.stated == "Prefers measurement to intuition."


def test_no_tier_means_no_size_restriction_not_a_guessed_one():
    """`tier=None` is 'unrestricted', because neither the valid-id set nor the
    highlight list is the post-filter count the tier is defined on. Guessing
    from either would silently invent a fact."""
    synthesis = fresh()
    target = axis(synthesis, "institutions_and_authority")
    target.evidence_ids = ["111"]
    prune_unsourced(synthesis, IDS, AXES)  # two valid ids, no tier passed
    assert target.inferred, "an absent tier must not be read as thin"


# -- the tier reaches the model, and is not the model's to decide ----------


@pytest.mark.parametrize("count", [12, 90, 900])
def test_the_prompt_states_the_tier_and_the_count(count):
    rules = classify_corpus(count)
    block = prompt_block(rules)
    assert rules.name.upper() in block
    assert str(count) in block
    assert "ground truth" in block


def test_the_thin_prompt_forbids_the_model_from_second_guessing_it():
    block = prompt_block(classify_corpus(12))
    assert "Do\nnot second-guess it" in block
    assert "will be deleted" in block


def test_the_moderate_prompt_names_the_three_document_floor():
    assert "at least 3 distinct documents" in prompt_block(classify_corpus(90))


def test_the_tier_block_lands_in_the_reduce_system_prompt():
    prompt = build_reduce_system(AXES, classify_corpus(12))
    assert "Corpus size: THIN (12 documents)" in prompt
    assert "epistemics" in prompt, "the axes must still be there too"


def test_synthesize_threads_the_tier_through(client, cache):
    """The fail-open default in prune_unsourced cannot hide a wiring bug: the
    real run has to compute the tier and pass it on."""
    docs, _ = hydrate(client, load("tweets.json"), "testsubject", log=lambda _: None)
    fake = FakeAnthropic()
    result = asyncio.run(
        synthesize(
            docs,
            compute_signals(docs),
            Budget(limit=10.0, cache=cache),
            axes=AXES,
            client=fake,
            log=lambda _: None,
        )
    )
    assert result.tier is not None
    # The fixture corpus is small, so this run is thin — and the tier has to
    # have been computed from the post-filter count, not the corpus on disk.
    assert result.tier.name == "thin"
    assert result.tier.document_count == result.analyzed_documents
    assert result.as_dict()["corpus_tier"] == "thin"

    reduce_prompt = fake.calls[-1]["system"]
    assert "Corpus size: THIN" in reduce_prompt

    assert result.synthesis is not None
    assert result.synthesis.coverage.confidence == "low"
    assert result.synthesis.evolution == []
    assert result.synthesis.reasoning.blind_spots == []
    assert all(a.inferred == "" for a in result.synthesis.axes)


def test_the_tier_is_computed_after_filtering_not_before(cache):
    """A corpus that is moderate on disk and thin once the acknowledgements are
    gone must be treated as thin. Classifying before the filter is the exact
    mistake the tiers exist to catch."""
    real = [
        _doc(f"r{i}", "A substantive claim about hiring that runs well past the floor")
        for i in range(THIN_BELOW - 1)
    ]
    acks = [_doc(f"a{i}", "Thanks!", kind="reply", context="here you go") for i in range(10)]
    docs = real + acks
    assert len(docs) >= THIN_BELOW, "the pre-filter count must land in moderate"

    result = asyncio.run(
        synthesize(
            docs,
            compute_signals(docs),
            Budget(limit=10.0, cache=cache),
            axes=AXES,
            client=FakeAnthropic(),
            log=lambda _: None,
        )
    )
    assert result.analyzed_documents == THIN_BELOW - 1
    assert result.tier is not None and result.tier.name == "thin"


def test_the_same_corpus_is_moderate_when_the_filter_is_off(cache):
    """The mirror of the test above: --no-filter keeps the acknowledgements, so
    the same documents classify one tier higher. The tier follows what the model
    actually sees, which is the whole point."""
    real = [
        _doc(f"r{i}", "A substantive claim about hiring that runs well past the floor")
        for i in range(THIN_BELOW - 1)
    ]
    acks = [_doc(f"a{i}", "Thanks!", kind="reply", context="here you go") for i in range(10)]
    docs = real + acks

    result = asyncio.run(
        synthesize(
            docs,
            compute_signals(docs),
            Budget(limit=10.0, cache=cache),
            axes=AXES,
            prefilter=False,
            client=FakeAnthropic(),
            log=lambda _: None,
        )
    )
    assert result.analyzed_documents == len(docs)
    assert result.tier is not None and result.tier.name == "moderate"


# -- the report says so -----------------------------------------------------


def test_thin_report_explains_itself_and_names_the_remedy(client):
    docs, _ = hydrate(client, load("tweets.json"), "testsubject", log=lambda _: None)
    synthesis = fresh(" ".join(f"[id: {d.source_id}]" for d in docs[:2]))
    prune_unsourced(synthesis, {d.source_id for d in docs}, AXES, classify_corpus(12))
    report = render_report(
        handle="testsubject",
        synthesis=synthesis,
        docs=docs,
        signals=compute_signals(docs),
        budget_lines=[],
        run_meta={"analyzed_documents": 12, "corpus_tier": "thin"},
    )
    assert "## This corpus is too small for inference" in report
    assert "12 documents" in report
    # Name the fix, not just the problem.
    assert "--rss" in report and "--substack" in report and "--url" in report
    assert "--max-posts" in report
    # And place it above the analysis it qualifies.
    assert report.index("too small for inference") < report.index("## Beliefs, without")


def test_moderate_report_states_the_stricter_bar(client):
    docs, _ = hydrate(client, load("tweets.json"), "testsubject", log=lambda _: None)
    report = render_report(
        handle="testsubject",
        synthesis=fresh(" ".join(f"[id: {d.source_id}]" for d in docs[:2])),
        docs=docs,
        signals=compute_signals(docs),
        budget_lines=[],
        run_meta={"analyzed_documents": 90, "corpus_tier": "moderate"},
    )
    assert "Corpus tier: moderate (90 documents)" in report
    assert "at least 3 distinct documents" in report
    assert "too small for inference" not in report


def test_rich_report_carries_no_size_warning(client):
    docs, _ = hydrate(client, load("tweets.json"), "testsubject", log=lambda _: None)
    report = render_report(
        handle="testsubject",
        synthesis=fresh(" ".join(f"[id: {d.source_id}]" for d in docs[:2])),
        docs=docs,
        signals=compute_signals(docs),
        budget_lines=[],
        run_meta={"analyzed_documents": 900, "corpus_tier": "rich"},
    )
    assert "Corpus tier: rich (900 documents)" in report
    assert "too small for inference" not in report
    assert "stricter bar" not in report


def test_render_only_recovers_the_tier_without_run_meta(client):
    """`--render-only` may meet a directory with no run_meta.json. The tier has
    to come back from coverage.total_documents, which is the same post-filter
    count Python classified on."""
    docs, _ = hydrate(client, load("tweets.json"), "testsubject", log=lambda _: None)
    synthesis = fresh(" ".join(f"[id: {d.source_id}]" for d in docs[:2]))
    synthesis.coverage.total_documents = 12
    report = render_report(
        handle="testsubject",
        synthesis=synthesis,
        docs=docs,
        signals=compute_signals(docs),
        budget_lines=[],
        run_meta={},
    )
    assert "## This corpus is too small for inference" in report


def test_the_module_constants_are_the_ones_the_tiers_use():
    """Guards against the tables and the classifier drifting apart."""
    assert THIN.name == "thin" and not THIN.allow_inference
    assert MODERATE.name == "moderate" and MODERATE.min_chain_documents == 3
    assert RICH.name == "rich" and RICH.min_chain_documents == 1


def test_the_confidence_label_stops_claiming_the_model_assessed_it(client):
    """At thin the model's assessment is precisely what was overridden, so
    calling the result "model-assessed" would be a lie."""
    docs, _ = hydrate(client, load("tweets.json"), "testsubject", log=lambda _: None)
    synthesis = fresh(" ".join(f"[id: {d.source_id}]" for d in docs[:2]))
    # As enforce_signal_counts would have left it on a thin run.
    synthesis.coverage.confidence = "low"

    thin = render_report(
        handle="t",
        synthesis=synthesis,
        docs=docs,
        signals={},
        budget_lines=[],
        run_meta={"analyzed_documents": 12, "corpus_tier": "thin"},
    )
    assert "Confidence (set in code, not by the model)" in thin
    assert "Model-assessed confidence" not in thin

    # The same synthesis at a tier that forces nothing: the model's own value,
    # honestly labelled as the model's.
    rich = render_report(
        handle="t",
        synthesis=synthesis,
        docs=docs,
        signals={},
        budget_lines=[],
        run_meta={"analyzed_documents": 900, "corpus_tier": "rich"},
    )
    assert "Model-assessed confidence" in rich
    assert "set in code" not in rich


# -- the dry run says so before the money is spent -------------------------


def _dry_run(monkeypatch, tmp_path, public_posts: int, extra: list[str] | None = None):
    """Drive `corpus run --dry-run` against a profile with `public_posts`.

    The cache is redirected to tmp_path. Without that, `run` opens the real
    `~/.corpus/cache.db`, the first call caches the profile, and every later
    call in the suite reads that instead of the fake — which is how this
    started out asserting against a number no test had set.
    """
    from fake_provider import FakeProvider
    from typer.testing import CliRunner

    import corpus.cli as cli_module

    class _Provider(FakeProvider):
        def user_info(self, handle):
            return {"userName": handle, "statusesCount": public_posts}

    tmp_path.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("CORPUS_CACHE_DB", str(tmp_path / "cache.db"))
    monkeypatch.setattr(cli_module, "get_provider", lambda **_kw: _Provider())
    monkeypatch.setenv("ANTHROPIC_API_KEY", "")
    return CliRunner().invoke(
        cli_module.app, ["run", "--x", "someone", "--dry-run", *(extra or [])]
    )


def test_dry_run_names_the_secondary_sources_when_the_corpus_will_be_thin(monkeypatch, tmp_path):
    """They are wired up and easy to miss, and the moment they matter most is
    before a thin run is paid for."""
    result = _dry_run(monkeypatch, tmp_path, public_posts=25)
    assert result.exit_code == 0, result.output
    assert "THIN corpus" in result.output
    assert "--substack DOMAIN" in result.output
    assert "--rss URL" in result.output
    assert "--url URL" in result.output
    # Say what it costs to use them, because "add more sources" is not advice
    # if the reader assumes it doubles the bill.
    assert "cost nothing" in result.output
    # And the other lever, which is free too.
    assert "--max-posts" in result.output


def test_dry_run_stays_quiet_when_the_corpus_will_be_big_enough(monkeypatch, tmp_path):
    result = _dry_run(monkeypatch, tmp_path, public_posts=5_000)
    assert result.exit_code == 0, result.output
    assert "THIN corpus" not in result.output
    assert "--substack DOMAIN" not in result.output


def test_dry_run_stays_quiet_when_synthesis_is_skipped(monkeypatch, tmp_path):
    """No synthesis, no tier, no advice about one."""
    result = _dry_run(monkeypatch, tmp_path, public_posts=25, extra=["--skip-synthesis"])
    assert result.exit_code == 0, result.output
    assert "THIN corpus" not in result.output


def test_the_dry_run_threshold_is_the_tier_threshold(monkeypatch, tmp_path):
    """One constant, so the warning can never disagree with the behaviour it
    is warning about."""
    below = _dry_run(monkeypatch, tmp_path / "below", public_posts=THIN_BELOW - 1)
    at = _dry_run(monkeypatch, tmp_path / "at", public_posts=THIN_BELOW)
    assert "THIN corpus" in below.output
    assert "THIN corpus" not in at.output


def test_the_forced_label_checks_the_value_it_describes(client):
    """A synthesis that never went through enforce_signal_counts must not be
    labelled as though code had set its confidence. `--render-only` can meet
    exactly that file."""
    docs, _ = hydrate(client, load("tweets.json"), "testsubject", log=lambda _: None)
    synthesis = fresh(" ".join(f"[id: {d.source_id}]" for d in docs[:2]))
    synthesis.coverage.confidence = "high"  # thin tier, but never enforced

    report = render_report(
        handle="t",
        synthesis=synthesis,
        docs=docs,
        signals={},
        budget_lines=[],
        run_meta={"analyzed_documents": 12, "corpus_tier": "thin"},
    )
    assert "Model-assessed confidence: **high**" in report
    assert "set in code" not in report


def test_thin_report_calls_the_flat_list_a_flat_list(client):
    """A flat list under "The generating model" reads as a considered tree that
    happens to have no branches. That is a stronger claim than the corpus
    supports, made by omission."""
    docs, _ = hydrate(client, load("tweets.json"), "testsubject", log=lambda _: None)
    synthesis = fresh(" ".join(f"[id: {d.source_id}]" for d in docs[:2]))
    prune_unsourced(synthesis, {d.source_id for d in docs}, AXES, classify_corpus(12))

    report = render_report(
        handle="t",
        synthesis=synthesis,
        docs=docs,
        signals={},
        budget_lines=[],
        run_meta={"analyzed_documents": 12, "corpus_tier": "thin"},
    )
    assert "## Beliefs, without the structure" in report
    assert "## The generating model" not in report
    assert "**This is a list, not a model.**" in report
    assert "12 documents cannot support it" in report
    # The belief and its evidence are still there.
    assert "### Process quality is measurable" in report
    assert "](https://x.com/testsubject/status/" in report
    # But no role claim and no arrows.
    assert "_load-bearing_" not in report
    assert "→" not in report
    # And the thin notice names it among what was switched off.
    assert "Beliefs are listed without structure" in report


def test_rich_report_still_presents_the_model_as_a_model(client):
    docs, _ = hydrate(client, load("tweets.json"), "testsubject", log=lambda _: None)
    report = render_report(
        handle="t",
        synthesis=fresh(" ".join(f"[id: {d.source_id}]" for d in docs[:2])),
        docs=docs,
        signals={},
        budget_lines=[],
        run_meta={"analyzed_documents": 900, "corpus_tier": "rich"},
    )
    assert "## The generating model" in report
    assert "This is a list, not a model" not in report
    assert "_load-bearing_" in report
    assert "→ hostility to interview puzzles" in report


def test_the_thin_prompt_asks_for_beliefs_without_structure():
    block = prompt_block(classify_corpus(12))
    assert "`core_model` beliefs are still expected" in block
    assert "leave `generates` as an **empty list**" in block
    assert 'overwritten with "unclassified"' in block
