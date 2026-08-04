"""Provider-agnostic X access.

We do not use the official X API. As of February 2026 X moved new developers to
pay-per-use at $0.005 per post read, killed the free tier, and gated
full-archive search behind Enterprise at roughly $42,000/month. The official
/2/users/:id/tweets endpoint also caps at ~3,200 posts and in practice drops
next_token far earlier on high-volume accounts.

twitterapi.io is the default: ~$0.15 per 1,000 tweets, ~$0.18 per 1,000
profiles, no 3,200 cap, reaches back to the early platform years.

To add a provider: implement XProvider and register it in PROVIDERS. Nothing
above this file knows which provider is in use.
"""

from __future__ import annotations

import os
import random
import time
from collections.abc import Callable
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any, Protocol, runtime_checkable

import httpx

from ..redact import RedactingError
from ..redact import redact as _redact
from .capture import RawCapture

TWITTERAPI_IO_BASE = "https://api.twitterapi.io"

# Maximum ids per /twitter/tweets call. MEASURED, not documented: a live run on
# 2026-08-02 failed hydration with
#
#   400 {"detail":"max 50 tweet_ids per request, please batch into multiple calls"}
#
# after batching at 100, which was the documented figure. The cap is enforced
# client-side as well as respected by the chunking in client.py, so a caller
# reaching the provider directly gets a clear error rather than a 400.
BATCH_LOOKUP_MAX = 50

# Minimum seconds between requests to the provider. MEASURED, not documented:
# a live run on 2026-08-04 died on
#
#   429 {"error":"Too Many Requests","message":"For free-tier users, the QPS
#   limit is one request every 5 seconds."}
#
# after burning all five reactive retries on a limit that fires on every call.
# The provider's docs say nothing about request pacing, so — like
# BATCH_LOOKUP_MAX above — this records the real shape rather than the
# documented one. Overridable with X_MIN_REQUEST_INTERVAL (seconds), because
# paid tiers are not subject to the free-tier rule; 0 disables the throttle.
MIN_REQUEST_INTERVAL_SECONDS = 5.0

# Raw page: (tweets, next_cursor, has_next_page)
Page = tuple[list[dict[str, Any]], str | None, bool]

__all__ = [
    "PROVIDERS",
    "Page",
    "ProviderError",
    "RawCapture",
    "ScopeViolation",
    "TwitterApiIoProvider",
    "XProvider",
    "_redact",
    "get_provider",
]


# -- retry policy ----------------------------------------------------------
# Five attempts, full jitter, no single sleep over a minute, and the server's
# own instruction wins whenever it gives one.

MAX_ATTEMPTS = 5
MAX_SLEEP_SECONDS = 60.0
BASE_BACKOFF_SECONDS = 1.0

# Headers a provider may use to say when to come back. Checked in this order.
RETRY_AFTER_HEADERS = ("retry-after",)
RATE_LIMIT_RESET_HEADERS = (
    "x-rate-limit-reset",
    "x-ratelimit-reset",
    "ratelimit-reset",
)

# Phrases that mean "your balance is gone", as opposed to "slow down". A spent
# balance does not clear by waiting, so it must fail fast with a message that
# says what to do.
_QUOTA_EXHAUSTED_MARKERS = (
    "insufficient",
    "balance",
    "credit",
    "quota exceeded",
    "out of credits",
    "payment required",
    "top up",
    "recharge",
)


def _backoff_delay(attempt: int) -> float:
    """Exponential backoff with full jitter, capped.

    Full jitter (uniform over [0, backoff]) rather than a fixed 2**attempt:
    with MAP_CONCURRENCY parallel callers, undithered backoff makes every
    retry fire at the same instant, so the thundering herd that caused the
    rate limit reconvenes precisely when the window reopens.
    """
    ceiling = min(BASE_BACKOFF_SECONDS * (2**attempt), MAX_SLEEP_SECONDS)
    return random.uniform(0.0, ceiling)


