"""Synthesis tests. Everything runs against fixtures — no paid calls."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone

from fake_anthropic import GOOD_CHAIN, FakeAnthropic
from fake_provider import load

from corpus.axes import AxisSpec
from corpus.budget import Budget
from corpus.models import Axis, Document, Synthesis
from corpus.synthesize import (
    CHUNK_TOKEN_TARGET,
    EVIDENCE_CAP,
    MAP_MODEL,
    REDUCE_MODEL,
    _is_schema_rejection,
    _strip_code_fences,
    build_reduce_system,
    chunk_documents,
    enforce_signal_counts,
    parse_synthesis,
    prune_unsourced,
    render_document,
    run_reduce,
    select_highlights,
    supports_effort,
    synthesize,
)
from corpus.tiers import classify_corpus
from corpus.x.hydrate import hydrate
from corpus.x.signals import compute_signals

AXES = [
    AxisSpec("institutions_and_authority", "Institutions", "probe"),
    AxisSpec("epistemics", "Epistemics", "probe"),
    AxisSpec("defense_intel_natsec", "Defense", "probe"),
]


def doc(doc_id: str, body: str, kind: str = "original", **kw) -> Document:
    return Document(
        source="x",
        source_id=doc_id,
        url=f"https://x.com/a/status/{doc_id}",
        author_handle="a",
        published_at=kw.pop("when", datetime(2024, 1, 1, tzinfo=timezone.utc)),
        kind=kind,  # type: ignore[arg-type]
        body=body,
        engagement={"likes": 1, "replies": 0, "reposts": 0},
        **kw,
    )


def fresh_synthesis(ids: str = "[id: 111] [id: 222]") -> Synthesis:
    raw = FakeAnthropic()._default_reduce({"messages": [{"c": ids}]})
    return Synthesis.model_validate(json.loads(raw))


# -- rendering: the rule that matters most ---------------------------------


def test_reply_context_is_labelled_as_someone_elses_words():
    d = doc(
        "1",
        "Completely backwards for anyone under 30 people.",
        kind="reply",
        context="Founders should hire ahead of the curve.",
        context_author="vcpartner",
    )
    text = render_document(d)
    assert "THEY REPLIED TO (@vcpartner): Founders should hire ahead of the curve." in text
    assert "THEY WROTE: Completely backwards" in text
    # context must appear above the subject's own words
    assert text.index("THEY REPLIED TO") < text.index("THEY WROTE")


def test_quote_context_is_labelled_as_a_quote():
    d = doc("1", "Show me the numbers.", kind="quote", context="RTO mandate", context_author="ceo")
    assert "THEY QUOTED (@ceo): RTO mandate" in render_document(d)


def test_document_with_no_context_has_no_context_line():
    assert "THEY REPLIED TO" not in render_document(doc("1", "standalone thought"))


def test_thread_part_count_is_visible_to_the_model():
    d = doc("1", "a\n\nb\n\nc", kind="thread")
    d.part_count = 3
    assert "thread of 3" in render_document(d)


# -- chunking ---------------------------------------------------------------


def test_chunks_are_chronological_and_bounded():
    docs = [
        doc(str(i), "word " * 400, when=datetime(2024, 1, 1, tzinfo=timezone.utc))
        for i in range(60)
    ]
    for i, d in enumerate(docs):
        d.published_at = datetime(2024, 1, 1, tzinfo=timezone.utc).replace(day=1 + (i % 28))
    chunks = chunk_documents(docs)
    assert len(chunks) > 1
    flat = [d for c in chunks for d in c]
    assert flat == sorted(flat, key=lambda d: d.published_at)
    for chunk in chunks:
        approx = sum(len(render_document(d)) // 4 + 8 for d in chunk)
        # only the first document may push a chunk over target
        assert approx <= CHUNK_TOKEN_TARGET * 1.3


def test_single_small_corpus_is_one_chunk():
    assert len(chunk_documents([doc("1", "short")])) == 1


# -- model selection --------------------------------------------------------


def test_map_is_cheap_and_reduce_is_not():
    """Map is extraction, reduce is judgment. Do not let them drift together."""
    assert MAP_MODEL == "claude-haiku-4-5-20251001"
    assert REDUCE_MODEL == "claude-opus-5"


def test_effort_is_only_sent_to_models_that_implement_it():
    """Haiku 4.5 rejects `effort` outright; sending it would 400 every slice."""
    assert not supports_effort("claude-haiku-4-5-20251001")
    assert not supports_effort("claude-sonnet-4-5")
    assert supports_effort("claude-opus-5")
    assert supports_effort("claude-sonnet-5")


def test_map_call_omits_effort_for_haiku(client, cache):
    docs, _ = hydrate(client, load("tweets.json"), "testsubject", log=lambda _: None)
    fake = FakeAnthropic()
    run_synth(fake, docs, compute_signals(docs), Budget(limit=10.0, cache=cache))
    map_call = fake.calls[0]
    assert "effort" not in map_call["output_config"]
    assert map_call["output_config"]["format"]["type"] == "json_schema"


def test_reduce_call_keeps_effort_and_constrained_decoding(client, cache):
    docs, _ = hydrate(client, load("tweets.json"), "testsubject", log=lambda _: None)
    fake = FakeAnthropic()
    run_synth(fake, docs, compute_signals(docs), Budget(limit=10.0, cache=cache))
    reduce_call = fake.calls[-1]
    assert reduce_call["output_config"]["effort"] == "high"
    assert reduce_call["output_config"]["format"]["type"] == "json_schema"


# -- fence stripping (belt and braces for the fallback path) ----------------


def test_bare_json_passes_through_untouched():
    assert _strip_code_fences('{"a": 1}') == '{"a": 1}'


def test_json_fence_is_stripped():
    assert _strip_code_fences('```json\n{"a": 1}\n```') == '{"a": 1}'


def test_bare_fence_is_stripped():
    assert _strip_code_fences('```\n{"a": 1}\n```') == '{"a": 1}'


def test_leading_prose_is_discarded():
    text = 'Here is the synthesis you asked for:\n\n{"a": 1}'
    assert _strip_code_fences(text) == '{"a": 1}'


def test_prose_and_fence_together():
    text = 'Sure! Here you go:\n\n```json\n{"a": 1}\n```\n\nLet me know.'
    assert _strip_code_fences(text) == '{"a": 1}'


def test_fenced_synthesis_validates():
    """The exact failure seen live: a full generation lost to backticks."""
    raw = FakeAnthropic()._default_reduce({"messages": [{"c": "[id: 111]"}]})
    assert parse_synthesis(f"```json\n{raw}\n```").summary


# -- unsourced-finding enforcement -----------------------------------------


def test_prune_drops_beliefs_citing_ids_not_in_the_corpus():
    synthesis = fresh_synthesis()
    assert len(synthesis.core_model) == 2
    notes = prune_unsourced(synthesis, {"111", "222"}, AXES)
    assert [b.belief for b in synthesis.core_model] == [
        "Process quality is measurable and most people refuse to measure it"
    ]
    assert any("invented belief" in n for n in notes)


def test_prune_drops_reasoning_moves_with_a_bogus_example_id():
    synthesis = fresh_synthesis()
    prune_unsourced(synthesis, {"111", "222"}, AXES)
    assert [m.move for m in synthesis.reasoning.moves] == ["concedes the strongest objection first"]


def test_everything_is_dropped_when_nothing_is_sourceable():
    synthesis = fresh_synthesis()
    prune_unsourced(synthesis, set(), AXES)
    assert synthesis.core_model == []
    assert synthesis.evolution == []
    assert synthesis.open_questions == []
    assert synthesis.misreadings == []
    assert synthesis.reasoning.blind_spots == []


# -- the inference tier: reasoning is the evidence --------------------------


def test_hand_waving_inference_is_dropped_but_the_stated_tier_survives():
    synthesis = fresh_synthesis()
    notes = prune_unsourced(synthesis, {"111", "222"}, AXES)
    epistemics = next(a for a in synthesis.axes if a.axis == "epistemics")
    assert epistemics.inferred == "", "an inference with no chain must not survive"
    assert epistemics.reasoning == ""
    assert epistemics.stated == "Prefers measurement to intuition.", (
        "the stated tier has its own sourcing and must not be collateral damage"
    )
    assert any("not a chain" in n for n in notes)


def test_inference_with_a_real_chain_is_kept():
    synthesis = fresh_synthesis()
    prune_unsourced(synthesis, {"111", "222"}, AXES)
    axis = next(a for a in synthesis.axes if a.axis == "institutions_and_authority")
    assert axis.inferred
    assert axis.reasoning == GOOD_CHAIN
    assert axis.confidence == "medium"


def test_blind_spot_without_a_basis_is_dropped():
    synthesis = fresh_synthesis()
    prune_unsourced(synthesis, {"111", "222"}, AXES)
    assert [s.pattern for s in synthesis.reasoning.blind_spots] == [
        "Treats their own sample as representative"
    ]


def test_inference_that_merely_restates_itself_is_not_a_chain():
    synthesis = fresh_synthesis()
    axis = next(a for a in synthesis.axes if a.axis == "institutions_and_authority")
    axis.inferred = "They treat institutional legitimacy as something earned per claim"
    axis.reasoning = "They treat institutional legitimacy as something earned per claim"
    prune_unsourced(synthesis, {"111", "222"}, AXES)
    assert axis.inferred == ""


# -- signal: none is a first-class result -----------------------------------


def test_empty_signal_axis_survives_validation_without_invented_content():
    """The paulg corpus had zero defense/intel content. The correct report says
    so. An axis that reports nothing must round-trip intact."""
    synthesis = fresh_synthesis()
    prune_unsourced(synthesis, {"111", "222"}, AXES)
    axis = next(a for a in synthesis.axes if a.axis == "defense_intel_natsec")
    assert axis.signal == "none"
    assert axis.stated == ""
    assert axis.inferred == ""
    assert axis.reasoning == ""
    assert axis.evidence_ids == []
    # and it survives a JSON round trip, so the report and downstream tools see it
    assert Synthesis.model_validate(synthesis.model_dump()).axes[-1].signal == "none"


def test_content_on_a_no_signal_axis_is_cleared_not_trusted():
    synthesis = fresh_synthesis()
    axis = next(a for a in synthesis.axes if a.axis == "defense_intel_natsec")
    axis.stated = "They are hawkish on China."
    axis.inferred = "They would support export controls."
    notes = prune_unsourced(synthesis, {"111", "222"}, AXES)
    assert axis.stated == ""
    assert axis.inferred == ""
    assert any("cleared invented content" in n for n in notes)


def test_axis_claiming_signal_without_evidence_is_demoted_to_none():
    synthesis = fresh_synthesis()
    axis = next(a for a in synthesis.axes if a.axis == "institutions_and_authority")
    axis.evidence_ids = ["9999999999"]
    notes = prune_unsourced(synthesis, {"111", "222"}, AXES)
    assert axis.signal == "none"
    assert axis.stated == ""
    assert any("demoted to signal none" in n for n in notes)


def test_every_requested_axis_appears_even_if_the_model_forgot_it():
    synthesis = fresh_synthesis()
    synthesis.axes = [a for a in synthesis.axes if a.axis != "epistemics"]
    notes = prune_unsourced(synthesis, {"111", "222"}, AXES)
    assert [a.axis for a in synthesis.axes] == [s.name for s in AXES]
    assert next(a for a in synthesis.axes if a.axis == "epistemics").signal == "none"
    assert any("was not returned" in n for n in notes)


def test_axes_outside_the_requested_set_are_dropped():
    synthesis = fresh_synthesis()
    synthesis.axes.append(Axis(axis="astrology", signal="strong", stated="x", evidence_ids=["111"]))
    notes = prune_unsourced(synthesis, {"111", "222"}, AXES)
    assert "astrology" not in [a.axis for a in synthesis.axes]
    assert any("not among the requested axes" in n for n in notes)


def test_reduce_prompt_names_the_selected_axes_and_the_none_rule():
    prompt = build_reduce_system(AXES, classify_corpus(400))
    assert "defense_intel_natsec" in prompt
    assert "technology_and_ai" not in prompt, "unselected axes must not leak in"
    assert '`signal: "none"` is a required, valid, expected output' in prompt


# -- evidence cap -----------------------------------------------------------


def test_evidence_is_capped_at_three_ids():
    synthesis = fresh_synthesis()
    synthesis.core_model[0].evidence_ids = [str(i) for i in range(10)]
    prune_unsourced(synthesis, {str(i) for i in range(10)}, AXES)
    assert len(synthesis.core_model[0].evidence_ids) == EVIDENCE_CAP
    assert synthesis.core_model[0].evidence_ids == ["0", "1", "2"]


def test_duplicate_evidence_ids_are_collapsed():
    synthesis = fresh_synthesis()
    synthesis.core_model[0].evidence_ids = ["111", "111", "222"]
    prune_unsourced(synthesis, {"111", "222"}, AXES)
    assert synthesis.core_model[0].evidence_ids == ["111", "222"]


# -- counts -----------------------------------------------------------------


def test_signal_counts_override_whatever_the_model_said():
    synthesis = fresh_synthesis()
    signals = {"date_range": "2024-01-08 to 2024-08-16"}
    notes = enforce_signal_counts(synthesis, signals, analyzed_documents=7)
    assert synthesis.coverage.total_documents == 7
    assert synthesis.coverage.date_range == "2024-01-08 to 2024-08-16"
    assert len(notes) == 2


def test_analyzed_count_reflects_the_filter_not_the_corpus():
    synthesis = fresh_synthesis()
    enforce_signal_counts(synthesis, {}, analyzed_documents=42)
    assert synthesis.coverage.total_documents == 42


def test_select_highlights_dedupes_and_orders():
    docs = [doc(str(i), f"body {i}") for i in range(5)]
    for i, d in enumerate(docs):
        d.published_at = datetime(2024, 1, 1 + i, tzinfo=timezone.utc)
    outputs = [
        {"highest_signal_document_ids": ["3", "1"]},
        {"highest_signal_document_ids": ["1", "0"]},
    ]
    picked = select_highlights(docs, outputs)
    assert [p.source_id for p in picked] == ["0", "1", "3"]


# -- end-to-end against fixtures -------------------------------------------


def run_synth(client, docs, signals, budget, **kw):
    kw.setdefault("axes", AXES)
    return asyncio.run(synthesize(docs, signals, budget, client=client, log=lambda _: None, **kw))


def test_end_to_end_map_reduce(client, cache):
    docs, _ = hydrate(client, load("tweets.json"), "testsubject", log=lambda _: None)
    signals = compute_signals(docs)
    budget = Budget(limit=10.0, cache=cache)
    fake = FakeAnthropic()

    result = run_synth(fake, docs, signals, budget)

    assert result.synthesis is not None
    assert result.error == ""
    assert result.chunks >= 1
    assert result.map_failures == 0
    # unsourced belief was dropped by the Python enforcement, not trusted away
    assert len(result.synthesis.core_model) == 1
    assert result.dropped_findings
    # both stages were charged, at their own rates
    assert budget.total_for("anthropic") > 0
    assert any(c.endpoint == MAP_MODEL for c in budget.charges)
    assert any(c.endpoint == REDUCE_MODEL for c in budget.charges)


def test_reposts_and_media_only_are_not_sent_to_the_model(client, cache):
    docs, _ = hydrate(
        client, load("tweets.json"), "testsubject", include_reposts=True, log=lambda _: None
    )
    fake = FakeAnthropic()
    run_synth(fake, docs, compute_signals(docs), Budget(limit=10.0, cache=cache))
    sent = json.dumps([c.get("messages") for c in fake.calls], default=str)
    assert "1700000000000000140" not in sent, "repost leaked into synthesis input"
    assert "1700000000000000150" not in sent, "media-only leaked into synthesis input"


def test_corpus_block_is_cached_for_the_retry(client, cache):
    docs, _ = hydrate(client, load("tweets.json"), "testsubject", log=lambda _: None)
    fake = FakeAnthropic()
    run_synth(fake, docs, compute_signals(docs), Budget(limit=10.0, cache=cache))
    reduce_call = fake.calls[-1]
    blocks = reduce_call["messages"][0]["content"]
    assert blocks[0]["cache_control"] == {"type": "ephemeral"}


def test_signals_are_injected_as_ground_truth(client, cache):
    docs, _ = hydrate(client, load("tweets.json"), "testsubject", log=lambda _: None)
    signals = compute_signals(docs)
    fake = FakeAnthropic()
    run_synth(fake, docs, signals, Budget(limit=10.0, cache=cache))
    corpus_block = fake.calls[-1]["messages"][0]["content"][0]["text"]
    assert "SIGNALS (ground truth" in corpus_block
    assert "vocabulary_drift" in corpus_block


def test_reduce_retries_once_then_gives_up(client, cache):
    docs, _ = hydrate(client, load("tweets.json"), "testsubject", log=lambda _: None)
    attempts: list[int] = []

    def bad_reduce(_kwargs):
        attempts.append(1)
        return "{not json at all"

    fake = FakeAnthropic(reduce_response=bad_reduce)
    result = run_synth(fake, docs, compute_signals(docs), Budget(limit=10.0, cache=cache))

    assert result.synthesis is None
    assert len(attempts) == 2, "must retry exactly once"
    assert "validation failed twice" in result.error
    assert result.raw_reduce_output  # dumped for inspection


def test_fenced_reduce_output_no_longer_burns_a_generation(client, cache):
    """The live failure: constrained decoding unavailable, model wraps in
    fences, validation fails, a full billed generation is lost."""
    docs, _ = hydrate(client, load("tweets.json"), "testsubject", log=lambda _: None)
    attempts: list[int] = []

    def fenced(kwargs):
        attempts.append(1)
        return "```json\n" + FakeAnthropic()._default_reduce(kwargs) + "\n```"

    fake = FakeAnthropic(reduce_response=fenced)
    result = run_synth(fake, docs, compute_signals(docs), Budget(limit=10.0, cache=cache))
    assert result.synthesis is not None
    assert len(attempts) == 1, "no retry should have been needed"


def test_retry_includes_the_validation_error(client, cache):
    docs, _ = hydrate(client, load("tweets.json"), "testsubject", log=lambda _: None)
    fake = FakeAnthropic(reduce_response=lambda _k: "{}")
    run_synth(fake, docs, compute_signals(docs), Budget(limit=10.0, cache=cache))
    second_attempt = fake.calls[-1]["messages"][0]["content"][1]["text"]
    assert "failed schema validation" in second_attempt or "validation" in second_attempt.lower()


def test_map_failure_does_not_lose_the_other_slices(client, cache):
    docs, _ = hydrate(client, load("tweets.json"), "testsubject", log=lambda _: None)
    calls = {"n": 0}

    def flaky_map(kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            return "not json"
        return FakeAnthropic()._default_map(kwargs)

    fake = FakeAnthropic(map_response=flaky_map)
    result = run_synth(fake, docs, compute_signals(docs), Budget(limit=10.0, cache=cache))
    assert result.map_failures >= 1
    # with only one chunk in the fixture corpus this correctly aborts before reduce
    assert result.synthesis is None or result.map_outputs


def test_budget_stop_during_synthesis_is_not_a_crash(client, cache):
    docs, _ = hydrate(client, load("tweets.json"), "testsubject", log=lambda _: None)
    budget = Budget(limit=0.000001, cache=cache)
    result = run_synth(FakeAnthropic(), docs, compute_signals(docs), budget)
    assert result.synthesis is None
    assert "budget" in result.error.lower()


# --------------------------------------------------------------------------
# Schema rejection: found live 2026-08-02, and now the *unused* path
# --------------------------------------------------------------------------
# The API compiles the output schema into a grammar and refuses one above an
# internal size limit. The pre-redesign REDUCE_SCHEMA was 4,826 bytes and over
# it, so reduce could never have succeeded; the fallback to prompt-guided JSON
# was what made the tool work at all.
#
# The cognition-first schema is 3,251 bytes and fits, so the fallback is no
# longer the normal path — which makes these tests the only thing keeping it
# honest. Do not delete them because "the schema fits now": the limit belongs
# to the provider, and the next field added could put us back over it in
# production, after ingestion has been paid for.


def _grammar_error() -> Exception:
    return RuntimeError(
        "Error code: 400 - {'type': 'error', 'error': {'type': "
        "'invalid_request_error', 'message': 'The compiled grammar is too "
        "large, which would cause performance issues. Simplify your tool "
        "schemas or reduce the number of strict tools.'}}"
    )


def _refuse_schema(client: FakeAnthropic, billed: list[int] | None = None) -> list[int]:
    """Make the stub behave like the live API: refuse any request carrying a
    `format`, before generating anything."""
    original_stream = client.messages.stream
    refusals: list[int] = []

    def stream(**kwargs):
        if "format" in (kwargs.get("output_config") or {}):
            refusals.append(1)
            raise _grammar_error()
        if billed is not None:
            billed.append(1)
        return original_stream(**kwargs)

    client.messages.stream = stream  # type: ignore[method-assign]
    return refusals


def test_schema_rejection_is_recognised():
    assert _is_schema_rejection(_grammar_error())
    assert not _is_schema_rejection(RuntimeError("500 internal server error"))
    assert not _is_schema_rejection(RuntimeError("429 rate limited"))


def test_reduce_falls_back_when_the_schema_is_refused(cache):
    """Degrade to prompt-guided JSON rather than losing the whole run."""
    client = FakeAnthropic()
    refusals = _refuse_schema(client)

    synthesis, _raw, error, structured = asyncio.run(
        run_reduce(
            client,
            [{"topics": [], "claims": []}],
            {"date_range": "2024"},
            [],
            Budget(limit=100.0, cache=cache),
            axes=AXES,
            log=lambda _: None,
        )
    )
    assert refusals, "the structured attempt never happened"
    assert structured is False, "the fallback flag was not recorded"
    assert error == "", f"the fallback did not produce a synthesis: {error}"
    assert synthesis is not None


def test_the_schema_fallback_does_not_consume_a_paid_attempt(cache):
    """The refusal is free — nothing is generated — so it must not cost a retry."""
    client = FakeAnthropic(reduce_response=lambda _kwargs: "{not json")
    billed: list[int] = []
    _refuse_schema(client, billed)

    synthesis, _raw, error, structured = asyncio.run(
        run_reduce(
            client,
            [{"topics": []}],
            {},
            [],
            Budget(limit=100.0, cache=cache),
            axes=AXES,
            log=lambda _: None,
        )
    )
    assert synthesis is None and "validation failed" in error
    assert len(billed) == 2, f"expected 2 billed attempts, got {len(billed)}"
    assert structured is False


def test_the_fallback_prompt_carries_the_schema_and_forbids_fences(cache):
    """Without `format` the schema has to be in the prompt, and the fence
    instruction is what stops the failure `_strip_code_fences` cleans up."""
    client = FakeAnthropic()
    _refuse_schema(client)
    asyncio.run(
        run_reduce(
            client,
            [{"topics": []}],
            {},
            [],
            Budget(limit=100.0, cache=cache),
            axes=AXES,
            log=lambda _: None,
        )
    )
    fallback_prompt = json.dumps(client.calls[-1]["messages"], default=str)
    assert "core_model" in fallback_prompt, "the schema was not sent in the prompt"
    assert "no markdown fences" in fallback_prompt
