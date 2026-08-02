"""Substack adapter. Optional bolt-on; X remains the corpus.

Paginates /api/v1/archive, fetches bodies via /api/v1/posts/{slug}, falls back
to /feed if the JSON API is unavailable. Paywalled posts keep title and
subtitle only — the paywall is respected, never worked around.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from ..cache import Cache
from ..models import Document
from .base import SourceError, html_to_text, http_client, make_document, parse_date
from .rss import RSSSource

PAGE_SIZE = 50


class SubstackSource:
    name = "substack"

    def fetch(
        self,
        target: str,
        *,
        author_handle: str,
        cache: Cache,
        limit: int = 50,
        log: Callable[[str], None] = print,
    ) -> list[Document]:
        domain = target.replace("https://", "").replace("http://", "").strip("/")
        base = f"https://{domain}"
        cache_key = f"substack:{domain}:index"

        cached = cache.get("substack", cache_key)
        if cached is None and cache.offline:
            raise SourceError(f"--offline: no cached Substack archive for {domain}")

        client = http_client()
        try:
            index = cached if cached is not None else self._archive(client, base, limit, log)
            if not index:
                log(f"  substack: archive API empty for {domain}, falling back to /feed")
                return RSSSource().fetch(
                    f"{base}/feed",
                    author_handle=author_handle,
                    cache=cache,
                    limit=limit,
                    log=log,
                )
            cache.put("substack", cache_key, index)

            docs: list[Document] = []
            for entry in index[:limit]:
                slug = entry.get("slug")
                if not slug:
                    continue
                doc = self._post(client, base, domain, slug, entry, author_handle, cache, log)
                if doc:
                    docs.append(doc)
            log(f"  substack: {len(docs)} posts from {domain}")
            return docs
        finally:
            client.close()

    # -- internals --------------------------------------------------------

    def _archive(
        self, client: Any, base: str, limit: int, log: Callable[[str], None]
    ) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        offset = 0
        while len(out) < limit:
            url = f"{base}/api/v1/archive?sort=new&limit={PAGE_SIZE}&offset={offset}"
            resp = client.get(url)
            if resp.status_code >= 400:
                log(f"  substack: archive returned {resp.status_code}")
                break
            try:
                page = resp.json()
            except ValueError:
                break
            if not isinstance(page, list) or not page:
                break
            out.extend(page)
            if len(page) < PAGE_SIZE:
                break
            offset += PAGE_SIZE
        return out

    def _post(
        self,
        client: Any,
        base: str,
        domain: str,
        slug: str,
        entry: dict[str, Any],
        author_handle: str,
        cache: Cache,
        log: Callable[[str], None],
    ) -> Document | None:
        key = f"substack:{domain}:{slug}"
        payload = cache.get("substack", key)
        if payload is None:
            if cache.offline:
                payload = entry
            else:
                resp = client.get(f"{base}/api/v1/posts/{slug}")
                payload = resp.json() if resp.status_code < 400 else entry
                if not isinstance(payload, dict):
                    payload = entry
                # Published posts do not change; cache permanently.
                cache.put("substack", key, payload, permanent=True)

        body_html = payload.get("body_html") or ""
        audience = payload.get("audience") or entry.get("audience") or "everyone"
        paywalled = bool(payload.get("paywalled")) or (audience != "everyone" and not body_html)

        title = payload.get("title") or entry.get("title") or ""
        subtitle = payload.get("subtitle") or entry.get("subtitle") or ""
        links: list[str]
        if paywalled or not body_html:
            body, links = subtitle, []
            paywalled = True
        else:
            body, links = html_to_text(body_html)

        url = payload.get("canonical_url") or entry.get("canonical_url") or f"{base}/p/{slug}"
        return make_document(
            source="substack",
            source_id=f"{domain}:{slug}",
            url=url,
            author_handle=author_handle,
            published_at=parse_date(payload.get("post_date") or entry.get("post_date")),
            title=title,
            body=body,
            links=links,
            raw={"slug": slug, "audience": audience, "domain": domain},
            paywalled=paywalled,
        )
