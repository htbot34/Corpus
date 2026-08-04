"""Single-page adapter with readability-style extraction.

The date resolution chain below exists because of a live run in which every
page from the subject's own blog was stamped with the run date: the old
parser read one meta-tag shape, found nothing, and `parse_date` silently
substituted the clock. The page in question carried its date in four places
— the URL path, `og:updated_time`, visible "Posted 16th July 2026" text, and
a dated archive link — and the parser saw none of them. A sixth of that
corpus landed in one fake chronological bucket, and the report inverted two
"what moved" entries.

So the chain tries, in order: JSON-LD, the date meta tags (unix or ISO),
`<time datetime>`, a date in the URL path, and visible "Posted Nth Month
Year" prose. If none resolve, the date is **unknown** — recorded as such,
never approximated with the clock, the fetch time, or anything else.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any

from ..cache import Cache
from ..models import Document
from .base import SourceError, html_to_text, http_client, make_document, parse_date

_TITLE = re.compile(r"<title[^>]*>(.*?)</title>", re.I | re.S)

# Meta tags that carry a date, in both attribute orders. Priority is the key
# list in `_META_DATE_KEYS`, not document order: a published time beats an
# updated time beats the generic shapes.
_META = re.compile(
    r"<meta\b[^>]*?(?:property|name)\s*=\s*[\"']([^\"']+)[\"'][^>]*?"
    r"content\s*=\s*[\"']([^\"']*)[\"'][^>]*>",
    re.I,
)
_META_REVERSED = re.compile(
    r"<meta\b[^>]*?content\s*=\s*[\"']([^\"']*)[\"'][^>]*?"
    r"(?:property|name)\s*=\s*[\"']([^\"']+)[\"'][^>]*>",
    re.I,
)
_META_DATE_KEYS = (
    "article:published_time",
    "og:published_time",
    "og:updated_time",
    "article:modified_time",
    "date",
    "pubdate",
)

_JSON_LD = re.compile(
    r"<script\b[^>]*type\s*=\s*[\"']application/ld\+json[\"'][^>]*>(.*?)</script>",
    re.I | re.S,
)
_TIME_TAG = re.compile(r"<time\b[^>]*\bdatetime\s*=\s*[\"']([^\"']+)[\"']", re.I)

_MONTHS = {
    name.lower(): number
    for number, names in enumerate(
        (
            ("january", "jan"),
            ("february", "feb"),
            ("march", "mar"),
            ("april", "apr"),
            ("may",),
            ("june", "jun"),
            ("july", "jul"),
            ("august", "aug"),
            ("september", "sep", "sept"),
            ("october", "oct"),
            ("november", "nov"),
            ("december", "dec"),
        ),
        start=1,
    )
    for name in names
}

# /2026/Jul/16/ or /2026/07/16/ in a URL path — the shape dated blog archives
# use, and the one place the date cannot be missing from a dated permalink.
_URL_DATE = re.compile(r"/(\d{4})/([A-Za-z]{3,9}|\d{1,2})/(\d{1,2})(?:/|$)")

# Visible "Posted 16th July 2026" (or "Posted on 16 July 2026") prose.
_POSTED_TEXT = re.compile(
    r"\bposted\s+(?:on\s+)?(\d{1,2})(?:st|nd|rd|th)?\s+([A-Za-z]{3,9}),?\s+(\d{4})",
    re.I,
)

# og:updated_time frequently arrives as unix seconds rather than ISO.
_UNIX_SECONDS = re.compile(r"^\d{9,11}$")


def _date_or_none(raw: str) -> datetime | None:
    """One candidate string to a datetime, unix seconds included."""
    raw = (raw or "").strip()
    if not raw:
        return None
    if _UNIX_SECONDS.fullmatch(raw):
        try:
            return datetime.fromtimestamp(float(raw), tz=timezone.utc)
        except (ValueError, OverflowError, OSError):
            return None
    return parse_date(raw)


def _json_ld_date(html: str) -> datetime | None:
    """`datePublished` from any JSON-LD block, else `dateModified`."""
    published: list[str] = []
    modified: list[str] = []

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                lowered = str(key).lower()
                if lowered == "datepublished" and isinstance(value, str):
                    published.append(value)
                elif lowered == "datemodified" and isinstance(value, str):
                    modified.append(value)
                else:
                    walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    for block in _JSON_LD.findall(html):
        try:
            walk(json.loads(block))
        except (ValueError, TypeError):
            continue  # malformed JSON-LD is common; it is not a dated page
    for candidate in published + modified:
        parsed = _date_or_none(candidate)
        if parsed is not None:
            return parsed
    return None


def _meta_date(html: str) -> datetime | None:
    pairs = [(k.strip().lower(), v.strip()) for k, v in _META.findall(html)]
    pairs += [(k.strip().lower(), v.strip()) for v, k in _META_REVERSED.findall(html)]
    by_key: dict[str, str] = {}
    for key, value in pairs:
        by_key.setdefault(key, value)
    for key in _META_DATE_KEYS:
        parsed = _date_or_none(by_key.get(key, ""))
        if parsed is not None:
            return parsed
    return None


def _time_tag_date(html: str) -> datetime | None:
    for raw in _TIME_TAG.findall(html):
        parsed = _date_or_none(raw)
        if parsed is not None:
            return parsed
    return None


def _url_date(url: str) -> datetime | None:
    for year_s, month_s, day_s in _URL_DATE.findall(url):
        month = _MONTHS.get(month_s.lower()) if not month_s.isdigit() else int(month_s)
        if month is None or not 1 <= month <= 12:
            continue
        try:
            return datetime(int(year_s), month, int(day_s), tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


def _posted_text_date(text: str) -> datetime | None:
    for day_s, month_s, year_s in _POSTED_TEXT.findall(text):
        month = _MONTHS.get(month_s.lower())
        if month is None:
            continue
        try:
            return datetime(int(year_s), month, int(day_s), tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


def resolve_published_at(html: str, url: str, text: str) -> datetime | None:
    """The page's publication date, or None — never the clock.

    Tried in order of how deliberately the date was declared: structured data
    first, then meta tags, then markup, then the URL, then visible prose.
    None means none of them resolved, and the caller records the date as
    unknown rather than approximating it.
    """
    return (
        _json_ld_date(html)
        or _meta_date(html)
        or _time_tag_date(html)
        or _url_date(url)
        or _posted_text_date(text)
    )


class WebSource:
    name = "web"

    def fetch(
        self,
        target: str,
        *,
        author_handle: str,
        cache: Cache,
        limit: int = 1,
        log: Callable[[str], None] = print,
    ) -> list[Document]:
        key = f"web:{target}"
        html = cache.get("web", key)
        if html is None:
            if cache.offline:
                raise SourceError(f"--offline: no cached page for {target}")
            client = http_client()
            try:
                resp = client.get(target)
                if resp.status_code >= 400:
                    raise SourceError(f"{resp.status_code} fetching {target}")
                html = resp.text
            finally:
                client.close()
            cache.put("web", key, html)

        title_match = _TITLE.search(html)
        title = re.sub(r"\s+", " ", title_match.group(1)).strip() if title_match else ""
        body, links = html_to_text(html)
        published = resolve_published_at(html, target, body)
        log(
            f"  web: {len(body)} chars from {target}"
            + ("" if published is not None else " (no publication date found; recorded as unknown)")
        )
        return [
            make_document(
                source="web",
                source_id=target,
                url=target,
                author_handle=author_handle,
                published_at=published,
                title=title,
                body=body,
                links=links,
                raw={"url": target},
            )
        ]
