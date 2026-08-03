"""Provider-agnostic web search.

Mirrors `corpus/x/providers.py`, which has survived one provider change well:
a Protocol naming the whole surface, one fully-implemented provider, and stubs
that raise `NotImplementedError` naming exactly which env var and endpoint to
add. Nothing above this file knows which provider is in use.

`anthropic_search` is the implemented one, using the Messages API's server-side
`web_search_20250305` tool with the `ANTHROPIC_API_KEY` the tool already needs
for synthesis. That means no second vendor, no second key, and no second
billing relationship for the common case.

Two properties of that tool shape this file:

* **Billing is per search, not per call.** The unit is
  `usage.server_tool_use.web_search_requests` at $10/1,000, and a search that
  errors is not billed. So `search()` pins `max_uses=1` — one query in, one
  billable search out — which is what makes the pre-flight reservation exact
  rather than a guess.
* **There is no snippet field.** A `web_search_result` carries `url`, `title`,
  `page_age`, and an opaque `encrypted_content` blob. The readable text comes
  from the model's *citations* (`cited_text`, up to 150 characters, and not
  billed as tokens), which is what the snippet is built from. It is a real
  quotation from the page rather than a summary — and Phase 2 treats it as a
  lead either way, because a snippet is never evidence. Only a fetched page is.

On the tool version
-------------------
`web_search_20250305` is the basic variant and works on every model that
supports web search, including the Haiku that drives it here. The newer
`web_search_20260209` and `web_search_20260318` add dynamic filtering, which
runs the search from inside code execution and needs a 4.6-or-later model —
more machinery than "hand me the result URLs" requires, and it would rule out
the cheap model. `SEARCH_TOOL_TYPE` is the one line to change if that trade
ever flips.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Protocol, runtime_checkable

from pydantic import BaseModel, Field

from ..redact import RedactingError
from .capture import SearchCapture

# The server-side tool. See the module docstring for why the basic variant.
SEARCH_TOOL_TYPE = "web_search_20250305"
SEARCH_TOOL_NAME = "web_search"

# Search is mechanical: run this query, hand back what came out. That is
# extraction, not judgment, which is the same reasoning that puts the map stage
# on Haiku. The dated id is pinned for the same reason map pins it — an alias
# re-point must not silently change what we billed against mid-run.
SEARCH_MODEL = "claude-haiku-4-5-20251001"

# The model only has to name what it found; the results arrive as content
# blocks regardless of what it writes. Small on purpose — output tokens here
# are pure overhead.
SEARCH_MAX_TOKENS = 1_024

# Exactly one billable search per call. Without this the model decides how many
# searches a query is worth, and a reservation cannot be exact against a number
# the model picks after we have paid for it.
MAX_USES_PER_QUERY = 1

SEARCH_SYSTEM = """You are a search runner. You do not answer questions and you do not \
summarize.

Run the user's text as a single web search query, verbatim, using the web_search \
tool. Do not rewrite it, do not broaden it, do not split it into several \
searches, and do not add terms of your own.

Then reply with the single word: done.

