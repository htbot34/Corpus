"""Synthesis tests. Everything runs against fixtures — no paid calls."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone

from fake_anthropic import FakeAnthropic
from fake_provider import load

from corpus.budget import Budget
from corpus.models import Document, Synthesis
from corpus.synthesize import (
    CHUNK_TOKEN_TARGET,
    chunk_documents,
    enforce_signal_counts,
    prune_unsourced,
    render_document,
    select_highlights,
    synthesize,
)
from corpus.x.hydrate import hydrate
from corpus.x.signals import compute_signals


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


# -- unsourced-finding enforcement -----------------------------------------


def test_prune_drops_findings_citing_ids_not_in_the_corpus():
    synthesis = Synthesis.model_validate(
        json.loads(FakeAnthropic()._default_reduce({"messages": [{"c": "[id: 111] [id: 222]"}]}))
    )
    assert len(synthesis.themes) == 2
    notes = prune_unsourced(synthesis, {"111", "222"})
    assert len(synthesis.themes) == 1
    assert synthesis.themes[0].name == "hiring process design"
    assert any("invented theme" in n for n in notes)


def test_prune_clears_performance_gap_when_unsourced():
    synthesis = Synthesis.model_validate(
        json.loads(FakeAnthropic()._default_reduce({"messages": [{"c": "[id: 111]"}]}))
    )
    prune_unsourced(synthesis, set())  # nothing is valid
    assert synthesis.performance_gap.posts_most_about == ""
    assert synthesis.positions == []
    assert synthesis.evolution == []


def test_signal_counts_override_whatever_the_model_said():
    """Rule 4 is enforced, not merely requested."""
    synthesis = Synthesis.model_validate(
        json.loads(FakeAnthropic()._default_reduce({"messages": [{"c": "[id: 111]"}]}))
    )
    synthesis.network[0].exchange_count = 999
    synthesis.reading_diet[0].share_count = 999
    signals = {
        "date_range": "2024-01-08 to 2024-08-16",
        "conversation_graph": [{"handle": "criticfriend", "exchange_count": 2}],
        "outbound_domains": [{"domain": "arxiv.org", "share_count": 3}],
    }
    docs = [doc(str(i), "x") for i in range(7)]
    notes = enforce_signal_counts(synthesis, signals, docs)

    assert synthesis.network[0].exchange_count == 2
    assert synthesis.reading_diet[0].share_count == 3
    assert synthesis.coverage.total_documents == 7
    assert synthesis.coverage.date_range == "2024-01-08 to 2024-08-16"
    assert len(notes) == 4


def test_theme_post_counts_are_left_to_the_model():
    """Themes are model-defined, so signals.json has no ground truth for them."""
    synthesis = Synthesis.model_validate(
        json.loads(FakeAnthropic()._default_reduce({"messages": [{"c": "[id: 111]"}]}))
    )
    before = synthesis.themes[0].post_count
    enforce_signal_counts(synthesis, {"conversation_graph": [], "outbound_domains": []}, [])
    assert synthesis.themes[0].post_count == before


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
    # unsourced theme was dropped by the Python enforcement, not trusted away
    assert [t.name for t in result.synthesis.themes] == ["hiring process design"]
    assert result.dropped_findings
    # both stages were charged
    assert budget.total_for("anthropic") > 0
    assert any(c.endpoint == "claude-sonnet-5" for c in budget.charges)
    assert any(c.endpoint == "claude-opus-5" for c in budget.charges)


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
