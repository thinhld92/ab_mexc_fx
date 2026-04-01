"""Order worker for the dedicated MT5 process."""

from __future__ import annotations

import threading
import time
from typing import Any

import orjson

from ..utils.constants import (
    MT5_MAGIC,
    REDIS_MT5_ORDER_PROCESSING,
    REDIS_MT5_ORDER_QUEUE,
    REDIS_MT5_ORDER_RESULT,
    REDIS_MT5_POSITIONS_RESPONSE,
    REDIS_MT5_QUERY_POSITIONS,
)
from ..utils.logger import log


class OrderWorker(threading.Thread):
    """Execute MT5 orders and serve reconciliation position queries."""

    def __init__(
        self,
        *,
        mt5_api: Any,
        api_lock: Any,
        redis_client,
        symbol: str,
        shutdown_event: threading.Event,
        magic: int = MT5_MAGIC,
    ) -> None:
        super().__init__(name="MT5-Order", daemon=True)
        self._mt5 = mt5_api
        self._api_lock = api_lock
        self._redis = redis_client
        self._symbol = symbol
        self._shutdown_event = shutdown_event
        self._magic = magic

        self.order_count = 0
        self.recovered_count = 0
        self.position_query_count = 0

    @staticmethod
    def serialize_positions(positions: list[Any] | tuple[Any, ...], *, magic: int = MT5_MAGIC) -> list[dict]:
        """Serialize only our tracked MT5 positions into the shared response payload."""
        serialized: list[dict] = []
        for pos in positions or []:
            if getattr(pos, "magic", None) != magic:
                continue
            serialized.append(
                {
                    "ticket": int(getattr(pos, "ticket", 0)),
                    "type": "BUY"
                    if int(getattr(pos, "type", 0)) == 0
                    else "SELL",
                    "volume": float(getattr(pos, "volume", 0.0)),
                    "price": float(getattr(pos, "price_open", 0.0)),
                    "profit": float(getattr(pos, "profit", 0.0)),
                    "magic": int(getattr(pos, "magic", 0)),
                    "comment": str(getattr(pos, "comment", "")),
                }
            )
        return serialized

    def run(self) -> None:
        log("INFO", "MT5O", "Order worker started", symbol=self._symbol)
        self._recover_processing_queue()

        while not self._shutdown_event.is_set():
            try:
                if self._handle_position_query():
                    continue

                raw_command = self._redis.brpoplpush(
                    REDIS_MT5_ORDER_QUEUE,
                    REDIS_MT5_ORDER_PROCESSING,
                    timeout=1,
                )
                if raw_command is None:
                    continue

                self._process_live_command(raw_command)
            except Exception as exc:
                log("ERROR", "MT5O", f"Order worker error: {exc}")
                time.sleep(1)

        log("INFO", "MT5O", "Order worker stopped")

    def _process_live_command(self, raw_command: str) -> None:
        command = self._decode_command(raw_command)
        job_id = str(command.get("job_id", ""))
        processed = False

        try:
            result = self._execute_command(command)
            self._push_result(result)
            processed = True
            self.order_count += 1
        except Exception as exc:
            log("ERROR", "MT5O", f"Command execution failed: {exc}", job_id=job_id)
            failure = {
                "success": False,
                "job_id": job_id,
                "error": str(exc),
                "latency_ms": 0.0,
            }
            try:
                self._push_result(failure)
                processed = True
            except Exception as push_exc:
                log("ERROR", "MT5O", f"Failed to publish MT5 failure result: {push_exc}")

        if processed:
            self._remove_processing_item(raw_command)

    def _recover_processing_queue(self) -> None:
        pending = self._redis.lrange(REDIS_MT5_ORDER_PROCESSING, 0, -1)
        if not pending:
            return

        log("WARN", "MT5O", "Recovering unfinished MT5 commands", count=len(pending))

        for raw_command in reversed(pending):
            if self._shutdown_event.is_set():
                return

            try:
                command = self._decode_command(raw_command)
                recovered = self._recover_command(command)
                if recovered is not None:
                    self._push_result(recovered)
                    self._remove_processing_item(raw_command)
                    self.recovered_count += 1
                    continue

                self._requeue_processing_item(raw_command)
            except Exception as exc:
                log("ERROR", "MT5O", f"Recovery failed: {exc}")

    def _handle_position_query(self) -> bool:
        raw_query = self._redis.rpop(REDIS_MT5_QUERY_POSITIONS)
        if raw_query is None:
            return False

        query = self._decode_command(raw_query)
        query_id = str(query.get("query_id", ""))

        with self._api_lock:
            positions = self._mt5.positions_get(symbol=self._symbol)

        response = {
            "query_id": query_id,
            "ts": time.time(),
            "positions": self.serialize_positions(positions, magic=self._magic),
        }
        self._redis.lpush(
            REDIS_MT5_POSITIONS_RESPONSE,
            orjson.dumps(response).decode("utf-8"),
        )
        self.position_query_count += 1
        return True

    def _recover_command(self, command: dict) -> dict | None:
        action = str(command.get("action", "")).upper()
        if action == "CLOSE":
            return self._recover_close_command(command)
        if action in {"BUY", "SELL"}:
            return self._recover_open_command(command)
        return {
            "success": False,
            "job_id": str(command.get("job_id", "")),
            "error": f"Unknown action during recovery: {action}",
            "latency_ms": 0.0,
            "recovered": True,
        }

    def _recover_open_command(self, command: dict) -> dict | None:
        action = str(command.get("action", "")).upper()
        comment = str(command.get("comment", ""))
        job_id = str(command.get("job_id", ""))

        try:
            volume = float(command.get("volume", 0.0))
        except (TypeError, ValueError):
            volume = 0.0

        with self._api_lock:
            positions = self._mt5.positions_get(symbol=self._symbol)

        expected_type = 0 if action == "BUY" else 1
        for pos in positions or []:
            if getattr(pos, "magic", None) != self._magic:
                continue
            if int(getattr(pos, "type", -1)) != expected_type:
                continue
            if comment and str(getattr(pos, "comment", "")) != comment:
                continue
            if volume and abs(float(getattr(pos, "volume", 0.0)) - volume) > 1e-9:
                continue
            return {
                "success": True,
                "job_id": job_id,
                "ticket": int(getattr(pos, "ticket", 0)),
                "price": float(getattr(pos, "price_open", 0.0)),
                "volume": float(getattr(pos, "volume", 0.0)),
                "action": action,
                "latency_ms": 0.0,
                "recovered": True,
            }

        return None

    def _recover_close_command(self, command: dict) -> dict | None:
        job_id = str(command.get("job_id", ""))
        try:
            ticket = int(command.get("ticket", 0))
        except (TypeError, ValueError):
            ticket = 0

        if ticket <= 0:
            return {
                "success": False,
                "job_id": job_id,
                "error": "Invalid CLOSE command without ticket",
                "latency_ms": 0.0,
                "recovered": True,
            }

        with self._api_lock:
            positions = self._mt5.positions_get(ticket=ticket)

        if positions:
            return None

        return {
            "success": True,
            "job_id": job_id,
            "ticket": ticket,
            "action": "CLOSE",
            "close_price": None,
            "volume": float(command.get("volume", 0.0) or 0.0),
            "profit": 0.0,
            "latency_ms": 0.0,
            "recovered": True,
        }

    def _execute_command(self, command: dict) -> dict:
        action = str(command.get("action", "")).upper()
        job_id = str(command.get("job_id", ""))
        started_at = time.perf_counter()

        if action == "CLOSE":
            result = self._close_position(command)
        elif action in {"BUY", "SELL"}:
            result = self._open_position(action, command)
        else:
            result = {
                "success": False,
                "error": f"Unknown action: {action}",
            }

        result["job_id"] = job_id
        result["latency_ms"] = round((time.perf_counter() - started_at) * 1000, 3)
        return result

    def _open_position(self, action: str, command: dict) -> dict:
        try:
            volume = float(command.get("volume", 0.0))
        except (TypeError, ValueError):
            return {"success": False, "error": "Invalid volume"}

        comment = str(command.get("comment", "arb"))

        with self._api_lock:
            tick = self._mt5.symbol_info_tick(self._symbol)
            if tick is None:
                return {"success": False, "error": "No tick data"}

            order_type = (
                self._mt5.ORDER_TYPE_BUY if action == "BUY" else self._mt5.ORDER_TYPE_SELL
            )
            price = tick.ask if action == "BUY" else tick.bid
            request = {
                "action": self._mt5.TRADE_ACTION_DEAL,
                "symbol": self._symbol,
                "volume": volume,
                "type": order_type,
                "price": price,
                "deviation": 50,
                "magic": self._magic,
                "comment": comment,
                "type_time": self._mt5.ORDER_TIME_GTC,
                "type_filling": self._mt5.ORDER_FILLING_IOC,
            }
            result = self._mt5.order_send(request)

        if result is None:
            return {"success": False, "error": str(self._mt5.last_error())}

        if result.retcode == self._mt5.TRADE_RETCODE_DONE:
            return {
                "success": True,
                "ticket": int(getattr(result, "order", 0)),
                "price": float(getattr(result, "price", 0.0)),
                "volume": volume,
                "action": action,
            }

        return {
            "success": False,
            "error": f"retcode={result.retcode}: {result.comment}",
            "retcode": int(result.retcode),
        }

    def _close_position(self, command: dict) -> dict:
        try:
            ticket = int(command.get("ticket", 0))
        except (TypeError, ValueError):
            ticket = 0

        if ticket <= 0:
            return {"success": False, "error": "No ticket provided"}

        volume = command.get("volume")

        with self._api_lock:
            positions = self._mt5.positions_get(ticket=ticket)
            if not positions:
                return {"success": False, "error": f"Position {ticket} not found"}

            pos = positions[0]
            close_type = (
                self._mt5.ORDER_TYPE_SELL
                if pos.type == self._mt5.ORDER_TYPE_BUY
                else self._mt5.ORDER_TYPE_BUY
            )
            tick = self._mt5.symbol_info_tick(self._symbol)
            if tick is None:
                return {"success": False, "error": "No tick data"}

            request = {
                "action": self._mt5.TRADE_ACTION_DEAL,
                "symbol": self._symbol,
                "volume": float(volume or pos.volume),
                "type": close_type,
                "position": ticket,
                "price": tick.bid if close_type == self._mt5.ORDER_TYPE_SELL else tick.ask,
                "deviation": 50,
                "magic": self._magic,
                "comment": "arb_close",
                "type_time": self._mt5.ORDER_TIME_GTC,
                "type_filling": self._mt5.ORDER_FILLING_IOC,
            }
            result = self._mt5.order_send(request)

        if result is None:
            return {"success": False, "error": str(self._mt5.last_error())}

        if result.retcode == self._mt5.TRADE_RETCODE_DONE:
            return {
                "success": True,
                "ticket": ticket,
                "close_price": float(getattr(result, "price", 0.0)),
                "volume": float(volume or pos.volume),
                "profit": self._get_deal_profit(ticket),
                "action": "CLOSE",
            }

        return {
            "success": False,
            "error": f"retcode={result.retcode}: {result.comment}",
            "retcode": int(result.retcode),
        }

    def _get_deal_profit(self, position_ticket: int) -> float:
        time.sleep(0.1)
        with self._api_lock:
            deals = self._mt5.history_deals_get(position=position_ticket)
        if not deals:
            return 0.0
        return float(sum(float(getattr(deal, "profit", 0.0)) for deal in deals))

    def _push_result(self, result: dict) -> None:
        self._redis.lpush(
            REDIS_MT5_ORDER_RESULT,
            orjson.dumps(result).decode("utf-8"),
        )

    def _remove_processing_item(self, raw_command: str) -> None:
        self._redis.lrem(REDIS_MT5_ORDER_PROCESSING, 1, raw_command)

    def _requeue_processing_item(self, raw_command: str) -> None:
        self._redis.rpush(REDIS_MT5_ORDER_QUEUE, raw_command)
        self._remove_processing_item(raw_command)

    @staticmethod
    def _decode_command(raw_command: str) -> dict:
        data = orjson.loads(raw_command)
        if not isinstance(data, dict):
            raise ValueError("Expected JSON object payload")
        return data
