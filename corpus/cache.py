"""SQLite content cache at ~/.corpus/cache.db.

Keyed by (source, source_id), storing raw payloads with fetch timestamps.
Default TTL 7 days. Hydrated parents are stored with permanent=1 — old tweets
do not change, so re-paying for them is pure waste.

Concurrency
-----------
The cache is shared: two runs against different handles hit the same database,
and the whole point of the permanent rows is that a second run does not re-pay
for what the first already fetched. Under SQLite's default rollback journal, a
writer takes an exclusive lock over the entire database, so a second run gets
`database is locked` immediately and dies — having paid for the data it was
about to write.

Three settings fix that, and none of them are optional for this workload:

* **WAL** lets readers proceed during a write, so a long-running run no longer
  blocks a `corpus cache stats` or a second ingest.
* **busy_timeout** makes a contended write *wait* rather than fail. Without it
  SQLite returns SQLITE_BUSY the instant it cannot get the lock; the default
  timeout is zero.
* **synchronous=NORMAL** is the standard WAL pairing. FULL fsyncs every commit,
  which for a cache of re-fetchable data buys durability we do not need at a
  cost we pay on every tweet.

WAL also means the database is three files (`-wal`, `-shm`), which matters when
copying one by hand.
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

DEFAULT_TTL_SECONDS = 7 * 24 * 3600

# How long a contended write waits before giving up. Generous on purpose: the
# alternative to waiting is a failed run that has already spent money, and the
# writes here are small and short.
BUSY_TIMEOUT_SECONDS = 30.0

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

CREATE TABLE IF NOT EXISTS estimates (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          REAL NOT NULL,
    run_id      TEXT NOT NULL,
    handle      TEXT NOT NULL,
    category    TEXT NOT NULL,
    estimated   REAL NOT NULL,
    actual      REAL NOT NULL,
    posts_estimated INTEGER NOT NULL DEFAULT 0,
    posts_actual    INTEGER NOT NULL DEFAULT 0,
    note        TEXT
);

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
        # check_same_thread=False because Budget.charge() is reachable from the
        # asyncio map stage and from worker threads; every write below is
        # serialized by self._lock, which is the guarantee sqlite3 wants.
        self.conn = sqlite3.connect(
            str(self.path),
            timeout=BUSY_TIMEOUT_SECONDS,
            check_same_thread=False,
        )
        self.conn.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        self._configure()
        self.conn.executescript(_SCHEMA)
        self.conn.commit()

    def _configure(self) -> None:
        """Pragmas that make a shared cache survive two runs at once."""
        # journal_mode is persistent (stored in the file header), so this is a
        # no-op after the first time. It can fail on exotic filesystems where
        # WAL's shared memory is unavailable — a network mount, some containers
        # — and there the rollback journal is still correct, just less
        # concurrent. Degrading is right; refusing to open the cache is not.
        try:
            self.conn.execute("PRAGMA journal_mode=WAL")
        except sqlite3.DatabaseError:
            pass
        self.conn.execute(f"PRAGMA busy_timeout={int(BUSY_TIMEOUT_SECONDS * 1000)}")
        self.conn.execute("PRAGMA synchronous=NORMAL")

    @property
    def journal_mode(self) -> str:
        row = self.conn.execute("PRAGMA journal_mode").fetchone()
        return str(row[0]).lower() if row else ""

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
        """Write an entry, never downgrading a permanent row to expirable.

        The permanent flag is max()'d in SQL rather than read-then-written in
        Python. The old version did a SELECT, decided, then INSERT OR REPLACE —
        two statements with a gap in between. Under two concurrent runs, a
        permanent write landing in that gap was silently clobbered by the
        second run's TTL'd write, quietly making already-paid-for tweets
        expirable again. Doing it in one statement closes the window; the lock
        below closes it for the same-process case too.
        """
        with self._lock:
            self.conn.execute(
                "INSERT INTO entries (source, source_id, payload, fetched_at, permanent) "
                "VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT(source, source_id) DO UPDATE SET "
                "  payload=excluded.payload, "
                "  fetched_at=excluded.fetched_at, "
                # max(): permanent is a one-way door.
                "  permanent=max(entries.permanent, excluded.permanent)",
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
        with self._lock:
            self.conn.execute(
                "INSERT INTO spend (ts, run_id, category, endpoint, units, unit_cost, cost, note) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (time.time(), run_id, category, endpoint, units, unit_cost, cost, note),
            )
            self.conn.commit()

    def log_estimate(
        self,
        run_id: str,
        handle: str,
        category: str,
        estimated: float,
        actual: float,
        posts_estimated: int = 0,
        posts_actual: int = 0,
        note: str = "",
    ) -> None:
        """Record what a run was predicted to cost against what it did cost.

        An estimator nobody checks is decoration, so every run leaves a row here
        whether or not anyone looks.
        """
        with self._lock:
            self.conn.execute(
                "INSERT INTO estimates (ts, run_id, handle, category, estimated, actual, "
                "posts_estimated, posts_actual, note) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    time.time(), run_id, handle, category, estimated, actual,
                    posts_estimated, posts_actual, note,
                ),
            )
            self.conn.commit()

    def estimate_log(self, limit: int = 200) -> list[sqlite3.Row]:
        return list(
            self.conn.execute(
                "SELECT * FROM estimates ORDER BY ts DESC LIMIT ?", (limit,)
            ).fetchall()
        )

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
        with self._lock:
            if keep_permanent:
                cur = self.conn.execute("DELETE FROM entries WHERE permanent=0")
            else:
                cur = self.conn.execute("DELETE FROM entries")
            self.conn.commit()
            return cur.rowcount

    def vacuum(self) -> dict[str, Any]:
        """Reclaim space and checkpoint the WAL.

        Deleting rows leaves the file the same size, and the -wal file grows
        until something checkpoints it. Neither is a correctness problem, which
        is exactly why it goes unnoticed until a cache directory is surprisingly
        large. Returns before/after sizes so the command can show its work.
        """
        with self._lock:
            before = self.total_bytes()
            try:
                self.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            except sqlite3.DatabaseError:
                pass  # not in WAL mode; nothing to checkpoint
            # VACUUM cannot run inside a transaction, which sqlite3 opens
            # implicitly for DML.
            self.conn.commit()
            self.conn.execute("VACUUM")
            self.conn.commit()
            after = self.total_bytes()
            return {"before_bytes": before, "after_bytes": after, "reclaimed_bytes": before - after}

    def total_bytes(self) -> int:
        """Size of the database *and* its WAL sidecars.

        Measuring only cache.db understates a WAL database badly — recent writes
        live in cache.db-wal until a checkpoint, so a "vacuum" could appear to
        make things bigger simply by moving bytes from a file nobody counted
        into the one everybody does.
        """
        total = 0
        for suffix in ("", "-wal", "-shm"):
            candidate = self.path.with_name(self.path.name + suffix)
            if candidate.exists():
                total += candidate.stat().st_size
        return total

    def close(self) -> None:
        self.conn.close()
