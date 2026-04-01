"""SQLite DB helper with WAL mode and small transaction helpers."""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from .schema import initialize_schema


class Database:
    """Thin SQLite wrapper used by Brain and Reconciler."""

    def __init__(self, config_or_path=None):
        self.path = self._resolve_path(config_or_path)
        self._is_memory = str(self.path) in (":memory:", "")
        self._shared_conn: sqlite3.Connection | None = None

    @staticmethod
    def _resolve_path(config_or_path) -> str | Path:
        if config_or_path is None:
            raw_path = "data/arb.db"
        elif isinstance(config_or_path, str):
            raw_path = config_or_path
        else:
            raw_path = getattr(config_or_path, "database_path", "data/arb.db")

        # SQLite special paths — pass through as-is
        if raw_path in (":memory:", ""):
            return raw_path

        path = Path(raw_path)
        if not path.is_absolute():
            project_root = Path(__file__).resolve().parents[2]
            path = project_root / path

        path.parent.mkdir(parents=True, exist_ok=True)
        return path

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(
            str(self.path),
            timeout=30,
            isolation_level=None,
        )
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=5000")
        return conn

    def initialize(self) -> None:
        with self.connection() as conn:
            initialize_schema(conn)

    @contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        if self._is_memory:
            # In-memory DB: reuse a single connection so tables persist
            if self._shared_conn is None:
                self._shared_conn = self._connect()
            yield self._shared_conn
        else:
            conn = self._connect()
            try:
                yield conn
            finally:
                conn.close()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        with self.connection() as conn:
            try:
                conn.execute("BEGIN IMMEDIATE")
                yield conn
                conn.commit()
            except Exception:
                conn.rollback()
                raise

    @staticmethod
    def row_to_dict(row: sqlite3.Row | None) -> dict | None:
        if row is None:
            return None
        return {key: row[key] for key in row.keys()}
