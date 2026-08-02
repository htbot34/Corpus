"""Phase 3.3: two runs must be able to share one cache.

The cache is global (~/.corpus/cache.db), and its permanent rows exist so a
second run does not re-pay for what a first already fetched. Under SQLite's
default rollback journal a writer locks the whole database, so the second run
gets `database is locked` and dies — after paying.

The permanent-row protection at the old cache.py:97-99 had a second, quieter
problem: SELECT-then-INSERT is two statements, and a permanent write landing in
the gap was silently downgraded to expirable by a concurrent TTL'd write. That
loses no data today and costs money next week, which is the worst kind of bug.

Offline.
"""

from __future__ import annotations

import sqlite3
import threading
from pathlib import Path

import pytest

from corpus.cache import BUSY_TIMEOUT_SECONDS, Cache


# -- pragmas ---------------------------------------------------------------


def test_wal_is_enabled(tmp_path: Path) -> None:
    c = Cache(path=tmp_path / "c.db")
    assert c.journal_mode == "wal"
    c.close()


def test_busy_timeout_is_set(tmp_path: Path) -> None:
    c = Cache(path=tmp_path / "c.db")
    timeout = c.conn.execute("PRAGMA busy_timeout").fetchone()[0]
    assert timeout == int(BUSY_TIMEOUT_SECONDS * 1000)
    assert timeout > 0, "the SQLite default is 0, which fails instantly under contention"
    c.close()


def test_wal_survives_reopening(tmp_path: Path) -> None:
    """journal_mode lives in the file header; a second opener must inherit it."""
    first = Cache(path=tmp_path / "c.db")
    first.close()
    second = Cache(path=tmp_path / "c.db")
    assert second.journal_mode == "wal"
    second.close()


# -- concurrent access ------------------------------------------------------


def test_two_caches_can_write_the_same_database(tmp_path: Path) -> None:
    """The headline case: two runs, one cache file, neither dies."""
    path = tmp_path / "shared.db"
    a, b = Cache(path=path), Cache(path=path)
    a.put("x", "tweet:1", {"from": "a"}, permanent=True)
    b.put("x", "tweet:2", {"from": "b"}, permanent=True)
    assert a.get("x", "tweet:2") == {"from": "b"}
    assert b.get("x", "tweet:1") == {"from": "a"}
    a.close()
    b.close()


def test_a_reader_is_not_blocked_by_a_writer(tmp_path: Path) -> None:
    """WAL's whole point: `corpus cache stats` during a long run."""
    path = tmp_path / "shared.db"
    writer, reader = Cache(path=path), Cache(path=path)
    writer.put("x", "tweet:1", {"a": 1}, permanent=True)
    for i in range(200):
        writer.put("x", f"tweet:{i + 10}", {"i": i})
    assert reader.stats()["entries"] >= 200
    writer.close()
    reader.close()