The caller reads the search results directly out of the tool result blocks. \
Anything else you write is discarded, so write nothing else."""


class SearchError(RedactingError):
    """A search provider failed.

    Redacting for the same reason `ProviderError` is: these messages quote
    upstream errors, and an SDK exception carries the request URL.
    """


class SearchResult(BaseModel):
    """One search hit. A lead, never evidence."""

    url: str
    title: str = ""
    #: Verbatim text the model cited from the page, when it cited any. Empty is
    #: normal and is not a defect: a result the model did not quote still has a
    #: URL, and the URL is the only part Phase 2 actually acts on.
    snippet: str = ""
    published_at: datetime | None = None
    raw: dict[str, Any] = Field(default_factory=dict)


@dataclass
class SearchUsage:
    """What one search call actually consumed, for the budget to reconcile.

    Carried on the provider rather than returned from `search()`, so the
    Protocol keeps the signature the rest of the tool codes against. This is
    the same split as X: providers fetch, the client bills.
    """

    searches: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    model: str = ""
    errors: list[str] = field(default_factory=list)


@runtime_checkable
class SearchProvider(Protocol):
    """The whole surface the rest of the tool depends on."""

    name: str
    #: Usage from the most recent `search()`. Reset per call.
    last_usage: SearchUsage

    def search(self, query: str, limit: int) -> list[SearchResult]:
        """Run one query. At most `limit` results, best first."""

    def close(self) -> None: ...


# --------------------------------------------------------------------------
# response shape
# --------------------------------------------------------------------------
# The SDK returns pydantic objects; the fixtures and the fake return plain
# dicts. Both are read through these accessors so the parsing code is written
# once and the tests exercise the same path production does.


def _get(obj: Any, key: str, default: Any = None) -> Any:
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


def _blocks(message: Any) -> list[Any]:
    content = _get(message, "content") or []
    return list(content) if isinstance(content, (list, tuple)) else []


# `page_age` is human-readable prose ("April 30, 2025"), not a timestamp. Parsed
# best-effort: a date we cannot read is left as None rather than guessed at,
# because a wrong published_at silently reorders a corpus.
_PAGE_AGE_FORMATS = ("%B %d, %Y", "%b %d, %Y", "%Y-%m-%d", "%d %B %Y", "%B %Y", "%Y")


def parse_page_age(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    for fmt in _PAGE_AGE_FORMATS:
        try:
            return datetime.strptime(text, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).astimezone(timezone.utc)
    except ValueError:
        return None


def citations_by_url(message: Any) -> dict[str, str]:
    """Cited text per URL, from the model's own citations.

    This is where a snippet comes from: `cited_text` is a verbatim quotation of
    up to 150 characters, and the docs are explicit that citation text is not
    billed as tokens. A result the model did not cite simply has no snippet.
    """
    found: dict[str, str] = {}
    for block in _blocks(message):
        if _get(block, "type") != "text":
            continue
        for citation in _get(block, "citations") or []:
            url = str(_get(citation, "url") or "")
            cited = str(_get(citation, "cited_text") or "").strip()
            if url and cited and url not in found:
                found[url] = cited
    return found


def results_from_message(message: Any, limit: int) -> tuple[list[SearchResult], list[str]]:
    """Pull search results out of a Messages response.

    Returns (results, errors). An errored search is a 200 with an error object
    where the result list would be — the tool never raises for a failed search,
    so a caller that only catches exceptions sees silence instead of a problem.
    """
    snippets = citations_by_url(message)
    results: list[SearchResult] = []
    errors: list[str] = []
    seen: set[str] = set()

    # A paused turn is the same silent-failure class as a renamed block: the
    # server stopped mid-search, whatever arrived is partial, and without this
    # the run reports fewer results — or none — as though that were the answer.
    # Not resumed, because `max_uses=1` makes a pause unexpected; if it starts
    # happening, the single-search assumption behind the reservation is wrong
    # and that is worth surfacing rather than papering over.
    if _get(message, "stop_reason") == "pause_turn":
        errors.append("pause_turn: the search turn was paused; results may be incomplete")

    for block in _blocks(message):
        if _get(block, "type") != "web_search_tool_result":
            continue
        content = _get(block, "content")
        # On an error, `content` is a single object rather than a list. A
        # successful search that matched nothing is an empty list, which is a
        # finding rather than a failure.
        if isinstance(content, dict) or not isinstance(content, (list, tuple)):
            code = _get(content, "error_code") or _get(content, "type") or "unknown"
            errors.append(str(code))
            continue
        for entry in content:
            if _get(entry, "type") != "web_search_result":
                continue
            url = str(_get(entry, "url") or "").strip()
            if not url or url in seen:
                continue
            seen.add(url)
            results.append(
                SearchResult(
                    url=url,
                    title=str(_get(entry, "title") or "").strip(),
                    snippet=snippets.get(url, ""),
                    published_at=parse_page_age(_get(entry, "page_age")),
                    # `encrypted_content` is an opaque blob worth kilobytes per
                    # result and is only meaningful when replayed to the same
                    # API. Dropping it keeps discovery.json readable.
                    raw={
                        k: _get(entry, k)
                        for k in ("url", "title", "page_age")
                        if _get(entry, k) is not None
                    },
                )
            )
    return results[:limit], errors


def usage_from_message(message: Any, model: str) -> SearchUsage:
    usage = _get(message, "usage")
    server = _get(usage, "server_tool_use") if usage is not None else None
    return SearchUsage(
        searches=int(_get(server, "web_search_requests", 0) or 0),
        input_tokens=int(_get(usage, "input_tokens", 0) or 0),
        output_tokens=int(_get(usage, "output_tokens", 0) or 0),
        cache_read_tokens=int(_get(usage, "cache_read_input_tokens", 0) or 0),
        cache_write_tokens=int(_get(usage, "cache_creation_input_tokens", 0) or 0),
        model=model,
    )


# --------------------------------------------------------------------------


class AnthropicSearchProvider:
    """Anthropic's server-side web search tool. Key from ANTHROPIC_API_KEY."""

    name = "anthropic_search"

    def __init__(
        self,
        api_key: str | None = None,
        model: str = SEARCH_MODEL,
        client: Any = None,
        capture: SearchCapture | None = None,
        log: Callable[[str], None] = lambda _msg: None,
    ) -> None:
        self.model = model
        self.capture = capture
        self.log = log
        self.last_usage = SearchUsage(model=model)
        #: The most recent response, untouched. Kept so the live contract
        #: checker can inspect what actually arrived rather than re-parsing a
        #: capture file — and so a shape change is checkable in the same run
        #: that hit it.
        self.last_raw_message: Any = None
        self._client = client
        self._owns_client = client is None
        self._api_key = api_key or os.environ.get("ANTHROPIC_API_KEY", "")
        if client is None and not self._api_key:
            raise SearchError(
                "ANTHROPIC_API_KEY is not set, so search cannot run. It is the same "
                "key synthesis uses (see .env.example). Run with --no-search to use "
                "anchors and link-following only."
            )

    def _ensure_client(self) -> Any:
        """Construct the SDK client on first use, never at import.

        Deferred so that a test which never searches cannot accidentally
        require a key, and so `tests/` can fail loudly if a client is built at
        all — the same guard the discovery tests put on `http_client`.
        """
        if self._client is None:
            from anthropic import Anthropic

            self._client = Anthropic(api_key=self._api_key)
        return self._client

    def search(self, query: str, limit: int) -> list[SearchResult]:
        client = self._ensure_client()
        tool: dict[str, Any] = {
            "type": SEARCH_TOOL_TYPE,
            "name": SEARCH_TOOL_NAME,
            "max_uses": MAX_USES_PER_QUERY,
        }
        try:
            message = client.messages.create(
                model=self.model,
                max_tokens=SEARCH_MAX_TOKENS,
                system=SEARCH_SYSTEM,
                tools=[tool],
                messages=[{"role": "user", "content": query}],
            )
        except Exception as exc:
            raise SearchError(f"search for {query!r} failed: {exc}") from exc

        self.last_raw_message = message
        self.last_usage = usage_from_message(message, self.model)
        results, errors = results_from_message(message, limit)
        self.last_usage.errors = errors
        for code in errors:
            # A search error is a 200 with an error object. Saying so is the
            # difference between "this person has no web presence" and "the
            # rate limiter said no".
            self.log(f"  [search] {query!r}: the provider returned error {code}")
        if self.capture is not None:
            self.capture.record(
                query=query,
                provider=self.name,
                model=self.model,
                message=message,
                results=[r.model_dump(mode="json") for r in results],
                usage=self.last_usage,
            )
        if not self.last_usage.searches and not results:
            self.log(f"  [search] {query!r}: the model answered without searching")
        return results

    def close(self) -> None:
        if self._owns_client and self._client is not None:
            close = getattr(self._client, "close", None)
            if close is not None:
                close()
            self._client = None


