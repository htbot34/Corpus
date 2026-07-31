"""Shared plumbing for secondary sources.

Scope boundary, enforced here so every adapter inherits it: public content
published under the person's own name only. No private or protected content, no
authentication, no paywall circumvention. An adapter that would need any of
that must fail with an explanation instead.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from html import unescape
from html.parser import HTMLParser
from typing import Any, Callable, Protocol, runtime_checkable

import httpx

from ..cache import Cache
from ..models import Document

USER_AGENT = "corpus/0.1 (personal research tool; +https://github.com/)"


class SourceError(RuntimeError):
    pass


class PaywallEncountered(SourceError):
    """Raised when content requires payment or login. Never worked around."""


@runtime_checkable
class SecondarySource(Protocol):
    name: str

    def fetch(
        self,
        target: str,
        *,
        author_handle: str,
        cache: Cache,
        limit: int = 50,
        log: Callable[[str], None] = print,
    ) -> list[Document]: ...


def http_client(timeout: float = 30.0) -> httpx.Client:
    return httpx.Client(
        timeout=timeout,
        follow_redirects=True,
        headers={"User-Agent": USER_AGENT, "Accept": "*/*"},
    )


# --------------------------------------------------------------------------
# HTML -> text
# --------------------------------------------------------------------------
# A readability-style extractor in ~60 lines of stdlib. Pulling in
# beautifulsoup/readability/trafilatura for three optional adapters would cost
# more dependency surface than the feature is worth; if extraction quality
# becomes the bottleneck, swap this one function.

_DROP_TAGS = {
    "script", "style", "noscript", "nav", "header", "footer", "aside",
    "form", "svg", "iframe", "button", "figcaption",
}
_BLOCK_TAGS = {
    "p", "div", "br", "li", "tr", "section", "article",
    "h1", "h2", "h3", "h4", "h5", "h6", "blockquote", "pre",
}


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.links: list[str] = []
        self._skip_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in _DROP_TAGS:
            self._skip_depth += 1
            return
        if tag == "a":
            for key, value in attrs:
                if key == "href" and value and value.startswith("http"):
                    self.links.append(value)
        if tag in _BLOCK_TAGS:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in _DROP_TAGS and self._skip_depth:
            self._skip_depth -= 1
            return
        if tag in _BLOCK_TAGS:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if self._skip_depth:
            return
        if data.strip():
            self.parts.append(data)


def html_to_text(html: str) -> tuple[str, list[str]]:
    """Return (plain text, outbound links)."""
    if not html:
        return "", []
    parser = _TextExtractor()
    try:
        parser.feed(html)
        parser.close()
    except Exception:  # malformed markup is common; salvage what we parsed
        pass
    text = unescape("".join(parser.parts))
    text = re.sub(r"[ \t ]+", " ", text)
    text = re.sub(r"\n\s*\n\s*\n+", "\n\n", text)
    return text.strip(), list(dict.fromkeys(parser.links))


def parse_date(value: Any) -> datetime:
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc)
    text = str(value or "").strip()
    if not text:
        return datetime.now(tz=timezone.utc)
    for fmt in (
        "%Y-%m-%dT%H:%M:%S.%fZ",
        "%Y-%m-%dT%H:%M:%SZ",
        "%a, %d %b %Y %H:%M:%S %z",
        "%a, %d %b %Y %H:%M:%S %Z",
        "%Y-%m-%d",
    ):
        try:
            parsed = datetime.strptime(text, fmt)
            return parsed.replace(tzinfo=parsed.tzinfo or timezone.utc).astimezone(timezone.utc)
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).astimezone(timezone.utc)
    except ValueError:
        return datetime.now(tz=timezone.utc)


def make_document(
    *,
    source: str,
    source_id: str,
    url: str,
    author_handle: str,
    published_at: datetime,
    title: str,
    body: str,
    links: list[str],
    raw: dict[str, Any],
    paywalled: bool = False,
) -> Document:
    text = f"{title}\n\n{body}".strip() if title else body.strip()
    if paywalled:
        text += "\n\n[paywalled: title and subtitle only]"
    return Document(
        source=source,
        source_id=source_id,
        url=url,
        author_handle=author_handle,
        published_at=published_at,
        kind="original",
        body=text,
        engagement={},
        outbound_links=links,
        part_count=1,
        raw=raw,
    )
