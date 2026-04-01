"""Unit tests for MEXC terminal-state handling."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.exchanges.base import ExchangeOrderStatus, OrderSide
from src.exchanges.mexc_handler import MEXCHandler
import src.exchanges.mexc_handler as mexc_module


class FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def json(self, content_type=None):
        return self._payload


class FakeSession:
    def __init__(self, post_payload):
        self.closed = False
        self._post_payload = post_payload

    def post(self, url, headers=None, data=None):
        return FakeResponse(self._post_payload)


def _config():
    exchange = {
        "web_token": "token",
        "symbol": "BTC_USDT",
        "leverage": 10,
        "open_type": 2,
        "volume_min": 1,
        "volume_max": 1000,
        "volume_step": 1,
    }
    return SimpleNamespace(
        exchange=exchange,
        mexc={},
        mexc_web_token="token",
        mexc_symbol="BTC_USDT",
        mexc_leverage=10,
    )


def _position(*, side: str, volume: float, entry_price: float = 0.0):
    return {
        "position_id": "P1",
        "symbol": "BTC_USDT",
        "side": side,
        "volume": volume,
        "volume_raw": int(volume),
        "entry_price": entry_price,
        "mark_price": entry_price,
        "unrealized_pnl": 0.0,
        "leverage": 10,
        "margin_mode": "CROSS",
        "updated_at": 0.0,
    }


def test_place_order_waits_for_entry_fill():
    async def _run():
        handler = MEXCHandler(_config(), asyncio.Event())
        handler._session = FakeSession({"success": True, "data": {"orderId": "OID-1"}})

        snapshots = [
            [],
            [_position(side="SHORT", volume=1.0, entry_price=62500.0)],
        ]

        async def fake_fetch_open_positions():
            if snapshots:
                return snapshots.pop(0)
            return [_position(side="SHORT", volume=1.0, entry_price=62500.0)]

        handler._fetch_open_positions = fake_fetch_open_positions
        result = await handler.place_order(OrderSide.OPEN_SHORT, 1)

        assert result.success is True
        assert result.order_status == ExchangeOrderStatus.FILLED
        assert result.order_id == "OID-1"
        assert result.fill_price == 62500.0
        assert result.fill_volume == 1.0

    asyncio.run(_run())
    print("[OK] MEXC place_order waits for entry terminal fill")


def test_place_order_waits_for_close_fill():
    async def _run():
        handler = MEXCHandler(_config(), asyncio.Event())
        handler._session = FakeSession({"success": True, "data": {"orderId": "OID-2"}})

        snapshots = [
            [_position(side="LONG", volume=1.0, entry_price=62000.0)],
            [],
        ]

        async def fake_fetch_open_positions():
            if snapshots:
                return snapshots.pop(0)
            return []

        handler._fetch_open_positions = fake_fetch_open_positions
        result = await handler.place_order(OrderSide.CLOSE_LONG, 1)

        assert result.success is True
        assert result.order_status == ExchangeOrderStatus.FILLED
        assert result.order_id == "OID-2"
        assert result.fill_volume == 1.0

    asyncio.run(_run())
    print("[OK] MEXC place_order waits for close terminal fill")


def test_place_order_timeout_returns_unknown():
    async def _run():
        original_timeout = mexc_module._ORDER_TERMINAL_TIMEOUT_SEC
        original_interval = mexc_module._ORDER_POLL_INTERVAL_SEC
        mexc_module._ORDER_TERMINAL_TIMEOUT_SEC = 0.05
        mexc_module._ORDER_POLL_INTERVAL_SEC = 0.01
        try:
            handler = MEXCHandler(_config(), asyncio.Event())
            handler._session = FakeSession({"success": True, "data": {"orderId": "OID-3"}})

            async def fake_fetch_open_positions():
                return None

            handler._fetch_open_positions = fake_fetch_open_positions
            result = await handler.place_order(OrderSide.OPEN_LONG, 1)
        finally:
            mexc_module._ORDER_TERMINAL_TIMEOUT_SEC = original_timeout
            mexc_module._ORDER_POLL_INTERVAL_SEC = original_interval

        assert result.success is False
        assert result.order_status == ExchangeOrderStatus.UNKNOWN
        assert result.order_id == "OID-3"

    asyncio.run(_run())
    print("[OK] MEXC place_order timeout returns UNKNOWN")


if __name__ == "__main__":
    test_place_order_waits_for_entry_fill()
    test_place_order_waits_for_close_fill()
    test_place_order_timeout_returns_unknown()
    print("\n=== test_mexc_handler: ALL PASSED ===")
