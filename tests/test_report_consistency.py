"""The report must not contradict itself.

Two bugs from a live run on @paulg, both the same shape: two numbers or two
claims about the same corpus, printed a few lines apart, disagreeing — with
the honest version in the caveats where a reader is least likely to weigh it.

1. The `defense_intel_natsec` axis reported `weak signal` on one glancing
   AI-influence-operations exchange plus a shared link to a .gov visa portal,
   while the coverage block correctly said there were no documents on the
   subject beyond one glancing exchange.
2. The header said `94 documents`; the coverage block said `90 of 94`.
"""

from __future__ import annotations

from datetime import datetime, timezone

from corpus.models import Axis, Coverage, Document, Synthesis
from corpus.render import render_report
from corpus.synthesize import MIN_WEAK_DOCUMENTS, prune_unsourced


def doc(doc_id: str, body: str, **kw: object) -> Document:
    return Document(
        source="x",
        source_id=doc_id,
        url=f"https://x.com/a/status/{doc_id}",
        author_handle="a",
        published_at=datetime(2024, 1, 1, tzinfo=timezone.utc),
        kind="original",
        body=body,
        **kw,  # type: ignore[arg-type]
    )


ENGAGED = "The case for export controls rests on a claim about diffusion rates that nobody checks."
ALSO_ENGAGED = "Adversary capability estimates are downstream of who is allowed to publish."


def synthesis_with(axis: Axis) -> Synthesis:
    return Synthesis(summary="s", axes=[axis], coverage=Coverage(total_documents=90))


# -- 8a: the weak/none boundary ---------------------------------------------


def test_one_glancing_mention_plus_a_shared_link_is_no_signal() -> None:
    """The live failure, reproduced.

    A tangential remark and a bare URL with no commentary. The axis used to
    come back `weak`; it must come back `none`.
    """
    docs = {
        "1": doc("1", "AI influence ops are a real thing I guess."),
        "2": doc(
            "2", "https://travel.state.gov/visa", outbound_links=["https://travel.state.gov/visa"]
        ),
    }
    synthesis = synthesis_with(
        Axis(
            axis="defense_intel_natsec",
            signal="weak",
            stated="Mildly concerned about influence operations.",
            evidence_ids=["1", "2"],
        )
    )

    notes = prune_unsourced(synthesis, set(docs), None, None, documents=docs)

    assert synthesis.axes[0].signal == "none"
    assert synthesis.axes[0].stated == "", "a no-signal axis carries no content"
    assert any("weak -> none" in n for n in notes)


def test_a_link_with_no_commentary_is_not_engagement() -> None:
    """Two documents, so the count alone would pass. One of them is a bare
    link, which is not engagement with what it links to."""
    docs = {
        "1": doc("1", ENGAGED),
        "2": doc("2", "https://example.com/x", outbound_links=["https://example.com/x"]),
    }
    synthesis = synthesis_with(
        Axis(axis="defense_intel_natsec", signal="weak", stated="x", evidence_ids=["1", "2"])
    )

    prune_unsourced(synthesis, set(docs), None, None, documents=docs)

    assert synthesis.axes[0].signal == "none"


def test_two_substantively_engaged_documents_hold_a_weak_signal() -> None:
    """The rule tightens the floor; it does not delete the tier."""
    docs = {"1": doc("1", ENGAGED), "2": doc("2", ALSO_ENGAGED)}
    synthesis = synthesis_with(
        Axis(
            axis="defense_intel_natsec",
            signal="weak",
            stated="Sceptical of diffusion-rate arguments for export controls.",
            evidence_ids=["1", "2"],
        )
    )

    prune_unsourced(synthesis, set(docs), None, None, documents=docs)

    assert synthesis.axes[0].signal == "weak"
    assert synthesis.axes[0].stated


def test_a_strong_signal_is_not_touched_by_the_weak_floor() -> None:
    docs = {"1": doc("1", ENGAGED)}
    synthesis = synthesis_with(
        Axis(axis="politics_and_ideology", signal="strong", stated="x", evidence_ids=["1"])
    )

    prune_unsourced(synthesis, set(docs), None, None, documents=docs)

    assert synthesis.axes[0].signal == "strong"


def test_the_count_is_enforced_even_without_the_corpus_to_hand() -> None:
    """An older caller, or a hand-edited synthesis. The "at least two
    documents" half needs only the ids."""
    synthesis = synthesis_with(
        Axis(axis="defense_intel_natsec", signal="weak", stated="x", evidence_ids=["1"])
    )

    prune_unsourced(synthesis, {"1"}, None, None)

    assert MIN_WEAK_DOCUMENTS == 2
    assert synthesis.axes[0].signal == "none"


# -- 8b: the header count ---------------------------------------------------


def _report(analyzed: int, corpus: int) -> str:
    docs = [doc(str(i), f"body {i}") for i in range(corpus)]
    synthesis = Synthesis(summary="s", coverage=Coverage(total_documents=analyzed))
    return render_report(
        handle="a",
        synthesis=synthesis,
        docs=docs,
        signals={"date_range": "2024"},
        budget_lines=[],
        run_meta={"analyzed_documents": analyzed, "corpus_tier": "moderate"},
    )


def test_the_header_and_the_coverage_block_agree() -> None:
    """The header read `len(docs)` while the corrected count lives on
    `coverage.total_documents`, so the two disagreed whenever the prefilter
    dropped anything."""
    report = _report(analyzed=90, corpus=94)
    header = report.split("\n")[2]

    assert "90 of 94 documents" in header
    assert "· 94 documents" not in header, "the header printed the raw corpus count"
    assert "Documents analyzed: 90 of 94 in corpus" in report


def test_the_header_stays_simple_when_nothing_was_filtered() -> None:
    report = _report(analyzed=94, corpus=94)
    header = report.split("\n")[2]

    assert "94 documents" in header
    assert " of " not in header


def test_a_report_with_no_synthesis_falls_back_to_the_corpus_count() -> None:
    docs = [doc(str(i), f"body {i}") for i in range(7)]
    report = render_report(
        handle="a",
        synthesis=None,
        docs=docs,
        signals={},
        budget_lines=[],
        run_meta={"synthesis_error": "boom"},
    )

    assert "7 documents" in report.split("\n")[2]
