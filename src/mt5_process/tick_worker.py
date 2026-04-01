"""Tick polling worker for the dedicated MT5 process."""

from __future__ import annotations

import threading
import time
from typing import Any

import orjson

from ..utils.constants import (
    REDIS_MT5_POSITION_GONE,
    REDIS_MT5_TICK,
    REDIS_MT5_TICK_CHANNEL,
    REDIS_MT5_TRACKED_TICKETS,
)
from ..utils.logger import log


class TickWorker(threading.Thread):
    """Poll MT5 ticks and monitor tracked tickets for missing positions."""

    def __init__(
        self,
        *,
        mt5_api: Any,
        api_lock: Any,
        redis_client,
        symbol: str,
        shutdown_event: threading.Event,
        position_check_interval_ms: int = 30,
    ) -> None:
        super().__init__(name="MT5-Tick", daemon=True)
        self._mt5 = mt5_api
        self._api_lock = api_lock
        self._redis = redis_client
        self._symbol = symbol
        self._shutdown_event = shutdown_event
        self._position_check_interval = max(position_check_interval_ms, 1) / 1000.0

        self.tick_count = 0
        self.position_gone_count = 0
        self._last_tick_time_msc = 0
        self._next_position_check = time.perf_counter()

    @staticmethod
    def build_tick_payload(
        tick: Any,
        *,
        now: float | None = None,
        symbol: str | None = None,
    ) -> dict[str, float | int | str]:
        """Convert the MT5 tick object into the shared Redis payload."""
        local_ts = time.time() if now is None else now
        payload: dict[str, float | int | str] = {
            "bid": float(getattr(tick, "bid", 0.0)),
            "ask": float(getattr(tick, "ask", 0.0)),
            "last": float(getattr(tick, "last", 0.0)),
            "time_msc": int(getattr(tick, "time_msc", int(local_ts * 1000))),
            "local_ts": local_ts,
        }
        if symbol:
            payload["symbol"] = str(symbol)
        return payload

    def run(self) -> None:
        log("INFO", "MT5T", "Tick worker started", symbol=self._symbol)
        while not self._shutdown_event.is_set():
            try:
                self._poll_tick()
                self._maybe_check_tracked_positions()
                time.sleep(0.001)
            except Exception as exc:
                log("ERROR", "MT5T", f"Tick worker error: {exc}")
                time.sleep(1)
        log("INFO", "MT5T", "Tick worker stopped")

    def _poll_tick(self) -> None:
        with self._api_lock:
            tick = self._mt5.symbol_info_tick(self._symbol)

        if tick is None:
            return

        payload = self.build_tick_payload(tick, symbol=self._symbol)
        if payload["time_msc"] == self._last_tick_time_msc:
            return

        self._last_tick_time_msc = int(payload["time_msc"])
        self.tick_count += 1

        raw = orjson.dumps(payload).decode("utf-8")
        self._redis.set(REDIS_MT5_TICK, raw)
        self._redis.publish(REDIS_MT5_TICK_CHANNEL, raw)

    def _maybe_check_tracked_positions(self) -> None:
        now = time.perf_counter()
        if now < self._next_position_check:
            return
        self._next_position_check = now + self._position_check_interval

        tracked_tickets = self._get_tracked_tickets()
        if not tracked_tickets:
            return

        for ticket in tracked_tickets:
            if self._shutdown_event.is_set():
                return
            self._check_single_ticket(ticket)

    def _get_tracked_tickets(self) -> list[int]:
        try:
            raw_tickets = self._redis.smembers(REDIS_MT5_TRACKED_TICKETS)
        except Exception as exc:
            log("ERROR", "MT5T", f"Failed to read tracked tickets: {exc}")
            return []

        tickets: list[int] = []
        for value in raw_tickets:
            try:
                tickets.append(int(value))
            except (TypeError, ValueError):
                log("WARN", "MT5T", "Ignoring invalid tracked ticket", value=value)
        return tickets

    def _check_single_ticket(self, ticket: int) -> None:
        with self._api_lock:
            positions = self._mt5.positions_get(ticket=ticket)

        if positions:
            return

        payload = {
            "ticket": ticket,
            "ts": time.time(),
            "reason": "missing_position",
        }
        raw = orjson.dumps(payload).decode("utf-8")
        self._redis.publish(REDIS_MT5_POSITION_GONE, raw)
        self._redis.srem(REDIS_MT5_TRACKED_TICKETS, str(ticket))
        self.position_gone_count += 1
        log("WARN", "MT5T", "Tracked MT5 position disappeared", ticket=ticket)
