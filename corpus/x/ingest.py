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
from .client import TimestampParseError, XClient, _tweet_id, parse_created_at
from .validate import validate_handle

# X launched 2006-03-21; nothing exists before this.
PLATFORM_EPOCH = int(datetime(2006, 3, 21, tzinfo=timezone.utc).timestamp())

# Above this, the window logic is suspect rather than merely inefficient.
DEDUPE_ALARM_RATE = 0.25

# Consecutive empty windows before the walk gives up.
#
# Was 3, which at the default 30-day window means any real silence longer than
# 90 days ends ingestion and discards everything older. That is the wrong
# default for the actual targets: founders and writers go quiet for a quarter
# routinely. An unnecessary empty window costs about $0.0002; a truncated
# history costs the entire analysis.
DEFAULT_EMPTY_WINDOW_TOLERANCE = 8

# How far back the adaptive probe looks when the tolerance is reached.
#
# Rather than guessing whether the silence is real, spend roughly one more call
# to find out: sweep the next twelve months in a single window. Anything found
# means the silence was not the end of history and normal windowing resumes
# from there. Nothing found turns a guess into evidence.
HIATUS_PROBE_DAYS = 365


class IngestCorruption(RuntimeError):
    """An impossible result that means the walk can no longer be trusted.

    Distinct from a provider error or a budget stop: those are expected
    conditions with partial results worth keeping. This one means a core
    assumption failed, and continuing spends money on data that cannot be
    reasoned about.
    """


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
    timestamp_errors: int = 0  # documents skipped for an unparseable timestamp
    timestamp_error_samples: list[str] = field(default_factory=list)
    hiatus_probes: int = 0
    hiatus_probes_productive: int = 0
    probe_recovered_ranges: list[tuple[str, str]] = field(default_factory=list)
    stopped_on_tolerance: bool = False
    # True when the final stop was preceded by a wide probe that found nothing.
    # That is the difference between "we gave up" and "we checked", and it is
    # what the report needs in order to be honest in both directions.
    probe_confirmed_end: bool = False
    last_date_reached: str = ""
    public_post_count: int | None = None  # statusesCount, for ingested-vs-total
    stop_reason: str = ""
    integrity_warning: str = ""

    @property
    def ingested_share(self) -> float | None:
        """Ingested count as a fraction of the profile's public post count.

        Surfaced so that "400 of 53,901" is obvious at a glance rather than
        something a reader has to infer from stop_reason.
        """
        if not self.public_post_count:
            return None
        return round(self.unique / self.public_post_count, 4)

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
            "timestamp_errors": self.timestamp_errors,
            "timestamp_error_samples": self.timestamp_error_samples,
            "hiatus_probes": self.hiatus_probes,
            "hiatus_probes_productive": self.hiatus_probes_productive,
            "probe_recovered_ranges": self.probe_recovered_ranges,
            "stopped_on_tolerance": self.stopped_on_tolerance,
            "probe_confirmed_end": self.probe_confirmed_end,
            "last_date_reached": self.last_date_reached,
            "public_post_count": self.public_post_count,
            "ingested_share": self.ingested_share,
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
    empty_window_tolerance: int = DEFAULT_EMPTY_WINDOW_TOLERANCE,
    max_pages: int = 20,
    include_replies: bool = True,
    probe_enabled: bool = True,
    public_post_count: int | None = None,
    log: Callable[[str], None] = print,
) -> tuple[list[dict[str, Any]], IngestStats]:
    """Walk a handle's history backwards in fixed windows.

    Stops on: `since` reached, `max_posts` hit, budget exhausted, or
    `empty_window_tolerance` consecutive empty windows exceeded.
    Returns (raw tweets newest-first, stats).
    """
    # Validated again here, not only at the CLI: this is a public function and
    # the handle goes straight into a query string below.
    handle = validate_handle(handle)
    stats = IngestStats(public_post_count=public_post_count)
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
                    # A malformed timestamp must not kill the run, and must not
                    # be allowed to poison window_earliest either — the old
                    # silent now() default made the walk advance one second per
                    # window while paying full price for every page.
                    try:
                        ts = int(
                            parse_created_at(
                                raw.get("createdAt")
                                or raw.get("created_at")
                                or raw.get("timestamp")
                            ).timestamp()
                        )
                    except TimestampParseError as exc:
                        stats.timestamp_errors += 1
                        if len(stats.timestamp_error_samples) < 5:
                            stats.timestamp_error_samples.append(f"{tid}: {exc.value!r}")
                        log(f"  [timestamp] skipping {tid}: {exc}")
                        continue
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
                    # Before giving up, spend roughly one more call to find out
                    # whether the silence is real. A lying empty window and a
                    # genuine hiatus are indistinguishable one window at a time
                    # — but not across a year at once.
                    probe_until = since_ts
                    probe_since = max(floor_ts, probe_until - HIATUS_PROBE_DAYS * 24 * 3600)
                    probed_ok = False
                    if probe_since < probe_until and probe_enabled:
                        stats.hiatus_probes += 1
                        log(
                            f"  [probe] {empty_window_tolerance} empty windows — "
                            f"sweeping {_iso(probe_since)}..{_iso(probe_until)} "
                            f"({HIATUS_PROBE_DAYS}d) before concluding the history ends here"
                        )
                        probe_query = f"from:{handle} since_time:{probe_since} until_time:{probe_until}"
                        if not include_replies:
                            probe_query += " -filter:replies"
                        probe_tweets, _pc, _ph = client.advanced_search(probe_query, None)
                        stats.pages += 1
                        stats.fetched += len(probe_tweets)
                        probe_latest: int | None = None
                        for raw in probe_tweets:
                            tid = _tweet_id(raw)
                            if not tid:
                                continue
                            try:
                                ts = int(
                                    parse_created_at(
                                        raw.get("createdAt")
                                        or raw.get("created_at")
                                        or raw.get("timestamp")
                                    ).timestamp()
                                )
                            except TimestampParseError as exc:
                                stats.timestamp_errors += 1
                                if len(stats.timestamp_error_samples) < 5:
                                    stats.timestamp_error_samples.append(
                                        f"{tid}: {exc.value!r}"
                                    )
                                continue
                            probe_latest = ts if probe_latest is None else max(probe_latest, ts)
                            if tid not in seen:
                                seen[tid] = raw

                        if probe_latest is not None:
                            probed_ok = True
                            stats.hiatus_probes_productive += 1
                            stats.probe_recovered_ranges.append(
                                (_iso(probe_since), _iso(probe_until))
                            )
                            log(
                                f"  [probe] found posts as recent as {_iso(probe_latest)} — "
                                f"the silence was not the end of history; resuming"
                            )
                            consecutive_empty = 0
                            # Resume normal windowing just below the newest post
                            # the probe found, so the region between it and the
                            # probe ceiling is not re-walked.
                            until_ts = min(probe_latest + 1, probe_until)
                            continue

                    stats.stop_reason = (
                        f"{consecutive_empty} consecutive empty windows "
                        f"(--empty-window-tolerance {empty_window_tolerance})"
                        + (
                            f", and a {HIATUS_PROBE_DAYS}-day probe back to "
                            f"{_iso(probe_since)} found nothing"
                            if stats.hiatus_probes and not probed_ok
                            else ""
                        )
                    )
                    stats.stopped_on_tolerance = True
                    stats.probe_confirmed_end = bool(stats.hiatus_probes) and not probed_ok
                    stats.last_date_reached = _iso(until_ts)
                    break
                until_ts = since_ts
                continue

            consecutive_empty = 0
            log(
                f"  {_iso(since_ts)}..{_iso(until_ts)}: +{window_new} new "
                f"(total {len(seen)}, ${client.budget.total:.4f})"
            )

            # A tweet older than the window's own ceiling is impossible: the
            # query bounded it. If this fires, either the provider ignored
            # until_time: or a timestamp is being misparsed — and both of those
            # corrupt the walk in ways that cost money quietly. Fail loudly.
            if window_earliest is not None and window_earliest > until_ts:
                raise IngestCorruption(
                    f"window {_iso(since_ts)}..{_iso(until_ts)} returned a tweet "
                    f"dated {_iso(window_earliest)} ({window_earliest}), which is "
                    f"later than the window's own until_time ({until_ts}). Either "
                    f"until_time: was not honoured or timestamps are being "
                    f"misparsed; continuing would walk backwards by seconds while "
                    f"paying full price per page."
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

    ordered = _newest_first(seen.values())
    return ordered[:max_posts], stats


def _sort_key(raw: dict[str, Any]) -> datetime:
    """Sort key that cannot raise.

    Anything reaching here already parsed once during the walk, so a failure is
    not expected — but sorting is not the place to discover it, and a crash
    while ordering results would throw away a corpus that was already paid for.
    Unparseable entries sort to the epoch, i.e. last in a newest-first list.
    """
    try:
        return parse_created_at(
            raw.get("createdAt") or raw.get("created_at") or raw.get("timestamp")
        )
    except TimestampParseError:
        return datetime.fromtimestamp(0, tz=timezone.utc)


def _newest_first(raws: Any) -> list[dict[str, Any]]:
    return sorted(raws, key=_sort_key, reverse=True)


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
    handle = validate_handle(handle)
    stats = IngestStats(public_post_count=public_post_count)
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
    return _newest_first(seen.values())[:max_posts], stats
