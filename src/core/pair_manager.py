"""SQLite-backed pair lifecycle manager for the Phase 3 core."""

from __future__ import annotations

import time
import uuid

from ..domain import CloseReason, PairCreate, PairEventType, PairRecord, PairStatus
from ..exchanges.base import ExchangeOrderStatus, OrderResult
from ..storage import JournalRepository, PairRepository
from ..utils.constants import REDIS_MT5_TRACKED_TICKETS
from ..utils.logger import log


_MT5_TRACKED_STATUSES = {
    PairStatus.ENTRY_MT5_FILLED,
    PairStatus.OPEN,
    PairStatus.CLOSE_SENT,
    PairStatus.CLOSE_EXCHANGE_DONE,
    PairStatus.ORPHAN_MT5,
}


class PairManager:
    """Owns all writes to `pairs` and `pair_events`."""

    def __init__(self, redis_client, repo: PairRepository, journal: JournalRepository) -> None:
        self._redis = redis_client
        self._repo = repo
        self._journal = journal

        self._pairs: dict[str, PairRecord] = {}
        self.safe_mode = False
        self.safe_mode_reason = ""

    def bootstrap(self) -> list[PairRecord]:
        pairs = self._repo.list_non_terminal()
        self._pairs = {pair.pair_token: pair for pair in pairs}
        self.safe_mode = False
        self.safe_mode_reason = ""

        if len(pairs) > 1:
            self._set_safe_mode(f"Recovered {len(pairs)} non-terminal pairs")
        elif pairs and pairs[0].status != PairStatus.OPEN:
            self._set_safe_mode(f"Recovered {pairs[0].status.value}")

        self._sync_tracked_tickets()
        if pairs:
            log("WARN", "PAIR", "Recovered pairs from DB", count=len(pairs), safe_mode=self.safe_mode)
        return pairs

    def list_non_terminal(self) -> list[PairRecord]:
        return sorted(self._pairs.values(), key=lambda pair: pair.entry_time)

    def get_pair(self, pair_token: str) -> PairRecord | None:
        return self._pairs.get(pair_token)

    def get_open_pair(self) -> PairRecord | None:
        open_pairs = [pair for pair in self._pairs.values() if pair.status == PairStatus.OPEN]
        if len(open_pairs) != 1:
            return None
        return open_pairs[0]

    def get_pair_by_mt5_ticket(self, ticket: int) -> PairRecord | None:
        for pair in self._pairs.values():
            if pair.mt5_ticket == ticket:
                return pair
        return None

    @property
    def pair_count(self) -> int:
        return len(self._pairs)

    @property
    def has_pairs(self) -> bool:
        return bool(self._pairs)

    @property
    def allows_new_entries(self) -> bool:
        return not self.safe_mode and not self._pairs

    def create_entry(
        self,
        *,
        direction: str,
        spread: float,
        conf_dev_entry: float,
        conf_dev_close: float,
    ) -> PairRecord:
        pair_token = uuid.uuid4().hex[:16]
        payload = PairCreate(
            pair_token=pair_token,
            direction=direction,
            entry_time=time.time(),
            entry_spread=spread,
            conf_dev_entry=conf_dev_entry,
            conf_dev_close=conf_dev_close,
        )
        with self._repo.db.transaction() as conn:
            pair = self._repo.insert_pending_pair(payload, conn=conn)
            self._journal.add_event(
                pair_token,
                PairEventType.CREATED,
                {
                    "direction": direction,
                    "spread": spread,
                    "conf_dev_entry": conf_dev_entry,
                    "conf_dev_close": conf_dev_close,
                },
                conn=conn,
            )
        return self._store(pair)

    def mark_entry_dispatched(self, pair_token: str) -> PairRecord:
        with self._repo.db.transaction() as conn:
            pair = self._repo.transition(
                pair_token,
                PairStatus.ENTRY_SENT,
                expected_current=PairStatus.PENDING,
                conn=conn,
            )
            self._journal.add_event(pair_token, PairEventType.ENTRY_DISPATCHED, {}, conn=conn)
        return self._store(pair)

    def apply_entry_results(
        self,
        pair_token: str,
        *,
        mt5_result: dict | None,
        exchange_result: OrderResult,
        exchange_volume: int,
        exchange_side: int,
    ) -> PairRecord:
        mt5_success = bool(mt5_result and mt5_result.get("success"))
        mt5_rejected = mt5_result is not None and not mt5_success
        mt5_unknown = mt5_result is None

        exchange_status = ExchangeOrderStatus(exchange_result.order_status)
        exchange_success = exchange_status == ExchangeOrderStatus.FILLED and exchange_result.success
        exchange_rejected = exchange_status == ExchangeOrderStatus.REJECTED
        exchange_unknown = exchange_status == ExchangeOrderStatus.UNKNOWN

        with self._repo.db.transaction() as conn:
            if mt5_success and exchange_success:
                pair = self._repo.transition(
                    pair_token,
                    PairStatus.OPEN,
                    expected_current=PairStatus.ENTRY_SENT,
                    fields={
                        "mt5_ticket": int(mt5_result.get("ticket", 0)),
                        "mt5_action": str(mt5_result.get("action", "")),
                        "mt5_volume": float(mt5_result.get("volume", 0.0)),
                        "mt5_entry_price": self._float_or_none(mt5_result.get("price")),
                        "mt5_entry_latency_ms": self._float_or_none(mt5_result.get("latency_ms")),
                        "exchange_order_id": exchange_result.order_id,
                        "exchange_volume": int(exchange_volume),
                        "exchange_side": int(exchange_side),
                        "exchange_entry_latency_ms": self._float_or_none(exchange_result.latency_ms),
                    },
                    conn=conn,
                )
                self._journal.add_event(pair_token, PairEventType.MT5_FILLED, self._mt5_event(mt5_result), conn=conn)
                self._journal.add_event(
                    pair_token,
                    PairEventType.EXCHANGE_FILLED,
                    self._exchange_event(exchange_result),
                    conn=conn,
                )
                self._journal.add_event(pair_token, PairEventType.ENTRY_COMPLETE, {}, conn=conn)
                return self._store(pair)

            if mt5_success and exchange_rejected:
                pair = self._repo.mark_mt5_entry_filled(
                    pair_token,
                    mt5_ticket=int(mt5_result.get("ticket", 0)),
                    mt5_action=str(mt5_result.get("action", "")),
                    mt5_volume=float(mt5_result.get("volume", 0.0)),
                    mt5_entry_price=self._float_or_none(mt5_result.get("price")),
                    mt5_entry_latency_ms=self._float_or_none(mt5_result.get("latency_ms")),
                    conn=conn,
                )
                self._journal.add_event(pair_token, PairEventType.MT5_FILLED, self._mt5_event(mt5_result), conn=conn)
                pair = self._repo.transition(
                    pair_token,
                    PairStatus.ORPHAN_MT5,
                    expected_current=PairStatus.ENTRY_MT5_FILLED,
                    conn=conn,
                )
                self._journal.add_event(
                    pair_token,
                    PairEventType.ENTRY_PARTIAL,
                    {
                        "filled_side": "MT5",
                        "error_side": "EXCHANGE",
                        "error": exchange_result.error,
                        "error_category": exchange_result.error_category.value if exchange_result.error_category else None,
                    },
                    conn=conn,
                )
                return self._store(pair)

            if exchange_success and mt5_rejected:
                pair = self._repo.mark_exchange_entry_filled(
                    pair_token,
                    exchange_order_id=exchange_result.order_id or "",
                    exchange_volume=int(exchange_volume),
                    exchange_side=int(exchange_side),
                    exchange_entry_latency_ms=self._float_or_none(exchange_result.latency_ms),
                    conn=conn,
                )
                self._journal.add_event(
                    pair_token,
                    PairEventType.EXCHANGE_FILLED,
                    self._exchange_event(exchange_result),
                    conn=conn,
                )
                pair = self._repo.transition(
                    pair_token,
                    PairStatus.ORPHAN_EXCHANGE,
                    expected_current=PairStatus.ENTRY_EXCHANGE_FILLED,
                    conn=conn,
                )
                self._journal.add_event(
                    pair_token,
                    PairEventType.ENTRY_PARTIAL,
                    {
                        "filled_side": "EXCHANGE",
                        "error_side": "MT5",
                        "error": mt5_result.get("error"),
                    },
                    conn=conn,
                )
                return self._store(pair)

            if mt5_success and exchange_unknown:
                pair = self._repo.mark_mt5_entry_filled(
                    pair_token,
                    mt5_ticket=int(mt5_result.get("ticket", 0)),
                    mt5_action=str(mt5_result.get("action", "")),
                    mt5_volume=float(mt5_result.get("volume", 0.0)),
                    mt5_entry_price=self._float_or_none(mt5_result.get("price")),
                    mt5_entry_latency_ms=self._float_or_none(mt5_result.get("latency_ms")),
                    conn=conn,
                )
                self._journal.add_event(pair_token, PairEventType.MT5_FILLED, self._mt5_event(mt5_result), conn=conn)
                return self._store(pair)

            if exchange_success and mt5_unknown:
                pair = self._repo.mark_exchange_entry_filled(
                    pair_token,
                    exchange_order_id=exchange_result.order_id or "",
                    exchange_volume=int(exchange_volume),
                    exchange_side=int(exchange_side),
                    exchange_entry_latency_ms=self._float_or_none(exchange_result.latency_ms),
                    conn=conn,
                )
                self._journal.add_event(
                    pair_token,
                    PairEventType.EXCHANGE_FILLED,
                    self._exchange_event(exchange_result),
                    conn=conn,
                )
                return self._store(pair)

            if mt5_rejected and exchange_rejected:
                pair = self._repo.transition(
                    pair_token,
                    PairStatus.FAILED,
                    expected_current=PairStatus.ENTRY_SENT,
                    conn=conn,
                )
                self._journal.add_event(
                    pair_token,
                    PairEventType.ENTRY_FAILED,
                    {
                        "mt5_error": mt5_result.get("error"),
                        "exchange_error": exchange_result.error,
                        "exchange_error_category": exchange_result.error_category.value if exchange_result.error_category else None,
                    },
                    conn=conn,
                )
                return self._store(pair)

        return self._pairs[pair_token]

    def begin_close(
        self,
        pair_token: str,
        *,
        close_reason: CloseReason,
        close_spread: float | None,
        force_terminal: bool = False,
    ) -> PairRecord:
        with self._repo.db.transaction() as conn:
            pair = self._repo.transition(
                pair_token,
                PairStatus.CLOSE_SENT,
                expected_current=PairStatus.OPEN,
                fields={
                    "close_reason": close_reason.value,
                    "close_spread": close_spread,
                },
                conn=conn,
            )
            event_type = PairEventType.FORCE_CLOSE if force_terminal else PairEventType.CLOSE_DISPATCHED
            self._journal.add_event(
                pair_token,
                event_type,
                {"close_reason": close_reason.value, "close_spread": close_spread},
                conn=conn,
            )
        return self._store(pair)

    def apply_close_results(
        self,
        pair_token: str,
        *,
        mt5_result: dict | None,
        exchange_result: OrderResult,
        success_status: PairStatus = PairStatus.CLOSED,
    ) -> PairRecord:
        mt5_success = bool(mt5_result and mt5_result.get("success"))
        mt5_rejected = mt5_result is not None and not mt5_success
        mt5_unknown = mt5_result is None

        exchange_status = ExchangeOrderStatus(exchange_result.order_status)
        exchange_success = exchange_status == ExchangeOrderStatus.FILLED and exchange_result.success
        exchange_rejected = exchange_status == ExchangeOrderStatus.REJECTED
        exchange_unknown = exchange_status == ExchangeOrderStatus.UNKNOWN

        with self._repo.db.transaction() as conn:
            if mt5_success and exchange_success:
                pair = self._repo.transition(
                    pair_token,
                    success_status,
                    expected_current=PairStatus.CLOSE_SENT,
                    fields={
                        "close_time": time.time(),
                        "mt5_close_price": self._float_or_none(mt5_result.get("close_price")),
                        "mt5_profit": self._float_or_none(mt5_result.get("profit")),
                        "mt5_close_latency_ms": self._float_or_none(mt5_result.get("latency_ms")),
                        "exchange_close_order_id": exchange_result.order_id,
                        "exchange_close_latency_ms": self._float_or_none(exchange_result.latency_ms),
                    },
                    conn=conn,
                )
                self._journal.add_event(pair_token, PairEventType.MT5_CLOSED, self._mt5_event(mt5_result), conn=conn)
                self._journal.add_event(
                    pair_token,
                    PairEventType.EXCHANGE_CLOSED,
                    self._exchange_event(exchange_result),
                    conn=conn,
                )
                self._journal.add_event(pair_token, PairEventType.CLOSE_COMPLETE, {}, conn=conn)
                return self._store(pair)

            if mt5_success and exchange_rejected:
                pair = self._repo.mark_mt5_close_done(
                    pair_token,
                    mt5_close_price=self._float_or_none(mt5_result.get("close_price")),
                    mt5_profit=self._float_or_none(mt5_result.get("profit")),
                    mt5_close_latency_ms=self._float_or_none(mt5_result.get("latency_ms")),
                    conn=conn,
                )
                self._journal.add_event(pair_token, PairEventType.MT5_CLOSED, self._mt5_event(mt5_result), conn=conn)
                pair = self._repo.transition(
                    pair_token,
                    PairStatus.ORPHAN_EXCHANGE,
                    expected_current=PairStatus.CLOSE_MT5_DONE,
                    conn=conn,
                )
                self._journal.add_event(
                    pair_token,
                    PairEventType.CLOSE_PARTIAL_FAIL,
                    {
                        "closed_side": "MT5",
                        "error_side": "EXCHANGE",
                        "error": exchange_result.error,
                        "error_category": exchange_result.error_category.value if exchange_result.error_category else None,
                    },
                    conn=conn,
                )
                return self._store(pair)

            if exchange_success and mt5_rejected:
                pair = self._repo.mark_exchange_close_done(
                    pair_token,
                    exchange_close_order_id=exchange_result.order_id or "",
                    exchange_close_latency_ms=self._float_or_none(exchange_result.latency_ms),
                    conn=conn,
                )
                self._journal.add_event(
                    pair_token,
                    PairEventType.EXCHANGE_CLOSED,
                    self._exchange_event(exchange_result),
                    conn=conn,
                )
                pair = self._repo.transition(
                    pair_token,
                    PairStatus.ORPHAN_MT5,
                    expected_current=PairStatus.CLOSE_EXCHANGE_DONE,
                    conn=conn,
                )
                self._journal.add_event(
                    pair_token,
                    PairEventType.CLOSE_PARTIAL_FAIL,
                    {
                        "closed_side": "EXCHANGE",
                        "error_side": "MT5",
                        "error": mt5_result.get("error"),
                    },
                    conn=conn,
                )
                return self._store(pair)

            if mt5_success and exchange_unknown:
                pair = self._repo.mark_mt5_close_done(
                    pair_token,
                    mt5_close_price=self._float_or_none(mt5_result.get("close_price")),
                    mt5_profit=self._float_or_none(mt5_result.get("profit")),
                    mt5_close_latency_ms=self._float_or_none(mt5_result.get("latency_ms")),
                    conn=conn,
                )
                self._journal.add_event(pair_token, PairEventType.MT5_CLOSED, self._mt5_event(mt5_result), conn=conn)
                return self._store(pair)

            if exchange_success and mt5_unknown:
                pair = self._repo.mark_exchange_close_done(
                    pair_token,
                    exchange_close_order_id=exchange_result.order_id or "",
                    exchange_close_latency_ms=self._float_or_none(exchange_result.latency_ms),
                    conn=conn,
                )
                self._journal.add_event(
                    pair_token,
                    PairEventType.EXCHANGE_CLOSED,
                    self._exchange_event(exchange_result),
                    conn=conn,
                )
                return self._store(pair)

        return self._pairs[pair_token]

    def mark_mt5_position_gone(self, ticket: int) -> PairRecord | None:
        pair = self.get_pair_by_mt5_ticket(ticket)
        if pair is None or pair.status != PairStatus.OPEN:
            return None

        with self._repo.db.transaction() as conn:
            updated = self._repo.transition(
                pair.pair_token,
                PairStatus.ORPHAN_EXCHANGE,
                expected_current=PairStatus.OPEN,
                conn=conn,
            )
            self._journal.add_event(
                pair.pair_token,
                PairEventType.STOPOUT_DETECTED,
                {"mt5_ticket": ticket},
                conn=conn,
            )
        return self._store(updated)

    def _store(self, pair: PairRecord) -> PairRecord:
        if pair.status in {PairStatus.CLOSED, PairStatus.FAILED, PairStatus.HEALED, PairStatus.FORCE_CLOSED}:
            self._pairs.pop(pair.pair_token, None)
        else:
            self._pairs[pair.pair_token] = pair
        self._sync_tracked_tickets()
        return pair

    def _sync_tracked_tickets(self) -> None:
        tickets = [
            str(pair.mt5_ticket)
            for pair in self._pairs.values()
            if pair.mt5_ticket is not None and pair.status in _MT5_TRACKED_STATUSES
        ]
        try:
            self._redis.delete(REDIS_MT5_TRACKED_TICKETS)
            if tickets:
                self._redis.sadd(REDIS_MT5_TRACKED_TICKETS, *tickets)
        except Exception as exc:
            log("ERROR", "PAIR", f"Failed to sync tracked tickets: {exc}")

    def _set_safe_mode(self, reason: str) -> None:
        self.safe_mode = True
        self.safe_mode_reason = reason
        log("WARN", "PAIR", "PairManager safe mode enabled", reason=reason)

    @staticmethod
    def _mt5_event(result: dict | None) -> dict:
        result = result or {}
        return {
            "ticket": result.get("ticket"),
            "action": result.get("action"),
            "volume": result.get("volume"),
            "price": result.get("price") or result.get("close_price"),
            "profit": result.get("profit"),
            "latency_ms": result.get("latency_ms"),
        }

    @staticmethod
    def _exchange_event(result: OrderResult) -> dict:
        return {
            "order_id": result.order_id,
            "status": result.order_status.value,
            "latency_ms": result.latency_ms,
            "error": result.error,
            "error_category": result.error_category.value if result.error_category else None,
        }

    @staticmethod
    def _float_or_none(value) -> float | None:
        if value is None:
            return None
        try:
            return float(value)
        except (TypeError, ValueError):
            return None
