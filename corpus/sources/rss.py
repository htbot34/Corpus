"""RSS/Atom adapter — Medium, Ghost, WordPress, personal blogs, any feed.

Parsed with stdlib ElementTree rather than feedparser: feeds are two shapes and
we need four fields from each.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from collections.abc import Callable

from ..cache import Cache
from ..models import Document
from .base import SourceError, html_to_text, http_client, make_document, parse_date

_ATOM = "{http://www.w3.org/2005/Atom}"
_CONTENT = "{http://purl.org/rss/1.0/modules/content/}"


def _text(node: ET.Element | None) -> str:
    return (node.text or "").strip() if node is not None else ""


class RSSSource:
    name = "rss"

    def fetch(
        self,
        target: str,
        *,
        author_handle: str,
        cache: Cache,
        limit: int = 50,
        log: Callable[[str], None] = print,
    ) -> list[Document]:
        key = f"rss:{target}"
        xml = cache.get("rss", key)
        if xml is None:
            if cache.offline:
                raise SourceError(f"--offline: no cached feed for {target}")
            client = http_client()
            try:
                resp = client.get(target)
                if resp.status_code >= 400:
                    raise SourceError(f"{resp.status_code} fetching feed {target}")
                xml = resp.text
            finally:
                client.close()
            cache.put("rss", key, xml)

        try:
            root = ET.fromstring(xml)
        except ET.ParseError as exc:
            raise SourceError(f"could not parse feed {target}: {exc}") from exc

        docs: list[Document] = []
        for item in root.iter():
            tag = item.tag
            if tag not in ("item", f"{_ATOM}entry"):
                continue
            docs.append(self._entry(item, target, author_handle))
            if len(docs) >= limit:
                break
        log(f"  rss: {len(docs)} entries from {target}")
        return docs

    def _entry(self, item: ET.Element, feed_url: str, author_handle: str) -> Document:
        is_atom = item.tag.startswith(_ATOM)
        if is_atom:
            title = _text(item.find(f"{_ATOM}title"))
            link_node = item.find(f"{_ATOM}link")
            url = (link_node.get("href") if link_node is not None else "") or feed_url
            published = _text(item.find(f"{_ATOM}published")) or _text(item.find(f"{_ATOM}updated"))
            html = _text(item.find(f"{_ATOM}content")) or _text(item.find(f"{_ATOM}summary"))
            guid = _text(item.find(f"{_ATOM}id")) or url
        else:
            title = _text(item.find("title"))
            url = _text(item.find("link")) or feed_url
            published = _text(item.find("pubDate"))
            html = _text(item.find(f"{_CONTENT}encoded")) or _text(item.find("description"))
            guid = _text(item.find("guid")) or url

        body, links = html_to_text(html)
        return make_document(
            source="rss",
            source_id=guid,
            url=url,
            author_handle=author_handle,
            published_at=parse_date(published),
            title=title,
            body=body,
            links=links,
            raw={"feed": feed_url},
        )
