"""Hydration tests — thread stitching and context, the quality lever."""

from __future__ import annotations

from fake_provider import load

from corpus.x.client import normalize_tweet
from corpus.x.hydrate import UNAVAILABLE, hydrate, stitch_threads
from corpus.x.providers import BATCH_LOOKUP_MAX


def docs_from_fixture():
    return [normalize_tweet(t) for t in load("tweets.json")]


def test_normalize_kinds():
    by_id = {d.source_id: d for d in docs_from_fixture()}
    assert by_id["1700000000000000101"].kind == "original"
    assert by_id["1700000000000000102"].kind == "reply"
    assert by_id["1700000000000000130"].kind == "quote"
    assert by_id["1700000000000000140"].kind == "repost"
    assert by_id["1700000000000000150"].kind == "media_only"


def test_media_only_needs_short_text_and_media():
    by_id = {d.source_id: d for d in docs_from_fixture()}
    # "this" + a photo
    assert by_id["1700000000000000150"].kind == "media_only"
    # long text, no media
    assert by_id["1700000000000000111"].kind == "original"


def test_links_expanded_and_self_links_dropped():
    by_id = {d.source_id: d for d in docs_from_fixture()}
    doc = by_id["1700000000000000110"]
    assert "t.co" not in doc.body
    assert doc.outbound_links == ["https://www.nature.com/articles/s41586-024-00001"]


def test_thread_stitching_collapses_self_replies():
    docs, stats = stitch_threads(docs_from_fixture(), "testsubject")
    threads = [d for d in docs if d.kind == "thread"]
    assert len(threads) == 1
    thread = threads[0]
    assert thread.part_count == 4
    assert thread.source_id == "1700000000000000101"  # root identity kept
    assert thread.url.endswith("1700000000000000101")
    # all four bodies present, joined
    assert "red-black tree" in thread.body
    assert "attrition went from 22% to 6%" in thread.body
    assert thread.body.count("\n\n") >= 3
    # engagement from the root only
    assert thread.engagement["likes"] == 1840
    assert stats.threads_stitched == 1
    assert stats.thread_parts_absorbed == 3


def test_thread_parts_do_not_also_appear_standalone():
    docs, _ = stitch_threads(docs_from_fixture(), "testsubject")
    ids = {d.source_id for d in docs}
    for part in ("1700000000000000102", "1700000000000000103", "1700000000000000104"):
        assert part not in ids


def test_replies_to_others_are_not_stitched():
    docs, _ = stitch_threads(docs_from_fixture(), "testsubject")
    by_id = {d.source_id: d for d in docs}
    assert by_id["1700000000000000121"].kind == "reply"


def test_reply_parents_hydrated(client):
    docs, stats = hydrate(client, load("tweets.json"), "testsubject", log=lambda _: None)
    by_id = {d.source_id: d for d in docs}
    reply = by_id["1700000000000000121"]
    assert reply.context is not None
    assert "Hiring ahead of the curve" not in reply.context  # that is the subject's line
    assert "Founders should hire ahead of the curve" in reply.context
    assert reply.context_author == "vcpartner"
    assert reply.context_url.endswith("1700000000000000202")
    assert stats.replies_hydrated >= 3


def test_quote_targets_hydrated(client):
    docs, stats = hydrate(client, load("tweets.json"), "testsubject", log=lambda _: None)
    by_id = {d.source_id: d for d in docs}
    quote = by_id["1700000000000000130"]
    assert "mandating five days in office" in (quote.context or "")
    assert quote.context_author == "bigcoexec"


def test_deleted_parent_marked_unavailable_but_document_kept(client):
    docs, stats = hydrate(client, load("tweets.json"), "testsubject", log=lambda _: None)
    by_id = {d.source_id: d for d in docs}
    orphan = by_id["1700000000000000170"]
    assert orphan.context == UNAVAILABLE
    assert stats.context_unavailable == 1


def test_reposts_excluded_by_default_and_kept_on_request(client):
    docs, stats = hydrate(client, load("tweets.json"), "testsubject", log=lambda _: None)
    assert all(d.kind != "repost" for d in docs)
    assert stats.reposts_dropped == 1

    docs2, _ = hydrate(
        client, load("tweets.json"), "testsubject", include_reposts=True, log=lambda _: None
    )
    assert any(d.kind == "repost" for d in docs2)


def test_no_replies_flag(client):
    docs, stats = hydrate(
        client, load("tweets.json"), "testsubject", include_replies=False, log=lambda _: None
    )
    assert all(d.kind != "reply" for d in docs)
    assert stats.replies_dropped > 0


def test_hydration_batches_at_the_provider_ceiling(client):
    hydrate(client, load("tweets.json"), "testsubject", log=lambda _: None)
    for batch in client.provider.batch_calls:
        assert len(batch) <= BATCH_LOOKUP_MAX


def test_hydrated_parents_cached_permanently(client, cache):
    hydrate(client, load("tweets.json"), "testsubject", log=lambda _: None)
    first_batch = client.provider.batch_calls[0]
    assert len(first_batch) > 1

    # A second pass must re-fetch nothing that was found: old tweets do not
    # change, and re-paying for them is pure waste. The one id that does not
    # exist is the only thing worth asking about again.
    hydrate(client, load("tweets.json"), "testsubject", log=lambda _: None)
    second_batch = client.provider.batch_calls[1]
    assert second_batch == ["1700000000000000999"]
