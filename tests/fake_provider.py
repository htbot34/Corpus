"""A fixture-backed XProvider that can reproduce both documented regressions.

`lying_empty_windows` forces a window to return nothing even though tweets
exist in it — failure mode 2. `duplicate_cursor_windows` makes a window's
second page repeat its first page under a fresh cursor value — failure mode 1,
the one that makes a naive cursor loop run forever.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

FIXTURES = Path(__file__).parent / "fixtures"

_SINCE = re.compile(r"since_time:(\d+)")
_UNTIL = re.compile(r"until_time:(\d+)")


def load(name: str) -> Any:
    return json.loads((FIXTURES / name).read_text())


# A tweet whose timestamp the *provider* cannot read. Real providers do not
# validate their own payloads — they hand the malformed record over and the
# client discovers it. Sorting these to the newest end and serving them in every
# window models that faithfully, and lets tests drive a bad timestamp all the
# way into ingest.py where Phase 2.6 has to cope with it.
UNPARSEABLE_TS = -1


def tweet_ts(raw: dict[str, Any]) -> int:
    try:
        return int(
            datetime.strptime(raw["createdAt"], "%a %b %d %H:%M:%S %z %Y")
            .astimezone(timezone.utc)
            .timestamp()
        )
    except (ValueError, KeyError, TypeError):
        return UNPARSEABLE_TS


class FakeProvider:
    name = "fake"

    def __init__(
        self,
        *,
        page_size: int = 5,
        lying_empty_windows: int = 0,
        duplicate_cursor_windows: int = 0,
        subject: list[dict[str, Any]] | None = None,
        parents: list[dict[str, Any]] | None = None,
    ) -> None:
        self.subject = subject if subject is not None else load("tweets.json")
        self.parents = parents if parents is not None else load("parents.json")
        self.page_size = page_size
        self.lying_empty_windows = lying_empty_windows
        self.duplicate_cursor_windows = duplicate_cursor_windows

        self.calls: list[str] = []
        self.batch_calls: list[list[str]] = []
        self._windows_seen: list[tuple[int, int]] = []
        self._lies_told = 0
        self._dupes_served = 0
        self._by_id = {t["id"]: t for t in self.subject + self.parents}

    # -- XProvider --------------------------------------------------------

    def user_info(self, handle: str) -> dict[str, Any]:
        self.calls.append(f"user_info:{handle}")
        return load("user_info.json")["data"]

    def last_tweets(self, handle: str, cursor: str | None = None):  # type: ignore[no-untyped-def]
        self.calls.append(f"last_tweets:{handle}:{cursor}")
        page = self.subject[: self.page_size]
        return page, "cursor-2", False

    def advanced_search(self, query: str, cursor: str | None = None):  # type: ignore[no-untyped-def]
        self.calls.append(f"search:{query}:{cursor}")
        since_m, until_m = _SINCE.search(query), _UNTIL.search(query)
        assert since_m and until_m, "ingest must use since_time/until_time, not since/until"
        since_ts, until_ts = int(since_m.group(1)), int(until_m.group(1))
        window = (since_ts, until_ts)
        if window not in self._windows_seen:
            self._windows_seen.append(window)

        # Malformed records are returned in every window: the provider has no
        # idea they are malformed, which is precisely the situation under test.
        in_range = sorted(
            (
                t
                for t in self.subject
                if tweet_ts(t) == UNPARSEABLE_TS or since_ts <= tweet_ts(t) < until_ts
            ),
            key=tweet_ts,
            reverse=True,
        )

        # Failure mode 2: a window that contains tweets but returns nothing.
        if in_range and self._lies_told < self.lying_empty_windows:
            self._lies_told += 1
            return [], None, False

        page_index = int(cursor.split("-")[-1]) if cursor else 0
        start = page_index * self.page_size
        page = in_range[start : start + self.page_size]

        # Failure mode 1: cursor advances, payload does not.
        if (
            cursor
            and self._dupes_served < self.duplicate_cursor_windows
            and len(in_range) > self.page_size
        ):
            self._dupes_served += 1
            return in_range[: self.page_size], f"cursor-{page_index + 1}", True

        has_next = start + self.page_size < len(in_range)
        next_cursor = f"cursor-{page_index + 1}" if has_next else None
        return page, next_cursor, has_next

    def tweets_by_ids(self, ids: list[str]) -> list[dict[str, Any]]:
        self.batch_calls.append(list(ids))
        assert len(ids) <= 100, "batch lookup must be capped at 100 ids"
        return [self._by_id[i] for i in ids if i in self._by_id]

    def close(self) -> None:
        return None
