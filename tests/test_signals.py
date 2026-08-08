"""Signals are pure functions over the corpus. Zero API calls, fully checked."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fake_provider import load

from corpus.models import Document
from corpus.x.hydrate import hydrate
from corpus.x.signals import (
    cadence,
    compute_signals,
    conversation_graph,
    engagement_baselines,
    kind_mix,
    outbound_domains,
    register_split,
    vocabulary_drift,
)


def doc(
    doc_id: str,
    when: datetime,
    body: str,
    kind: str = "original",
    likes: int = 10,
    links: list[str] | None = None,
    context_author: str | None = None,
) -> Document:
    return Document(
        source="x",
        source_id=doc_id,
        url=f"https://x.com/a/status/{doc_id}",
        author_handle="a",
        published_at=when,
        kind=kind,  # type: ignore[arg-type]
        body=body,
        context_author=context_author,
        engagement={"likes": likes, "replies": 1, "reposts": 2, "views": 100},
        outbound_links=links or [],
    )


BASE = datetime(2024, 1, 1, tzinfo=timezone.utc)


def test_cadence_counts_and_fills_silent_months():
    docs = [
        doc("1", BASE, "a"),
        doc("2", BASE + timedelta(days=1), "b"),
        doc("3", BASE + timedelta(days=90), "c"),
    ]
    result = cadence(docs)
    # Jan has 2, Feb is silent, Mar 31 has 1.
    assert result["posts_per_month"]["2024-01"] == 2
    assert result["posts_per_month"]["2024-02"] == 0  # silence is a signal
    assert result["posts_per_month"]["2024-03"] == 1
    assert result["silent_months"] == 1
    assert result["active_months"] == 2


def test_cadence_finds_hiatuses_of_14_plus_days():
    docs = [
        doc("1", BASE, "a"),
        doc("2", BASE + timedelta(days=3), "b"),
        doc("3", BASE + timedelta(days=40), "c"),
    ]
    result = cadence(docs)
    assert len(result["hiatuses"]) == 1
    assert result["hiatuses"][0]["days"] == 37


def test_kind_mix_shares_sum_to_one():
    docs = [
        doc("1", BASE, "a"),
        doc("2", BASE, "b", kind="reply"),
        doc("3", BASE, "c", kind="reply"),
    ]
    mix = kind_mix(docs)
    assert mix["counts"] == {"original": 1, "reply": 2}
    assert abs(sum(mix["shares"].values()) - 1.0) < 1e-9


def test_conversation_graph_ranks_by_count_and_excludes_self():
    docs = [
        doc("1", BASE, "x", kind="reply", context_author="bob"),
        doc("2", BASE, "y", kind="reply", context_author="bob"),
        doc("3", BASE, "z", kind="reply", context_author="carol"),
        doc("4", BASE, "w", kind="reply", context_author="a"),  # self
    ]
    graph = conversation_graph(docs)
    assert [g["handle"] for g in graph] == ["bob", "carol"]
    assert graph[0]["exchange_count"] == 2


def test_outbound_domains_normalizes_www():
    docs = [
        doc("1", BASE, "x", links=["https://www.nature.com/a"]),
        doc("2", BASE, "y", links=["https://nature.com/b"]),
        doc("3", BASE, "z", links=["https://arxiv.org/abs/1"]),
    ]
    domains = outbound_domains(docs)
    assert domains[0] == {"domain": "nature.com", "share_count": 2, "example_ids": ["1", "2"]}


def test_engagement_baselines_are_per_kind_medians():
    docs = [
        doc("1", BASE, "x", likes=10),
        doc("2", BASE, "y", likes=20),
        doc("3", BASE, "z", likes=30),
        doc("4", BASE, "r", kind="reply", likes=1),
    ]
    base = engagement_baselines(docs)
    assert base["by_kind"]["original"]["median_likes"] == 20
    assert base["by_kind"]["reply"]["median_likes"] == 1


def test_engagement_outliers_are_relative_to_own_median():
    docs = [doc(str(i), BASE, "x", likes=10) for i in range(10)]
    docs.append(doc("99", BASE, "viral", likes=900))
    base = engagement_baselines(docs)
    ids = [o["id"] for o in base["outliers"]]
    assert ids == ["99"]
    assert base["outliers"][0]["multiple_of_median"] == 90.0


def test_register_split_separates_originals_from_replies():
    docs = [
        doc("1", BASE, "one two three four five six seven eight nine ten."),
        doc("2", BASE, "short.", kind="reply"),
    ]
    reg = register_split(docs)
    assert reg["original"]["mean_word_count"] == 10
    assert reg["reply"]["mean_word_count"] == 1


def test_register_split_also_breaks_out_sources():
    """The cross-platform tell: same person, two registers, visible in arithmetic."""
    docs = [
        doc("1", BASE, "one two three four five six seven eight nine ten."),
        Document(
            source="hn",
            source_id="hn-1",
            url="https://news.ycombinator.com/item?id=1",
            author_handle="a",
            published_at=BASE,
            kind="reply",
            body=" ".join(["word"] * 90) + ".",
        ),
    ]
    reg = register_split(docs)
    assert reg["by_source"]["x"]["mean_word_count"] == 10
    assert reg["by_source"]["hn"]["mean_word_count"] == 90
    # The by-kind view is unchanged by the addition.
    assert reg["original"]["mean_word_count"] == 10


def test_vocabulary_drift_surfaces_time_localized_terms():
    early = [
        doc(f"e{i}", BASE, "hiring rubric interview candidate hiring rubric process")
        for i in range(12)
    ]
    late = [
        doc(f"l{i}", BASE + timedelta(days=300), "evaluation harness dataset evaluation model")
        for i in range(12)
    ]
    drift = vocabulary_drift(early + late)
    assert len(drift) == 2
    first_terms = {t["term"] for t in drift[0]["terms"]}
    last_terms = {t["term"] for t in drift[-1]["terms"]}
    assert "hiring" in first_terms
    assert "evaluation" in last_terms
    assert "hiring" not in last_terms


def test_vocabulary_drift_needs_two_buckets():
    docs = [doc(str(i), BASE, "one two three four five six") for i in range(20)]
    assert vocabulary_drift(docs) == []


def test_compute_signals_over_real_fixture_corpus(client):
    docs, _ = hydrate(client, load("tweets.json"), "testsubject", log=lambda _: None)
    signals = compute_signals(docs)
    assert signals["author_handle"] == "testsubject"
    assert signals["total_documents"] == len(docs)
    assert signals["synthesizable_documents"] < signals["total_documents"]  # media_only excluded
    assert signals["cadence"]["hiatuses"]
    assert any(g["handle"] == "criticfriend" for g in signals["conversation_graph"])
    assert any(d["domain"] == "arxiv.org" for d in signals["outbound_domains"])
    assert signals["register_split"]["thread"]["mean_word_count"] > 0
    # signals.json must be JSON-serializable — it is injected into the prompt
    import json

    json.dumps(signals, default=str)


# -- vocabulary is words a person said, not fragments code left behind -------


def _wordy(body: str) -> str:
    """Enough filler that a bucket clears the 30-token floor."""
    filler = (
        "rubrics evaluation shipping software careful writing reading "
        "thinking building testing designing reviewing hiring teaching "
    )
    return f"{body} {filler * 3}"


def test_vocabulary_drift_drops_markup_and_implausible_tokens():
    """A live run's drift came back "swh, snp, ecd, bcc, abf, def" with
    "href" — hex shrapnel, vowelless fragments, and markup reaching the
    tokenizer. None of it is vocabulary."""
    garbage = '<a href="https://x.example/y">link</a> swh snp bcc ecd abf def &nbsp;'
    early = [
        doc(str(i), BASE + timedelta(days=i), _wordy(f"quantum computing {garbage}"))
        for i in range(5)
    ]
    late = [
        doc(str(100 + i), BASE + timedelta(days=400 + i), _wordy(f"neural networks {garbage}"))
        for i in range(5)
    ]
    drift = vocabulary_drift(early + late)
    seen = {t["term"] for bucket in drift for t in bucket["terms"]}
    for fragment in ("href", "swh", "snp", "bcc", "ecd", "abf", "def", "nbsp"):
        assert fragment not in seen, f"{fragment!r} is not a word the subject said"
    assert "quantum" in seen or "neural" in seen, "real vocabulary must survive the filters"


def test_vocabulary_drift_excludes_the_subjects_own_name():
    """Page boilerplate repeats the subject's name; a name is not vocabulary."""
    early = [
        doc(str(i), BASE + timedelta(days=i), _wordy("simon willison writes about quantum"))
        for i in range(5)
    ]
    late = [
        doc(str(100 + i), BASE + timedelta(days=400 + i), _wordy("simon willison ships neural"))
        for i in range(5)
    ]
    signals = compute_signals(early + late, subject_terms=["Simon Willison", "simonw"])
    seen = {t["term"] for bucket in signals["vocabulary_drift"] for t in bucket["terms"]}
    assert "simon" not in seen and "willison" not in seen


