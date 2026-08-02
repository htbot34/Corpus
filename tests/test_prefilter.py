"""The low-signal filter. Structural only — never by subject."""

from __future__ import annotations

from datetime import datetime, timezone

from corpus.models import Document
from corpus.prefilter import classify, filter_low_signal


def doc(body: str, **kw) -> Document:
    return Document(
        source="x",
        source_id=kw.pop("source_id", "1"),
        url="https://x.com/a/status/1",
        author_handle="a",
        published_at=datetime(2024, 1, 1, tzinfo=timezone.utc),
        kind=kw.pop("kind", "original"),
        body=body,
        **kw,
    )


# -- what gets dropped ------------------------------------------------------


def test_bare_acknowledgement_is_dropped():
    assert classify(doc("Thanks!", kind="reply", context="here you go")) == "acknowledgement"
    assert classify(doc("agreed", kind="reply", context="a claim")) == "acknowledgement"


def test_link_only_post_with_no_commentary_is_dropped():
    d = doc("https://example.com/paper", outbound_links=["https://example.com/paper"])
    assert classify(d) == "link_only"


def test_short_standalone_fragment_is_dropped():
    assert classify(doc("soon")) == "too_short_no_context"


def test_media_only_style_empty_body_is_dropped():
    assert classify(doc("@someone")) == "empty"


# -- what survives, which matters more --------------------------------------


def test_a_short_reply_with_context_is_kept():
    """'Completely backwards.' is two words and the most revealing document in
    the corpus, because the parent is attached."""
    d = doc("Completely backwards.", kind="reply", context="Hire ahead of the curve.")
    assert classify(d) is None


def test_link_with_real_commentary_is_kept():
    d = doc(
        "This is the study everyone cites and nobody reads https://example.com/p",
        outbound_links=["https://example.com/p"],
    )
    assert classify(d) is None


def test_threads_are_never_dropped():
    d = doc("a", kind="thread")
    d.part_count = 3
    assert classify(d) is None


def test_hobby_minutiae_survive():
    """Topic filtering would delete the evidence that they treat everything
    with the same analytic seriousness. That pattern is the finding."""
    d = doc(
        "The correct grind size for a moka pot is finer than drip but coarser "
        "than espresso, and everyone gets this wrong because they extrapolate "
        "from pressure rather than from contact time."
    )
    assert classify(d) is None


def test_an_ack_shaped_word_inside_a_real_sentence_is_kept():
    assert classify(doc("True, but only if the sample is randomized properly.")) is None


# -- stats ------------------------------------------------------------------


def test_filter_reports_what_it_dropped_and_why():
    docs = [
        doc("Thanks!", source_id="1", kind="reply", context="x"),
        doc("soon", source_id="2"),
        doc("A real argument about hiring that runs several words long", source_id="3"),
    ]
    kept, stats = filter_low_signal(docs)
    assert [d.source_id for d in kept] == ["3"]
    assert stats.dropped == 2
    assert stats.by_reason == {"acknowledgement": 1, "too_short_no_context": 1}
    assert stats.as_dict()["input_documents"] == 3
    assert "kept" in stats.summary_line()


def test_empty_corpus_is_not_a_crash():
    kept, stats = filter_low_signal([])
    assert kept == []
    assert stats.summary_line() == "0 documents, none filtered"