def _server_directed_delay(resp: httpx.Response) -> float | None:
    """Seconds to wait according to the response, or None if it does not say.

    Retry-After is either a delay in seconds or an HTTP date. Rate-limit reset
    headers are a unix timestamp. Both shapes appear in the wild; which one
    twitterapi.io actually sends is UNVERIFIED (docs/wire-contract.md), so all
    of them are handled rather than guessed at.
    """
    for name in RETRY_AFTER_HEADERS:
        raw = resp.headers.get(name)
        if not raw:
            continue
        try:
            return max(0.0, float(raw.strip()))
        except ValueError:
            pass
        try:
            when = parsedate_to_datetime(raw.strip())
            if when.tzinfo is None:
                when = when.replace(tzinfo=timezone.utc)
            return max(0.0, (when - datetime.now(tz=timezone.utc)).total_seconds())
        except (TypeError, ValueError):
            pass

    for name in RATE_LIMIT_RESET_HEADERS:
        raw = resp.headers.get(name)
        if not raw:
            continue
        try:
            reset = float(raw.strip())
        except ValueError:
            continue
        # A small number is a delta; a large one is an absolute unix time.
        # 10^6 seconds is ~11 days as a delta and 1970 as a timestamp, so the
        # split is unambiguous either way.
        delta = reset if reset < 1_000_000 else reset - datetime.now(tz=timezone.utc).timestamp()
        return max(0.0, delta)
    return None


def _retry_delay(resp: httpx.Response, attempt: int) -> float:
    """How long to wait before retrying this response.

    The server's instruction wins when there is one — it knows when the window
    actually reopens and we are guessing. Jitter is still added on top so that
    concurrent callers handed the same Retry-After do not all wake together.
    """
    directed = _server_directed_delay(resp)
    if directed is None:
        return _backoff_delay(attempt)
    jitter = random.uniform(0.0, min(1.0, directed * 0.1))
    return min(directed + jitter, MAX_SLEEP_SECONDS)


def _is_quota_exhausted(resp: httpx.Response) -> bool:
    """Distinguish a spent balance from ordinary rate limiting."""
    body = (resp.text or "")[:500].lower()
    return any(marker in body for marker in _QUOTA_EXHAUSTED_MARKERS)


def _min_request_interval_from_env() -> float:
    """The configured request spacing, in seconds.

    A malformed value fails loudly rather than silently falling back: a paid
    key whose typo'd override was ignored would throttle at 5s forever, and
    nothing in the output would say why the run is slow.
    """
    raw = (os.environ.get("X_MIN_REQUEST_INTERVAL") or "").strip()
    if not raw:
        return MIN_REQUEST_INTERVAL_SECONDS
    try:
        value = float(raw)
    except ValueError as exc:
        raise ProviderError(
            f"X_MIN_REQUEST_INTERVAL must be a number of seconds, got {raw!r}"
        ) from exc
    return max(0.0, value)


def _is_retryable_transport_error(exc: Exception) -> bool:
    """Is this transport failure worth trying again?

    Timeouts and dropped connections are flakes. A proxy refusing CONNECT, or
    an unresolvable host, will fail identically every time — retrying only adds
    the backoff total to how long the user waits for the same error.
    """
    if isinstance(exc, httpx.ProxyError):
        return False
    if isinstance(exc, httpx.UnsupportedProtocol):
        return False
    if isinstance(exc, httpx.ConnectError):
        text = str(exc).lower()
        return not any(
            marker in text
            for marker in ("name or service not known", "nodename nor servname", "403")
        )
    return isinstance(exc, (httpx.TimeoutException, httpx.NetworkError, httpx.RemoteProtocolError))


class ProviderError(RedactingError):
    """A provider call failed.

    Messages here routinely quote an upstream response body and an upstream URL,
    either of which can contain the API key (a 4xx that echoes the request, a
    base URL carrying the key as a query parameter). ``RedactingError`` masks
    secrets at construction, so every ``raise ProviderError(...)`` in this file —
    and any added later — is safe to print, log, or paste into a bug report.
    """


