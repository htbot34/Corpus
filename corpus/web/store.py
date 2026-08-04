"""SQLite for the web layer: the run queue, its logs, and the audit trail.

Same pragmas as `corpus/cache.py`, for the same reasons: WAL so the worker's
writes never block a page render, busy_timeout so a contended write waits
instead of dying, synchronous=NORMAL as the standard WAL pairing.

A run moves through exactly one path:

    proposed -> queued -> running -> done | failed

`proposed` is the dry-run gate. The only way to reach `queued` is
`confirm()`, which requires an existing proposal and its token — there is no
API that creates a run already queued, so posting directly at the run
endpoint cannot skip the estimate screen.

The audit table is append-only by construction: this class has no method
that deletes or updates a row of it, and none should ever be added. It
exists so "who spent what on whom, and why" survives every other table.
"""

from __future__ import annotations

import contextlib
import json
import os
import secrets
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

BUSY_TIMEOUT_SECONDS = 30.0

_SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at  REAL NOT NULL,
    started_at  REAL,
    finished_at REAL,
    status      TEXT NOT NULL DEFAULT 'proposed',
    target_key  TEXT NOT NULL,
    name        TEXT NOT NULL,
    params      TEXT NOT NULL,
    reason      TEXT NOT NULL,
    token       TEXT NOT NULL,
    dry_run_output TEXT NOT NULL DEFAULT '',
    out_dir     TEXT NOT NULL DEFAULT '',
    spend       REAL NOT NULL DEFAULT 0,
    documents   INTEGER NOT NULL DEFAULT 0,
    tier        TEXT NOT NULL DEFAULT '',
    error       TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS run_log (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id  INTEGER NOT NULL,
    seq     INTEGER NOT NULL,
    line    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS run_log_run_idx ON run_log(run_id, seq);

CREATE TABLE IF NOT EXISTS audit (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          REAL NOT NULL,
    who         TEXT NOT NULL,
    target_key  TEXT NOT NULL,
    reason      TEXT NOT NULL,
    spend       REAL NOT NULL DEFAULT 0,
    documents   INTEGER NOT NULL DEFAULT 0,
    tier        TEXT NOT NULL DEFAULT '',
    status      TEXT NOT NULL DEFAULT '',
    run_id      INTEGER
);
"""


def default_web_db_path() -> Path:
    env = os.environ.get("CORPUS_WEB_DB")
    if env:
        return Path(env).expanduser()
    return Path.home() / ".corpus" / "web.db"


class WebStore:
    def __init__(self, path: Path | None = None) -> None:
        self.path = path or default_web_db_path()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(
            str(self.path), timeout=BUSY_TIMEOUT_SECONDS, check_same_thread=False
        )
        self.conn.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        with contextlib.suppress(sqlite3.DatabaseError):
            self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute(f"PRAGMA busy_timeout={int(BUSY_TIMEOUT_SECONDS * 1000)}")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self.conn.executescript(_SCHEMA)
        self.conn.commit()

    # -- proposals and the queue ------------------------------------------

    def propose(
        self,
        *,
        target_key: str,
        name: str,
        params: dict[str, Any],
        reason: str,
        dry_run_output: str,
    ) -> tuple[int, str]:
        """Record a dry run and return (run_id, confirm_token).

        The token is the proof that this exact proposal was seen: the confirm
        form carries it back, and nothing else can flip the row to queued.
        """
        token = secrets.token_hex(16)
        with self._lock:
            cur = self.conn.execute(
                "INSERT INTO runs (created_at, status, target_key, name, params, reason, "
                "token, dry_run_output) VALUES (?, 'proposed', ?, ?, ?, ?, ?, ?)",
                (time.time(), target_key, name, json.dumps(params), reason, token, dry_run_output),
            )
            self.conn.commit()
            return int(cur.lastrowid or 0), token

    def confirm(self, run_id: int, token: str) -> bool:
        """proposed -> queued, exactly once, token required.

        Returns False for a wrong token, an unknown id, or a run in any other
        state — the caller turns that into an error, never into a queued run.
        """
        if not token:
            return False
        with self._lock:
            cur = self.conn.execute(
                "UPDATE runs SET status='queued' WHERE id=? AND status='proposed' AND token=?",
                (run_id, token),
            )
            self.conn.commit()
            return cur.rowcount == 1

    def claim_next(self) -> sqlite3.Row | None:
        """Oldest queued run -> running, atomically. None when the queue is empty."""
        with self._lock:
            row = self.conn.execute(
                "SELECT * FROM runs WHERE status='queued' ORDER BY id LIMIT 1"
            ).fetchone()
            if row is None:
                return None
            cur = self.conn.execute(
                "UPDATE runs SET status='running', started_at=? WHERE id=? AND status='queued'",
                (time.time(), row["id"]),
            )
            self.conn.commit()
            if cur.rowcount != 1:
                return None
            return self.get_run(int(row["id"]))

    def finish(
        self,
        run_id: int,
        *,
        status: str,
        error: str = "",
        out_dir: str = "",
        spend: float = 0.0,
        documents: int = 0,
        tier: str = "",
    ) -> None:
        with self._lock:
            self.conn.execute(
                "UPDATE runs SET status=?, finished_at=?, error=?, out_dir=?, spend=?, "
                "documents=?, tier=? WHERE id=?",
                (status, time.time(), error, out_dir, spend, documents, tier, run_id),
            )
            self.conn.commit()

    def get_run(self, run_id: int) -> sqlite3.Row | None:
        row = self.conn.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
        return row if isinstance(row, sqlite3.Row) else None

    def runs(self, limit: int = 100) -> list[sqlite3.Row]:
        return list(
            self.conn.execute("SELECT * FROM runs ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        )

    # -- progress log ------------------------------------------------------

    def append_log(self, run_id: int, line: str) -> None:
        with self._lock:
            row = self.conn.execute(
                "SELECT COALESCE(MAX(seq), 0) AS s FROM run_log WHERE run_id=?", (run_id,)
            ).fetchone()
            self.conn.execute(
                "INSERT INTO run_log (run_id, seq, line) VALUES (?, ?, ?)",
                (run_id, int(row["s"]) + 1, line),
            )
            self.conn.commit()

    def log_lines(self, run_id: int, after: int = 0) -> list[sqlite3.Row]:
        return list(
            self.conn.execute(
                "SELECT seq, line FROM run_log WHERE run_id=? AND seq>? ORDER BY seq",
                (run_id, after),
            ).fetchall()
        )

    # -- audit: append and read, nothing else ------------------------------

    def append_audit(
        self,
        *,
        who: str,
        target_key: str,
        reason: str,
        spend: float,
        documents: int,
        tier: str,
        status: str,
        run_id: int | None,
    ) -> None:
        with self._lock:
            self.conn.execute(
                "INSERT INTO audit (ts, who, target_key, reason, spend, documents, tier, "
                "status, run_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (time.time(), who, target_key, reason, spend, documents, tier, status, run_id),
            )
            self.conn.commit()

    def audit_rows(self, limit: int = 500) -> list[sqlite3.Row]:
        return list(
            self.conn.execute("SELECT * FROM audit ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        )

    def close(self) -> None:
        self.conn.close()
