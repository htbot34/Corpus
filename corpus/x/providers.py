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
import time
from typing import Any, Protocol, runtime_checkable

import httpx

TWITTERAPI_IO_BASE = "https://api.twitterapi.io"

# Raw page: (tweets, next_cursor, has_next_page)
Page = tuple[list[dict[str, Any]], str | None, bool]


class ProviderError(RuntimeError):
    pass


class ScopeViolation(RuntimeError):
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
        max_retries: int = 4,
    ) -> None:
        self.api_key = api_key or os.environ.get("X_API_KEY", "")
        if not self.api_key:
            raise ProviderError(
                "X_API_KEY is not set. Get a key at https://twitterapi.io and put it "
                "in .env (see .env.example), or run with --offline to work from cache."
            )
        self.base_url = (base_url or os.environ.get("X_BASE_URL") or TWITTERAPI_IO_BASE).rstrip("/")
        self.max_retries = max_retries
        self.client = httpx.Client(
            base_url=self.base_url,
            headers={"X-API-Key": self.api_key},
            timeout=timeout,
        )

    # -- transport --------------------------------------------------------

    def _get(self, path: str, params: dict[str, Any]) -> dict[str, Any]:
        params = {k: v for k, v in params.items() if v is not None}
        last_exc: Exception | None = None
        for attempt in range(self.max_retries):
            try:
                resp = self.client.get(path, params=params)
            except httpx.HTTPError as exc:  # network flake
                last_exc = exc
                time.sleep(2**attempt)
                continue
            if resp.status_code == 429 or resp.status_code >= 500:
                # Rate limit / upstream wobble: back off and retry.
                last_exc = ProviderError(f"{resp.status_code} from {path}: {resp.text[:200]}")
                time.sleep(2**attempt)
                continue
            if resp.status_code == 401 or resp.status_code == 403:
                raise ProviderError(
                    f"{resp.status_code} from {path}. Check X_API_KEY, and note that "
                    "protected/private accounts are out of scope for this tool."
                )
            if resp.status_code >= 400:
                raise ProviderError(f"{resp.status_code} from {path}: {resp.text[:400]}")
            try:
                return resp.json()
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
            (payload.get("data") or {}).get("tweets") if isinstance(payload.get("data"), dict) else None,
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
        if len(ids) > 100:
            raise ProviderError("batch tweet lookup takes at most 100 ids per call")
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

    def user_info(self, handle: str) -> dict[str, Any]: ...
    def last_tweets(self, handle: str, cursor: str | None = None) -> Page: ...
    def advanced_search(self, query: str, cursor: str | None = None) -> Page: ...
    def tweets_by_ids(self, ids: list[str]) -> list[dict[str, Any]]: ...
    def close(self) -> None: ...


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
        raise ProviderError(
            f"Unknown X_PROVIDER {name!r}. Known: {', '.join(sorted(PROVIDERS))}"
        )
    return PROVIDERS[name](**kwargs)  # type: ignore[return-value]
