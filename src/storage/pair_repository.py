"""Repository for the `pairs` ledger table."""

from __future__ import annotations

import sqlite3
import time

from ..domain.enums import PairStatus, ReconStatus
from ..domain.models import PairCreate, PairRecord
from ..domain.state_machine import NON_TERMINAL_STATUSES, ensure_transition
from .db import Database


class PairRepository:
    def __init__(self, db: Database):
        self.db = db

    def insert_pending_pair(
        self,
        pair: PairCreate,
        conn: sqlite3.Connection | None = None,
    ) -> PairRecord:
        if conn is None:
            with self.db.transaction() as tx:
                return self.insert_pending_pair(pair, conn=tx)

        conn.execute(
            """
            INSERT INTO pairs (
                pair_token, direction, status, entry_time, entry_spread, conf_dev_entry, conf_dev_close
            )
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                pair.pair_token,
                pair.direction,
                pair.status.value,
                pair.entry_time,
                pair.entry_spread,
                pair.conf_dev_entry,
                pair.conf_dev_close,
            ),
        )
        row = self._fetch_by_token(pair.pair_token, conn)
        return PairRecord.from_row(row)

    def get_by_token(
        self,
        pair_token: str,
        conn: sqlite3.Connection | None = None,
    ) -> PairRecord | None:
        if conn is None:
            with self.db.connection() as read_conn:
                return self.get_by_token(pair_token, conn=read_conn)

        row = self._fetch_by_token(pair_token, conn)
        if row is None:
            return None
        return PairRecord.from_row(row)

    def list_non_terminal(self, conn: sqlite3.Connection | None = None) -> list[PairRecord]:
        return self.list_by_statuses(NON_TERMINAL_STATUSES, conn=conn)

    def list_by_statuses(
        self,
        statuses,
        conn: sqlite3.Connection | None = None,
    ) -> list[PairRecord]:
        if conn is None:
            with self.db.connection() as read_conn:
                return self.list_by_statuses(statuses, conn=read_conn)

        values = [PairStatus(status).value for status in statuses]
        placeholders = ",".join("?" for _ in values)
        rows = conn.execute(
            f"""
            SELECT *
            FROM pairs
            WHERE status IN ({placeholders})
            ORDER BY entry_time ASC
            """,
            values,
        ).fetchall()
        return [PairRecord.from_row(self.db.row_to_dict(row)) for row in rows]

    def transition(
        self,
        pair_token: str,
        target_status: PairStatus | str,
        *,
        fields: dict | None = None,
        expected_current: PairStatus | str | None = None,
        conn: sqlite3.Connection | None = None,
    ) -> PairRecord:
        if conn is None:
            with self.db.transaction() as tx:
                return self.transition(
                    pair_token,
                    target_status,
                    fields=fields,
                    expected_current=expected_current,
                    conn=tx,
                )

        row = self._fetch_by_token(pair_token, conn)
        if row is None:
            raise KeyError(f"Pair not found: {pair_token}")

        current_status = PairStatus(row["status"])
        if expected_current is not None and current_status != PairStatus(expected_current):
            raise ValueError(
                f"Pair {pair_token} expected {PairStatus(expected_current).value}, got {current_status.value}"
            )

        next_status = ensure_transition(current_status, target_status)
        payload = dict(fields or {})
        payload["status"] = next_status.value
        payload["updated_at"] = self._utc_now_text()

        assignments = ", ".join(f"{column} = ?" for column in payload)
        params = list(payload.values()) + [pair_token]

        conn.execute(
            f"UPDATE pairs SET {assignments} WHERE pair_token = ?",
            params,
        )
        updated = self._fetch_by_token(pair_token, conn)
        return PairRecord.from_row(updated)

    def update_recon_status(
        self,
        pair_token: str,
        recon_status: ReconStatus | str,
        *,
        recon_note: str | None = None,
        last_recon_at: float | None = None,
        conn: sqlite3.Connection | None = None,
    ) -> PairRecord:
        if conn is None:
            with self.db.transaction() as tx:
                return self.update_recon_status(
                    pair_token,
                    recon_status,
                    recon_note=recon_note,
                    last_recon_at=last_recon_at,
                    conn=tx,
                )

        payload = {
            "recon_status": ReconStatus(recon_status).value,
            "recon_note": recon_note,
            "last_recon_at": last_recon_at if last_recon_at is not None else time.time(),
            "updated_at": self._utc_now_text(),
        }
        assignments = ", ".join(f"{column} = ?" for column in payload)
        params = list(payload.values()) + [pair_token]
        conn.execute(
            f"UPDATE pairs SET {assignments} WHERE pair_token = ?",
            params,
        )
        row = self._fetch_by_token(pair_token, conn)
        if row is None:
            raise KeyError(f"Pair not found: {pair_token}")
        return PairRecord.from_row(row)

    def mark_mt5_entry_filled(
        self,
        pair_token: str,
        *,
        mt5_ticket: int,
        mt5_action: str,
        mt5_volume: float,
        mt5_entry_price: float | None = None,
        mt5_entry_latency_ms: float | None = None,
        expected_current: PairStatus | str = PairStatus.ENTRY_SENT,
        conn: sqlite3.Connection | None = None,
    ) -> PairRecord:
        return self.transition(
            pair_token,
            PairStatus.ENTRY_MT5_FILLED,
            fields={
                "mt5_ticket": mt5_ticket,
                "mt5_action": mt5_action,
                "mt5_volume": mt5_volume,
                "mt5_entry_price": mt5_entry_price,
                "mt5_entry_latency_ms": mt5_entry_latency_ms,
            },
            expected_current=expected_current,
            conn=conn,
        )

    def mark_exchange_entry_filled(
        self,
        pair_token: str,
        *,
        exchange_order_id: str,
        exchange_volume: int,
        exchange_side: int,
        exchange_entry_latency_ms: float | None = None,
        expected_current: PairStatus | str = PairStatus.ENTRY_SENT,
        conn: sqlite3.Connection | None = None,
    ) -> PairRecord:
        return self.transition(
            pair_token,
            PairStatus.ENTRY_EXCHANGE_FILLED,
            fields={
                "exchange_order_id": exchange_order_id,
                "exchange_volume": exchange_volume,
                "exchange_side": exchange_side,
                "exchange_entry_latency_ms": exchange_entry_latency_ms,
            },
            expected_current=expected_current,
            conn=conn,
        )

    def mark_mt5_close_done(
        self,
        pair_token: str,
        *,
        mt5_close_price: float | None = None,
        mt5_profit: float | None = None,
        mt5_close_latency_ms: float | None = None,
        expected_current: PairStatus | str = PairStatus.CLOSE_SENT,
        conn: sqlite3.Connection | None = None,
    ) -> PairRecord:
        return self.transition(
            pair_token,
            PairStatus.CLOSE_MT5_DONE,
            fields={
                "mt5_close_price": mt5_close_price,
                "mt5_profit": mt5_profit,
                "mt5_close_latency_ms": mt5_close_latency_ms,
            },
            expected_current=expected_current,
            conn=conn,
        )

    def mark_exchange_close_done(
        self,
        pair_token: str,
        *,
        exchange_close_order_id: str,
        exchange_close_latency_ms: float | None = None,
        expected_current: PairStatus | str = PairStatus.CLOSE_SENT,
        conn: sqlite3.Connection | None = None,
    ) -> PairRecord:
        return self.transition(
            pair_token,
            PairStatus.CLOSE_EXCHANGE_DONE,
            fields={
                "exchange_close_order_id": exchange_close_order_id,
                "exchange_close_latency_ms": exchange_close_latency_ms,
            },
            expected_current=expected_current,
            conn=conn,
        )

    def _fetch_by_token(self, pair_token: str, conn: sqlite3.Connection) -> dict | None:
        row = conn.execute(
            "SELECT * FROM pairs WHERE pair_token = ?",
            (pair_token,),
        ).fetchone()
        return self.db.row_to_dict(row)

    @staticmethod
    def _utc_now_text() -> str:
        return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
