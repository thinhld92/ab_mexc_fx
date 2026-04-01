"""Unit tests for Phase 3 pre-execution gate checks."""

import asyncio
import sys
import time
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.core.trading_brain import TradingBrain
from src.domain import PairStatus


class DummyRedis:
    def __init__(self):
        self._data = {}

    def pubsub(self, ignore_subscribe_messages=True):
        return DummyPubSub()

    def get(self, key):
        return self._data.get(key)

    def set(self, key, value, ex=None):
        self._data[key] = value

    def lpush(self, key, value):
        pass

    def brpop(self, key, timeout=0):
        return None


class DummyPubSub:
    def subscribe(self, *channels):
        return None

    def get_message(self, ignore_subscribe_messages=True, timeout=0):
        return None

    def close(self):
        return None


class DummyExchange:
    def set_tick_callback(self, callback):
        return None

    def get_latest_tick(self):
        return None

    async def place_order(self, side, volume):
        return None

    async def check_auth_alive(self):
        return True


class DummyPairManager:
    def __init__(self):
        self.safe_mode = False
        self.safe_mode_reason = ""
        self.pair_count = 0
        self._open_pair = None

    @property
    def allows_new_entries(self):
        return self.pair_count == 0

    def get_open_pair(self):
        return self._open_pair


def _config():
    return SimpleNamespace(
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
        brain_heartbeat_interval_sec=10,
        auth_check_interval_sec=300,
        brain_log_interval_sec=999,
        schedule={"trading_hours": []},
        cooldown_entry=0,
        cooldown_close=0,
        mt5_volume=0.01,
        mexc_volume=1000,
        on_shutdown_positions="leave",
    )


def _fresh_ticks():
    now = time.time()
    return (
        {"bid": 100.0, "ask": 100.2, "local_ts": now, "time_msc": 1},
        {"bid": 99.0, "ask": 99.2, "local_ts": now, "ts": 1},
    )


def _brain():
    pair_manager = DummyPairManager()
    brain = TradingBrain(_config(), DummyRedis(), DummyExchange(), pair_manager, asyncio.Event())
    brain._warmup_complete = True
    brain._pivot_ema = 0.0
    brain._latest_mt5, brain._latest_exchange = _fresh_ticks()
    return brain, pair_manager


def test_gate_signal_changed():
    brain, _ = _brain()
    assert brain._gate_abort_reason("ENTRY_LONG") == "signal_changed"
    print("[OK] gate: signal changed")


def test_gate_stale_tick():
    brain, _ = _brain()
    brain._latest_mt5["local_ts"] = time.time() - 99
    assert brain._gate_abort_reason("ENTRY_SHORT") == "stale_tick"
    print("[OK] gate: stale tick")


def test_gate_risk_flag():
    brain, _ = _brain()
    brain._risk_flag = True
    assert brain._gate_abort_reason("ENTRY_SHORT") == "risk_flag"
    print("[OK] gate: risk flag")


def test_gate_close_pair_state_invalid():
    brain, pair_manager = _brain()
    pair_manager._open_pair = SimpleNamespace(direction="LONG", status=PairStatus.CLOSE_SENT)
    pair_manager.pair_count = 1
    brain._latest_mt5 = {"bid": 100.0, "ask": 100.2, "local_ts": time.time(), "time_msc": 1}
    brain._latest_exchange = {"bid": 100.05, "ask": 100.1, "local_ts": time.time(), "ts": 1}
    assert brain._gate_abort_reason("CLOSE") == "pair_state"
    print("[OK] gate: close pair state invalid")


def test_gate_executing():
    brain, _ = _brain()
    brain._is_executing = True
    assert brain._gate_abort_reason("ENTRY_SHORT") == "executing"
    print("[OK] gate: executing")


if __name__ == "__main__":
    test_gate_signal_changed()
    test_gate_stale_tick()
    test_gate_risk_flag()
    test_gate_close_pair_state_invalid()
    test_gate_executing()
    print("\n=== test_gate_check: ALL PASSED ===")
