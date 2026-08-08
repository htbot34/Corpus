"""Reddit as a corpus source.

Public profile listings over the keyless JSON API: `/user/{u}/comments.json`
and `/user/{u}/submitted.json` need no credentials and no session — what the
person published under their own name, which is the only scope this package
has.

Two properties of the wire shape shape the design:

**A comment's context is its submission title.** The listing carries
`link_title` inline, so every reply keeps enough context to be legible with
no extra requests. Parent *comment* text is not hydrated — unlike Bluesky it
is not inline, and unlike HN each hydration would be a live request against a
site that rate-limits anonymous readers aggressively. The comment keeps the
submission title and its own permalink; deep-thread nuance is the known gap,
stated in the stats rather than hidden.

**Timestamps are epoch seconds, not ISO strings.** `created_utc` is a float;
it is converted here rather than handed to a string parser.

Reddit answers some anonymous reads with a 403 or an HTML error page. Both
degrade to a note and a thinner corpus, like every other source — the run is
never the price of a platform's mood.

Fixture provenance: `tests/fixtures/reddit_*.json` are SYNTHETIC, written to
the documented listing shape (the offline rule means nothing is captured
live). The adapter reads defensively so a shape change degrades to fewer
documents with notes, never to wrong attribution.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from ..cache import Cache
from ..models import DATE_UNKNOWN, Document
from .base import JsonReader

SITE = "https://www.reddit.com"

PER_PAGE = 100  # the documented listing limit ceiling
MAX_COMMENT_PAGES = 5  # 500 comments back
MAX_SUBMISSION_PAGES = 3  # 300 submissions back


@dataclass
class RedditStats:
    """What was read and what was skipped, so an absence is never silent."""

    comment_pages: int = 0
    submission_pages: int = 0
    comments: int = 0
    submissions: int = 0
    earliest: str = ""
    latest: str = ""
    notes: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "comment_pages": self.comment_pages,
            "submission_pages": self.submission_pages,
            "comments": self.comments,
            "submissions": self.submissions,
            "date_range": f"{self.earliest} to {self.latest}" if self.earliest else "",
            "notes": list(self.notes),
        }

    def summary_line(self) -> str:
        return f"{self.comments} comment(s), {self.submissions} submission(s)"


def _username_of(target: str) -> str:
    """`reddit.com/user/x`, `reddit.com/u/x`, `u/x`, or the bare username."""
    value = target.strip().rstrip("/")
    for marker in ("/user/", "/u/"):
        if marker in value:
            value = value.split(marker, 1)[-1]
    return value.removeprefix("u/").split("/", 1)[0]


def _when(data: dict[str, Any]) -> datetime | None:
    raw = data.get("created_utc")
    if raw is None:
        return None
    try:
        return datetime.fromtimestamp(float(raw), tz=timezone.utc)
    except (TypeError, ValueError, OverflowError):
        return None


def _permalink_url(data: dict[str, Any]) -> str:
    permalink = str(data.get("permalink") or "")
    return f"{SITE}{permalink}" if permalink.startswith("/") else permalink


class RedditSource:
    """Reads one Reddit account's public comments and submissions."""

    name = "reddit"

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
    ) -> tuple[list[Document], RedditStats]:
        username = _username_of(target)
        stats = RedditStats()
        reader = JsonReader("reddit", cache, log)
        docs: list[Document] = []
        try:
            docs.extend(self._listing(username, author_handle, reader, stats, kind="comments"))
            docs.extend(self._listing(username, author_handle, reader, stats, kind="submitted"))
        finally:
            reader.close()

        docs.sort(key=lambda d: d.published_at, reverse=True)
        docs = docs[:limit]

        dated = [d for d in docs if not d.date_unknown]
        if dated:
            stats.earliest = min(d.published_at for d in dated).strftime("%Y-%m-%d")
            stats.latest = max(d.published_at for d in dated).strftime("%Y-%m-%d")
        stats.notes.extend(reader.errors[:5])
        log(f"  reddit: {stats.summary_line()}")
        for note in stats.notes:
            log(f"    {note}")
        return docs, stats

    def _listing(
        self,
        username: str,
        author_handle: str,
        reader: JsonReader,
        stats: RedditStats,
        *,
        kind: str,
    ) -> list[Document]:
        max_pages = MAX_COMMENT_PAGES if kind == "comments" else MAX_SUBMISSION_PAGES
        docs: list[Document] = []
        after: str | None = None
        for _ in range(max_pages):
            url = f"{SITE}/user/{username}/{kind}.json?limit={PER_PAGE}"
            if after:
                url += f"&after={after}"
            payload = reader.get(url)
            if not isinstance(payload, dict):
                break
            data = payload.get("data")
            children = data.get("children") if isinstance(data, dict) else None
            if not isinstance(children, list) or not children:
                break
            if kind == "comments":
                stats.comment_pages += 1
            else:
                stats.submission_pages += 1
            for child in children:
                if isinstance(child, dict) and isinstance(child.get("data"), dict):
                    doc = self._from_child(child["data"], username, author_handle, stats, kind)
                    if doc is not None:
                        docs.append(doc)
            after = data.get("after") if isinstance(data, dict) else None
            if not after:
                break
        return docs

    def _from_child(
        self,
        data: dict[str, Any],
        username: str,
        author_handle: str,
        stats: RedditStats,
        kind: str,
    ) -> Document | None:
        # The listing only returns their writing; the check costs one
        # comparison and the failure it prevents is wrong attribution.
        if str(data.get("author", "")).lower() != username.lower():
            return None
        fullname = str(data.get("name") or "")
        if not fullname:
            return None
        when = _when(data)
        score = data.get("score")
        engagement = {"score": int(score)} if isinstance(score, (int, float)) else {}

        if kind == "comments":
            body = str(data.get("body") or "").strip()
            if not body:
                return None
            stats.comments += 1
            title = str(data.get("link_title") or "").strip()
            context_url = str(data.get("link_permalink") or "")
            return Document(
                source="reddit",
                source_id=f"reddit-{fullname}",
                url=_permalink_url(data),
                author_handle=author_handle,
                published_at=when if when is not None else DATE_UNKNOWN,
                date_unknown=when is None,
                kind="reply",
                body=body,
                context=title or None,
                context_author=None,
                context_url=context_url or None,
                engagement=engagement,
                outbound_links=[],
                part_count=1,
                raw={"kind": "comment", "subreddit": str(data.get("subreddit") or "")},
            )

        title = str(data.get("title") or "").strip()
        selftext = str(data.get("selftext") or "").strip()
        body = f"{title}\n\n{selftext}".strip() if selftext else title
        if not body:
            return None
        stats.submissions += 1
        outbound = str(data.get("url") or "")
        is_self = bool(data.get("is_self"))
        return Document(
            source="reddit",
            source_id=f"reddit-{fullname}",
            url=_permalink_url(data),
            author_handle=author_handle,
            published_at=when if when is not None else DATE_UNKNOWN,
            date_unknown=when is None,
            kind="original",
            body=body,
            engagement=engagement,
            outbound_links=[outbound] if outbound.startswith("http") and not is_self else [],
            part_count=1,
            raw={"kind": "submission", "subreddit": str(data.get("subreddit") or "")},
        )
