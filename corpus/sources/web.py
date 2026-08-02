"""Single-page adapter with readability-style extraction."""

from __future__ import annotations

import re
from collections.abc import Callable

from ..cache import Cache
from ..models import Document
from .base import SourceError, html_to_text, http_client, make_document, parse_date

_TITLE = re.compile(r"<title[^>]*>(.*?)</title>", re.I | re.S)
_PUBLISHED = re.compile(
    r'<meta[^>]+(?:property|name)=["\'](?:article:published_time|date|pubdate)["\']'
    r'[^>]+content=["\']([^"\']+)["\']',
    re.I,
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
        published_match = _PUBLISHED.search(html)
        body, links = html_to_text(html)
        log(f"  web: {len(body)} chars from {target}")
        return [
            make_document(
                source="web",
                source_id=target,
                url=target,
                author_handle=author_handle,
                published_at=parse_date(published_match.group(1) if published_match else None),
                title=title,
                body=body,
                links=links,
                raw={"url": target},
            )
        ]
