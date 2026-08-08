"""Reader-service fallback: one more try for pages that 403 an honest fetch.

OFF BY DEFAULT. Enabling it (`--reader-fallback`, or CORPUS_READER_FALLBACK=1)
means every URL that reaches the fallback is SENT TO A THIRD PARTY — the
reader service fetches the page on our behalf and returns extracted text.
That is a real privacy trade, which is why it is a decision the operator
makes explicitly, never a default.

What a reader returns is extracted text, not the original markup. Meta tags,
bylines, `rel="author"` links and the rest of the author furniture
`pagefacts.py` reads may be absent or reconstructed, so attribution signals
are weaker on reader output than on original HTML. Everything recovered this
way is marked (`Fetcher.via_reader` → `attribution_basis`), and the report
says so rather than pretending an extraction is a fetch.

The seam mirrors `search/providers.py`: a Protocol naming the surface, one
implemented provider, a registry, and a factory reading an env var — a second
reader is a new class and a registry line, with no caller changes. The
Protocol the fetch layer types against lives in `discovery.py`
(`ReaderService`), structurally, because this module imports `search.scoring`
(for the people-search refusal) which imports `discovery`.

Two refusals are load-bearing:

* A people-search or data-broker host is never sent to a reader. scoring.py
  rejects those on sight, so they never reach the fetcher's fallback path at
  all — and this module refuses them again anyway, because a fallback that
  routes around a refusal is not a fetch improvement.
* A URL the identity card excludes is never sent either; the fetcher carries
  `card.is_excluded` as its blocked predicate.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from typing import Any, Protocol, runtime_checkable
from urllib.parse import urlparse

from .redact import RedactingError

#: r.jina.ai works keyless at low volume; a key raises the rate limit.
READER_API_KEY_ENV = "READER_API_KEY"
#: Which reader to use when the fallback is enabled.
READER_PROVIDER_ENV = "READER_PROVIDER"
#: The env-var half of the switch (the CLI flag is the other half).
READER_FALLBACK_ENV = "CORPUS_READER_FALLBACK"

JINA_READER_BASE = "https://r.jina.ai/"

#: Reader responses past this are not a page's prose. Same reasoning as
#: discovery.MAX_FETCH_BODY_BYTES, applied to the fallback's own output.
MAX_READER_BODY_BYTES = 5_000_000


class ReaderError(RedactingError):
    """A reader service failed. Redacting because messages carry URLs."""


@runtime_checkable
class ReaderProvider(Protocol):
    """The whole surface a reader service exposes to this tool."""

    name: str

    def read(self, url: str) -> str | None:
        """Extracted text for one URL, or None. Never raises past itself."""

    def close(self) -> None: ...


def _refused_host(url: str) -> str:
    """The reason this URL must never reach a reader, or "".

    Imported lazily: `search.scoring` imports `discovery`, and this module is
    imported by the CLI alongside both.
    """
    from .discovery import suffix_match
    from .search.scoring import PEOPLE_SEARCH_HOSTS

    host = (urlparse(url).hostname or "").lower().removeprefix("www.")
    match = suffix_match(host, PEOPLE_SEARCH_HOSTS)
    if match:
        return f"{match} is a people-search or data-broker site, which this tool never reads"
    return ""


class JinaReaderProvider:
    """r.jina.ai: GET r.jina.ai/{url} returns the page as clean markdown.

    Keyless at low volume; READER_API_KEY raises the limit. UNVERIFIED
    against the wire like every documented shape in this repo — but the
    failure mode is benign by construction: a changed response is just text
    that scores worse, and a failed request is a page already lost.
    """

    name = "jina"

    def __init__(
        self,
        api_key: str | None = None,
        client: Any = None,
        log: Callable[[str], None] = lambda _msg: None,
    ) -> None:
        self.log = log
        self.errors: list[str] = []
        self._client = client
        self._owns_client = client is None
        self._api_key = api_key if api_key is not None else os.environ.get(READER_API_KEY_ENV, "")

    def headers(self) -> dict[str, str]:
        from .sources.base import USER_AGENT

        built = {"User-Agent": USER_AGENT, "Accept": "text/plain"}
        if self._api_key:
            built["Authorization"] = f"Bearer {self._api_key}"
        return built

    def _ensure_client(self) -> Any:
        """Built on first use, never at import or __init__ — the same seam the
        search providers have, and the conftest guard patches it so no test
        can reach r.jina.ai."""
        if self._client is None:
            import httpx

            self._client = httpx.Client(timeout=30.0, headers=self.headers())
        return self._client

    def read(self, url: str) -> str | None:
        refused = _refused_host(url)
        if refused:
            self.errors.append(f"reader refused {url}: {refused}")
            return None
        client = self._ensure_client()
        try:
            resp = client.get(f"{JINA_READER_BASE}{url}")
        except Exception as exc:
            self.errors.append(str(ReaderError(f"reader fetch of {url}: {exc}")))
            return None
        if resp.status_code >= 400:
            self.errors.append(f"reader returned {resp.status_code} for {url}")
            return None
        text = str(resp.text).strip()
        if not text:
            return None
        if len(text.encode("utf-8", errors="ignore")) > MAX_READER_BODY_BYTES:
            self.errors.append(f"reader output for {url} is implausibly large; dropped")
            return None
        return text

    def close(self) -> None:
        if self._owns_client and self._client is not None:
            close = getattr(self._client, "close", None)
            if close is not None:
                close()
            self._client = None


READERS: dict[str, type] = {
    "jina": JinaReaderProvider,
}


def reader_fallback_enabled(flag: bool) -> bool:
    """The two halves of the switch: the CLI flag, or the env var."""
    env = os.environ.get(READER_FALLBACK_ENV, "").strip().lower()
    return flag or env in ("1", "true", "yes", "on")


def get_reader(name: str | None = None, **kwargs: Any) -> ReaderProvider:
    name = (name or os.environ.get(READER_PROVIDER_ENV) or "jina").strip()
    if name not in READERS:
        raise ReaderError(
            f"Unknown {READER_PROVIDER_ENV} {name!r}. Known: {', '.join(sorted(READERS))}"
        )
    provider: ReaderProvider = READERS[name](**kwargs)
    return provider
