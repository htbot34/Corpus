"""The two-part report: a verdict page, then the evidence.

Part one is selection and compression, never new analysis — and the tests
here hold it to the two rules that keep a short report honest: a no-signal
axis appears at full weight, and every claim carries the count it rests on.
Plus the data-quality bound: confidence and "what moved" are capped by date
reliability, not only by document count.
"""

from __future__ import annotations

from datetime import datetime, timezone

from corpus.models import (
    Axis,
    CoreBelief,
    Coverage,
    Document,
    EvolutionEntry,
    Misreading,
    Synthesis,
)
from corpus.render import DATE_QUALITY_FLOOR, render_report


def doc(doc_id: str, *, date_unknown: bool = False) -> Document:
    return Document(
        source="rss",
        source_id=doc_id,
        url=f"https://blog.example.com/{doc_id}",
        author_handle="jane",
        published_at=datetime(1970, 1, 1, tzinfo=timezone.utc)
        if date_unknown
        else datetime(2025, 3, 1, tzinfo=timezone.utc),
        date_unknown=date_unknown,
        kind="original",
        body="An argument, at length, about rubrics and why they beat interviews.",
    )


def synthesis() -> Synthesis:
    return Synthesis(
        summary=(
            "They reason from mechanisms, not authorities. Claims get reduced to "
            "what would falsify them. Third sentence about their habits. A fourth "
            "sentence that part one must trim away."
        ),
        core_model=[
            CoreBelief(
                belief="Rubrics beat interviews",
                role="load_bearing",
                evidence_ids=["1", "2"],
            ),
            CoreBelief(belief="Evals are the hard part", role="derived", evidence_ids=["2"]),
        ],
        axes=[
            Axis(
                axis="epistemics",
                signal="strong",
                stated="They demand falsifiable claims. They distrust credentials.",
                inferred="They would reject most punditry.",
                reasoning="Stated repeatedly across years.",
                confidence="high",
                evidence_ids=["1", "2", "3"],
            ),
            Axis(axis="defense_intel_natsec", signal="none"),
            Axis(
                axis="technology_and_ai",
                signal="weak",
                stated="They treat AI tooling as leverage, not identity.",
                confidence="medium",
                evidence_ids=["2"],
            ),
        ],
        evolution=[
            EvolutionEntry(
                topic="Coding agents",
                earlier="Skeptical of autonomy.",
                later="Uses them daily.",
                evidence_ids=["1", "2"],
            )
        ],
        misreadings=[
            Misreading(misreading="Reading testing zeal as bureaucracy", evidence_ids=["1"]),
            Misreading(misreading="Reading brevity as dismissal", evidence_ids=["2"]),
        ],
        coverage=Coverage(
            date_range="2020-01-01 to 2025-03-01", total_documents=20, confidence="high"
        ),
    )


def render(docs: list[Document]) -> str:
    return render_report(
        handle="jane",
        synthesis=synthesis(),
        docs=docs,
        signals={"date_range": "2020-01-01 to 2025-03-01"},
        budget_lines=[],
        run_meta={"analyzed_documents": 100, "corpus_tier": "moderate"},
    )


def dated_corpus(unknown: int = 0, total: int = 20) -> list[Document]:
    return [doc(str(i), date_unknown=i < unknown) for i in range(total)]


# -- the shape --------------------------------------------------------------


def test_the_verdict_page_comes_before_the_evidence() -> None:
    report = render(dated_corpus())
    assert report.index("## Where they land") < report.index("# The evidence")
    assert report.index("# The evidence") < report.index("## Summary")
    assert report.index("## Summary") < report.index("## The axes in full")


def test_part_one_leads_with_the_named_axis_order() -> None:
    report = render(dated_corpus())
    page_one = report.split("# The evidence")[0]
    assert (
        page_one.index("**technology and ai**")
        < page_one.index("**defense intel natsec**")
        < page_one.index("**epistemics**")
    )


def test_part_one_trims_the_lead_to_three_sentences() -> None:
    page_one = render(dated_corpus()).split("# The evidence")[0]
    assert "They reason from mechanisms" in page_one
    assert "must trim away" not in page_one


