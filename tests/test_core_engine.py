"""Smoke test for Phase 3 CoreEngine bootstrap."""

import asyncio
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import orjson

from src.core.engine import CoreEngine
from src.utils.constants import REDIS_CORE_CMD_SHUTDOWN


class FakePubSub:
    def subscribe(self, *channels):
        return None

    def get_message(self, ignore_subscribe_messages=True, timeout=0):
        time.sleep(min(timeout, 0.01))
        return None

    def close(self):
        return None


class FakeRedis:
    def __init__(self):
        self._data = {
            "mt5:heartbeat": orjson.dumps({"ts": time.time(), "connected": True}).decode("utf-8")
        }
        self._lists = {}
        self._sets = {}

    def get(self, key):
        return self._data.get(key)

    def set(self, key, value, ex=None):
        self._data[key] = value

    def lpush(self, key, value):
        self._lists.setdefault(key, []).insert(0, value)

    def brpop(self, key, timeout=0):
        return None

    def delete(self, *keys):
        for key in keys:
            self._sets.pop(key, None)

    def sadd(self, key, *values):
        self._sets.setdefault(key, set()).update(values)

    def pubsub(self, ignore_subscribe_messages=True):
        return FakePubSub()


class FakeExchange:
    def __init__(self):
        self._callback = None
        self.connected = False
        self.stop_called = False
        self.tick_count = 0

    def set_tick_callback(self, callback):
        self._callback = callback

    async def connect(self):
        self.connected = True

    async def run_ws(self):
        await asyncio.sleep(0.05)
        self.tick_count += 1
        if self._callback is not None:
            self._callback(
                {"bid": 99.0, "ask": 99.2, "last": 99.1, "ts": 1, "local_ts": time.time()}
            )
        while not self.stop_called:
            await asyncio.sleep(0.05)

    async def stop(self):
        self.stop_called = True

    async def check_auth_alive(self):
        return True

    def get_latest_tick(self):
        return None

    @property
    def is_connected(self):
        return self.connected


class DummySpreadLogger:
    def write(self, **kwargs):
        return None

    def close(self):
        return None


def _config():
    return SimpleNamespace(
        redis={},
        database_path=":memory:",
        exchange_name="mexc",
        mexc_symbol="XAUT_USDT",
        mt5_symbol="XAUUSD",
        max_orders=1,
        stable_time_ms=1,
        dev_entry=0.15,
        dev_close=0.05,
        pivot_ema_sec=15 * 60,
        warmup_pivot=-20.0,
        allow_entry_during_warmup=False,
        warmup_sec=0,
        warmup_min_ticks=0,
        max_tick_delay=10.0,
        brain_heartbeat_interval_sec=1,
        auth_check_interval_sec=300,
        brain_log_interval_sec=999,
        schedule={"trading_hours": []},
        cooldown_entry=0,
        cooldown_close=0,
        mt5_volume=0.01,
        mexc_volume=1000,
        on_shutdown_positions="leave",
    )


async def _run_smoke_test():
    fake_redis = FakeRedis()
    fake_exchange = FakeExchange()

    with patch("src.core.engine.Config", return_value=_config()), \
         patch("src.core.engine.get_redis", return_value=fake_redis), \
         patch("src.core.engine.create_exchange", return_value=fake_exchange), \
         patch("src.core.trading_brain.SpreadLogger", return_value=DummySpreadLogger()):
        engine = CoreEngine("ignored.json")
        task = asyncio.create_task(engine.start())
        await asyncio.sleep(0.2)

        assert fake_exchange.connected is True
        assert engine.brain._latest_exchange is not None
        assert fake_exchange.tick_count >= 1

        await engine.shutdown()
        await task

    print("[OK] CoreEngine bootstrap smoke test")


async def _run_shutdown_monitor_test():
    fake_redis = FakeRedis()
    fake_exchange = FakeExchange()

    with patch("src.core.engine.Config", return_value=_config()), \
         patch("src.core.engine.get_redis", return_value=fake_redis), \
         patch("src.core.engine.create_exchange", return_value=fake_exchange), \
         patch("src.core.trading_brain.SpreadLogger", return_value=DummySpreadLogger()):
        engine = CoreEngine("ignored.json")
        called = {"value": False}

        async def _request_shutdown():
            called["value"] = True

        engine.brain.request_shutdown = _request_shutdown
        task = asyncio.create_task(engine._shutdown_monitor_loop())
        await asyncio.sleep(0.05)
        fake_redis.set(REDIS_CORE_CMD_SHUTDOWN, "1")
        await asyncio.sleep(0.6)
        assert engine.shutdown_event.is_set() is True
        assert called["value"] is True
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    print("[OK] CoreEngine shutdown monitor reacts to core:cmd:shutdown")


if __name__ == "__main__":
    asyncio.run(_run_smoke_test())
    asyncio.run(_run_shutdown_monitor_test())
    print("\n=== test_core_engine: ALL PASSED ===")
