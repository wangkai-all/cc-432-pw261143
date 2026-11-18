"""Thin SQLite helper. Each service owns its own database file (independence)."""
from __future__ import annotations

import sqlite3
import threading
from typing import Any, Iterable, Sequence


class Database:
    def __init__(self, path: str):
        self.path = path
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(
            path, check_same_thread=False, isolation_level=None
        )
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")

    def executescript(self, script: str) -> None:
        with self._lock:
            self._conn.executescript(script)

    def execute(self, sql: str, params: Sequence[Any] | dict = ()) -> sqlite3.Cursor:
        with self._lock:
            return self._conn.execute(sql, params)

    def query_one(self, sql: str, params: Sequence[Any] | dict = ()) -> dict | None:
        with self._lock:
            row = self._conn.execute(sql, params).fetchone()
            return dict(row) if row else None

    def query_all(self, sql: str, params: Sequence[Any] | dict = ()) -> list[dict]:
        with self._lock:
            return [dict(r) for r in self._conn.execute(sql, params).fetchall()]

    def insert(self, table: str, row: dict) -> str:
        cols = ", ".join(row.keys())
        placeholders = ", ".join(f":{k}" for k in row)
        with self._lock:
            cur = self._conn.execute(
                f"INSERT INTO {table} ({cols}) VALUES ({placeholders})", row
            )
            return cur.lastrowid

    def transaction(self):
        """Context manager for an atomic multi-statement write.

        Serializes writers process-wide (the connection lock) and wraps the
        statements in a single SQLite transaction so a snapshot can never
        capture an intermediate state of a retraction.
        """
        return _Transaction(self._conn, self._lock)

    def close(self) -> None:
        with self._lock:
            self._conn.close()


class _Transaction:
    def __init__(self, conn, lock):
        self._conn = conn
        self._lock = lock

    def __enter__(self):
        self._lock.acquire()
        self._conn.execute("BEGIN IMMEDIATE")
        return self._conn

    def __exit__(self, exc_type, exc, tb):
        try:
            if exc_type is None:
                self._conn.execute("COMMIT")
            else:
                self._conn.execute("ROLLBACK")
        finally:
            self._lock.release()
        return False