# -- cadence is a timeline concept -------------------------------------------


def _web_doc(doc_id: str, when: datetime, body: str, kind: str = "original") -> Document:
    d = doc(doc_id, when, body, kind=kind)
    return d.model_copy(update={"source": "web", "url": f"https://blog.example.com/{doc_id}"})


def test_cadence_is_omitted_with_a_reason_when_there_is_no_x_timeline():
    """ "2.82 posts/month mean, 0 median" on a 205-document corpus with no X
    data measured what the fetcher found, not how often the subject writes."""
    docs = [
        _web_doc(str(i), BASE + timedelta(days=30 * i), "An essay about rubrics at length.")
        for i in range(6)
    ]
    signals = compute_signals(docs)
    assert "omitted" in signals["cadence"]
    assert "no X timeline" in signals["cadence"]["omitted"]
    assert "mean_posts_per_month" not in signals["cadence"]


def test_cadence_still_computes_over_an_x_timeline():
    docs = [doc(str(i), BASE + timedelta(days=7 * i), "A post about rubrics.") for i in range(10)]
    signals = compute_signals(docs)
    assert signals["cadence"]["mean_posts_per_month"] > 0


def test_a_mixed_corpus_computes_cadence_over_the_x_timeline_only():
    x_docs = [doc(str(i), BASE + timedelta(days=7 * i), "A post.") for i in range(8)]
    web = [_web_doc(f"w{i}", BASE - timedelta(days=900 + i), "An old page.") for i in range(3)]
    signals = compute_signals(x_docs + web)
    # The web documents predate the timeline by years; if they leaked into
    # cadence the hiatus list would open with a ~900-day gap.
    assert all(h["days"] < 400 for h in signals["cadence"]["hiatuses"])


def test_kind_mix_is_broken_out_per_source():
    """A GitHub "reply" is a review comment, not a timeline conversation.
    The mix carries per-source shares so the report can label them."""
    docs = [
        _web_doc("w1", BASE, "An essay."),
        doc("x1", BASE, "A reply.", kind="reply"),
        doc("x2", BASE, "A post."),
    ]
    mix = kind_mix(docs)
    assert mix["by_source"]["web"]["shares"] == {"original": 1.0}
    assert mix["by_source"]["x"]["counts"] == {"reply": 1, "original": 1}
    # Overall keys survive for consumers of older signals.json shapes.
    assert mix["counts"]["original"] == 2 and mix["total"] == 3
