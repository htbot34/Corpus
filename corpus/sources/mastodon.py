"""Mastodon as a corpus source.

Every instance serves a local account's public statuses over a keyless JSON
API — `accounts/lookup` then `accounts/{id}/statuses` — no credentials, no
session, what the person published under their own name.

Three properties of the wire shape shape the design:

**Statuses are HTML.** `content` goes through the same stdlib extractor every
other adapter uses, keeping outbound links.

**Replies carry an id, not a parent.** `in_reply_to_id` is resolved with one
request per answered status, capped (`MAX_PARENT_LOOKUPS`) and cached
permanently — an old status never changes. A parent that cannot be read
becomes `[unavailable]`, the X pipeline's rule, and the reply is kept.

**Visibility is on the status.** Only `public` and `unlisted` are read, and
anything else is skipped and counted rather than worked around — the scope
boundary is enforced per document, not assumed from the endpoint.
`exclude_reblogs=true` keeps boosts out entirely: a boost is someone else's
writing, the same rule every adapter here applies to reposts.

Consecutive self-replies are stitched into one thread `Document` — Mastodon
threads are where people make actual arguments, same as everywhere else.

Fixture provenance: `tests/fixtures/mastodon_*.json` are SYNTHETIC, written
to the documented Mastodon API shape (the offline rule means nothing is
captured live). The adapter reads defensively so a shape change degrades to
fewer documents with notes, never to wrong attribution.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from ..cache import Cache
from ..models import DATE_UNKNOWN, Document
from .base import JsonReader, collapse_self_threads, html_to_text, parse_date

PER_PAGE = 40  # the documented statuses ceiling
MAX_PAGES = 10  # 400 statuses back; each page is one request

# Same rule as HN: one hydration request per answered status, capped, so a
# prolific arguer cannot turn a free source into hundreds of requests.
MAX_PARENT_LOOKUPS = 25

_ACCT = re.compile(r"^@?(?P<user>[A-Za-z0-9_]+)@(?P<host>[A-Za-z0-9.-]+\.[A-Za-z]{2,})$")


@dataclass
class MastodonStats:
    """What was read and what was skipped, so an absence is never silent."""

    pages: int = 0
    posts: int = 0
    replies: int = 0
    threads_stitched: int = 0
    parent_lookups: int = 0
    parents_hydrated: int = 0
    parents_unavailable: int = 0
    parent_lookups_capped: int = 0
    non_public_skipped: int = 0
    earliest: str = ""
    latest: str = ""
    notes: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "pages": self.pages,
            "posts": self.posts,
            "replies": self.replies,
            "threads_stitched": self.threads_stitched,
            "parent_lookups": self.parent_lookups,
            "parents_hydrated": self.parents_hydrated,
            "parents_unavailable": self.parents_unavailable,
            "parent_lookups_capped": self.parent_lookups_capped,
            "non_public_skipped": self.non_public_skipped,
            "date_range": f"{self.earliest} to {self.latest}" if self.earliest else "",
            "notes": list(self.notes),
        }

    def summary_line(self) -> str:
        return (
            f"{self.posts} post(s), {self.replies} repl(ies), "
            f"{self.threads_stitched} thread(s) stitched"
        )


def parse_account(target: str) -> tuple[str, str]:
    """(user, host) from `@user@host`, `user@host`, or a profile URL."""
    value = target.strip().rstrip("/")
    match = _ACCT.match(value)
    if match:
        return match.group("user"), match.group("host").lower()
    for prefix in ("https://", "http://"):
        if value.startswith(prefix):
            rest = value[len(prefix) :]
            host, _, path = rest.partition("/")
            user = path.removeprefix("@").removeprefix("users/").split("/", 1)[0]
            if host and user:
                return user, host.lower()
    return "", ""


class MastodonSource:
    """Reads one Mastodon account's public statuses. `target` is `@user@host`."""

    name = "mastodon"

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
    ) -> tuple[list[Document], MastodonStats]:
        user, host = parse_account(target)
        stats = MastodonStats()
        if not user or not host:
            stats.notes.append(f"cannot parse a Mastodon account from {target!r}; want @user@host")
            log(f"  mastodon: {stats.notes[-1]}")
            return [], stats
        reader = JsonReader("mastodon", cache, log)
        docs: list[Document] = []
        parent_ids: dict[str, str | None] = {}
        try:
            account = reader.get(f"https://{host}/api/v1/accounts/lookup?acct={user}")
            account_id = str(account.get("id") or "") if isinstance(account, dict) else ""
            if not account_id:
                stats.notes.append(f"no account {user}@{host} (lookup failed or unknown)")
            else:
                self._read_statuses(
                    host, account_id, author_handle, reader, stats, docs, parent_ids
                )
        finally:
            reader.close()

        before = len(docs)
        docs = collapse_self_threads(docs, parent_ids)
        stats.threads_stitched = sum(1 for d in docs if d.part_count > 1)
        if stats.threads_stitched:
            log(f"  mastodon: stitched {before - len(docs)} self-repl(ies) into threads")

        docs.sort(key=lambda d: d.published_at, reverse=True)
        docs = docs[:limit]

        dated = [d for d in docs if not d.date_unknown]
        if dated:
            stats.earliest = min(d.published_at for d in dated).strftime("%Y-%m-%d")
            stats.latest = max(d.published_at for d in dated).strftime("%Y-%m-%d")
        stats.notes.extend(reader.errors[:5])
        log(f"  mastodon: {stats.summary_line()}")
        for note in stats.notes:
            log(f"    {note}")
        return docs, stats

    def _read_statuses(
        self,
        host: str,
        account_id: str,
        author_handle: str,
        reader: JsonReader,
        stats: MastodonStats,
        docs: list[Document],
        parent_ids: dict[str, str | None],
    ) -> None:
        max_id: str | None = None
        for _ in range(MAX_PAGES):
            url = (
                f"https://{host}/api/v1/accounts/{account_id}/statuses"
                f"?limit={PER_PAGE}&exclude_reblogs=true"
            )
            if max_id:
                url += f"&max_id={max_id}"
            payload = reader.get(url)
            if not isinstance(payload, list) or not payload:
                break
            stats.pages += 1
            for status in payload:
                if isinstance(status, dict):
                    doc = self._from_status(status, host, author_handle, reader, stats, parent_ids)
                    if doc is not None:
                        docs.append(doc)
            last = payload[-1]
            next_id = str(last.get("id") or "") if isinstance(last, dict) else ""
            if not next_id or len(payload) < PER_PAGE:
                break
            max_id = next_id

    def _from_status(
        self,
        status: dict[str, Any],
        host: str,
        author_handle: str,
        reader: JsonReader,
        stats: MastodonStats,
        parent_ids: dict[str, str | None],
    ) -> Document | None:
        visibility = str(status.get("visibility") or "public")
        if visibility not in ("public", "unlisted"):
            # The scope boundary, per document: never read around it.
            stats.non_public_skipped += 1
            return None
        status_id = str(status.get("id") or "")
        body, links = html_to_text(str(status.get("content") or ""))
        if not status_id or not body.strip():
            return None

        reply_to = status.get("in_reply_to_id")
        is_reply = reply_to is not None and str(reply_to) != ""
        context: str | None = None
        context_author: str | None = None
        context_url: str | None = None
        if is_reply:
            stats.replies += 1
            parent_ids[status_id] = str(reply_to)
            context, context_author, context_url = self._parent(host, str(reply_to), reader, stats)
        else:
            stats.posts += 1
            parent_ids[status_id] = None

        when = parse_date(status.get("created_at"))
        return Document(
            source="mastodon",
            source_id=status_id,
            url=str(status.get("url") or ""),
            author_handle=author_handle,
            published_at=when if when is not None else DATE_UNKNOWN,
            date_unknown=when is None,
            kind="reply" if is_reply else "original",
            body=body.strip(),
            context=context,
            context_author=context_author,
            context_url=context_url,
            engagement={
                "likes": int(status.get("favourites_count") or 0),
                "replies": int(status.get("replies_count") or 0),
                "reposts": int(status.get("reblogs_count") or 0),
            },
            outbound_links=links,
            part_count=1,
            raw={"kind": "reply" if is_reply else "status", "host": host},
        )

    def _parent(
        self, host: str, parent_id: str, reader: JsonReader, stats: MastodonStats
    ) -> tuple[str | None, str | None, str | None]:
        if stats.parent_lookups >= MAX_PARENT_LOOKUPS:
            stats.parent_lookups_capped += 1
            return None, None, None
        stats.parent_lookups += 1  # the lookup was spent, whatever it found
        payload = reader.get(f"https://{host}/api/v1/statuses/{parent_id}", permanent=True)
        if not isinstance(payload, dict):
            stats.parents_unavailable += 1
            return "[unavailable]", None, None
        text, _ = html_to_text(str(payload.get("content") or ""))
        if not text.strip():
            stats.parents_unavailable += 1
            return "[unavailable]", None, None
        stats.parents_hydrated += 1
        raw_account = payload.get("account")
        account: dict[str, Any] = raw_account if isinstance(raw_account, dict) else {}
        author = str(account.get("acct") or "") or None
        url = str(payload.get("url") or "") or None
        return text.strip(), author, url
