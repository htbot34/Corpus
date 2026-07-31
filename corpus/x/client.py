"""Provider-agnostic X client: caching, billing, and raw-tweet normalization.

Everything above this module works in `Document`s. Everything below works in
whatever shape the provider returns. This file is the seam — if a provider's
JSON shape drifts, `normalize_tweet` is the only thing that needs to change.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any, Iterable

from ..budget import (
    Budget,
    X_COST_PER_PROFILE,
    X_MIN_CHARGE_PER_REQUEST,
    estimate_x_batch_cost,
    estimate_x_page_cost,
)
from ..cache import Cache
from ..models import Document
from .providers import Page, ProviderError, XProvider

_TCO = re.compile(r"https?://t\.co/\w+")
_SELF_HOSTS = ("twitter.com", "x.com", "mobile.twitter.com")

MEDIA_ONLY_MAX_CHARS = 15


def _first(d: dict[str, Any], *keys: str, default: Any = None) -> Any:
    for k in keys:
        if k in d and d[k] is not None:
            return d[k]
    return default


def parse_created_at(value: Any) -> datetime:
    """Parse the several timestamp formats seen in the wild, UTC-normalized."""
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(float(value), tz=timezone.utc)
    if not isinstance(value, str) or not value.strip():
        return datetime.now(tz=timezone.utc)
    text = value.strip()
    for fmt in ("%a %b %d %H:%M:%S %z %Y", "%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%d %H:%M:%S%z"):
        try:
            return datetime.strptime(text, fmt).astimezone(timezone.utc)
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).astimezone(timezone.utc)
    except ValueError:
        return datetime.now(tz=timezone.utc)


def _author_handle(raw: dict[str, Any]) -> str:
    author = raw.get("author") or raw.get("user") or {}
    if isinstance(author, dict):
        handle = _first(author, "userName", "screen_name", "username", default="")
        if handle:
            return str(handle).lstrip("@")
    return str(_first(raw, "screen_name", "username", default="")).lstrip("@")


def _tweet_url(raw: dict[str, Any]) -> str:
    url = _first(raw, "url", "twitterUrl", "tweet_url")
    if url:
        return str(url)
    handle = _author_handle(raw) or "i"
    return f"https://x.com/{handle}/status/{_tweet_id(raw)}"


def _tweet_id(raw: dict[str, Any]) -> str:
    return str(_first(raw, "id", "id_str", "tweet_id", default=""))


def _entities_urls(raw: dict[str, Any]) -> list[dict[str, Any]]:
    entities = raw.get("entities") or {}
    urls = entities.get("urls") if isinstance(entities, dict) else None
    return [u for u in urls or [] if isinstance(u, dict)]


def _has_media(raw: dict[str, Any]) -> bool:
    for key in ("extendedEntities", "extended_entities", "entities"):
        block = raw.get(key)
        if isinstance(block, dict) and block.get("media"):
            return True
    if raw.get("media"):
        return True
    return False


def _expand_links(text: str, raw: dict[str, Any]) -> tuple[str, list[str]]:
    """Replace t.co shorteners with their targets; collect outbound domains.

    Self-links (x.com/twitter.com) are dropped from `outbound_links`: they are
    quote-tweet and media references, not reading diet.
    """
    outbound: list[str] = []
    for entry in _entities_urls(raw):
        short = entry.get("url")
        expanded = entry.get("expanded_url") or entry.get("unwound_url") or short
        if not expanded:
            continue
        if short:
            text = text.replace(short, expanded)
        if not any(host in expanded for host in _SELF_HOSTS) and expanded not in outbound:
            outbound.append(expanded)
    return text, outbound


def normalize_tweet(raw: dict[str, Any]) -> Document:
    """One provider tweet dict -> one Document (pre-hydration)."""
    text = str(_first(raw, "text", "full_text", "fullText", default="") or "")
    retweeted = raw.get("retweeted_tweet") or raw.get("retweetedTweet")
    quoted = raw.get("quoted_tweet") or raw.get("quotedTweet")
    in_reply_to_id = _first(raw, "inReplyToId", "in_reply_to_status_id_str", "in_reply_to_id")
    is_reply = bool(_first(raw, "isReply", "is_reply", default=False)) or bool(in_reply_to_id)

    if retweeted:
        kind = "repost"
    elif is_reply:
        kind = "reply"
    elif quoted:
        kind = "quote"
    else:
        kind = "original"

    body, outbound = _expand_links(text, raw)
    stripped = _TCO.sub("", body).strip()
    if len(stripped) < MEDIA_ONLY_MAX_CHARS and _has_media(raw) and kind != "repost":
        kind = "media_only"

    engagement = {
        "likes": int(_first(raw, "likeCount", "favorite_count", "likes", default=0) or 0),
        "replies": int(_first(raw, "replyCount", "reply_count", "replies", default=0) or 0),
        "reposts": int(_first(raw, "retweetCount", "retweet_count", "reposts", default=0) or 0),
        "quotes": int(_first(raw, "quoteCount", "quote_count", "quotes", default=0) or 0),
        "views": int(_first(raw, "viewCount", "view_count", "views", default=0) or 0),
    }

    return Document(
        source="x",
        source_id=_tweet_id(raw),
        url=_tweet_url(raw),
        author_handle=_author_handle(raw),
        published_at=parse_created_at(_first(raw, "createdAt", "created_at", "timestamp")),
        kind=kind,  # type: ignore[arg-type]
        body=body,
        engagement=engagement,
        outbound_links=outbound,
        part_count=1,
        raw=raw,
    )


class XClient:
    """Caching, billing wrapper around an XProvider."""

    def __init__(self, provider: XProvider, cache: Cache, budget: Budget) -> None:
        self.provider = provider
        self.cache = cache
        self.budget = budget
        # Reservations need a page size, and the documented value of 20 is
        # UNVERIFIED against the wire (docs/wire-contract.md). Rather than
        # trusting it, seed with it and then use what the provider actually
        # returns. A pessimistic estimate is not free: it refuses calls that
        # would have fit, which on a small budget means refusing everything.
        self._observed_page_sizes: list[int] = []

    def _page_reservation(self) -> float:
        """Worst-case cost of the next page, from observation where possible.

        Deliberately the largest page seen rather than the mean: a reservation
        that is usually right and occasionally low is a reservation that
        occasionally overshoots, which is the bug being fixed.
        """
        if not self._observed_page_sizes:
            return estimate_x_page_cost()
        return estimate_x_page_cost(max(self._observed_page_sizes))

    def _observe_page(self, count: int) -> None:
        self._observed_page_sizes.append(count)

    # -- profile ----------------------------------------------------------

    def user_info(self, handle: str) -> dict[str, Any]:
        key = f"profile:{handle.lower()}"
        cached = self.cache.get("x", key)
        if cached is not None:
            return cached
        if self.cache.offline:
            raise ProviderError(
                f"--offline: no cached profile for @{handle}. Run once online first."
            )
        # Pre-flight: refuse the call rather than making it and reporting the
        # overshoot afterwards.
        with self.budget.reserved_for(
            max(X_COST_PER_PROFILE, X_MIN_CHARGE_PER_REQUEST), "user/info"
        ) as reservation:
            info = self.provider.user_info(handle)
            charge = self.budget.charge_x_profiles("user/info", 1, note=handle)
            reservation.actual = charge.cost
        self.cache.put("x", key, info)
        return info

    # -- search / timeline ------------------------------------------------

    def advanced_search(self, query: str, cursor: str | None = None) -> Page:
        # A page is billed per tweet returned, which we cannot know until it
        # returns — so reserve a full page and reconcile down afterwards.
        with self.budget.reserved_for(
            self._page_reservation(), "tweet/advanced_search"
        ) as reservation:
            tweets, next_cursor, has_next = self.provider.advanced_search(query, cursor)
            self._observe_page(len(tweets))
            charge = self.budget.charge_x_tweets(
                "tweet/advanced_search", len(tweets), note=query[:80]
            )
            reservation.actual = charge.cost
        self._cache_tweets(tweets)
        return tweets, next_cursor, has_next

    def last_tweets(self, handle: str, cursor: str | None = None) -> Page:
        with self.budget.reserved_for(
            self._page_reservation(), "user/last_tweets"
        ) as reservation:
            tweets, next_cursor, has_next = self.provider.last_tweets(handle, cursor)
            self._observe_page(len(tweets))
            charge = self.budget.charge_x_tweets(
                "user/last_tweets", len(tweets), note=handle
            )
            reservation.actual = charge.cost
        self._cache_tweets(tweets)
        return tweets, next_cursor, has_next

    # -- hydration --------------------------------------------------------

    def tweets_by_ids(self, ids: Iterable[str]) -> dict[str, dict[str, Any]]:
        """Batch-fetch tweets by id, in groups of 100, cache-first.

        Hydrated tweets are cached permanently: an old tweet's text does not
        change, so paying for it twice is pure waste.
        """
        wanted = [i for i in dict.fromkeys(str(i) for i in ids) if i]
        found: dict[str, dict[str, Any]] = {}
        missing: list[str] = []
        for tid in wanted:
            cached = self.cache.get("x", f"tweet:{tid}")
            if cached is not None:
                found[tid] = cached
            else:
                missing.append(tid)

        if missing and self.cache.offline:
            # Not fatal: unavailable parents are marked "[unavailable]" upstream.
            return found

        for start in range(0, len(missing), 100):
            batch = missing[start : start + 100]
            # Reserve as though every id resolves; a deleted parent that comes
            # back empty simply reconciles down.
            with self.budget.reserved_for(
                estimate_x_batch_cost(len(batch)), "tweets (batch lookup)"
            ) as reservation:
                tweets = self.provider.tweets_by_ids(batch)
                charge = self.budget.charge_x_tweets(
                    "tweets (batch lookup)", len(tweets), note=f"hydrate x{len(batch)}"
                )
                reservation.actual = charge.cost
            for raw in tweets:
                tid = _tweet_id(raw)
                if tid:
                    found[tid] = raw
                    self.cache.put("x", f"tweet:{tid}", raw, permanent=True)
        return found

    # -- internals --------------------------------------------------------

    def _cache_tweets(self, tweets: list[dict[str, Any]]) -> None:
        for raw in tweets:
            tid = _tweet_id(raw)
            if tid:
                # permanent: a tweet we already paid to read never changes text.
                self.cache.put("x", f"tweet:{tid}", raw, permanent=True)

    def close(self) -> None:
        self.provider.close()
