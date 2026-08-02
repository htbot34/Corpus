"""Hydration pipeline — the quality lever.

This is what separates the tool from a profile summarizer. Runs after
ingestion, before synthesis:

1. Thread stitching. Consecutive self-replies collapse into one Document.
   Threads are where people make actual arguments, so they must never fragment.
2. Reply parent hydration. Every reply gets its parent's verbatim text.
3. Quote target hydration. The quoted post is the subject of the commentary.
4. Media-only classification. Excluded from synthesis, counted in cadence.
5. Repost handling. Excluded by default; when kept they can only ever support
   amplification claims, never stated positions.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from ..models import Document, Thread
from .client import (
    TimestampParseError,
    XClient,
    _author_handle,
    _first,
    _tweet_id,
    normalize_tweet,
)
from .providers import BATCH_LOOKUP_MAX

UNAVAILABLE = "[unavailable]"


@dataclass
class HydrationStats:
    input_documents: int = 0
    threads_stitched: int = 0
    thread_parts_absorbed: int = 0
    replies_hydrated: int = 0
    quotes_hydrated: int = 0
    context_unavailable: int = 0
    quotes_inline: int = 0
    parents_fetched: int = 0
    media_only: int = 0
    timestamp_errors: int = 0  # documents dropped for an unparseable timestamp
    reposts_dropped: int = 0
    replies_dropped: int = 0
    output_documents: int = 0
    notes: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return dict(self.__dict__)


# --------------------------------------------------------------------------
# raw accessors that work for both plain docs and collapsed threads
# --------------------------------------------------------------------------


def _root_raw(doc: Document) -> dict[str, Any]:
    if doc.kind == "thread" and isinstance(doc.raw.get("root"), dict):
        root: dict[str, Any] = doc.raw["root"]
        return root
    return doc.raw


def reply_parent_id(doc: Document) -> str | None:
    raw = _root_raw(doc)
    value = _first(raw, "inReplyToId", "in_reply_to_status_id_str", "in_reply_to_id")
    return str(value) if value else None


def reply_parent_handle(doc: Document) -> str | None:
    raw = _root_raw(doc)
    value = _first(raw, "inReplyToUsername", "in_reply_to_screen_name", "inReplyToUserName")
    return str(value).lstrip("@") if value else None


def quoted_inline(doc: Document) -> dict[str, Any] | None:
    raw = _root_raw(doc)
    quoted = raw.get("quoted_tweet") or raw.get("quotedTweet")
    return quoted if isinstance(quoted, dict) else None


def quoted_id(doc: Document) -> str | None:
    raw = _root_raw(doc)
    inline = quoted_inline(doc)
    if inline:
        tid = _tweet_id(inline)
        if tid:
            return tid
    value = _first(raw, "quotedStatusId", "quoted_status_id_str", "quoted_status_id")
    return str(value) if value else None


# --------------------------------------------------------------------------
# 1. thread stitching
# --------------------------------------------------------------------------


def stitch_threads(
    docs: list[Document], author_handle: str
) -> tuple[list[Document], HydrationStats]:
    """Collapse runs of consecutive self-replies into single thread Documents."""
    stats = HydrationStats(input_documents=len(docs))
    author = author_handle.lstrip("@").lower()
    by_id = {d.source_id: d for d in docs}

    # child -> parent, but only when the parent is the same author and present.
    parent_of: dict[str, str] = {}
    children_of: dict[str, list[str]] = {}
    for doc in docs:
        if doc.kind != "reply":
            continue
        pid = reply_parent_id(doc)
        if not pid or pid not in by_id:
            continue
        parent = by_id[pid]
        if parent.author_handle.lower() != author or doc.author_handle.lower() != author:
            continue
        parent_of[doc.source_id] = pid
        children_of.setdefault(pid, []).append(doc.source_id)

    consumed: set[str] = set()
    out: list[Document] = []

    for doc in docs:
        if doc.source_id in consumed or doc.source_id in parent_of:
            # Either already absorbed, or it is a continuation whose root will
            # pull it in.
            continue
        chain = [doc]
        cursor = doc.source_id
        while True:
            kids = children_of.get(cursor) or []
            if not kids:
                break
            # Follow the earliest continuation; branches are separate replies.
            kids_docs = sorted((by_id[k] for k in kids), key=lambda d: d.published_at)
            nxt = kids_docs[0]
            chain.append(nxt)
            consumed.add(nxt.source_id)
            cursor = nxt.source_id

        if len(chain) > 1:
            thread = Thread(root_id=doc.source_id, parts=chain)
            out.append(thread.collapse())
            stats.threads_stitched += 1
            stats.thread_parts_absorbed += len(chain) - 1
        else:
            out.append(doc)

    out.sort(key=lambda d: d.published_at, reverse=True)
    return out, stats


# --------------------------------------------------------------------------
# 2 + 3. context hydration
# --------------------------------------------------------------------------


def hydrate_context(
    client: XClient,
    docs: list[Document],
    stats: HydrationStats,
    log: Callable[[str], None] = print,
) -> list[Document]:
    """Populate context/context_author/context_url on replies and quotes.

    Batched by the client at the provider's measured ceiling
    (BATCH_LOOKUP_MAX). A deleted or protected parent gets
    context "[unavailable]" and the document is kept — a reply we cannot
    contextualize is still evidence of cadence and register.
    """
    needed: list[str] = []
    inline_quotes: dict[str, dict[str, Any]] = {}

    for doc in docs:
        if doc.kind == "repost":
            continue
        if doc.kind in ("reply", "thread"):
            pid = reply_parent_id(doc)
            if pid:
                needed.append(pid)
        if doc.kind in ("quote", "thread", "reply"):
            inline = quoted_inline(doc)
            qid = quoted_id(doc)
            if inline and qid:
                inline_quotes[qid] = inline
            elif qid:
                needed.append(qid)

    to_fetch = [i for i in dict.fromkeys(needed) if i not in inline_quotes]
    fetched: dict[str, dict[str, Any]] = {}
    if to_fetch:
        log(f"  hydrating {len(to_fetch)} parent/quote targets in batches of {BATCH_LOOKUP_MAX}")
        fetched = client.tweets_by_ids(to_fetch)
        stats.parents_fetched = len(fetched)
    lookup: dict[str, dict[str, Any]] = {**inline_quotes, **fetched}
    stats.quotes_inline = len(inline_quotes)

    for doc in docs:
        if doc.kind == "repost":
            continue
        target_id: str | None = None
        is_quote = False
        if doc.kind in ("reply", "thread") and reply_parent_id(doc):
            target_id = reply_parent_id(doc)
        elif quoted_id(doc):
            target_id = quoted_id(doc)
            is_quote = True

        if not target_id:
            continue

        raw = lookup.get(target_id)
        if raw is None:
            doc.context = UNAVAILABLE
            doc.context_author = reply_parent_handle(doc) if not is_quote else None
            doc.context_url = None
            stats.context_unavailable += 1
        else:
            # A parent's timestamp is never used for pagination, cadence, or
            # ordering — only its body, author, and URL matter here. Falling
            # back to the child's date keeps the context rather than trading a
            # real quality loss for no correctness gain.
            parent = normalize_tweet(raw, timestamp_fallback=doc.published_at)
            doc.context = parent.body.strip() or UNAVAILABLE
            doc.context_author = parent.author_handle or _author_handle(raw) or None
            doc.context_url = parent.url
            if is_quote:
                stats.quotes_hydrated += 1
            else:
                stats.replies_hydrated += 1

        # A reply that also quotes something: the parent is what they are
        # answering, the quote is what they are pointing at. Keep both rather
        # than silently dropping half the context.
        if not is_quote:
            qid = quoted_id(doc)
            if qid and qid in lookup:
                q = normalize_tweet(lookup[qid], timestamp_fallback=doc.published_at)
                if q.body.strip():
                    doc.context = (
                        f"{doc.context}\n\n[they also quoted @{q.author_handle}]: {q.body.strip()}"
                    )

    return docs


# --------------------------------------------------------------------------
# 4 + 5. classification and filtering
# --------------------------------------------------------------------------


def apply_filters(
    docs: list[Document],
    stats: HydrationStats,
    *,
    include_reposts: bool = False,
    include_replies: bool = True,
) -> list[Document]:
    kept: list[Document] = []
    for doc in docs:
        if doc.kind == "repost" and not include_reposts:
            stats.reposts_dropped += 1
            continue
        if doc.kind == "reply" and not include_replies:
            stats.replies_dropped += 1
            continue
        if doc.kind == "media_only":
            stats.media_only += 1
        kept.append(doc)
    stats.output_documents = len(kept)
    return kept


def hydrate(
    client: XClient,
    raw_tweets: list[dict[str, Any]],
    author_handle: str,
    *,
    include_reposts: bool = False,
    include_replies: bool = True,
    log: Callable[[str], None] = print,
) -> tuple[list[Document], HydrationStats]:
    """Full pipeline: normalize -> stitch -> hydrate context -> classify -> filter."""
    # ingest_timeline already skips unparseable timestamps, but hydrate() also
    # runs over cached corpora written before that guard existed, and over
    # --offline input this process never fetched. One malformed document must
    # not cost the other 9,999.
    docs = []
    timestamp_errors = 0
    for raw in raw_tweets:
        try:
            docs.append(normalize_tweet(raw))
        except TimestampParseError as exc:
            timestamp_errors += 1
            log(f"  [timestamp] dropping {_tweet_id(raw) or '<no id>'}: {exc}")
    docs = [d for d in docs if d.source_id]

    stitched, stats = stitch_threads(docs, author_handle)
    stats.timestamp_errors = timestamp_errors
    if timestamp_errors:
        log(f"  {timestamp_errors} document(s) dropped: unparseable timestamp")
    log(
        f"  stitched {stats.threads_stitched} threads "
        f"({stats.thread_parts_absorbed} parts absorbed)"
    )

    hydrate_context(client, stitched, stats, log=log)
    log(
        f"  context: {stats.replies_hydrated} reply parents, "
        f"{stats.quotes_hydrated} quote targets, {stats.context_unavailable} unavailable"
    )

    kept = apply_filters(
        stitched, stats, include_reposts=include_reposts, include_replies=include_replies
    )
    log(
        f"  kept {len(kept)} documents "
        f"({stats.reposts_dropped} reposts dropped, {stats.media_only} media-only flagged)"
    )
    return kept, stats
