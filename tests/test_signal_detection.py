"""Unit tests for Phase 3 signal detection and warmup."""

import asyncio
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.core.trading_brain import TradingBrain


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
    def __init__(self):
        self._callback = None

    def set_tick_callback(self, callback):
        self._callback = callback

    def get_latest_tick(self):
        return None

    async def place_order(self, side, volume):
        raise AssertionError("place_order should not be called in signal tests")

    async def check_auth_alive(self):
        return True


class DummyPairManager:
    def __init__(self, direction=None):
        self.safe_mode = False
        self.safe_mode_reason = ""
        self.pair_count = 0
        self._open_pair = None
        if direction is not None:
            self.pair_count = 1
            self._open_pair = SimpleNamespace(direction=direction, status="OPEN")

    @property
    def allows_new_entries(self):
        return self.pair_count == 0 and not self.safe_mode

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
        warmup_min_ticks=2,
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


def _brain(pair_manager=None, *, warmup_complete=True, **config_overrides):
    config = _config()
    for key, value in config_overrides.items():
        setattr(config, key, value)
    brain = TradingBrain(
        config,
        DummyRedis(),
        DummyExchange(),
        pair_manager or DummyPairManager(),
        asyncio.Event(),
    )
    brain._warmup_complete = warmup_complete
    if warmup_complete:
        brain._pivot_ema = 0.0
    return brain


def test_entry_signals_and_regression_bid_ask_shape():
    brain = _brain()
    mt5_tick = {"bid": 100.0, "ask": 100.2, "local_ts": time.time(), "time_msc": 1}
    exchange_tick = {"bid": 99.0, "ask": 99.2, "local_ts": time.time(), "ts": 1}
    spread = brain._calculate_spread(mt5_tick, exchange_tick)
    assert spread > 0.15
    assert brain._detect_signal(spread) == "ENTRY_SHORT"

    exchange_tick = {"bid": 101.0, "ask": 101.2, "local_ts": time.time(), "ts": 2}
    spread = brain._calculate_spread(mt5_tick, exchange_tick)
    assert spread < -0.15
    assert brain._detect_signal(spread) == "ENTRY_LONG"
    print("[OK] spread detection uses bid/ask shape")


def test_close_signal_for_open_pair():
    brain = _brain(DummyPairManager(direction="LONG"))
    assert brain._detect_signal(0.01) == "CLOSE"
    assert brain._detect_signal(0.20) == "NONE"
    print("[OK] close signal from open pair")


def test_close_signal_can_use_opposite_band():
    long_brain = _brain(DummyPairManager(direction="LONG"), dev_close=-0.25)
    short_brain = _brain(DummyPairManager(direction="SHORT"), dev_close=-0.25)
    assert long_brain._detect_signal(0.26) == "CLOSE"
    assert long_brain._detect_signal(0.10) == "NONE"
    assert short_brain._detect_signal(-0.26) == "CLOSE"
    assert short_brain._detect_signal(-0.10) == "NONE"
    print("[OK] negative dev_close uses opposite-side close band")


def test_signal_detection_respects_configured_pivot():
    brain = _brain()
    assert brain._detect_signal(-19.70, pivot=-20.0) == "ENTRY_SHORT"
    assert brain._detect_signal(-20.30, pivot=-20.0) == "ENTRY_LONG"
    close_brain = _brain(DummyPairManager(direction="LONG"))
    assert close_brain._detect_signal(-20.01, pivot=-20.0) == "CLOSE"
    opposite_close_brain = _brain(DummyPairManager(direction="LONG"), dev_close=-0.25)
    assert opposite_close_brain._detect_signal(-19.74, pivot=-20.0) == "CLOSE"
    print("[OK] signal detection uses pivot-centered thresholds")


def test_current_pivot_uses_warmup_fallback_until_ready():
    brain = _brain(warmup_complete=False)
    brain._pivot_ema = -18.75
    assert brain._current_pivot(-10.0) == -20.0
    brain._warmup_complete = True
    assert brain._current_pivot(-10.0) == -18.75
    print("[OK] warmup fallback pivot switches to EMA after warmup")


def test_entry_allowed_during_warmup_is_configurable():
    warmup_blocked = _brain(warmup_complete=False, allow_entry_during_warmup=False)
    warmup_allowed = _brain(warmup_complete=False, allow_entry_during_warmup=True)
    assert warmup_blocked._entry_allowed_now() is False
    assert warmup_allowed._entry_allowed_now() is True
    print("[OK] entry during warmup follows config")


def test_schedule_windows_use_utc():
    brain = _brain(schedule={"trading_hours": ["00:10-12:00"], "force_close_hours": ["20:15-20:20"]})
    brain._utc_now = lambda: datetime(2026, 4, 1, 0, 30, tzinfo=timezone.utc)
    assert brain._is_trading_time() is True
    assert brain._is_force_close_time() is False

    brain._utc_now = lambda: datetime(2026, 4, 1, 20, 16, tzinfo=timezone.utc)
    assert brain._is_trading_time() is False
    assert brain._is_force_close_time() is True
    print("[OK] schedule windows use UTC clock")


def test_orphan_cooldown_blocks_new_entries():
    brain = _brain(orphan_cooldown_sec=900)
    now = time.time()
    brain._latest_mt5 = {"bid": 100.0, "ask": 100.2, "local_ts": now}
    brain._latest_exchange = {"bid": 101.0, "ask": 101.2, "local_ts": now}
    brain._orphan_cooldown_until = now + 60
    assert brain._gate_abort_reason("ENTRY_LONG") == "orphan_cooldown"
    print("[OK] orphan cooldown blocks new entries")


def test_warmup_completion_requires_min_ticks():
    brain = _brain(warmup_complete=False)
    brain._note_warmup_sample()
    assert brain._warmup_complete is False
    brain._note_warmup_sample()
    assert brain._warmup_complete is True
    print("[OK] warmup waits for min ticks")


if __name__ == "__main__":
    test_entry_signals_and_regression_bid_ask_shape()
    test_close_signal_for_open_pair()
    test_close_signal_can_use_opposite_band()
    test_signal_detection_respects_configured_pivot()
    test_current_pivot_uses_warmup_fallback_until_ready()
    test_entry_allowed_during_warmup_is_configurable()
    test_schedule_windows_use_utc()
    test_orphan_cooldown_blocks_new_entries()
    test_warmup_completion_requires_min_ticks()
    print("\n=== test_signal_detection: ALL PASSED ===")