def test_concurrent_writers_do_not_lose_entries(tmp_path: Path) -> None:
    path = tmp_path / "shared.db"
    errors: list[Exception] = []

    def worker(worker_id: int) -> None:
        cache = Cache(path=path)
        try:
            for i in range(25):
                cache.put("x", f"tweet:{worker_id}-{i}", {"w": worker_id, "i": i}, permanent=True)
        except Exception as exc:  # noqa: BLE001 - the point is to see it
            errors.append(exc)
        finally:
            cache.close()

    threads = [threading.Thread(target=worker, args=(w,)) for w in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, f"concurrent writes failed: {errors}"
    check = Cache(path=path)
    assert check.stats()["entries"] == 6 * 25
    check.close()


def test_permanent_flag_survives_a_concurrent_ttl_write(tmp_path: Path) -> None:
    """The read-then-write race: permanent must never be downgraded.

    One thread writes the row as permanent, another writes the same row as
    expirable, repeatedly and interleaved. The flag is a one-way door, so
    whatever the ordering, the row must end up permanent.
    """
    path = tmp_path / "shared.db"
    key = "tweet:contended"
    stop = threading.Event()

    def permanent_writer() -> None:
        cache = Cache(path=path)
        for _ in range(60):
            cache.put("x", key, {"kind": "permanent"}, permanent=True)
        stop.set()
        cache.close()

    def ttl_writer() -> None:
        cache = Cache(path=path)
        while not stop.is_set():
            cache.put("x", key, {"kind": "ttl"}, permanent=False)
        cache.close()

    threads = [threading.Thread(target=permanent_writer), threading.Thread(target=ttl_writer)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    check = Cache(path=path, ttl_seconds=0)
    row = check.conn.execute(
        "SELECT permanent FROM entries WHERE source='x' AND source_id=?", (key,)
    ).fetchone()
    assert row["permanent"] == 1, "a concurrent TTL write downgraded a permanent row"
    # And the practical consequence: it still survives expiry.
    assert check.get("x", key) is not None
    check.close()


def test_permanent_flag_is_not_downgraded_in_a_single_process(tmp_path: Path) -> None:
    c = Cache(path=tmp_path / "c.db", ttl_seconds=0)
    c.put("x", "tweet:1", {"a": 1}, permanent=True)
    c.put("x", "tweet:1", {"a": 2})
    assert c.get("x", "tweet:1") == {"a": 2}, "payload should update"
    row = c.conn.execute(
        "SELECT permanent FROM entries WHERE source='x' AND source_id='tweet:1'"
    ).fetchone()
    assert row["permanent"] == 1
    c.close()


def test_spend_log_is_safe_under_concurrent_charges(tmp_path: Path) -> None:
    """Budget.charge() is reachable from the asyncio map stage and from threads."""
    from corpus.budget import Budget

    path = tmp_path / "shared.db"
    cache = Cache(path=path)
    budget = Budget(limit=1000.0, cache=cache)
    errors: list[Exception] = []

    def worker() -> None:
        try:
            for _ in range(20):
                budget.charge("x", "search", 1, 0.0001)
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, f"concurrent spend logging failed: {errors}"
    assert len(cache.spend_log(limit=500)) == 100
    cache.close()


# -- vacuum ----------------------------------------------------------------


def test_vacuum_reclaims_space(tmp_path: Path) -> None:
    c = Cache(path=tmp_path / "c.db")
    for i in range(400):
        c.put("x", f"tweet:{i}", {"body": "x" * 400})
    c.clear()
    result = c.vacuum()
    assert result["after_bytes"] <= result["before_bytes"]
    assert result["reclaimed_bytes"] >= 0
    c.close()


def test_vacuum_keeps_the_data(tmp_path: Path) -> None:
    c = Cache(path=tmp_path / "c.db")
    c.put("x", "tweet:1", {"a": 1}, permanent=True)
    c.vacuum()
    assert c.get("x", "tweet:1") == {"a": 1}
    assert c.journal_mode == "wal", "vacuum must not silently drop WAL mode"
    c.close()


def test_vacuum_is_reachable_from_the_cli() -> None:
    from corpus.cli import cache_app

    names = {c.name or c.callback.__name__ for c in cache_app.registered_commands}
    assert "vacuum" in names


# -- degradation ------------------------------------------------------------


def test_a_filesystem_without_wal_still_opens(tmp_path: Path, monkeypatch) -> None:
    """WAL needs shared memory; some mounts and containers do not provide it.

    Degrading to the rollback journal is correct. Refusing to open the cache —
    and therefore refusing to run at all — is not.

    sqlite3.Connection is an immutable type, so the refusal is injected by
    wrapping the connection rather than patching the class.
    """
    real_connect = sqlite3.connect

    class NoWalConnection:
        def __init__(self, inner: sqlite3.Connection) -> None:
            self._inner = inner

        def execute(self, sql: str, *args, **kwargs):  # type: ignore[no-untyped-def]
            if "journal_mode=WAL" in sql:
                raise sqlite3.OperationalError("unable to open database file")
            return self._inner.execute(sql, *args, **kwargs)

        def __getattr__(self, name: str):  # type: ignore[no-untyped-def]
            return getattr(self._inner, name)

        def __setattr__(self, name: str, value) -> None:  # type: ignore[no-untyped-def]
            if name == "_inner":
                object.__setattr__(self, name, value)
            else:
                setattr(self._inner, name, value)

    monkeypatch.setattr(
        sqlite3, "connect", lambda *a, **k: NoWalConnection(real_connect(*a, **k))
    )
    c = Cache(path=tmp_path / "c.db")
    c.put("x", "tweet:1", {"a": 1}, permanent=True)
    assert c.get("x", "tweet:1") == {"a": 1}
    assert c.journal_mode != "wal", "the refusal did not take effect"
    c.close()


def test_vacuum_counts_the_wal_sidecar(tmp_path: Path) -> None:
    """Measuring only cache.db understates a WAL database."""
    c = Cache(path=tmp_path / "c.db")
    for i in range(200):
        c.put("x", f"tweet:{i}", {"body": "y" * 300})
    assert c.total_bytes() >= (tmp_path / "c.db").stat().st_size
    c.close()
