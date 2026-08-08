"""Bluesky as a corpus source.

The closest thing to X coverage that still works: full post and reply history
over a public, unauthenticated API. `app.bsky.feed.getAuthorFeed` on the
public AppView (`public.api.bsky.app`) needs no key and no session — a
personal research tool reading what someone published under their own name,
which is the only scope this package has.

Two properties of the wire shape shape the design:

**Reply context arrives inline.** Each feed item for a reply carries
`reply.parent` (and `reply.root`) as a post view with the parent's verbatim
text and author. That is the hydration the X pipeline pays a metered API for,
free — so every reply keeps its parent, and a deleted or blocked parent
becomes `[unavailable]` rather than vanishing with the document.

**Reposts are labelled, not silent.** A feed item with a `reason` field is a
repost of someone else's post; the `post` on it is the *original author's*
writing. They are skipped and counted, the same rule the X pipeline applies:
reposts only ever support amplification claims, and this adapter makes no
claims.

Consecutive self-replies are stitched into one thread `Document` — Bluesky
threads are where people make actual arguments, same as X.

Fixture provenance: `tests/fixtures/bluesky_*.json` are SYNTHETIC — written
to the documented `app.bsky.feed.getAuthorFeed` shape (the repo's offline
rule means nothing here has been captured live). `bluesky_contract`-style
drift detection would need a live capture first; the adapter reads defensively
(`.get` everywhere, isinstance checks) so a shape change degrades to fewer
documents with notes, never to wrong attribution.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from ..cache import Cache
from ..models import DATE_UNKNOWN, Document
from .base import JsonReader, collapse_self_threads, parse_date

API = "https://public.api.bsky.app/xrpc"
APPVIEW = "https://bsky.app"

PER_PAGE = 100  # the documented maximum for getAuthorFeed
MAX_PAGES = 10  # 1,000 posts back; each page is one request


@dataclass
class BlueskyStats:
    """What was read and what was skipped, so an absence is never silent."""

    pages: int = 0
    posts: int = 0
    replies: int = 0
    threads_stitched: int = 0
    reposts_skipped: int = 0
    unavailable_parents: int = 0
    earliest: str = ""
    latest: str = ""
    notes: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "pages": self.pages,
            "posts": self.posts,
            "replies": self.replies,
            "threads_stitched": self.threads_stitched,
            "reposts_skipped": self.reposts_skipped,
            "unavailable_parents": self.unavailable_parents,
            "date_range": f"{self.earliest} to {self.latest}" if self.earliest else "",
            "notes": list(self.notes),
        }

    def summary_line(self) -> str:
        return (
            f"{self.posts} post(s), {self.replies} repl(ies), "
            f"{self.threads_stitched} thread(s) stitched, "
            f"{self.reposts_skipped} repost(s) skipped"
        )


def _handle_of(target: str) -> str:
    """`bsky.app/profile/x.bsky.social`, `@x.bsky.social`, or the bare handle."""
    handle = target.strip().rstrip("/")
    for prefix in ("https://bsky.app/profile/", "http://bsky.app/profile/"):
        if handle.startswith(prefix):
            handle = handle[len(prefix) :]
    return handle.lstrip("@").rsplit("/", 1)[-1].lower()


def _post_url(handle: str, uri: str) -> str:
    rkey = uri.rsplit("/", 1)[-1]
    return f"{APPVIEW}/profile/{handle}/post/{rkey}" if rkey else ""


def _links_from_facets(record: dict[str, Any]) -> list[str]:
    links: list[str] = []
    for facet in record.get("facets") or []:
        if not isinstance(facet, dict):
            continue
        for feature in facet.get("features") or []:
            if isinstance(feature, dict) and str(feature.get("$type", "")).endswith("#link"):
                uri = feature.get("uri")
                if isinstance(uri, str) and uri.startswith("http") and uri not in links:
                    links.append(uri)
    return links


def _context_from(
    item: dict[str, Any], stats: BlueskyStats
) -> tuple[str | None, str | None, str | None]:
    """(parent text, parent author, parent url) from the inline reply view.

    A blocked or deleted parent arrives as a `#blockedPost`/`#notFoundPost`
    view with no record — the same case the X hydration pipeline renders as
    `[unavailable]`, and the same handling: the document is kept.
    """
    reply = item.get("reply")
    if not isinstance(reply, dict):
        return None, None, None
    parent = reply.get("parent")
    if not isinstance(parent, dict):
        return None, None, None
    record = parent.get("record")
    raw_author = parent.get("author")
    author: dict[str, Any] = raw_author if isinstance(raw_author, dict) else {}
    author_handle = str(author.get("handle") or "")
    parent_uri = str(parent.get("uri") or "")
    parent_url = _post_url(author_handle, parent_uri) if author_handle and parent_uri else None
    if not isinstance(record, dict) or not str(record.get("text") or "").strip():
        stats.unavailable_parents += 1
        return "[unavailable]", author_handle or None, parent_url
    return str(record.get("text") or "").strip(), author_handle or None, parent_url


class BlueskySource:
    """Reads one Bluesky account's public posts and replies. `target` is the handle."""

    name = "bluesky"

    def fetch(
        self,
        target: str,
        *,
        author_handle: str,
        cache: Cache,
        limit: int = 200,
        log: Callable[[str], None] = print,
    ) -> list[Document]:
        docs, _ = self.fetch_with_stats(
            target, author_handle=author_handle, cache=cache, limit=limit, log=log
        )
        return docs

    def fetch_with_stats(
        self,
        target: str,
        *,
        author_handle: str,
        cache: Cache,
        limit: int = 200,
        log: Callable[[str], None] = print,
    ) -> tuple[list[Document], BlueskyStats]:
        handle = _handle_of(target)
        stats = BlueskyStats()
        reader = JsonReader("bluesky", cache, log)
        docs: list[Document] = []
        parent_ids: dict[str, str | None] = {}
        try:
            cursor: str | None = None
            for _ in range(MAX_PAGES):
                url = (
                    f"{API}/app.bsky.feed.getAuthorFeed?actor={handle}"
                    f"&limit={PER_PAGE}&filter=posts_with_replies"
                )
                if cursor:
                    url += f"&cursor={cursor}"
                payload = reader.get(url)
                if not isinstance(payload, dict):
                    break
                feed = payload.get("feed")
                if not isinstance(feed, list) or not feed:
                    break
                stats.pages += 1
                for item in feed:
                    if isinstance(item, dict):
                        doc = self._from_item(item, handle, author_handle, stats, parent_ids)
                        if doc is not None:
                            docs.append(doc)
                cursor = payload.get("cursor")
                if not cursor or len(feed) < PER_PAGE:
                    break
        finally:
            reader.close()

        before = len(docs)
        docs = collapse_self_threads(docs, parent_ids)
        stats.threads_stitched = sum(1 for d in docs if d.part_count > 1)
        if stats.threads_stitched:
            log(f"  bluesky: stitched {before - len(docs)} self-repl(ies) into threads")

        docs.sort(key=lambda d: d.published_at, reverse=True)
        docs = docs[:limit]

        dated = [d for d in docs if not d.date_unknown]
        if dated:
            stats.earliest = min(d.published_at for d in dated).strftime("%Y-%m-%d")
            stats.latest = max(d.published_at for d in dated).strftime("%Y-%m-%d")
        stats.notes.extend(reader.errors[:5])
        log(f"  bluesky: {stats.summary_line()}")
        for note in stats.notes:
            log(f"    {note}")
        return docs, stats

    def _from_item(
        self,
        item: dict[str, Any],
        handle: str,
        author_handle: str,
        stats: BlueskyStats,
        parent_ids: dict[str, str | None],
    ) -> Document | None:
        if isinstance(item.get("reason"), dict):
            # A repost: the post on this item is someone else's writing.
            stats.reposts_skipped += 1
            return None
        post = item.get("post")
        if not isinstance(post, dict):
            return None
        author = post.get("author")
        # The author feed only returns their posts; the check costs one
        # comparison and the failure it prevents is wrong attribution.
        if not isinstance(author, dict) or str(author.get("handle", "")).lower() != handle:
            return None
        record = post.get("record")
        if not isinstance(record, dict):
            return None
        text = str(record.get("text") or "").strip()
        uri = str(post.get("uri") or "")
        if not text or not uri:
            return None

        reply_ref = record.get("reply")
        parent_ref = reply_ref.get("parent") if isinstance(reply_ref, dict) else None
        context, context_author, context_url = _context_from(item, stats)
        if isinstance(parent_ref, dict):
            stats.replies += 1
            is_reply = True
            parent_ids[uri] = str(parent_ref.get("uri") or "") or None
        else:
            stats.posts += 1
            is_reply = False
            parent_ids[uri] = None

        when = parse_date(record.get("createdAt"))
        return Document(
            source="bluesky",
            source_id=uri,
            url=_post_url(handle, uri),
            author_handle=author_handle,
            published_at=when if when is not None else DATE_UNKNOWN,
            date_unknown=when is None,
            kind="reply" if is_reply else "original",
            body=text,
            context=context,
            context_author=context_author,
            context_url=context_url,
            engagement={
                key: int(post.get(field) or 0)
                for key, field in (
                    ("likes", "likeCount"),
                    ("replies", "replyCount"),
                    ("reposts", "repostCount"),
                )
            },
            outbound_links=_links_from_facets(record),
            part_count=1,
            raw={"kind": "reply" if is_reply else "post", "cid": str(post.get("cid") or "")},
        )