def test_a_no_signal_axis_appears_in_part_one_at_full_weight() -> None:
    """ "Nothing in this corpus locates them on this axis" is a finding, and
    it is the thing that keeps a short report honest. Same bold label shape
    as an axis with a verdict — never dropped, never softened."""
    page_one = render(dated_corpus()).split("# The evidence")[0]
    assert "**defense intel natsec** — no signal, 0 cited documents." in page_one
    assert "Nothing in this corpus locates them on this axis." in page_one


def test_every_part_one_claim_carries_a_document_count() -> None:
    page_one = render(dated_corpus()).split("# The evidence")[0]
    assert "strong signal, 3 cited document(s)" in page_one
    assert "Rubrics beat interviews (2 cited document(s))" in page_one
    assert "Reading testing zeal as bureaucracy (1 cited document(s))" in page_one


def test_part_one_links_into_part_two_by_section() -> None:
    page_one = render(dated_corpus()).split("# The evidence")[0]
    assert "(#the-axes-in-full)" in page_one
    assert "(#the-generating-model)" in page_one
    assert "(#how-to-misread-this)" in page_one


def test_part_one_confidence_line_names_tier_count_range_and_caveat() -> None:
    page_one = render(dated_corpus()).split("# The evidence")[0]
    assert "**Confidence:**" in page_one
    assert "moderate corpus" in page_one
    assert "20 documents" in page_one
    assert "2020-01-01 to 2025-03-01" in page_one
    assert "Most limiting:" in page_one


def test_part_two_keeps_everything() -> None:
    report = render(dated_corpus())
    evidence = report.split("# The evidence")[1]
    for section in (
        "## Summary",
        "## The generating model",
        "## How they reason",
        "## The axes in full",
        "## What moved",
        "## Unresolved",
        "## How to misread this",
        "## Computed signals (Python, not the model)",
        "## Spend",
    ):
        assert section in evidence, f"{section} was lost from the evidence half"


# -- confidence is capped by data quality -----------------------------------


def test_unreliable_dates_suppress_what_moved_with_a_stated_reason() -> None:
    report = render(dated_corpus(unknown=5))  # 25% > the 10% floor
    assert "## What moved" in report
    assert "_Suppressed: 5 of 20 documents (25%)" in report
    assert "Coding agents" not in report.split("## What moved")[1].split("## Unresolved")[0]


def test_unreliable_dates_cap_the_confidence_label() -> None:
    report = render(dated_corpus(unknown=5))
    assert "Confidence: **medium** — the model assessed high, capped in the render" in report
    assert "Model-assessed confidence: **high**" not in report


def test_part_one_confidence_line_says_when_dates_are_the_limit() -> None:
    page_one = render(dated_corpus(unknown=5)).split("# The evidence")[0]
    assert "carry no reliable publication date" in page_one
    assert '"what moved" is suppressed' in page_one


def test_reliable_dates_change_nothing() -> None:
    report = render(dated_corpus(unknown=1))  # 5% <= the 10% floor
    assert "Model-assessed confidence: **high**" in report
    assert "### Coding agents" in report
    assert "_Suppressed:" not in report
    # The one undated document is still disclosed in the coverage block.
    assert "1 of 20 documents (5%) carry no reliable publication date." in report


def test_the_floor_is_strictly_more_than_ten_percent() -> None:
    report = render(dated_corpus(unknown=2, total=20))  # exactly 10%
    assert DATE_QUALITY_FLOOR == 0.10
    assert "### Coding agents" in report, "10% is the floor, not past it"


def test_computed_signals_render_honest_cadence_and_kind_labels() -> None:
    report = render_report(
        handle="jane",
        synthesis=synthesis(),
        docs=dated_corpus(),
        signals={
            "date_range": "2020-01-01 to 2025-03-01",
            "cadence": {"omitted": "no X timeline in this corpus; posting cadence ..."},
            "kind_mix": {
                "by_source": {
                    "github": {"shares": {"reply": 0.56, "original": 0.44}},
                    "rss": {"shares": {"original": 1.0}},
                }
            },
        },
        budget_lines=[],
        run_meta={"analyzed_documents": 100, "corpus_tier": "moderate"},
    )
    assert "- Cadence: not computed — no X timeline" in report
    assert "Kind mix, per source — github: reply 56%, original 44%; rss: original 100%" in report
    assert "posts/month mean" not in report
