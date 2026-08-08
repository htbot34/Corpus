"""Provider-agnostic web search.

Mirrors `corpus/x/providers.py`, which has survived one provider change well:
a Protocol naming the whole surface, fully-implemented providers, and stubs
that raise `NotImplementedError` naming exactly which env var and endpoint to
add. Nothing above this file knows which provider is in use.

`anthropic_search` is the default, using the Messages API's server-side
`web_search_20250305` tool with the `ANTHROPIC_API_KEY` the tool already needs
for synthesis. That means no second vendor, no second key, and no second
billing relationship for the common case.

`exa` (SEARCH_PROVIDER=exa, key from EXA_API_KEY) exists because of the one
thing anthropic_search cannot provide: a snippet. Its `web_search_result`
carries no readable text, so snippets there are citation fragments that appear
only when the model happened to quote something — for many queries, never —
and Phase 2's fetch ranking runs nearly blind. Exa's `text` field is a real
page extract, and its `findSimilar` endpoint maps "more of this person's
writing" onto the anchor model in a way keyword search does not. See
`ExaSearchProvider` for what is and is not verified about its wire shapes.

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

import math
import os
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Protocol, runtime_checkable

from pydantic import BaseModel, Field

from ..budget import EXA_COST_PER_QUERY, SEARCH_COST_PER_QUERY
from ..redact import RedactingError
from .capture import SearchCapture
from .scoring import PEOPLE_SEARCH_HOSTS

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
    #: The dollar figure the provider's own response stated for the call, when
    #: the vendor reports one (Exa does). None means "bill the documented
    #: rate" — the invoice's number always beats our copy of the price list.
    cost_dollars: float | None = None
    errors: list[str] = field(default_factory=list)


@runtime_checkable
class SearchProvider(Protocol):
    """The whole surface the rest of the tool depends on.

    Two optional extras live *outside* this Protocol, read with `getattr`
    defaults so a provider written before they existed is not broken by their
    absence: `cost_per_search` (the provider's worst-case per-search rate in
    dollars; absent means Anthropic's) and the `find_similar` capability,
    declared by `supports_find_similar = True` and typed by
    `SimilarSearchProvider` below.
    """

    name: str
    #: Usage from the most recent `search()`. Reset per call.
    last_usage: SearchUsage

    def search(self, query: str, limit: int) -> list[SearchResult]:
        """Run one query. At most `limit` results, best first."""

    def close(self) -> None: ...


@runtime_checkable
class SimilarSearchProvider(SearchProvider, Protocol):
    """The optional half of the surface: "find more pages like this one".

    Deliberately not a member of `SearchProvider`: most search vendors cannot
    do it, and a Protocol member they lack would make every one of them wrong
    by omission. A provider that can advertises it with
    `supports_find_similar = True`; `SearchClient.find_similar` checks the
    flag and this Protocol before calling, and quietly does nothing for a
    provider that lacks either.
    """

    supports_find_similar: bool

    def find_similar(self, url: str, limit: int) -> list[SearchResult]:
        """Pages like the one at `url`. At most `limit` results, best first."""


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
    #: What one billable search worst-cases at, before tokens. The same number
    #: `budget.SEARCH_COST_BY_PROVIDER` maps for this name; a test pins them
    #: together.
    cost_per_search = SEARCH_COST_PER_QUERY

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


# --------------------------------------------------------------------------
# Exa
# --------------------------------------------------------------------------

EXA_SEARCH_ENDPOINT = "https://api.exa.ai/search"
EXA_FIND_SIMILAR_ENDPOINT = "https://api.exa.ai/findSimilar"

#: Ceiling on how much of Exa's `text` extract is carried into `snippet`. The
#: extract is the whole reason this provider exists — a real page excerpt
#: instead of a citation fragment — but it can run to tens of kilobytes, and
#: two things downstream assume snippets are snippet-sized: `snippet_promise`
#: ranks candidates partly on what their snippet carries, so one enormous
#: extract would buy its page fetch priority by sheer surface area; and the
#: search cache stores results permanently, so unbounded snippets would grow
#: it without limit. A thousand characters is a few paragraphs — enough for
#: `facts_from_snippet` to find a name, an employer, or a handle, which is all
#: ranking uses it for. It is still never evidence: only a fetched page is.
EXA_SNIPPET_MAX_CHARS = 1_000


def results_from_exa_payload(payload: Any, limit: int) -> tuple[list[SearchResult], list[str]]:
    """Map one Exa response body to SearchResults. Returns (results, errors).

    UNVERIFIED AGAINST THE WIRE: written to Exa's documented response shape —
    `results[]` carrying `url`, `title`, `publishedDate`, `author`, `text` —
    and this repo's bug history is four separate documented-shape-versus-real-
    shape failures (docs/wire-contract.md). So every read here is defensive: a
    missing or misshapen field degrades to a thinner result, never to a raise,
    and the whole hit is preserved in `raw` so a shape surprise is inspectable
    after the fact.
    """
    errors: list[str] = []
    hits = _get(payload, "results")
    if not isinstance(hits, (list, tuple)):
        errors.append("exa: the response carries no `results` list")
        return [], errors
    results: list[SearchResult] = []
    seen: set[str] = set()
    for hit in hits:
        url = str(_get(hit, "url") or "").strip()
        if not url or url in seen:
            continue
        seen.add(url)
        text = str(_get(hit, "text") or "")
        results.append(
            SearchResult(
                url=url,
                title=str(_get(hit, "title") or "").strip(),
                snippet=text[:EXA_SNIPPET_MAX_CHARS].strip(),
                # ISO 8601 in the docs; parse_page_age's fromisoformat fallback
                # reads that, and a malformed date is left as None rather than
                # guessed at — a wrong published_at silently reorders a corpus.
                published_at=parse_page_age(_get(hit, "publishedDate")),
                raw=dict(hit) if isinstance(hit, dict) else {"value": repr(hit)},
            )
        )
    return results[:limit], errors


def exa_cost_dollars(payload: Any) -> float | None:
    """The dollar figure Exa's response states for the call, if it states one.

    `costDollars.total` is documented but UNVERIFIED like the rest of the
    shape, so it is read defensively: anything absent, non-numeric, negative,
    or non-finite means None, and the caller bills the documented rate
    instead. A wire value can correct our price list; it must not be able to
    poison the ledger.
    """
    total = _get(_get(payload, "costDollars") or {}, "total")
    if isinstance(total, bool) or not isinstance(total, (int, float, str)):
        return None
    try:
        value = float(total)
    except ValueError:
        return None
    if not math.isfinite(value) or value < 0:
        return None
    return value


class ExaSearchProvider:
    """Exa's search API. Key from EXA_API_KEY, selected by SEARCH_PROVIDER=exa.

    What it buys over anthropic_search, both halves downstream of real text:

    * `contents: {text: true}` puts an actual page extract in `snippet`, so
      Phase 2's fetch *ranking* has something to rank on. It gates nothing and
      promotes nothing — verify.py's two passes are unchanged, and only a
      score against a fetched page can promote.
    * `find_similar` ("more pages like this one") maps onto the anchor model
      in a way keyword search does not: given a page already known to be the
      target's, ask for more of their writing.

    `excludeDomains` pushes the people-search and data-broker hosts out at the
    API level. That is an optimisation, never the guarantee — a rejected
    result still consumed a result slot, so excluding it recovers the slot —
    and scoring.py's post-hoc rejection stays exactly as it was.

    UNVERIFIED AGAINST THE WIRE: every request and response shape here is
    written to Exa's documentation, not to a capture, and the first live run
    is the test. See docs/wire-contract.md.
    """

    name = "exa"
    #: Worst case one search() or find_similar() call can bill: the request
    #: fee plus text contents for every result. The same number
    #: `budget.SEARCH_COST_BY_PROVIDER` maps for this name; a test pins them
    #: together, and budget.py records where the rate came from.
    cost_per_search = EXA_COST_PER_QUERY
    #: The capability flag SimilarSearchProvider documents.
    supports_find_similar = True

    def __init__(
        self,
        api_key: str | None = None,
        client: Any = None,
        capture: SearchCapture | None = None,
        log: Callable[[str], None] = lambda _msg: None,
    ) -> None:
        self.capture = capture
        self.log = log
        self.last_usage = SearchUsage()
        #: The most recent response body, untouched — same purpose as the
        #: Anthropic provider's `last_raw_message`: when the wire disagrees
        #: with the documented shape, the evidence is inspectable in the same
        #: run that hit it.
        self.last_raw_payload: Any = None
        self._client = client
        self._owns_client = client is None
        self._api_key = api_key or os.environ.get("EXA_API_KEY", "")
        if client is None and not self._api_key:
            raise SearchError(
                "EXA_API_KEY is not set, so the exa search provider cannot run. Set "
                "it, or unset SEARCH_PROVIDER to use anthropic_search, or run with "
                "--no-search."
            )

    def _ensure_client(self) -> Any:
        """Construct the HTTP client on first use, never at import or __init__.

        The same seam AnthropicSearchProvider has, for the same two reasons: a
        test that never searches cannot accidentally need a key, and the
        conftest guard patches this method so nothing in the suite can reach
        api.exa.ai.
        """
        if self._client is None:
            import httpx

            self._client = httpx.Client(timeout=30.0, headers={"x-api-key": self._api_key})
        return self._client

    def _post(self, endpoint: str, body: dict[str, Any], label: str) -> Any:
        client = self._ensure_client()
        try:
            resp = client.post(endpoint, json=body)
        except Exception as exc:
            raise SearchError(f"{label} failed: {exc}") from exc
        if resp.status_code >= 400:
            raise SearchError(f"{label} failed: exa returned HTTP {resp.status_code}")
        try:
            return resp.json()
        except Exception as exc:
            raise SearchError(f"{label} returned unreadable JSON: {exc}") from exc

    def search(self, query: str, limit: int) -> list[SearchResult]:
        body = {
            "query": query,
            "numResults": limit,
            "contents": {"text": True},
            "excludeDomains": list(PEOPLE_SEARCH_HOSTS),
        }
        payload = self._post(EXA_SEARCH_ENDPOINT, body, f"exa search for {query!r}")
        return self._absorb(query, payload, limit)

    def find_similar(self, url: str, limit: int) -> list[SearchResult]:
        body = {
            "url": url,
            "numResults": limit,
            "contents": {"text": True},
            "excludeDomains": list(PEOPLE_SEARCH_HOSTS),
        }
        payload = self._post(EXA_FIND_SIMILAR_ENDPOINT, body, f"exa find_similar for {url}")
        return self._absorb(f"more like {url}", payload, limit)

    def _absorb(self, label: str, payload: Any, limit: int) -> list[SearchResult]:
        """Parse one response and set `last_usage`, identically for both calls."""
        self.last_raw_payload = payload
        results, errors = results_from_exa_payload(payload, limit)
        self.last_usage = SearchUsage(
            searches=1, cost_dollars=exa_cost_dollars(payload), errors=errors
        )
        for code in errors:
            self.log(f"  [search] {label}: {code}")
        if self.capture is not None:
            self.capture.record(
                query=label,
                provider=self.name,
                model="",
                message=payload,
                results=[r.model_dump(mode="json") for r in results],
                usage=self.last_usage,
            )
        return results

    def close(self) -> None:
        if self._owns_client and self._client is not None:
            close = getattr(self._client, "close", None)
            if close is not None:
                close()
            self._client = None


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


def resolve_provider_name(name: str | None = None) -> str:
    """The provider a run would use: explicit name, SEARCH_PROVIDER, default.

    Split out of `get_search_provider` for the dry-run estimator, which needs
    the configured provider's *rate* without constructing the provider —
    construction requires the vendor's API key, and an estimate must not.
    """
    return (name or os.environ.get("SEARCH_PROVIDER") or "anthropic_search").strip()


def get_search_provider(name: str | None = None, **kwargs: Any) -> SearchProvider:
    name = resolve_provider_name(name)
    if name not in PROVIDERS:
        raise SearchError(
            f"Unknown SEARCH_PROVIDER {name!r}. Known: {', '.join(sorted(PROVIDERS))}"
        )
    provider: SearchProvider = PROVIDERS[name](**kwargs)
    return provider
