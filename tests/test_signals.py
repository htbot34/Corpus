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
