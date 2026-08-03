"""What the report says about where its evidence came from.

Three claims a reader has to be able to make from the page alone:

* how much of this was matched rather than supplied,
* which findings rest only on matched evidence,
* and whether "no signal" on an axis is a fact about the subject or about a
  corpus that is 95% one source.
"""

from __future__ import annotations

from datetime import datetime, timezone

from corpus.models import CoreBelief, Coverage, Document, Synthesis
from corpus.render import render_report
from corpus.tiers import SINGLE_SOURCE_DOMINANCE, classify_sources


def doc(doc_id: str, *, source: str = "rss", attribution: str = "anchor") -> Document:
    return Document(
        source=source,
        source_id=doc_id,
        url=f"https://example.com/{doc_id}",
        author_handle="jane",
        published_at=datetime(2024, 1, 1, tzinfo=timezone.utc),
        kind="original",
        body="An argument, at length, about rubrics and why they beat interviews.",
        attribution=attribution,  # type: ignore[arg-type]
        attribution_basis="found by search (verified against the fetched page)",
    )


def report_for(
    docs: list[Document],
    *,
    synthesis: Synthesis | None = None,
    run_meta: dict | None = None,
) -> str:
    return render_report(
        handle="jane",
        synthesis=synthesis or Synthesis(summary="s", coverage=Coverage(total_documents=len(docs))),
        docs=docs,
        signals={"date_range": "2024"},
        budget_lines=[],
        run_meta=run_meta or {},
    )


# -- the attribution mix ----------------------------------------------------


def test_the_coverage_block_shows_the_attribution_mix() -> None:
    docs = [
        doc("1"),
        doc("2", attribution="linked"),
        doc("3", attribution="corroborated"),
        doc("4", attribution="corroborated"),
    ]
    report = report_for(docs)

    assert "Attribution: 1 anchor, 1 linked, 2 corroborated" in report
    assert "2 document(s) were matched rather than supplied" in report


def test_an_all_anchor_corpus_says_nothing_about_attribution() -> None:
    """A line reading "100% certain" on every report teaches the reader to
    skip the one report where it matters."""
    assert "Attribution:" not in report_for([doc("1"), doc("2")])


def test_a_finding_resting_only_on_corroborated_evidence_is_flagged_in_the_body() -> None:
    """Not just in the coverage block. The flag has to be next to the claim,
    because that is where the reader decides whether to believe it."""
    docs = [doc("1", attribution="corroborated"), doc("2", attribution="corroborated")]
    synthesis = Synthesis(
        summary="s",
        core_model=[CoreBelief(belief="Rubrics beat interviews", evidence_ids=["1", "2"])],
        coverage=Coverage(total_documents=2),
    )
    report = report_for(docs, synthesis=synthesis)

    assert "Rubrics beat interviews" in report
    assert "corroborated sources only — attribution is not certain" in report


def test_a_finding_with_one_anchor_citation_is_not_flagged() -> None:
    docs = [doc("1", attribution="anchor"), doc("2", attribution="corroborated")]
    synthesis = Synthesis(
        summary="s",
        core_model=[CoreBelief(belief="Rubrics beat interviews", evidence_ids=["1", "2"])],
        coverage=Coverage(total_documents=2),
    )

    assert "attribution is not certain" not in report_for(docs, synthesis=synthesis)


# -- what search did --------------------------------------------------------


def test_the_report_says_what_search_ran_and_what_it_held() -> None:
    """A run that searched and found nothing and a run that never searched
    look identical in a report listing only what it ingested."""
    report = report_for(
        [doc("1")],
        run_meta={
            "search": {
                "queries": [{"text": '"Jane Smith" "Acme Corp"', "why": "name and employer"}],
                "searches": 9,
                "cached_searches": 3,
                "results_seen": 41,
                "verified": 6,
                "candidates": [{"url": "https://a.example/1"}],
                "held": [{"url": "https://b.example/2"}, {"url": "https://c.example/3"}],
                "context": [{"url": "https://d.example/4"}],
            }
        },
    )

    assert "Search: 9 quer(y/ies) run (3 served from cache), 41 result(s)" in report
    assert "6 verified, 1 ingested, 2 held" in report
    assert "unconfirmed.md" in report
    assert "about* the subject" in report
    # Held candidates were fetched and scored; the scoring is what held them.
    # "could not be verified" would blame the fetch.
    assert "not confirmed as theirs" in report
    assert "could not be verified" not in report


def test_a_run_that_never_searched_says_nothing_about_search() -> None:
    assert "Search:" not in report_for([doc("1")], run_meta={"search": {"queries": []}})


def test_a_common_name_run_says_so_in_the_report() -> None:
    report = report_for(
        [doc("1")],
        run_meta={
            "search": {
                "queries": [{"text": "q", "why": "w"}],
                "common_name": True,
                "disambiguators": ["employer", "location"],
            }
        },
    )

    assert "too common to resolve by search" in report
    assert "`employer`" in report


# -- one source wearing several hats ----------------------------------------


def test_a_corpus_that_is_almost_all_one_source_says_so() -> None:
    docs = [doc(str(i), source="github") for i in range(19)] + [doc("x", source="rss")]
    report = report_for(docs)

    assert "95% of this corpus is a single source (github)" in report
    assert "technical reasoning under disagreement" in report
    assert "may be a fact about the corpus rather than about the subject" in report


def test_a_spread_corpus_says_nothing() -> None:
    docs = [doc(str(i), source="github") for i in range(5)]
    docs += [doc(f"r{i}", source="rss") for i in range(5)]
    report = report_for(docs)

    assert "single source" not in report


def test_the_dominance_threshold_is_where_it_says_it_is() -> None:
    assert SINGLE_SOURCE_DOMINANCE == 0.80

    at = classify_sources(["github"] * 80 + ["rss"] * 20)
    below = classify_sources(["github"] * 79 + ["rss"] * 21)

    assert at.is_concentrated
    assert not below.is_concentrated
    assert at.dominant == "github"


def test_an_empty_corpus_is_not_concentrated() -> None:
    mix = classify_sources([])
    assert not mix.is_concentrated
    assert mix.dominant == ""
