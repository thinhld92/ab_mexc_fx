"""Audit trail and reconciliation log repository."""

from __future__ import annotations

import sqlite3
import time

import orjson

from ..domain.enums import PairEventType
from .db import Database


class JournalRepository:
    def __init__(self, db: Database):
        self.db = db

    def add_event(
        self,
        pair_token: str,
        event_type: PairEventType | str,
        event_data: dict | None = None,
        conn: sqlite3.Connection | None = None,
    ) -> int:
        if conn is None:
            with self.db.transaction() as tx:
                return self.add_event(pair_token, event_type, event_data, conn=tx)

        cursor = conn.execute(
            """
            INSERT INTO pair_events (pair_token, event_type, event_data)
            VALUES (?, ?, ?)
            """,
            (
                pair_token,
                str(PairEventType(event_type).value),
                orjson.dumps(event_data).decode("utf-8") if event_data is not None else None,
            ),
        )
        return int(cursor.lastrowid)

    def list_events(
        self,
        pair_token: str,
        conn: sqlite3.Connection | None = None,
    ) -> list[dict]:
        if conn is None:
            with self.db.connection() as read_conn:
                return self.list_events(pair_token, conn=read_conn)

        rows = conn.execute(
            """
            SELECT id, pair_token, event_type, event_data, created_at
            FROM pair_events
            WHERE pair_token = ?
            ORDER BY id ASC
            """,
            (pair_token,),
        ).fetchall()
        return [self.db.row_to_dict(row) for row in rows]

    def add_recon_log(
        self,
        *,
        run_at: float | None = None,
        mt5_positions: int | None = None,
        exchange_positions: int | None = None,
        local_pairs: int | None = None,
        mismatches: int = 0,
        actions_taken: dict | list | None = None,
        conn: sqlite3.Connection | None = None,
    ) -> int:
        if conn is None:
            with self.db.transaction() as tx:
                return self.add_recon_log(
                    run_at=run_at,
                    mt5_positions=mt5_positions,
                    exchange_positions=exchange_positions,
                    local_pairs=local_pairs,
                    mismatches=mismatches,
                    actions_taken=actions_taken,
                    conn=tx,
                )

        cursor = conn.execute(
            """
            INSERT INTO recon_log (
                run_at, mt5_positions, exchange_positions, local_pairs, mismatches, actions_taken
            )
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                run_at if run_at is not None else time.time(),
                mt5_positions,
                exchange_positions,
                local_pairs,
                mismatches,
                orjson.dumps(actions_taken).decode("utf-8")
                if actions_taken is not None
                else None,
            ),
        )
        return int(cursor.lastrowid)