class _StubSearchProvider:
    """Base for vendors we have not implemented."""

    name = "stub"
    _env_var = ""
    _endpoint = ""

    def __init__(self, *_: Any, **__: Any) -> None:
        raise NotImplementedError(
            f"The {self.name!r} search provider is not implemented. To add it: set "
            f"{self._env_var}, call {self._endpoint}, map each hit to SearchResult "
            f"(url, title, snippet, published_at, raw), and register the class in "
            f"PROVIDERS in corpus/search/providers.py. Nothing outside that file "
            f"needs to change. Then run with SEARCH_PROVIDER={self.name}."
        )

    # Present so the class satisfies SearchProvider structurally. __init__
    # always raises, so neither is reachable — but a body of `...` reads to a
    # type checker as "returns None", which is a lie about the signature.
    last_usage: SearchUsage = SearchUsage()

    def search(self, query: str, limit: int) -> list[SearchResult]:
        raise NotImplementedError(self.name)

    def close(self) -> None:
        return None


class ExaSearchProvider(_StubSearchProvider):
    name = "exa"
    _env_var = "EXA_API_KEY"
    _endpoint = (
        "POST https://api.exa.ai/search with header 'x-api-key: $EXA_API_KEY' and body "
        "{query, numResults, contents: {text: true}} — its `text` field is a real page "
        "extract rather than a citation fragment, so the snippet is richer than "
        "anthropic_search's"
    )


class BraveSearchProvider(_StubSearchProvider):
    name = "brave"
    _env_var = "BRAVE_API_KEY"
    _endpoint = (
        "GET https://api.search.brave.com/res/v1/web/search?q=&count= with headers "
        "'X-Subscription-Token: $BRAVE_API_KEY' and 'Accept: application/json' — read "
        "web.results[], each carrying url, title, description, and page_age"
    )


PROVIDERS: dict[str, type] = {
    "anthropic_search": AnthropicSearchProvider,
    "exa": ExaSearchProvider,
    "brave": BraveSearchProvider,
}


def get_search_provider(name: str | None = None, **kwargs: Any) -> SearchProvider:
    name = (name or os.environ.get("SEARCH_PROVIDER") or "anthropic_search").strip()
    if name not in PROVIDERS:
        raise SearchError(
            f"Unknown SEARCH_PROVIDER {name!r}. Known: {', '.join(sorted(PROVIDERS))}"
        )
    provider: SearchProvider = PROVIDERS[name](**kwargs)
    return provider
