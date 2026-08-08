"""Hacker News as a corpus source.

Years of argumentation, free and keyless: the Algolia HN API
(`hn.algolia.com/api/v1`) serves every story and comment an account has ever
posted, no authentication, and comments are the same class of evidence the
GitHub adapter prizes — reasoning under disagreement, in public, over years.

Two properties of the wire shape shape the design:

**A comment hit is not its context.** `search_by_date?tags=author:X` returns
`comment_text` plus `story_title`, but a comment on another comment — most of
any argument — is unreadable without the comment it answers, and the hit does
not carry it. So parent comments are hydrated from the `items` endpoint, one
request each, capped (`MAX_PARENT_LOOKUPS`) and cached permanently: old HN
items never change, so no run ever pays for the same parent twice.

**`comment_text` is HTML.** It goes through the same stdlib extractor every
other adapter uses rather than a new dependency.

Fixture provenance: `tests/fixtures/hn_*.json` are SYNTHETIC, written to the
documented Algolia shape (the offline rule means nothing is captured live).
The adapter reads defensively so a shape change degrades to fewer documents
with notes, never to wrong attribution.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from ..cache import Cache
from ..models import DATE_UNKNOWN, Document
from .base import JsonReader, html_to_text, parse_date

API = "https://hn.algolia.com/api/v1"
SITE = "https://news.ycombinator.com"

PER_PAGE = 100  # the documented hitsPerPage ceiling
MAX_PAGES = 5  # 500 items back; each page is one request

# Parent hydration is one request per answered comment. Uncapped, a prolific
# arguer would turn a free source into hundreds of requests against a service
# that asks for politeness; capped, the most recent arguments keep their
# context and the rest fall back to the story title.
MAX_PARENT_LOOKUPS = 25


@dataclass
class HNStats:
    """What was read and what was skipped, so an absence is never silent."""

    pages: int = 0
    stories: int = 0
    comments: int = 0
    parent_lookups: int = 0
    parents_hydrated: int = 0
    parents_unavailable: int = 0
    parent_lookups_capped: int = 0
    total_hits: int = 0
    earliest: str = ""
    latest: str = ""
    notes: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "pages": self.pages,
            "stories": self.stories,
            "comments": self.comments,
            "parent_lookups": self.parent_lookups,
            "parents_hydrated": self.parents_hydrated,
            "parents_unavailable": self.parents_unavailable,
            "parent_lookups_capped": self.parent_lookups_capped,
            "total_hits": self.total_hits,
            "date_range": f"{self.earliest} to {self.latest}" if self.earliest else "",
            "notes": list(self.notes),
        }

    def summary_line(self) -> str:
        return (
            f"{self.stories} stor(ies), {self.comments} comment(s), "
            f"{self.parents_hydrated} parent comment(s) hydrated"
        )


def _username_of(target: str) -> str:
    """`news.ycombinator.com/user?id=x`, or the bare username."""
    value = target.strip().rstrip("/")
    if "user?id=" in value:
        value = value.split("user?id=", 1)[-1].split("&", 1)[0]
    return value


def _item_url(object_id: str) -> str:
    return f"{SITE}/item?id={object_id}"


class HackerNewsSource:
    """Reads one HN account's stories and comments. `target` is the username."""

    name = "hn"

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
    ) -> tuple[list[Document], HNStats]:
        username = _username_of(target)
        stats = HNStats()
        reader = JsonReader("hn", cache, log)
        docs: list[Document] = []
        try:
            for page in range(MAX_PAGES):
                payload = reader.get(
                    f"{API}/search_by_date?tags=author_{username}"
                    f"&hitsPerPage={PER_PAGE}&page={page}"
                )
                if not isinstance(payload, dict):
                    break
                hits = payload.get("hits")
                if not isinstance(hits, list) or not hits:
                    break
                stats.pages += 1
                stats.total_hits = int(payload.get("nbHits") or 0)
                for hit in hits:
                    if isinstance(hit, dict):
                        doc = self._from_hit(hit, username, author_handle, reader, stats)
                        if doc is not None:
                            docs.append(doc)
                if page + 1 >= int(payload.get("nbPages") or 1):
                    break
        finally:
            reader.close()

        docs.sort(key=lambda d: d.published_at, reverse=True)
        docs = docs[:limit]

        dated = [d for d in docs if not d.date_unknown]
        if dated:
            stats.earliest = min(d.published_at for d in dated).strftime("%Y-%m-%d")
            stats.latest = max(d.published_at for d in dated).strftime("%Y-%m-%d")
        stats.notes.extend(reader.errors[:5])
        log(f"  hn: {stats.summary_line()}")
        for note in stats.notes:
            log(f"    {note}")
        return docs, stats

    def _from_hit(
        self,
        hit: dict[str, Any],
        username: str,
        author_handle: str,
        reader: JsonReader,
        stats: HNStats,
    ) -> Document | None:
        # The tags query only returns their writing; the check costs one
        # comparison and the failure it prevents is wrong attribution.
        if str(hit.get("author", "")).lower() != username.lower():
            return None
        object_id = str(hit.get("objectID") or "")
        if not object_id:
            return None
        when = parse_date(hit.get("created_at"))
        tags = hit.get("_tags")
        is_comment = isinstance(tags, list) and "comment" in tags

        if is_comment:
            return self._comment(hit, object_id, author_handle, when, reader, stats)

        title = str(hit.get("title") or "").strip()
        story_text, _ = html_to_text(str(hit.get("story_text") or ""))
        body = f"{title}\n\n{story_text}".strip() if story_text else title
        if not body:
            return None
        stats.stories += 1
        outbound = str(hit.get("url") or "")
        return Document(
            source="hn",
            source_id=f"hn-{object_id}",
            url=_item_url(object_id),
            author_handle=author_handle,
            published_at=when if when is not None else DATE_UNKNOWN,
            date_unknown=when is None,
            kind="original",
            body=body,
            engagement={"points": int(hit.get("points") or 0)},
            outbound_links=[outbound] if outbound.startswith("http") else [],
            part_count=1,
            raw={"kind": "story", "object_id": object_id},
        )

    def _comment(
        self,
        hit: dict[str, Any],
        object_id: str,
        author_handle: str,
        when: Any,
        reader: JsonReader,
        stats: HNStats,
    ) -> Document | None:
        body, links = html_to_text(str(hit.get("comment_text") or ""))
        if not body.strip():
            return None
        stats.comments += 1

        story_title = str(hit.get("story_title") or "").strip()
        story_url = str(hit.get("story_url") or "")
        story_id = hit.get("story_id")
        parent_id = hit.get("parent_id")

        # A comment on the story itself is made legible by the title. A
        # comment on another comment is not — hydrate the parent, the same
        # rule the X pipeline applies to every reply.
        context: str | None = story_title or None
        context_author: str | None = None
        context_url: str | None = story_url or None
        if parent_id is not None and story_id is not None and str(parent_id) != str(story_id):
            parent = self._parent(str(parent_id), reader, stats)
            if parent is not None:
                context, context_author, context_url = parent

        return Document(
            source="hn",
            source_id=f"hn-{object_id}",
            url=_item_url(object_id),
            author_handle=author_handle,
            published_at=when if when is not None else DATE_UNKNOWN,
            date_unknown=when is None,
            kind="reply",
            body=body.strip(),
            context=context,
            context_author=context_author,
            context_url=context_url,
            engagement={},
            outbound_links=links,
            part_count=1,
            raw={"kind": "comment", "object_id": object_id, "story_id": story_id},
        )

    def _parent(
        self, parent_id: str, reader: JsonReader, stats: HNStats
    ) -> tuple[str | None, str | None, str | None] | None:
        if stats.parent_lookups >= MAX_PARENT_LOOKUPS:
            stats.parent_lookups_capped += 1
            return None
        stats.parent_lookups += 1  # the lookup was spent, whatever it found
        payload = reader.get(f"{API}/items/{parent_id}", permanent=True)
        if not isinstance(payload, dict):
            stats.parents_unavailable += 1
            return "[unavailable]", None, _item_url(parent_id)
        text, _ = html_to_text(str(payload.get("text") or payload.get("title") or ""))
        if not text.strip():
            stats.parents_unavailable += 1
            return "[unavailable]", None, _item_url(parent_id)
        stats.parents_hydrated += 1
        author = str(payload.get("author") or "") or None
        return text.strip(), author, _item_url(parent_id)
