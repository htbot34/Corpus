"""Time-window sliding ingestion.

Two documented provider regressions shape this file. Naive cursor pagination
will hang or silently return partial data:

1. Cursor pagination is unreliable on historical data. On queries reaching
   older tweets — especially 2019 through 2022 — the API sometimes returns the
   same tweets again under a different cursor value, so a cursor loop never
   terminates. So: do not paginate deep history by cursor. Slide a fixed time
   window backwards instead, and cursor-paginate only *within* a window, capped
   by `max_pages`. Deduplicate by tweet id across windows regardless, and check
   that the dedupe rate stays low — a high rate means the window logic broke.

2. Some time windows return empty despite containing tweets. An empty window is
   therefore not the end of history. Track consecutive empties and only stop
   after `empty_window_tolerance` of them. Every empty window is logged so a
   systematic gap is visible rather than silent.

Note `since_time:` / `until_time:` (unix seconds) rather than `since:` / `until:`
— the date-level operators are no longer honoured by the underlying index and
return empty or unbounded results.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable

from ..budget import BudgetExceeded
from .client import XClient, _tweet_id, parse_created_at

# X launched 2006-03-21; nothing exists before this.
PLATFORM_EPOCH = int(datetime(2006, 3, 21, tzinfo=timezone.utc).timestamp())

# Above this, the window logic is suspect rather than merely inefficient.
DEDUPE_ALARM_RATE = 0.25


@dataclass
class IngestStats:
    windows: int = 0
    pages: int = 0
    fetched: int = 0  # raw tweets returned, including repeats
    unique: int = 0
    duplicates: int = 0
    empty_windows: int = 0
    max_consecutive_empty: int = 0
    empty_window_ranges: list[tuple[str, str]] = field(default_factory=list)
    cursor_repeat_breaks: int = 0  # failure mode 1, caught
    stop_reason: str = ""
    integrity_warning: str = ""

    @property
    def dedupe_rate(self) -> float:
        return (self.duplicates / self.fetched) if self.fetched else 0.0

    def as_dict(self) -> dict[str, Any]:
        return {
            "windows": self.windows,
            "pages": self.pages,
            "fetched": self.fetched,
            "unique": self.unique,
            "duplicates": self.duplicates,
            "dedupe_rate": round(self.dedupe_rate, 4),
            "empty_windows": self.empty_windows,
            "max_consecutive_empty": self.max_consecutive_empty,
            "empty_window_ranges": self.empty_window_ranges,
            "cursor_repeat_breaks": self.cursor_repeat_breaks,
            "stop_reason": self.stop_reason,
            "integrity_warning": self.integrity_warning,
        }


def _iso(ts: int) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d")


def ingest_timeline(
    client: XClient,
    handle: str,
    *,
    since: datetime | None = None,
    until: datetime | None = None,
    max_posts: int = 3000,
    window_days: int = 30,
    empty_window_tolerance: int = 3,
    max_pages: int = 20,
    include_replies: bool = True,
    log: Callable[[str], None] = print,
) -> tuple[list[dict[str, Any]], IngestStats]:
    """Walk a handle's history backwards in fixed windows.

    Stops on: `since` reached, `max_posts` hit, budget exhausted, or
    `empty_window_tolerance` consecutive empty windows exceeded.
    Returns (raw tweets newest-first, stats).
    """
    handle = handle.lstrip("@")
    stats = IngestStats()
    seen: dict[str, dict[str, Any]] = {}

    until_ts = int((until or datetime.now(tz=timezone.utc)).timestamp())
    floor_ts = int(since.timestamp()) if since else PLATFORM_EPOCH
    window_seconds = window_days * 24 * 3600
    consecutive_empty = 0

    try:
        while True:
            if until_ts <= floor_ts:
                stats.stop_reason = "reached --since" if since else "reached platform epoch"
                break
            if len(seen) >= max_posts:
                stats.stop_reason = f"reached --max-posts ({max_posts})"
                break

            # since_time is inclusive, until_time is exclusive.
            since_ts = max(floor_ts, until_ts - window_seconds)
            if since_ts >= until_ts:
                stats.stop_reason = "window collapsed"
                break

            query = f"from:{handle} since_time:{since_ts} until_time:{until_ts}"
            if not include_replies:
                query += " -filter:replies"

            stats.windows += 1
            window_new = 0
            window_earliest: int | None = None
            cursor: str | None = None
            seen_cursors: set[str] = set()
            # True only if we walked this window to its natural end. If we bailed
            # out on a repeating cursor or ran out of pages, the bottom of the
            # window is unexplored and we must not skip past it.
            window_complete = False

            for _page in range(max_pages):
                tweets, next_cursor, has_next = client.advanced_search(query, cursor)
                stats.pages += 1
                stats.fetched += len(tweets)

                page_new = 0
                for raw in tweets:
                    tid = _tweet_id(raw)
                    if not tid:
                        continue
                    ts = int(
                        parse_created_at(
                            raw.get("createdAt") or raw.get("created_at") or raw.get("timestamp")
                        ).timestamp()
                    )
                    window_earliest = ts if window_earliest is None else min(window_earliest, ts)
                    if tid in seen:
                        stats.duplicates += 1
                        continue
                    seen[tid] = raw
                    page_new += 1
                    window_new += 1

                if len(seen) >= max_posts:
                    break
                if not tweets:
                    window_complete = True
                    break
                if page_new == 0:
                    # Failure mode 1: the cursor advanced but the payload did
                    # not. Continuing here is the infinite loop.
                    stats.cursor_repeat_breaks += 1
                    log(
                        f"  [cursor-repeat] {_iso(since_ts)}..{_iso(until_ts)} returned "
                        f"{len(tweets)} tweets, all already seen — abandoning this window's cursor"
                    )
                    break
                if not has_next or not next_cursor:
                    window_complete = True
                    break
                if next_cursor in seen_cursors:
                    stats.cursor_repeat_breaks += 1
                    log(f"  [cursor-loop] repeated cursor in {_iso(since_ts)}..{_iso(until_ts)}")
                    break
                seen_cursors.add(next_cursor)
                cursor = next_cursor

            if window_new == 0 and window_earliest is None:
                # Failure mode 2: an empty window is not the end of history.
                consecutive_empty += 1
                stats.empty_windows += 1
                stats.max_consecutive_empty = max(stats.max_consecutive_empty, consecutive_empty)
                stats.empty_window_ranges.append((_iso(since_ts), _iso(until_ts)))
                log(
                    f"  [empty] {_iso(since_ts)}..{_iso(until_ts)} returned nothing "
                    f"({consecutive_empty}/{empty_window_tolerance} consecutive)"
                )
                if consecutive_empty >= empty_window_tolerance:
                    stats.stop_reason = (
                        f"{consecutive_empty} consecutive empty windows "
                        f"(--empty-window-tolerance {empty_window_tolerance})"
                    )
                    break
                until_ts = since_ts
                continue

            consecutive_empty = 0
            log(
                f"  {_iso(since_ts)}..{_iso(until_ts)}: +{window_new} new "
                f"(total {len(seen)}, ${client.budget.total:.4f})"
            )

            # Where the next window ends.
            #
            # If we walked this window to its end, everything down to since_ts is
            # accounted for and we drop straight to the window floor — no overlap,
            # nothing re-read, nothing paid for twice.
            #
            # If we bailed out early (repeating cursor, or max_pages), the region
            # below the oldest tweet we saw is unexplored. Then we deliberately
            # re-cover it by resuming at earliest_seen - 1, accepting some
            # duplicate reads in exchange for not punching a hole in the history.
            # The dedupe counter makes that cost visible.
            if window_complete or window_earliest is None:
                next_until = since_ts
            else:
                next_until = window_earliest - 1
            until_ts = min(next_until, until_ts - 1)

    except BudgetExceeded as exc:
        stats.stop_reason = f"budget exhausted: {exc}"
        log(f"  [budget] {exc}")

    if not stats.stop_reason:
        stats.stop_reason = "completed"

    stats.unique = len(seen)
    if stats.fetched > 100 and stats.dedupe_rate > DEDUPE_ALARM_RATE:
        stats.integrity_warning = (
            f"dedupe rate {stats.dedupe_rate:.1%} exceeds {DEDUPE_ALARM_RATE:.0%} — "
            "the window logic likely broke and coverage may be incomplete"
        )
        log(f"  [WARNING] {stats.integrity_warning}")

    ordered = sorted(
        seen.values(),
        key=lambda r: parse_created_at(
            r.get("createdAt") or r.get("created_at") or r.get("timestamp")
        ),
        reverse=True,
    )
    return ordered[:max_posts], stats


def ingest_recent(
    client: XClient,
    handle: str,
    *,
    max_posts: int = 200,
    max_pages: int = 10,
    log: Callable[[str], None] = print,
) -> tuple[list[dict[str, Any]], IngestStats]:
    """Recent timeline via /user/last_tweets.

    Cheaper and simpler than search for a shallow pull; cursor pagination is
    reliable here because it never reaches the historical index.
    """
    handle = handle.lstrip("@")
    stats = IngestStats()
    seen: dict[str, dict[str, Any]] = {}
    cursor: str | None = None
    try:
        for _ in range(max_pages):
            tweets, next_cursor, has_next = client.last_tweets(handle, cursor)
            stats.pages += 1
            stats.fetched += len(tweets)
            new = 0
            for raw in tweets:
                tid = _tweet_id(raw)
                if not tid:
                    continue
                if tid in seen:
                    stats.duplicates += 1
                    continue
                seen[tid] = raw
                new += 1
            log(f"  last_tweets page {stats.pages}: +{new} (total {len(seen)})")
            if len(seen) >= max_posts or not has_next or not next_cursor or new == 0:
                break
            cursor = next_cursor
    except BudgetExceeded as exc:
        stats.stop_reason = f"budget exhausted: {exc}"
        log(f"  [budget] {exc}")

    stats.unique = len(seen)
    stats.stop_reason = stats.stop_reason or "completed"
    ordered = sorted(
        seen.values(),
        key=lambda r: parse_created_at(
            r.get("createdAt") or r.get("created_at") or r.get("timestamp")
        ),
        reverse=True,
    )
    return ordered[:max_posts], stats