class ScopeViolation(RedactingError):
    """Raised when a request would require private data or authentication bypass."""


@runtime_checkable
class XProvider(Protocol):
    """The whole surface the rest of the tool depends on."""

    name: str

    def user_info(self, handle: str) -> dict[str, Any]:
        """Public profile for a handle. One profile-read of billing."""

    def last_tweets(self, handle: str, cursor: str | None = None) -> Page:
        """Recent timeline page."""

    def advanced_search(self, query: str, cursor: str | None = None) -> Page:
        """Full-archive search. `query` uses X search syntax."""

    def tweets_by_ids(self, ids: list[str]) -> list[dict[str, Any]]:
        """Batch lookup for hydrating reply parents and quote targets."""

    def close(self) -> None: ...


class TwitterApiIoProvider:
    """twitterapi.io. Key from X_API_KEY, sent as the X-API-Key header."""

    name = "twitterapi_io"

    def __init__(
        self,
        api_key: str | None = None,
        base_url: str | None = None,
        timeout: float = 45.0,
        max_retries: int = MAX_ATTEMPTS,
        capture: RawCapture | None = None,
        log: Callable[[str], None] = lambda _msg: None,
        min_request_interval: float | None = None,
    ) -> None:
        self.capture = capture
        # Every retry is logged with its attempt number and reason; silent
        # retries are how a run appears to hang. Wrapped in _redact because
        # these lines interpolate exception strings, and an httpx error carries
        # the request URL — which is one proxy misconfiguration away from
        # carrying the key.
        self._log_sink = log
        self.api_key = api_key or os.environ.get("X_API_KEY", "")
        if capture is not None:
            # Explicit-key construction bypasses the environment, so tell the
            # capture directly rather than hoping os.environ agrees.
            capture.add_secret(self.api_key)
        if not self.api_key:
            raise ProviderError(
                "X_API_KEY is not set. Get a key at https://twitterapi.io and put it "
                "in .env (see .env.example), or run with --offline to work from cache."
            )
        self.base_url = (base_url or os.environ.get("X_BASE_URL") or TWITTERAPI_IO_BASE).rstrip("/")
        self.max_retries = max_retries
        self.min_request_interval = (
            _min_request_interval_from_env()
            if min_request_interval is None
            else max(0.0, min_request_interval)
        )
        # Monotonic time before which no request may start. 0.0 means the
        # first request goes immediately.
        self._next_request_at = 0.0
        self.client = httpx.Client(
            base_url=self.base_url,
            headers={"X-API-Key": self.api_key},
            timeout=timeout,
        )

    def log(self, message: str) -> None:
        """Emit a line through the configured sink, redacted.

        Retry lines interpolate exception strings, and an httpx error carries
        the request URL — one proxy misconfiguration away from carrying the key.
        """
        self._log_sink(_redact(message))

    # -- transport --------------------------------------------------------

    def _throttle(self) -> None:
        """Space requests out instead of walking into the provider's QPS limit.

        The reactive backoff below handles a 429 after it happens; this stops
        the free tier's one-request-per-5s limit from firing on every call and
        burning the retry budget on a wall we know the height of. Start to
        start, on the monotonic clock, and in front of every request this
        provider makes — retries included — because the limit is on requests,
        not on endpoints or on successes.
        """
        if self.min_request_interval <= 0:
            return
        wait = self._next_request_at - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        self._next_request_at = time.monotonic() + self.min_request_interval

    def _get(self, path: str, params: dict[str, Any]) -> dict[str, Any]:
        params = {k: v for k, v in params.items() if v is not None}
        last_exc: Exception | None = None
        for attempt in range(self.max_retries):
            self._throttle()
            try:
                resp = self.client.get(path, params=params)
            except httpx.HTTPError as exc:
                # Not every transport error is a flake. A proxy policy denial,
                # a DNS failure, or an unreachable host will fail identically
                # five times in a row, so retrying just adds 30s to the error.
                if not _is_retryable_transport_error(exc):
                    raise ProviderError(
                        f"{path}: {type(exc).__name__}: {exc}. This is not a "
                        f"transient failure — not retrying."
                    ) from exc
                last_exc = exc
                delay = _backoff_delay(attempt)
                self.log(
                    f"  [retry {attempt + 1}/{self.max_retries}] {path}: "
                    f"{type(exc).__name__}: {exc} — sleeping {delay:.1f}s"
                )
                time.sleep(delay)
                continue
            # Capture before anything reads the payload, including the status
            # checks below: a 4xx body is often the most informative capture
            # there is, and it is exactly what gets lost when a run aborts.
            if self.capture is not None:
                self.capture.record(
                    method="GET",
                    url=str(resp.request.url),
                    path=path,
                    params=params,
                    status_code=resp.status_code,
                    headers=dict(resp.headers),
                    body_bytes=resp.content,
                )
            if resp.status_code == 429 or resp.status_code >= 500:
                # A 429 is not always retryable. Rate limiting clears on its
                # own; a spent balance does not, and burning five attempts on it
                # just delays a message the user needs immediately.
                if resp.status_code == 429 and _is_quota_exhausted(resp):
                    raise ProviderError(
                        f"429 from {path}, and the response says the account "
                        f"balance is exhausted rather than merely rate-limited. "
                        f"Retrying will not help — top up at https://twitterapi.io. "
                        f"Response: {resp.text[:200]}"
                    )
                last_exc = ProviderError(f"{resp.status_code} from {path}: {resp.text[:200]}")
                delay = _retry_delay(resp, attempt)
                self.log(
                    f"  [retry {attempt + 1}/{self.max_retries}] {path}: "
                    f"HTTP {resp.status_code} — sleeping {delay:.1f}s"
                    + (" (server-directed)" if _server_directed_delay(resp) is not None else "")
                )
                time.sleep(delay)
                continue
            if resp.status_code == 401 or resp.status_code == 403:
                raise ProviderError(
                    f"{resp.status_code} from {path}. Check X_API_KEY, and note that "
                    "protected/private accounts are out of scope for this tool."
                )
            if resp.status_code >= 400:
                raise ProviderError(f"{resp.status_code} from {path}: {resp.text[:400]}")
            try:
                payload: dict[str, Any] = resp.json()
                return payload
            except ValueError as exc:
                raise ProviderError(f"non-JSON response from {path}: {resp.text[:200]}") from exc
        raise ProviderError(f"{path} failed after {self.max_retries} attempts: {last_exc}")

    # -- response shape ---------------------------------------------------

    @staticmethod
    def _tweets_from(payload: dict[str, Any]) -> list[dict[str, Any]]:
        """Pull the tweet list out of a response.

        Shapes differ across twitterapi.io endpoints (`data.tweets` on
        last_tweets, top-level `tweets` on advanced_search) and have moved
        before, so probe the known locations rather than assuming one.
        """
        for candidate in (
            payload.get("tweets"),
            (payload.get("data") or {}).get("tweets")
            if isinstance(payload.get("data"), dict)
            else None,
            payload.get("data") if isinstance(payload.get("data"), list) else None,
            payload.get("results"),
        ):
            if isinstance(candidate, list):
                return [t for t in candidate if isinstance(t, dict)]
        return []

    @staticmethod
    def _cursor_from(payload: dict[str, Any]) -> tuple[str | None, bool]:
        cursor = payload.get("next_cursor") or payload.get("nextCursor")
        has_next = payload.get("has_next_page")
        if has_next is None:
            has_next = payload.get("hasNextPage")
        if has_next is None:
            has_next = bool(cursor)
        return (cursor or None), bool(has_next)

    # -- endpoints --------------------------------------------------------

    def user_info(self, handle: str) -> dict[str, Any]:
        payload = self._get("/twitter/user/info", {"userName": handle.lstrip("@")})
        data = payload.get("data")
        if isinstance(data, dict):
            return data
        return payload

    def last_tweets(self, handle: str, cursor: str | None = None) -> Page:
        payload = self._get(
            "/twitter/user/last_tweets",
            {"userName": handle.lstrip("@"), "cursor": cursor},
        )
        next_cursor, has_next = self._cursor_from(payload)
        return self._tweets_from(payload), next_cursor, has_next

    def advanced_search(self, query: str, cursor: str | None = None) -> Page:
        payload = self._get(
            "/twitter/tweet/advanced_search",
            {"query": query, "queryType": "Latest", "cursor": cursor},
        )
        next_cursor, has_next = self._cursor_from(payload)
        return self._tweets_from(payload), next_cursor, has_next

    def tweets_by_ids(self, ids: list[str]) -> list[dict[str, Any]]:
        if not ids:
            return []
        if len(ids) > BATCH_LOOKUP_MAX:
            raise ProviderError(
                f"batch tweet lookup takes at most {BATCH_LOOKUP_MAX} ids per call, got {len(ids)}"
            )
        payload = self._get("/twitter/tweets", {"tweet_ids": ",".join(ids)})
        return self._tweets_from(payload)

    def close(self) -> None:
        self.client.close()


