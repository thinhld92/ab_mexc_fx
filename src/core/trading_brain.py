"""Phase 3 event-driven trading brain."""

from __future__ import annotations

import asyncio
import math
import os
import time
import uuid
from datetime import datetime, timezone

import orjson

from ..domain import CloseReason, PairStatus
from ..exchanges.base import ErrorCategory, ExchangeOrderStatus, OrderResult, OrderSide
from ..utils.constants import (
    REDIS_BRAIN_HEARTBEAT,
    REDIS_EXCHANGE_TICK,
    REDIS_MT5_HEARTBEAT,
    REDIS_MT5_ORDER_QUEUE,
    REDIS_MT5_ORDER_RESULT,
    REDIS_MT5_POSITION_GONE,
    REDIS_MT5_TICK,
    REDIS_MT5_TICK_CHANNEL,
)
from ..utils.logger import log
from ..utils.spread_logger import SpreadLogger
from .pair_manager import PairManager


class TradingBrain:
    """Consumes MT5 + exchange events and manages pair lifecycle."""

    HEARTBEAT_TTL_SEC = 30

    def __init__(self, config, redis_client, exchange, pair_manager: PairManager, shutdown_event: asyncio.Event):
        self.config = config
        self.redis = redis_client
        self.exchange = exchange
        self.pair_manager = pair_manager
        self.shutdown_event = shutdown_event

        self._loop: asyncio.AbstractEventLoop | None = None
        self._event_queue: asyncio.Queue = asyncio.Queue()
        self._pubsub = None
        self._tasks: list[asyncio.Task] = []
        self._spread_logger = SpreadLogger()

        self._latest_mt5: dict | None = None
        self._latest_exchange: dict | None = None
        self._current_signal = "NONE"
        self._debounce_task: asyncio.Task | None = None
        self._is_executing = False
        self._risk_flag = False
        self._risk_reason = ""
        self._last_entry_time = 0.0
        self._last_close_time = 0.0
        self._warmup_started_at = 0.0
        self._warmup_tick_count = 0
        self._warmup_complete = False
        self._mt5_result_cache: dict[str, dict] = {}
        self._pivot_ema: float | None = None
        self._pivot_last_ts = 0.0
        self._orphan_seen_tokens: set[str] = set()
        self._orphan_cooldown_until = 0.0
        self._auth_last_checked_at = 0.0
        self._auth_last_alive: bool | None = None

        self._max_orders = max(0, min(int(self.config.max_orders), 1))
        if self.config.max_orders > 1:
            log("WARN", "BRAIN", "Clamping max_orders to 1 for Phase 3", configured=self.config.max_orders)

    async def run(self) -> None:
        self._loop = asyncio.get_running_loop()
        self.exchange.set_tick_callback(self._on_exchange_tick)
        self._load_initial_snapshots()
        self._start_background_tasks()

        log(
            "INFO",
            "BRAIN",
            "Trading brain started",
            safe_mode=self.pair_manager.safe_mode,
            recovered=self.pair_manager.pair_count,
        )

        try:
            while not self.shutdown_event.is_set():
                event = await self._event_queue.get()
                event_type = event.get("type")
                if event_type == "shutdown":
                    break
                if event_type == "mt5_tick":
                    self._latest_mt5 = event["payload"]
                    await self._handle_tick()
                elif event_type == "exchange_tick":
                    self._latest_exchange = event["payload"]
                    await self._handle_tick()
                elif event_type == "mt5_position_gone":
                    await self._handle_position_gone(event["payload"])
        finally:
            await self._shutdown()

    async def request_shutdown(self) -> None:
        if not self.shutdown_event.is_set():
            self.shutdown_event.set()
        await self._event_queue.put({"type": "shutdown"})

    def _start_background_tasks(self) -> None:
        self._pubsub = self.redis.pubsub(ignore_subscribe_messages=True)
        self._pubsub.subscribe(REDIS_MT5_TICK_CHANNEL, REDIS_MT5_POSITION_GONE)
        self._tasks = [
            asyncio.create_task(self._pubsub_loop(), name="brain-pubsub"),
            asyncio.create_task(self._heartbeat_loop(), name="brain-heartbeat"),
            asyncio.create_task(self._auth_check_loop(), name="brain-auth"),
            asyncio.create_task(self._maintenance_loop(), name="brain-maintenance"),
            asyncio.create_task(self._status_log_loop(), name="brain-log"),
        ]

    def _load_initial_snapshots(self) -> None:
        try:
            mt5_raw = self.redis.get(REDIS_MT5_TICK)
            if mt5_raw:
                mt5_tick = self._decode_payload(mt5_raw)
                if self._is_expected_mt5_tick(mt5_tick):
                    self._latest_mt5 = mt5_tick
        except Exception:
            self._latest_mt5 = None

        try:
            exchange_raw = self.redis.get(REDIS_EXCHANGE_TICK)
            if exchange_raw:
                exchange_tick = self._decode_payload(exchange_raw)
                if self._is_expected_exchange_tick(exchange_tick):
                    self._latest_exchange = exchange_tick
        except Exception:
            self._latest_exchange = None

        latest_exchange = self.exchange.get_latest_tick()
        if latest_exchange and self._is_expected_exchange_tick(latest_exchange):
            self._latest_exchange = latest_exchange

    async def _pubsub_loop(self) -> None:
        try:
            while not self.shutdown_event.is_set():
                message = await asyncio.to_thread(
                    self._pubsub.get_message,
                    ignore_subscribe_messages=True,
                    timeout=1.0,
                )
                if not message:
                    continue

                channel = message.get("channel")
                data = message.get("data")
                if channel == REDIS_MT5_TICK_CHANNEL:
                    await self._event_queue.put({"type": "mt5_tick", "payload": self._decode_payload(data)})
                elif channel == REDIS_MT5_POSITION_GONE:
                    await self._event_queue.put(
                        {"type": "mt5_position_gone", "payload": self._decode_payload(data)}
                    )
        except asyncio.CancelledError:
            pass

    def _on_exchange_tick(self, tick: dict) -> None:
        if self._loop is None:
            return
        self._loop.call_soon_threadsafe(
            self._event_queue.put_nowait,
            {"type": "exchange_tick", "payload": dict(tick)},
        )

    async def _heartbeat_loop(self) -> None:
        try:
            while not self.shutdown_event.is_set():
                payload = {
                    "ts": time.time(),
                    "pair_count": self.pair_manager.pair_count,
                    "safe_mode": self.pair_manager.safe_mode,
                    "risk_flag": self._risk_flag,
                    "warmup_complete": self._warmup_complete,
                    "pid": os.getpid(),
                }
                self.redis.set(
                    REDIS_BRAIN_HEARTBEAT,
                    orjson.dumps(payload).decode("utf-8"),
                    ex=self.HEARTBEAT_TTL_SEC,
                )
                await asyncio.sleep(max(self.config.brain_heartbeat_interval_sec, 1))
        except asyncio.CancelledError:
            pass

    async def _auth_check_loop(self) -> None:
        interval = max(self.config.auth_check_interval_sec, 1)
        try:
            while not self.shutdown_event.is_set():
                await asyncio.sleep(interval)
                alive = await self.exchange.check_auth_alive()
                self._record_auth_check(alive)
                if alive:
                    continue
                self._trip_risk("exchange_auth_dead", "Exchange auth check failed; risk latch enabled")
        except asyncio.CancelledError:
            pass

    async def _maintenance_loop(self) -> None:
        try:
            while not self.shutdown_event.is_set():
                await self._run_maintenance_checks()
                await asyncio.sleep(1.0)
        except asyncio.CancelledError:
            pass

    async def _run_maintenance_checks(self) -> None:
        self._enforce_mt5_equity_floor()
        await self._enforce_time_based_closes()

    def _enforce_mt5_equity_floor(self) -> None:
        threshold = max(float(getattr(self.config, "alert_equity_mt5", 0.0) or 0.0), 0.0)
        if threshold <= 0:
            return

        try:
            raw = self.redis.get(REDIS_MT5_HEARTBEAT)
            if not raw:
                return
            heartbeat = self._decode_payload(raw)
        except Exception:
            return

        try:
            equity = float(heartbeat.get("equity"))
        except (TypeError, ValueError):
            return

        if equity < threshold:
            self._trip_risk(
                f"mt5_equity_below:{equity:.2f}",
                "MT5 equity below configured threshold; risk latch enabled",
                equity=f"{equity:.2f}",
                threshold=f"{threshold:.2f}",
            )

    async def _enforce_time_based_closes(self) -> None:
        pair = self.pair_manager.get_open_pair()
        if pair is None or pair.status != PairStatus.OPEN or self._is_executing:
            return

        if self._is_force_close_time():
            log("WARN", "BRAIN", "Force-close schedule window active", pair=pair.pair_token)
            await self._execute_close(
                close_reason=CloseReason.FORCE_SCHEDULE,
                success_status=PairStatus.FORCE_CLOSED,
            )
            return

        hold_time_sec = max(int(getattr(self.config, "hold_time", 0) or 0), 0)
        if hold_time_sec <= 0:
            return

        age_sec = max(0.0, time.time() - float(pair.entry_time))
        if age_sec < hold_time_sec:
            return

        log(
            "WARN",
            "BRAIN",
            "Hold timeout reached; forcing close",
            pair=pair.pair_token,
            age=f"{age_sec:.1f}s",
            hold_time=hold_time_sec,
        )
        await self._execute_close(
            close_reason=CloseReason.HOLD_TIMEOUT,
            success_status=PairStatus.FORCE_CLOSED,
        )

    async def _status_log_loop(self) -> None:
        interval = max(self.config.brain_log_interval_sec, 1)
        try:
            while not self.shutdown_event.is_set():
                await asyncio.sleep(interval)
                if not self._latest_mt5 or not self._latest_exchange:
                    continue
                spread = self._calculate_spread(self._latest_mt5, self._latest_exchange)
                pivot = self._current_pivot(spread)
                pivot_source = self._current_pivot_source()
                signal = self._detect_signal(spread, pivot=pivot)
                log(
                    "INFO",
                    "BRAIN",
                    "Brain status",
                    spread=f"{spread:+.4f}",
                    pivot=f"{pivot:+.4f}",
                    gap=f"{(spread - pivot):+.4f}",
                    pivot_mode=pivot_source,
                    signal=signal,
                    pairs=self.pair_manager.pair_count,
                    safe_mode=self.pair_manager.safe_mode,
                    risk=self._risk_flag,
                    orphan_cooldown=max(0.0, self._orphan_cooldown_until - time.time()),
                )
        except asyncio.CancelledError:
            pass

    async def _handle_tick(self) -> None:
        if not self._latest_mt5 or not self._latest_exchange:
            return

        self._note_warmup_sample()
        spread = self._calculate_spread(self._latest_mt5, self._latest_exchange)
        self._update_pivot_ema(spread)
        pivot = self._current_pivot(spread)
        signal = self._detect_signal(spread, pivot=pivot)

        self._spread_logger.write(
            mt5_tick=self._latest_mt5,
            exchange_tick=self._latest_exchange,
            spread=spread,
            pivot=pivot,
            gap=spread - pivot,
            pivot_source=self._current_pivot_source(),
            signal_name=signal,
            pair_count=self.pair_manager.pair_count,
        )

        if self._risk_flag:
            self._cancel_debounce()
            return

        open_pair = self.pair_manager.get_open_pair()
        if signal == "CLOSE" and open_pair is not None:
            self._start_debounce(signal)
            return

        if signal in {"ENTRY_LONG", "ENTRY_SHORT"}:
            if (
                self._entry_allowed_now()
                and self.pair_manager.allows_new_entries
                and self._is_trading_time()
                and not self._is_orphan_cooldown_active()
            ):
                self._start_debounce(signal)
                return

        self._cancel_debounce()

    def _note_warmup_sample(self) -> None:
        if self._warmup_complete:
            return
        if self._warmup_started_at == 0.0:
            self._warmup_started_at = time.time()
            log("INFO", "BRAIN", "Warmup started")
        self._warmup_tick_count += 1

        elapsed = time.time() - self._warmup_started_at
        if elapsed < self.config.warmup_sec:
            return
        if self._warmup_tick_count < self.config.warmup_min_ticks:
            return

        self._warmup_complete = True
        log(
            "SUCCESS",
            "BRAIN",
            "Warmup complete",
            ticks=self._warmup_tick_count,
            elapsed=f"{elapsed:.1f}s",
        )

    def _start_debounce(self, signal: str) -> None:
        if self._current_signal == signal and self._debounce_task and not self._debounce_task.done():
            return
        self._cancel_debounce()
        self._current_signal = signal
        self._debounce_task = asyncio.create_task(self._debounce_execute(signal), name=f"debounce-{signal}")

    def _cancel_debounce(self) -> None:
        if self._debounce_task and not self._debounce_task.done():
            self._debounce_task.cancel()
        self._debounce_task = None
        self._current_signal = "NONE"

    async def _debounce_execute(self, signal: str) -> None:
        try:
            await asyncio.sleep(max(self.config.stable_time_ms, 0) / 1000)
            abort_reason = self._gate_abort_reason(signal)
            if abort_reason:
                log("INFO", "BRAIN", "Debounce aborted", signal=signal, reason=abort_reason)
                return
            await self._execute_signal(signal)
        except asyncio.CancelledError:
            pass
        finally:
            if self._current_signal == signal:
                self._current_signal = "NONE"
            if self._debounce_task and self._debounce_task.done():
                self._debounce_task = None

    def _gate_abort_reason(self, signal: str) -> str | None:
        if not self._latest_mt5 or not self._latest_exchange:
            return "missing_ticks"

        spread = self._calculate_spread(self._latest_mt5, self._latest_exchange)
        pivot = self._current_pivot(spread)
        if self._detect_signal(spread, pivot=pivot) != signal:
            return "signal_changed"
        if not self._is_data_fresh(self._latest_mt5, self._latest_exchange):
            return "stale_tick"
        if self._risk_flag:
            return "risk_flag"
        if signal == "CLOSE":
            pair = self.pair_manager.get_open_pair()
            if pair is None or pair.status != PairStatus.OPEN:
                return "pair_state"
        if signal.startswith("ENTRY"):
            if not self._entry_allowed_now():
                return "warmup"
            if not self.pair_manager.allows_new_entries:
                return "pair_state"
            if self._is_orphan_cooldown_active():
                return "orphan_cooldown"
            if not self._is_trading_time():
                return "outside_trading_hours"
            if self._max_orders == 0:
                return "max_orders_zero"
        if self._is_executing:
            return "executing"
        return None

    async def _execute_signal(self, signal: str) -> None:
        self._is_executing = True
        try:
            if signal == "ENTRY_LONG":
                await self._execute_entry(direction="LONG")
            elif signal == "ENTRY_SHORT":
                await self._execute_entry(direction="SHORT")
            elif signal == "CLOSE":
                await self._execute_close(close_reason=CloseReason.SIGNAL)
        finally:
            self._is_executing = False

    async def _execute_entry(self, *, direction: str) -> None:
        now = time.time()
        if now - self._last_entry_time < self.config.cooldown_entry:
            return
        if not self._latest_mt5 or not self._latest_exchange:
            return
        if not await self._ensure_exchange_auth_for_entry():
            return

        spread = self._calculate_spread(self._latest_mt5, self._latest_exchange)
        if direction == "LONG":
            mt5_action = "BUY"
            exchange_side = OrderSide.OPEN_SHORT
        else:
            mt5_action = "SELL"
            exchange_side = OrderSide.OPEN_LONG

        pair = self.pair_manager.create_entry(
            direction=direction,
            spread=spread,
            conf_dev_entry=self.config.dev_entry,
            conf_dev_close=self.config.dev_close,
        )
        self.pair_manager.mark_entry_dispatched(pair.pair_token)

        job_id = uuid_token()
        mt5_command = {
            "action": mt5_action,
            "volume": self.config.mt5_volume,
            "job_id": job_id,
            "pair_token": pair.pair_token,
            "comment": f"arb:{pair.pair_token}",
            "ts": time.time(),
        }
        self.redis.lpush(REDIS_MT5_ORDER_QUEUE, orjson.dumps(mt5_command).decode("utf-8"))

        exchange_result = await self._safe_exchange_order(exchange_side, self.config.mexc_volume)
        mt5_result = await self._wait_mt5_result(job_id, timeout=5.0)

        updated = self.pair_manager.apply_entry_results(
            pair.pair_token,
            mt5_result=mt5_result,
            exchange_result=exchange_result,
            exchange_volume=int(self.config.mexc_volume),
            exchange_side=int(exchange_side),
        )
        self._observe_pair_update(updated)

        if updated.status == PairStatus.OPEN:
            self._last_entry_time = now
            log("SUCCESS", "BRAIN", "Entry complete", pair=pair.pair_token, direction=direction)
        else:
            log("WARN", "BRAIN", "Entry ended non-terminal/partial", pair=pair.pair_token, status=updated.status.value)

    async def _ensure_exchange_auth_for_entry(self) -> bool:
        if self._risk_flag:
            return False

        ttl_sec = max(float(getattr(self.config, "auth_cache_ttl_sec", 2.0) or 0.0), 0.0)
        cache_age = time.time() - self._auth_last_checked_at
        if (
            self._auth_last_checked_at > 0.0
            and self._auth_last_alive is not None
            and cache_age <= ttl_sec
        ):
            return bool(self._auth_last_alive)

        try:
            alive = await self.exchange.check_auth_alive()
        except Exception as exc:
            self._trip_risk(
                "exchange_auth_dead",
                "Exchange auth preflight failed; entry blocked",
                error=str(exc),
            )
            return False

        self._record_auth_check(alive)
        if alive:
            return True

        self._trip_risk("exchange_auth_dead", "Exchange auth preflight failed; entry blocked")
        return False

    async def _execute_close(
        self,
        *,
        close_reason: CloseReason,
        success_status: PairStatus = PairStatus.CLOSED,
    ) -> None:
        now = time.time()
        if now - self._last_close_time < self.config.cooldown_close:
            return

        pair = self.pair_manager.get_open_pair()
        if pair is None:
            return

        spread = 0.0
        if self._latest_mt5 and self._latest_exchange:
            spread = self._calculate_spread(self._latest_mt5, self._latest_exchange)

        self.pair_manager.begin_close(
            pair.pair_token,
            close_reason=close_reason,
            close_spread=spread,
            force_terminal=success_status == PairStatus.FORCE_CLOSED,
        )

        if pair.direction == "LONG":
            exchange_side = OrderSide.CLOSE_SHORT
        else:
            exchange_side = OrderSide.CLOSE_LONG

        job_id = uuid_token()
        mt5_command = {
            "action": "CLOSE",
            "ticket": pair.mt5_ticket,
            "volume": pair.raw.get("mt5_volume", self.config.mt5_volume),
            "job_id": job_id,
            "pair_token": pair.pair_token,
            "ts": time.time(),
        }
        self.redis.lpush(REDIS_MT5_ORDER_QUEUE, orjson.dumps(mt5_command).decode("utf-8"))

        exchange_volume = int(pair.raw.get("exchange_volume") or self.config.mexc_volume)
        exchange_result = await self._safe_exchange_order(exchange_side, exchange_volume)
        mt5_result = await self._wait_mt5_result(job_id, timeout=5.0)

        updated = self.pair_manager.apply_close_results(
            pair.pair_token,
            mt5_result=mt5_result,
            exchange_result=exchange_result,
            success_status=success_status,
        )
        self._observe_pair_update(updated)
        if updated.status in {PairStatus.CLOSED, PairStatus.FORCE_CLOSED}:
            self._last_close_time = now
            log("SUCCESS", "BRAIN", "Close complete", pair=pair.pair_token, status=updated.status.value)
        else:
            log("WARN", "BRAIN", "Close ended non-terminal/partial", pair=pair.pair_token, status=updated.status.value)

    async def _handle_position_gone(self, payload: dict) -> None:
        ticket = int(payload.get("ticket", 0))
        if ticket <= 0:
            return
        self._trip_risk(f"mt5_position_gone:{ticket}", "MT5 position disappeared; risk latch enabled", ticket=ticket)
        pair = self.pair_manager.mark_mt5_position_gone(ticket)
        self._observe_pair_update(pair)
        if pair is not None:
            log("ERROR", "BRAIN", "MT5 position disappeared; orphan exchange state recorded", ticket=ticket)

    async def _wait_mt5_result(self, job_id: str, timeout: float) -> dict | None:
        cached = self._mt5_result_cache.pop(job_id, None)
        if cached is not None:
            return cached

        deadline = time.monotonic() + timeout
        while not self.shutdown_event.is_set():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None

            raw = await asyncio.to_thread(self.redis.brpop, REDIS_MT5_ORDER_RESULT, max(int(remaining), 1))
            if raw is None:
                continue

            if isinstance(raw, (list, tuple)):
                raw = raw[1]
            payload = self._decode_payload(raw)
            result_job_id = str(payload.get("job_id", ""))
            if result_job_id == job_id:
                return payload
            if result_job_id:
                self._mt5_result_cache[result_job_id] = payload
        return None

    async def _safe_exchange_order(self, side: OrderSide, volume: int | float) -> OrderResult:
        try:
            result = await self.exchange.place_order(side, volume)
        except Exception as exc:
            log("ERROR", "BRAIN", f"Exchange place_order crashed: {exc}")
            return OrderResult(
                success=False,
                order_status=ExchangeOrderStatus.UNKNOWN,
                error=str(exc),
                error_category=ErrorCategory.UNKNOWN,
            )
        if result.error_category == ErrorCategory.AUTH_EXPIRED:
            self._trip_risk("exchange_auth_dead", "Exchange order rejected due to auth failure; risk latch enabled")
        return result

    def _trip_risk(self, reason: str, message: str, **context) -> None:
        already_set = self._risk_flag and self._risk_reason == reason
        self._risk_flag = True
        self._risk_reason = reason
        self._cancel_debounce()
        if not already_set:
            log("ERROR", "BRAIN", message, reason=reason, **context)

    def _record_auth_check(self, alive: bool, *, checked_at: float | None = None) -> None:
        ts = time.time() if checked_at is None else float(checked_at)
        self._auth_last_checked_at = ts
        self._auth_last_alive = bool(alive)

    def _observe_pair_update(self, pair) -> None:
        if pair is None:
            return
        if pair.status not in {PairStatus.ORPHAN_MT5, PairStatus.ORPHAN_EXCHANGE}:
            return

        cooldown_sec = max(int(getattr(self.config, "orphan_cooldown_sec", 0) or 0), 0)
        if cooldown_sec > 0:
            self._orphan_cooldown_until = max(self._orphan_cooldown_until, time.time() + cooldown_sec)
            log(
                "WARN",
                "BRAIN",
                "Orphan cooldown armed",
                pair=pair.pair_token,
                status=pair.status.value,
                cooldown_sec=cooldown_sec,
            )

        if pair.pair_token in self._orphan_seen_tokens:
            return
        self._orphan_seen_tokens.add(pair.pair_token)

        orphan_max_count = max(int(getattr(self.config, "orphan_max_count", 0) or 0), 0)
        if orphan_max_count > 0 and len(self._orphan_seen_tokens) >= orphan_max_count:
            self._trip_risk(
                f"orphan_limit:{len(self._orphan_seen_tokens)}",
                "Orphan incident limit reached; risk latch enabled",
                count=len(self._orphan_seen_tokens),
                limit=orphan_max_count,
            )

    def _should_close_open_pair(self, gap: float, direction: str | None) -> bool:
        close_threshold = float(self.config.dev_close)
        if close_threshold >= 0.0:
            return abs(gap) < close_threshold

        opposite_threshold = abs(close_threshold)
        direction_name = str(direction or "").upper()
        if direction_name == "LONG":
            return gap >= opposite_threshold
        if direction_name == "SHORT":
            return gap <= -opposite_threshold
        return False

    def _detect_signal(self, spread: float, *, pivot: float = 0.0) -> str:
        gap = spread - pivot
        open_pair = self.pair_manager.get_open_pair()
        if open_pair is not None:
            if self._should_close_open_pair(gap, getattr(open_pair, "direction", None)):
                return "CLOSE"
            return "NONE"
        if gap > self.config.dev_entry:
            return "ENTRY_SHORT"
        if gap < -self.config.dev_entry:
            return "ENTRY_LONG"
        return "NONE"

    def _calculate_spread(self, mt5_tick: dict, exchange_tick: dict) -> float:
        mt5_mid = (float(mt5_tick["bid"]) + float(mt5_tick["ask"])) / 2
        exchange_mid = (float(exchange_tick["bid"]) + float(exchange_tick["ask"])) / 2
        return mt5_mid - exchange_mid

    def _update_pivot_ema(self, spread: float) -> None:
        event_ts = self._latest_event_ts()
        if self._pivot_ema is None or self._pivot_last_ts <= 0.0:
            self._pivot_ema = spread
            self._pivot_last_ts = event_ts
            return

        dt = max(0.001, event_ts - self._pivot_last_ts)
        alpha = 1.0 - math.exp(-dt / self.config.pivot_ema_sec)
        self._pivot_ema = self._pivot_ema + alpha * (spread - self._pivot_ema)
        self._pivot_last_ts = event_ts

    def _current_pivot(self, spread: float | None = None) -> float:
        if not self._warmup_complete:
            return self.config.warmup_pivot
        if self._pivot_ema is not None:
            return self._pivot_ema
        if spread is not None:
            return spread
        return self.config.warmup_pivot

    def _current_pivot_source(self) -> str:
        if not self._warmup_complete:
            return "warmup"
        if self._pivot_ema is not None:
            return "ema"
        return "spot"

    def _latest_event_ts(self) -> float:
        latest_mt5_ts = float((self._latest_mt5 or {}).get("local_ts", 0.0))
        latest_exchange_ts = float((self._latest_exchange or {}).get("local_ts", 0.0))
        return max(time.time(), latest_mt5_ts, latest_exchange_ts)

    def _entry_allowed_now(self) -> bool:
        return self._warmup_complete or self.config.allow_entry_during_warmup

    def _is_data_fresh(self, mt5_tick: dict, exchange_tick: dict) -> bool:
        now = time.time()
        return (
            now - float(mt5_tick.get("local_ts", 0.0)) <= self.config.max_tick_delay
            and now - float(exchange_tick.get("local_ts", 0.0)) <= self.config.max_tick_delay
        )

    def _is_trading_time(self) -> bool:
        windows = self.config.schedule.get("trading_hours", [])
        return self._utc_minutes_in_windows(windows, default_when_empty=True)

    def _is_force_close_time(self) -> bool:
        windows = self.config.schedule.get("force_close_hours", [])
        return self._utc_minutes_in_windows(windows, default_when_empty=False)

    def _is_orphan_cooldown_active(self) -> bool:
        return time.time() < self._orphan_cooldown_until

    def _utc_minutes_in_windows(self, windows, *, default_when_empty: bool) -> bool:
        if not windows:
            return default_when_empty

        now = self._utc_now()
        current_minutes = now.hour * 60 + now.minute
        for window in windows:
            parsed = self._parse_schedule_window(window)
            if parsed is None:
                continue
            start_minutes, end_minutes = parsed
            if start_minutes <= end_minutes:
                if start_minutes <= current_minutes <= end_minutes:
                    return True
            elif current_minutes >= start_minutes or current_minutes <= end_minutes:
                return True
        return False

    @staticmethod
    def _parse_schedule_window(window) -> tuple[int, int] | None:
        try:
            start_raw, end_raw = str(window).split("-", 1)
            start_h, start_m = [int(part) for part in start_raw.split(":", 1)]
            end_h, end_m = [int(part) for part in end_raw.split(":", 1)]
        except ValueError:
            return None

        if not (0 <= start_h <= 23 and 0 <= end_h <= 23 and 0 <= start_m <= 59 and 0 <= end_m <= 59):
            return None
        return start_h * 60 + start_m, end_h * 60 + end_m

    @staticmethod
    def _utc_now() -> datetime:
        return datetime.now(timezone.utc)

    async def _shutdown(self) -> None:
        self._cancel_debounce()

        if self.config.on_shutdown_positions == "force_close" and not self._risk_flag:
            try:
                await self._execute_close(
                    close_reason=CloseReason.FORCE_SHUTDOWN,
                    success_status=PairStatus.FORCE_CLOSED,
                )
            except Exception as exc:
                log("ERROR", "BRAIN", f"Shutdown force close failed: {exc}")

        for task in self._tasks:
            task.cancel()
        for task in self._tasks:
            try:
                await task
            except asyncio.CancelledError:
                pass

        if self._pubsub is not None:
            try:
                self._pubsub.close()
            except Exception:
                pass

        self._spread_logger.close()
        log("INFO", "BRAIN", "Trading brain stopped")

    def _is_expected_mt5_tick(self, tick: dict | None) -> bool:
        return self._tick_matches_symbol(tick, self.config.mt5_symbol)

    def _is_expected_exchange_tick(self, tick: dict | None) -> bool:
        return self._tick_matches_symbol(tick, self.config.mexc_symbol)

    @staticmethod
    def _tick_matches_symbol(tick: dict | None, expected_symbol: str) -> bool:
        if not tick:
            return False
        return str(tick.get("symbol", "")) == str(expected_symbol)

    @staticmethod
    def _decode_payload(raw) -> dict:
        if raw is None:
            return {}
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8")
        data = orjson.loads(raw)
        if not isinstance(data, dict):
            raise ValueError("Expected JSON object payload")
        return data


def uuid_token() -> str:
    return uuid.uuid4().hex[:8]

