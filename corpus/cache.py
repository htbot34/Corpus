"""SQLite content cache at ~/.corpus/cache.db.

Keyed by (source, source_id), storing raw payloads with fetch timestamps.
Default TTL 7 days. Hydrated parents are stored with permanent=1 — old tweets
do not change, so re-paying for them is pure waste.
"""

from __future__ import annotations

import json
import os
import sqlite3
import time
from pathlib import Path
from typing import Any

DEFAULT_TTL_SECONDS = 7 * 24 * 3600

_SCHEMA = """
CREATE TABLE IF NOT EXISTS entries (
    source      TEXT NOT NULL,
    source_id   TEXT NOT NULL,
    payload     TEXT NOT NULL,
    fetched_at  REAL NOT NULL,
    permanent   INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (source, source_id)
);
CREATE INDEX IF NOT EXISTS entries_source_idx ON entries(source);

CREATE TABLE IF NOT EXISTS spend (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          REAL NOT NULL,
    run_id      TEXT NOT NULL,
    category    TEXT NOT NULL,
    endpoint    TEXT NOT NULL,
    units       REAL NOT NULL,
    unit_cost   REAL NOT NULL,
    cost        REAL NOT NULL,
    note        TEXT
);
"""


def default_db_path() -> Path:
    env = os.environ.get("CORPUS_CACHE_DB")
    if env:
        return Path(env).expanduser()
    return Path.home() / ".corpus" / "cache.db"


class Cache:
    def __init__(
        self,
        path: Path | None = None,
        ttl_seconds: int = DEFAULT_TTL_SECONDS,
        refresh: bool = False,
        offline: bool = False,
    ) -> None:
        self.path = path or default_db_path()
        self.ttl_seconds = ttl_seconds
        self.refresh = refresh
        self.offline = offline
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(self.path))
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(_SCHEMA)
        self.conn.commit()

    # -- content ----------------------------------------------------------

    def get(self, source: str, source_id: str) -> Any | None:
        """Return the cached payload, or None on miss/expiry.

        --refresh bypasses reads entirely (but permanent rows are still
        honoured in --offline mode, where there is nothing to refresh from).
        """
        if self.refresh and not self.offline:
            return None
        row = self.conn.execute(
            "SELECT payload, fetched_at, permanent FROM entries WHERE source=? AND source_id=?",
            (source, source_id),
        ).fetchone()
        if row is None:
            return None
        if not row["permanent"] and not self.offline:
            if time.time() - row["fetched_at"] > self.ttl_seconds:
                return None
        return json.loads(row["payload"])

    def put(
        self, source: str, source_id: str, payload: Any, permanent: bool = False
    ) -> None:
        existing = self.conn.execute(
            "SELECT permanent FROM entries WHERE source=? AND source_id=?",
            (source, source_id),
        ).fetchone()
        # Never downgrade a permanent row: once we know a tweet is immutable
        # history, a later TTL'd write should not make it expirable again.
        if existing is not None and existing["permanent"]:
            permanent = True
        self.conn.execute(
            "INSERT OR REPLACE INTO entries (source, source_id, payload, fetched_at, permanent) "
            "VALUES (?, ?, ?, ?, ?)",
            (source, source_id, json.dumps(payload), time.time(), 1 if permanent else 0),
        )
        self.conn.commit()

    def has(self, source: str, source_id: str) -> bool:
        return self.get(source, source_id) is not None

    # -- spend log --------------------------------------------------------

    def log_spend(
        self,
        run_id: str,
        category: str,
        endpoint: str,
        units: float,
        unit_cost: float,
        cost: float,
        note: str = "",
    ) -> None:
        self.conn.execute(
            "INSERT INTO spend (ts, run_id, category, endpoint, units, unit_cost, cost, note) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (time.time(), run_id, category, endpoint, units, unit_cost, cost, note),
        )
        self.conn.commit()

    def spend_log(self, limit: int = 200) -> list[sqlite3.Row]:
        return list(
            self.conn.execute(
                "SELECT * FROM spend ORDER BY ts DESC LIMIT ?", (limit,)
            ).fetchall()
        )

    def spend_totals(self) -> list[sqlite3.Row]:
        return list(
            self.conn.execute(
                "SELECT category, SUM(cost) AS total, COUNT(*) AS calls "
                "FROM spend GROUP BY category ORDER BY total DESC"
            ).fetchall()
        )

    # -- admin ------------------------------------------------------------

    def stats(self) -> dict[str, Any]:
        rows = self.conn.execute(
            "SELECT source, COUNT(*) AS n, SUM(permanent) AS permanent FROM entries GROUP BY source"
        ).fetchall()
        total = self.conn.execute("SELECT COUNT(*) AS n FROM entries").fetchone()["n"]
        oldest = self.conn.execute("SELECT MIN(fetched_at) AS t FROM entries").fetchone()["t"]
        size = self.path.stat().st_size if self.path.exists() else 0
        return {
            "path": str(self.path),
            "entries": total,
            "size_bytes": size,
            "oldest_fetch": oldest,
            "by_source": {r["source"]: {"entries": r["n"], "permanent": r["permanent"] or 0} for r in rows},
        }

    def clear(self, keep_permanent: bool = False) -> int:
        if keep_permanent:
            cur = self.conn.execute("DELETE FROM entries WHERE permanent=0")
        else:
            cur = self.conn.execute("DELETE FROM entries")
        self.conn.commit()
        return cur.rowcount

    def close(self) -> None:
        self.conn.close()
