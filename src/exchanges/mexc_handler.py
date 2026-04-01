"""MEXC Futures exchange handler implementing the shared exchange contract."""

from __future__ import annotations

import asyncio
import hashlib
import time

import aiohttp
import orjson

from .base import (
    ErrorCategory,
    ExchangeHandler,
    ExchangeOrderStatus,
    ExchangePosition,
    MarginMode,
    OrderResult,
    OrderSide,
    TickData,
)
from ..utils.constants import (
    MEXC_CONTRACT_URL,
    MEXC_FUTURES_URL,
    MEXC_WS_PING_INTERVAL,
    MEXC_WS_URL,
    MexcOrderType,
)
from ..utils.logger import log


_SIDE_MAP = {
    OrderSide.OPEN_LONG: 1,
    OrderSide.CLOSE_SHORT: 2,
    OrderSide.OPEN_SHORT: 3,
    OrderSide.CLOSE_LONG: 4,
}

_ORDER_POLL_INTERVAL_SEC = 0.25
_ORDER_TERMINAL_TIMEOUT_SEC = 5.0
_VOLUME_EPSILON = 1e-9


class MEXCHandler(ExchangeHandler):
    """MEXC Futures handler using the WEB token flow."""

    def __init__(self, config, shutdown_event: asyncio.Event, redis_client=None):
        super().__init__(config, shutdown_event, redis_client)

        exchange_cfg = getattr(config, "exchange", config.mexc)

        self._token: str = exchange_cfg.get("web_token", config.mexc_web_token)
        self._symbol: str = exchange_cfg.get("symbol", config.mexc_symbol)
        self._leverage: int = exchange_cfg.get("leverage", config.mexc_leverage)
        self._open_type: int = exchange_cfg.get("open_type", MarginMode.CROSS)

        self._volume_min = exchange_cfg.get("volume_min", 1)
        self._volume_max = exchange_cfg.get("volume_max", 1_000_000)
        self._volume_step = exchange_cfg.get("volume_step", 1)

        self._latest_tick: TickData | None = None
        self._tick_count = 0
        self._ws_connected = False
        self._session: aiohttp.ClientSession | None = None

    @property
    def is_connected(self) -> bool:
        return self._ws_connected

    @property
    def exchange_name(self) -> str:
        return "mexc"

    async def connect(self) -> None:
        if self._session and not self._session.closed:
            return

        self._session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=10),
            connector=aiohttp.TCPConnector(keepalive_timeout=60, limit=5),
        )
        log("INFO", "MEXC", "HTTP session created")

    async def stop(self) -> None:
        self._ws_connected = False
        if self._session and not self._session.closed:
            await self._session.close()
            log("INFO", "MEXC", "HTTP session closed")

    def get_latest_tick(self) -> TickData | None:
        return self._latest_tick

    async def _ensure_session(self) -> None:
        if self._session is None or self._session.closed:
            await self.connect()

    @staticmethod
    def _md5(value: str) -> str:
        return hashlib.md5(value.encode("utf-8")).hexdigest()

    def _sign_request(self, payload: dict) -> tuple[dict, str]:
        timestamp = str(int(time.time() * 1000))
        g = self._md5(self._token + timestamp)[7:]
        body = orjson.dumps(payload).decode("utf-8")
        signature = self._md5(timestamp + body + g)

        headers = {
            "Accept": "*/*",
            "Authorization": self._token,
            "Content-Type": "application/json",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Origin": "https://futures.mexc.com",
            "Referer": "https://futures.mexc.com/",
            "x-mxc-nonce": timestamp,
            "x-mxc-sign": signature,
        }
        return headers, body

    def _auth_headers(self) -> dict:
        return {
            "Authorization": self._token,
            "Content-Type": "application/json",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Origin": "https://futures.mexc.com",
            "Referer": "https://futures.mexc.com/",
        }

    async def run_ws(self) -> None:
        await self._ensure_session()

        while not self._shutdown_event.is_set():
            try:
                log("INFO", "MEXC", "Connecting WebSocket...", url=MEXC_WS_URL)
                async with self._session.ws_connect(
                    MEXC_WS_URL,
                    heartbeat=MEXC_WS_PING_INTERVAL,
                    timeout=aiohttp.ClientTimeout(total=30),
                ) as ws:
                    self._ws_connected = True
                    log("SUCCESS", "MEXC", "WebSocket connected")

                    await ws.send_json(
                        {"method": "sub.ticker", "param": {"symbol": self._symbol}}
                    )
                    log("INFO", "MEXC", "Subscribed to ticker", symbol=self._symbol)

                    ping_task = asyncio.create_task(self._ws_ping(ws))
                    try:
                        async for msg in ws:
                            if self._shutdown_event.is_set():
                                break

                            if msg.type in (aiohttp.WSMsgType.TEXT, aiohttp.WSMsgType.BINARY):
                                self._process_ws_message(msg.data)
                            elif msg.type in (
                                aiohttp.WSMsgType.ERROR,
                                aiohttp.WSMsgType.CLOSED,
                            ):
                                log("WARN", "MEXC", "WebSocket closed/error")
                                break
                    finally:
                        ping_task.cancel()
            except asyncio.CancelledError:
                break
            except Exception as e:
                log("ERROR", "MEXC", f"WebSocket error: {e}")
            finally:
                self._ws_connected = False

            if not self._shutdown_event.is_set():
                log("INFO", "MEXC", "Reconnecting in 2s...")
                await asyncio.sleep(2)

    async def _ws_ping(self, ws) -> None:
        try:
            while True:
                await asyncio.sleep(MEXC_WS_PING_INTERVAL)
                if ws.closed:
                    break
                await ws.send_json({"method": "ping"})
        except asyncio.CancelledError:
            pass

    def _process_ws_message(self, raw) -> None:
        try:
            data = orjson.loads(raw if isinstance(raw, bytes) else raw.encode("utf-8"))
            if data.get("channel") != "push.ticker":
                return

            tick_data = data.get("data", {})
            tick: TickData = {
                "symbol": self._symbol,
                "bid": self._to_float(tick_data.get("bid1")),
                "ask": self._to_float(tick_data.get("ask1")),
                "last": self._to_float(tick_data.get("lastPrice")),
                "ts": int(data.get("ts", int(time.time() * 1000))),
                "local_ts": time.time(),
            }
            self._latest_tick = tick
            self._tick_count += 1
            self._publish_tick(tick)
        except Exception as e:
            log("ERROR", "MEXC", f"WS parse error: {e}")

    async def place_order(self, side: OrderSide, volume: int | float) -> OrderResult:
        await self._ensure_session()

        try:
            normalized_side = OrderSide(int(side))
        except (TypeError, ValueError):
            return OrderResult(
                success=False,
                order_status=ExchangeOrderStatus.REJECTED,
                error=f"Invalid side: {side}",
                error_category=ErrorCategory.UNKNOWN,
            )

        if not self.validate_volume(volume):
            return OrderResult(
                success=False,
                order_status=ExchangeOrderStatus.REJECTED,
                error=f"Invalid volume: {volume}",
                error_category=ErrorCategory.INVALID_VOLUME,
            )

        requested_volume = float(volume)
        baseline_snapshot = await self._fetch_open_positions()
        baseline_positions = self._summarize_positions(baseline_snapshot or [])

        payload = {
            "symbol": self._symbol,
            "price": "0",
            "vol": str(int(volume)),
            "side": _SIDE_MAP[normalized_side],
            "type": MexcOrderType.MARKET,
            "openType": self._open_type,
            "leverage": self._leverage,
            "positionMode": 2,
        }

        url = f"{MEXC_FUTURES_URL}/api/v1/private/order/submit"
        headers, body = self._sign_request(payload)

        try:
            t0 = time.perf_counter()
            async with self._session.post(url, headers=headers, data=body) as resp:
                result = await resp.json(content_type=None)
            latency_ms = (time.perf_counter() - t0) * 1000

            if result.get("success"):
                order_data = result.get("data")
                order_id = order_data
                if isinstance(order_data, dict):
                    order_id = order_data.get("orderId", order_data.get("id"))

                log(
                    "SUCCESS",
                    "MEXC",
                    "Order placed",
                    side=normalized_side.name,
                    vol=volume,
                    order_id=order_id,
                    latency=f"{latency_ms:.0f}ms",
                )
                order_id_text = str(order_id) if order_id is not None else None
                submit_status = self._extract_submit_status(result)
                if submit_status == ExchangeOrderStatus.FILLED:
                    fill_price = self._extract_fill_price(result)
                    fill_volume = self._extract_fill_volume(result, default=requested_volume)
                    return OrderResult(
                        success=True,
                        order_status=ExchangeOrderStatus.FILLED,
                        order_id=order_id_text,
                        fill_price=fill_price,
                        fill_volume=fill_volume,
                        fill_volume_raw=self._to_int(fill_volume, default=int(requested_volume)),
                        latency_ms=latency_ms,
                        raw_response=result,
                    )
                if submit_status in {
                    ExchangeOrderStatus.REJECTED,
                    ExchangeOrderStatus.CANCELLED,
                    ExchangeOrderStatus.EXPIRED,
                }:
                    return OrderResult(
                        success=False,
                        order_status=ExchangeOrderStatus.REJECTED,
                        order_id=order_id_text,
                        error="Order rejected by exchange",
                        error_category=ErrorCategory.UNKNOWN,
                        latency_ms=latency_ms,
                        raw_response=result,
                    )
                return await self._wait_for_terminal_state(
                    side=normalized_side,
                    requested_volume=requested_volume,
                    baseline_positions=baseline_positions,
                    order_id=order_id_text,
                    latency_ms=latency_ms,
                    submit_response=result,
                )

            error_msg = result.get("message", result.get("msg", str(result)))
            error_code = str(result.get("code", ""))
            category = self._categorize_error(error_code, error_msg)
            log(
                "ERROR",
                "MEXC",
                "Order failed",
                side=normalized_side.name,
                vol=volume,
                error=error_msg,
            )
            return OrderResult(
                success=False,
                order_status=ExchangeOrderStatus.REJECTED,
                error=error_msg,
                error_code=error_code,
                error_category=category,
                latency_ms=latency_ms,
                raw_response=result,
            )
        except asyncio.TimeoutError:
            return OrderResult(
                success=False,
                order_status=ExchangeOrderStatus.UNKNOWN,
                error="Request timeout",
                error_category=ErrorCategory.TIMEOUT,
            )
        except Exception as e:
            log("ERROR", "MEXC", f"Order exception: {e}")
            return OrderResult(
                success=False,
                order_status=ExchangeOrderStatus.UNKNOWN,
                error=str(e),
                error_category=ErrorCategory.NETWORK,
            )

    async def get_positions(self) -> list[ExchangePosition]:
        positions = await self._fetch_open_positions()
        return positions or []

    async def _fetch_open_positions(self) -> list[ExchangePosition] | None:
        await self._ensure_session()

        url = f"{MEXC_CONTRACT_URL}/api/v1/private/position/open_positions"
        try:
            async with self._session.get(url, headers=self._auth_headers()) as resp:
                data = await resp.json(content_type=None)

            if not data.get("success"):
                return None

            positions: list[ExchangePosition] = []
            for pos in data.get("data", []):
                open_type = pos.get("openType", pos.get("isOpenType", MarginMode.CROSS))
                positions.append(
                    {
                        "position_id": str(pos.get("positionId") or pos.get("id") or ""),
                        "symbol": pos.get("symbol", ""),
                        "side": "LONG" if int(pos.get("positionType", 1)) == 1 else "SHORT",
                        "volume": self._to_float(pos.get("holdVol")),
                        "volume_raw": self._to_int(pos.get("holdVol")),
                        "entry_price": self._to_float(pos.get("openAvgPrice")),
                        "mark_price": self._to_float(pos.get("markPrice")),
                        "unrealized_pnl": self._to_float(
                            pos.get("unrealisedPnl", pos.get("unRealizedPnl"))
                        ),
                        "leverage": self._to_int(pos.get("leverage", 1)),
                        "margin_mode": "CROSS"
                        if int(open_type) == MarginMode.CROSS
                        else "ISOLATED",
                        "updated_at": time.time(),
                    }
                )
            return positions
        except Exception as e:
            log("ERROR", "MEXC", f"Get positions error: {e}")
            return None

    async def check_auth_alive(self) -> bool:
        await self._ensure_session()

        try:
            url = f"{MEXC_CONTRACT_URL}/api/v1/private/position/open_positions"
            async with self._session.get(url, headers=self._auth_headers()) as resp:
                data = await resp.json(content_type=None)
            return bool(data.get("success", False))
        except Exception:
            return False

    def validate_volume(self, volume: int | float) -> bool:
        try:
            value = float(volume)
        except (TypeError, ValueError):
            return False

        if value < self._volume_min or value > self._volume_max:
            return False

        if self._volume_step > 0:
            steps = value / self._volume_step
            if abs(steps - round(steps)) > 1e-9:
                return False

        return True

    def get_volume_info(self) -> dict:
        return {
            "min": self._volume_min,
            "max": self._volume_max,
            "step": self._volume_step,
            "unit_name": "contracts",
        }

    async def open_long(self, volume: int | float) -> OrderResult:
        return await self.place_order(OrderSide.OPEN_LONG, volume)

    async def close_long(self, volume: int | float) -> OrderResult:
        return await self.place_order(OrderSide.CLOSE_LONG, volume)

    async def open_short(self, volume: int | float) -> OrderResult:
        return await self.place_order(OrderSide.OPEN_SHORT, volume)

    async def close_short(self, volume: int | float) -> OrderResult:
        return await self.place_order(OrderSide.CLOSE_SHORT, volume)

    async def get_account_assets(self) -> list[dict]:
        await self._ensure_session()

        url = f"{MEXC_CONTRACT_URL}/api/v1/private/account/assets"
        try:
            async with self._session.get(url, headers=self._auth_headers()) as resp:
                data = await resp.json(content_type=None)
            if data.get("success"):
                return data.get("data", [])
            return []
        except Exception as e:
            log("ERROR", "MEXC", f"Get assets error: {e}")
            return []

    async def _wait_for_terminal_state(
        self,
        *,
        side: OrderSide,
        requested_volume: float,
        baseline_positions: dict[str, dict[str, float | None]],
        order_id: str | None,
        latency_ms: float,
        submit_response: dict,
    ) -> OrderResult:
        deadline = time.monotonic() + _ORDER_TERMINAL_TIMEOUT_SEC
        while time.monotonic() < deadline and not self._shutdown_event.is_set():
            snapshot = await self._fetch_open_positions()
            if snapshot is None:
                await asyncio.sleep(_ORDER_POLL_INTERVAL_SEC)
                continue
            positions = self._summarize_positions(snapshot)
            resolved = self._resolve_position_result(
                side=side,
                requested_volume=requested_volume,
                baseline_positions=baseline_positions,
                current_positions=positions,
                order_id=order_id,
                latency_ms=latency_ms,
                submit_response=submit_response,
            )
            if resolved is not None:
                return resolved
            await asyncio.sleep(_ORDER_POLL_INTERVAL_SEC)

        log(
            "WARN",
            "MEXC",
            "Timed out waiting for terminal order state",
            side=side.name,
            vol=requested_volume,
            order_id=order_id,
        )
        return OrderResult(
            success=False,
            order_status=ExchangeOrderStatus.UNKNOWN,
            order_id=order_id,
            error="Timed out waiting for terminal state",
            error_category=ErrorCategory.TIMEOUT,
            latency_ms=latency_ms,
            raw_response=submit_response,
        )

    def _resolve_position_result(
        self,
        *,
        side: OrderSide,
        requested_volume: float,
        baseline_positions: dict[str, dict[str, float | None]],
        current_positions: dict[str, dict[str, float | None]],
        order_id: str | None,
        latency_ms: float,
        submit_response: dict,
    ) -> OrderResult | None:
        tracked_side = self._tracked_position_side(side)
        before = baseline_positions[tracked_side]
        current = current_positions[tracked_side]
        before_volume = float(before["volume"] or 0.0)
        current_volume = float(current["volume"] or 0.0)

        if side in {OrderSide.OPEN_LONG, OrderSide.OPEN_SHORT}:
            delta = current_volume - before_volume
            if delta + _VOLUME_EPSILON < requested_volume:
                return None
            return OrderResult(
                success=True,
                order_status=ExchangeOrderStatus.FILLED,
                order_id=order_id,
                fill_price=self._first_non_null(
                    self._extract_fill_price(submit_response),
                    current.get("entry_price"),
                ),
                fill_volume=delta,
                fill_volume_raw=self._to_int(current["volume_raw"] - before["volume_raw"], default=int(requested_volume)),
                latency_ms=latency_ms,
                raw_response=submit_response,
            )

        if before_volume <= _VOLUME_EPSILON:
            return None

        closed_volume = max(0.0, before_volume - current_volume)
        target_close = min(requested_volume, before_volume)
        if closed_volume + _VOLUME_EPSILON < target_close:
            return None

        return OrderResult(
            success=True,
            order_status=ExchangeOrderStatus.FILLED,
            order_id=order_id,
            fill_price=self._extract_fill_price(submit_response),
            fill_volume=closed_volume,
            fill_volume_raw=self._to_int(before["volume_raw"] - current["volume_raw"], default=int(target_close)),
            latency_ms=latency_ms,
            raw_response=submit_response,
        )

    def _summarize_positions(self, positions: list[ExchangePosition]) -> dict[str, dict[str, float | None]]:
        summary = {
            "LONG": {"volume": 0.0, "volume_raw": 0.0, "entry_price": None},
            "SHORT": {"volume": 0.0, "volume_raw": 0.0, "entry_price": None},
        }
        for position in positions:
            if str(position.get("symbol", "")) != self._symbol:
                continue
            side = str(position.get("side", "")).upper()
            if side not in summary:
                continue

            volume = self._to_float(position.get("volume"))
            volume_raw = self._to_float(position.get("volume_raw"), default=volume)
            entry_price = self._to_float(position.get("entry_price"), default=0.0)
            bucket = summary[side]
            previous_volume = float(bucket["volume"] or 0.0)
            new_volume = previous_volume + volume
            if entry_price > 0.0 and new_volume > 0.0:
                weighted_price = 0.0
                if bucket["entry_price"] is not None and previous_volume > 0.0:
                    weighted_price = float(bucket["entry_price"]) * previous_volume
                bucket["entry_price"] = (weighted_price + (entry_price * volume)) / new_volume
            bucket["volume"] = new_volume
            bucket["volume_raw"] = float(bucket["volume_raw"] or 0.0) + volume_raw
        return summary

    @staticmethod
    def _tracked_position_side(side: OrderSide) -> str:
        if side in {OrderSide.OPEN_LONG, OrderSide.CLOSE_LONG}:
            return "LONG"
        return "SHORT"

    @staticmethod
    def _first_non_null(*values):
        for value in values:
            if value is not None:
                return value
        return None

    def _extract_submit_status(self, payload: dict) -> ExchangeOrderStatus | None:
        candidates = [payload]
        data = payload.get("data")
        if isinstance(data, dict):
            candidates.append(data)

        status_map = {
            "FILLED": ExchangeOrderStatus.FILLED,
            "REJECTED": ExchangeOrderStatus.REJECTED,
            "FAILED": ExchangeOrderStatus.REJECTED,
            "FAIL": ExchangeOrderStatus.REJECTED,
            "CANCELLED": ExchangeOrderStatus.CANCELLED,
            "CANCELED": ExchangeOrderStatus.CANCELLED,
            "EXPIRED": ExchangeOrderStatus.EXPIRED,
            "PARTIAL": ExchangeOrderStatus.PARTIALLY_FILLED,
            "PARTIALLY_FILLED": ExchangeOrderStatus.PARTIALLY_FILLED,
            "ACCEPTED": ExchangeOrderStatus.ACCEPTED,
            "NEW": ExchangeOrderStatus.ACCEPTED,
            "PENDING": ExchangeOrderStatus.ACCEPTED,
        }

        for candidate in candidates:
            for key in ("status", "state", "orderStatus"):
                raw = candidate.get(key)
                if raw is None:
                    continue
                normalized = str(raw).strip().upper()
                if normalized in status_map:
                    return status_map[normalized]
        return None

    def _extract_fill_price(self, payload: dict) -> float | None:
        candidates = [payload]
        data = payload.get("data")
        if isinstance(data, dict):
            candidates.append(data)
        for candidate in candidates:
            for key in ("fillPrice", "dealAvgPrice", "avgPrice", "price"):
                value = candidate.get(key)
                if value in (None, ""):
                    continue
                return self._to_float(value)
        return None

    def _extract_fill_volume(self, payload: dict, *, default: float) -> float:
        candidates = [payload]
        data = payload.get("data")
        if isinstance(data, dict):
            candidates.append(data)
        for candidate in candidates:
            for key in ("fillVolume", "dealVolume", "executedVol", "vol"):
                value = candidate.get(key)
                if value in (None, ""):
                    continue
                return self._to_float(value, default=default)
        return default

    @staticmethod
    def _categorize_error(error_code: str, message: str | None) -> ErrorCategory:
        msg_lower = message.lower() if message else ""

        if "token" in msg_lower or error_code in {"401", "403"}:
            return ErrorCategory.AUTH_EXPIRED
        if error_code == "602" or "insufficient" in msg_lower or "balance" in msg_lower:
            return ErrorCategory.INSUFFICIENT_BALANCE
        if error_code == "429" or "rate" in msg_lower:
            return ErrorCategory.RATE_LIMIT
        if "vol" in msg_lower and ("min" in msg_lower or "max" in msg_lower):
            return ErrorCategory.INVALID_VOLUME
        if "position" in msg_lower and "not" in msg_lower:
            return ErrorCategory.POSITION_NOT_FOUND
        if "maintenance" in msg_lower or "closed" in msg_lower:
            return ErrorCategory.MARKET_CLOSED

        return ErrorCategory.UNKNOWN

    @staticmethod
    def _to_float(value, default: float = 0.0) -> float:
        try:
            return float(value)
        except (TypeError, ValueError):
            return default

    @staticmethod
    def _to_int(value, default: int = 0) -> int:
        try:
            return int(float(value))
        except (TypeError, ValueError):
            return default