class _StubProvider:
    """Base for alternates we have not implemented."""

    name = "stub"
    _endpoints = ""

    def __init__(self, *_: Any, **__: Any) -> None:
        raise NotImplementedError(
            f"The {self.name!r} provider is not implemented. To add it, implement "
            f"XProvider in corpus/x/providers.py with: {self._endpoints} — then "
            f"register the class in PROVIDERS. Nothing outside this file needs to change."
        )

    # These exist only so the class satisfies XProvider structurally. __init__
    # always raises, so none of them is ever reachable — but a body of `...`
    # reads to a type checker as "returns None", which is a lie about the
    # signature. Raising says the same thing honestly.
    def user_info(self, handle: str) -> dict[str, Any]:
        raise NotImplementedError(self.name)

    def last_tweets(self, handle: str, cursor: str | None = None) -> Page:
        raise NotImplementedError(self.name)

    def advanced_search(self, query: str, cursor: str | None = None) -> Page:
        raise NotImplementedError(self.name)

    def tweets_by_ids(self, ids: list[str]) -> list[dict[str, Any]]:
        raise NotImplementedError(self.name)

    def close(self) -> None:
        return None


class ApiDanceProvider(_StubProvider):
    name = "apidance"
    _endpoints = (
        "user_info via GET /1.1/users/show.json?screen_name=, "
        "advanced_search via GET /sapi/SearchTimeline?rawQuery=&product=Latest, "
        "tweets_by_ids via GET /sapi/TweetDetail (one id per call — no batch endpoint, "
        "so hydration cost will be ~100x twitterapi.io's), "
        "auth header 'apikey: <APIDANCE_KEY>'"
    )


class SocialDataProvider(_StubProvider):
    name = "socialdata"
    _endpoints = (
        "user_info via GET /twitter/user/<handle>, "
        "advanced_search via GET /twitter/search?query=&type=Latest (cursor param), "
        "tweets_by_ids via GET /twitter/tweets/<id> (one id per call), "
        "auth header 'Authorization: Bearer <SOCIALDATA_KEY>'"
    )


PROVIDERS: dict[str, type] = {
    "twitterapi_io": TwitterApiIoProvider,
    "apidance": ApiDanceProvider,
    "socialdata": SocialDataProvider,
}


def get_provider(name: str | None = None, **kwargs: Any) -> XProvider:
    name = (name or os.environ.get("X_PROVIDER") or "twitterapi_io").strip()
    if name not in PROVIDERS:
        raise ProviderError(f"Unknown X_PROVIDER {name!r}. Known: {', '.join(sorted(PROVIDERS))}")
    provider: XProvider = PROVIDERS[name](**kwargs)
    return provider
