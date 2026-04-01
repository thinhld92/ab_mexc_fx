"""Phase 3 core tests — PairManager + TradingBrain.

Reuses the same mock strategy from Phase 1 & 2:
  - Real :memory: SQLite for PairManager tests
  - MockRedis (in-memory) for IPC
  - Stub mocks for ExchangeHandler / Config
"""

from __future__ import annotations

import asyncio
import sys
import os
import time
import traceback
from datetime import datetime, timezone
from types import SimpleNamespace

import orjson

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.domain import PairStatus, PairEventType, CloseReason, PairRecord
from src.exchanges.base import (
    ExchangeOrderStatus,
    ErrorCategory,
    OrderResult,
    OrderSide,
)
from src.storage import Database, PairRepository, JournalRepository
from src.core.pair_manager import PairManager
from src.core.trading_brain import TradingBrain, uuid_token
from src.utils.constants import REDIS_MT5_HEARTBEAT, REDIS_MT5_ORDER_QUEUE


# ═══════════════════════════════════════════════════════════
# Mocks
# ═══════════════════════════════════════════════════════════


class MockRedis:
    """In-memory Redis stub (same as Phase 2, extended)."""

    def __init__(self):
        self._data = {}
        self._sets = {}
        self._lists = {}
        self._published = []
        self._pubsub_instance = None

    def get(self, key):
        return self._data.get(key)

    def set(self, key, value, ex=None):
        self._data[key] = value

    def delete(self, *keys):
        for key in keys:
            self._data.pop(key, None)
            self._sets.pop(key, None)
            self._lists.pop(key, None)

    def sadd(self, key, *members):
        if key not in self._sets:
            self._sets[key] = set()
        self._sets[key].update(str(m) for m in members)

    def smembers(self, key):
        return self._sets.get(key, set())

    def srem(self, key, *members):
        s = self._sets.get(key, set())
        for m in members:
            s.discard(str(m))

    def lpush(self, key, *values):
        if key not in self._lists:
            self._lists[key] = []
        for v in values:
            self._lists[key].insert(0, v)

    def brpop(self, key, timeout=0):
        lst = self._lists.get(key, [])
        if lst:
            return (key, lst.pop())
        return None

    def publish(self, channel, message):
        self._published.append((channel, message))

    def pubsub(self, **kwargs):
        self._pubsub_instance = MockPubSub()
        return self._pubsub_instance


class MockPubSub:
    def __init__(self):
        self._subscribed = []
        self._messages = []

    def subscribe(self, *channels):
        self._subscribed.extend(channels)

    def get_message(self, ignore_subscribe_messages=True, timeout=1.0):
        if self._messages:
            return self._messages.pop(0)
        return None

    def close(self):
        pass


class MockConfig:
    """Minimal config stub matching Config property API."""

    def __init__(self, **overrides):
        self._overrides = overrides

    def __getattr__(self, name):
        defaults = {
            "dev_entry": 0.15,
            "dev_close": 0.05,
            "pivot_ema_sec": 15 * 60,
            "warmup_pivot": -20.0,
            "allow_entry_during_warmup": False,
            "stable_time_ms": 0,  # 0ms for instant debounce in tests
            "max_orders": 1,
            "cooldown_entry": 0,
            "cooldown_close": 0,
            "hold_time": 240,
            "warmup_sec": 0,
            "warmup_min_ticks": 0,
            "max_tick_delay": 10.0,
            "auth_check_interval_sec": 300,
            "mt5_volume": 0.01,
            "mexc_volume": 1000,
            "mexc_symbol": "XAUT_USDT",
            "mt5_symbol": "XAUUSD",
            "exchange_name": "mexc",
            "brain_heartbeat_interval_sec": 10,
            "brain_log_interval_sec": 5,
            "on_shutdown_positions": "leave",
            "schedule": {"trading_hours": []},
        }
        if name in self._overrides:
            return self._overrides[name]
        if name in defaults:
            return defaults[name]
        raise AttributeError(f"MockConfig has no attribute '{name}'")


class MockExchange:
    """Stub exchange that returns configurable results."""

    def __init__(self):
        self.next_result = _filled_result()
        self._tick = None
        self._connected = True
        self._auth_alive = True
        self.auth_check_calls = 0
        self._tick_callback = None

    def set_tick_callback(self, cb):
        self._tick_callback = cb

    def get_latest_tick(self):
        return self._tick

    async def connect(self):
        pass

    async def run_ws(self):
        # Block until cancelled
        try:
            while True:
                await asyncio.sleep(3600)
        except asyncio.CancelledError:
            pass

    async def stop(self):
        pass

    async def check_auth_alive(self):
        self.auth_check_calls += 1
        return self._auth_alive

    async def place_order(self, side, volume):
        return self.next_result


# ═══════════════════════════════════════════════════════════
# Helpers
# ═══════════════════════════════════════════════════════════


def _make_pair_manager() -> tuple[PairManager, MockRedis, PairRepository, JournalRepository]:
    db = Database("")
    db.initialize()
    repo = PairRepository(db)
    journal = JournalRepository(db)
    redis = MockRedis()
    pm = PairManager(redis, repo, journal)
    return pm, redis, repo, journal


def _filled_result(**kwargs) -> OrderResult:
    defaults = dict(
        success=True,
        order_status=ExchangeOrderStatus.FILLED,
        order_id="EX123",
        fill_price=2950.0,
        fill_volume=1000.0,
        fill_volume_raw=1000,
        latency_ms=50.0,
    )
    defaults.update(kwargs)
    return OrderResult(**defaults)


def _rejected_result(**kwargs) -> OrderResult:
    defaults = dict(
        success=False,
        order_status=ExchangeOrderStatus.REJECTED,
        error="Insufficient balance",
        error_category=ErrorCategory.INSUFFICIENT_BALANCE,
        latency_ms=10.0,
    )
    defaults.update(kwargs)
    return OrderResult(**defaults)


def _unknown_result(**kwargs) -> OrderResult:
    defaults = dict(
        success=False,
        order_status=ExchangeOrderStatus.UNKNOWN,
        error="Timeout",
        error_category=ErrorCategory.TIMEOUT,
        latency_ms=5000.0,
    )
    defaults.update(kwargs)
    return OrderResult(**defaults)


def _mt5_success(**kwargs) -> dict:
    defaults = {
        "success": True,
        "ticket": 99999,
        "action": "BUY",
        "volume": 0.01,
        "price": 2950.50,
        "latency_ms": 120.0,
        "job_id": "test01",
    }
    defaults.update(kwargs)
    return defaults


def _mt5_failed(**kwargs) -> dict:
    defaults = {
        "success": False,
        "error": "Invalid volume",
        "job_id": "test01",
    }
    defaults.update(kwargs)
    return defaults


def _create_open_pair(pm: PairManager) -> "PairRecord":
    """Helper: create a pair and move it to OPEN status."""
    pair = pm.create_entry(
        direction="LONG", spread=-0.20, conf_dev_entry=0.15, conf_dev_close=0.05,
    )
    pm.mark_entry_dispatched(pair.pair_token)
    return pm.apply_entry_results(
        pair.pair_token,
        mt5_result=_mt5_success(),
        exchange_result=_filled_result(),
        exchange_volume=1000,
        exchange_side=int(OrderSide.OPEN_SHORT),
    )


# ═══════════════════════════════════════════════════════════
# Group A: PairManager Tests
# ═══════════════════════════════════════════════════════════


def test_bootstrap_empty():
    pm, redis, repo, journal = _make_pair_manager()
    pairs = pm.bootstrap()
    assert pairs == []
    assert pm.safe_mode is False
    assert pm.pair_count == 0
    print("[OK] bootstrap() empty DB")


def test_bootstrap_one_open():
    pm, redis, repo, journal = _make_pair_manager()
    _create_open_pair(pm)
    assert pm.pair_count == 1

    # Simulate restart: new PairManager with same DB
    pm2 = PairManager(redis, repo, journal)
    pairs = pm2.bootstrap()
    assert len(pairs) == 1
    assert pairs[0].status == PairStatus.OPEN
    assert pm2.safe_mode is False
    print("[OK] bootstrap() with 1 OPEN pair")


def test_bootstrap_multiple_non_terminal():
    pm, redis, repo, journal = _make_pair_manager()
    # Create 2 pairs stuck at ENTRY_SENT
    p1 = pm.create_entry(direction="LONG", spread=-0.20, conf_dev_entry=0.15, conf_dev_close=0.05)
    pm.mark_entry_dispatched(p1.pair_token)
    p2 = pm.create_entry(direction="SHORT", spread=0.20, conf_dev_entry=0.15, conf_dev_close=0.05)
    pm.mark_entry_dispatched(p2.pair_token)

    pm2 = PairManager(redis, repo, journal)
    pairs = pm2.bootstrap()
    assert len(pairs) == 2
    assert pm2.safe_mode is True
    assert "2 non-terminal" in pm2.safe_mode_reason
    print("[OK] bootstrap() with 2 non-terminal -> safe_mode")


def test_bootstrap_non_open_status():
    pm, redis, repo, journal = _make_pair_manager()
    p = pm.create_entry(direction="LONG", spread=-0.20, conf_dev_entry=0.15, conf_dev_close=0.05)
    pm.mark_entry_dispatched(p.pair_token)

    pm2 = PairManager(redis, repo, journal)
    pairs = pm2.bootstrap()
    assert len(pairs) == 1
    assert pm2.safe_mode is True
    print("[OK] bootstrap() with ENTRY_SENT -> safe_mode")


def test_allows_new_entries():
    pm, redis, repo, journal = _make_pair_manager()
    pm.bootstrap()
    assert pm.allows_new_entries is True

    _create_open_pair(pm)
    assert pm.allows_new_entries is False  # has pair

    pm2, _, _, _ = _make_pair_manager()
    pm2.bootstrap()
    pm2.safe_mode = True
    assert pm2.allows_new_entries is False  # safe mode
    print("[OK] allows_new_entries logic")


def test_create_entry():
    pm, redis, repo, journal = _make_pair_manager()
    pm.bootstrap()
    pair = pm.create_entry(direction="LONG", spread=-0.20, conf_dev_entry=0.15, conf_dev_close=0.05)
    assert pair.status == PairStatus.PENDING
    assert pair.direction == "LONG"
    assert pm.pair_count == 1

    events = journal.list_events(pair.pair_token)
    assert len(events) == 1
    assert events[0]["event_type"] == PairEventType.CREATED.value
    print("[OK] create_entry()")


def test_mark_entry_dispatched():
    pm, redis, repo, journal = _make_pair_manager()
    pm.bootstrap()
    pair = pm.create_entry(direction="LONG", spread=-0.20, conf_dev_entry=0.15, conf_dev_close=0.05)
    updated = pm.mark_entry_dispatched(pair.pair_token)
    assert updated.status == PairStatus.ENTRY_SENT

    events = journal.list_events(pair.pair_token)
    event_types = [e["event_type"] for e in events]
    assert PairEventType.ENTRY_DISPATCHED.value in event_types
    print("[OK] mark_entry_dispatched()")


def test_apply_entry_both_fill():
    pm, redis, repo, journal = _make_pair_manager()
    pm.bootstrap()
    pair = _create_open_pair(pm)
    assert pair.status == PairStatus.OPEN
    assert pair.mt5_ticket == 99999
    assert pair.raw.get("exchange_order_id") == "EX123"

    events = journal.list_events(pair.pair_token)
    event_types = [e["event_type"] for e in events]
    assert PairEventType.MT5_FILLED.value in event_types
    assert PairEventType.EXCHANGE_FILLED.value in event_types
    assert PairEventType.ENTRY_COMPLETE.value in event_types
    print("[OK] apply_entry_results -- both fill -> OPEN")


def test_apply_entry_mt5_ok_exchange_rejected():
    pm, redis, repo, journal = _make_pair_manager()
    pm.bootstrap()
    pair = pm.create_entry(direction="LONG", spread=-0.20, conf_dev_entry=0.15, conf_dev_close=0.05)
    pm.mark_entry_dispatched(pair.pair_token)

    updated = pm.apply_entry_results(
        pair.pair_token,
        mt5_result=_mt5_success(),
        exchange_result=_rejected_result(),
        exchange_volume=1000,
        exchange_side=int(OrderSide.OPEN_SHORT),
    )
    assert updated.status == PairStatus.ORPHAN_MT5
    print("[OK] apply_entry_results -- MT5 ok, exchange REJECTED -> ORPHAN_MT5")


def test_apply_entry_exchange_ok_mt5_rejected():
    pm, redis, repo, journal = _make_pair_manager()
    pm.bootstrap()
    pair = pm.create_entry(direction="LONG", spread=-0.20, conf_dev_entry=0.15, conf_dev_close=0.05)
    pm.mark_entry_dispatched(pair.pair_token)

    updated = pm.apply_entry_results(
        pair.pair_token,
        mt5_result=_mt5_failed(),
        exchange_result=_filled_result(),
        exchange_volume=1000,
        exchange_side=int(OrderSide.OPEN_SHORT),
    )
    assert updated.status == PairStatus.ORPHAN_EXCHANGE
    print("[OK] apply_entry_results -- exchange ok, MT5 rejected -> ORPHAN_EXCHANGE")


def test_apply_entry_both_rejected():
    pm, redis, repo, journal = _make_pair_manager()
    pm.bootstrap()
    pair = pm.create_entry(direction="LONG", spread=-0.20, conf_dev_entry=0.15, conf_dev_close=0.05)
    pm.mark_entry_dispatched(pair.pair_token)

    updated = pm.apply_entry_results(
        pair.pair_token,
        mt5_result=_mt5_failed(),
        exchange_result=_rejected_result(),
        exchange_volume=1000,
        exchange_side=int(OrderSide.OPEN_SHORT),
    )
    assert updated.status == PairStatus.FAILED
    assert pm.pair_count == 0  # terminal -> removed from cache
    print("[OK] apply_entry_results -- both rejected -> FAILED")


def test_apply_entry_mt5_ok_exchange_unknown():
    pm, redis, repo, journal = _make_pair_manager()
    pm.bootstrap()
    pair = pm.create_entry(direction="LONG", spread=-0.20, conf_dev_entry=0.15, conf_dev_close=0.05)
    pm.mark_entry_dispatched(pair.pair_token)

    updated = pm.apply_entry_results(
        pair.pair_token,
        mt5_result=_mt5_success(),
        exchange_result=_unknown_result(),
        exchange_volume=1000,
        exchange_side=int(OrderSide.OPEN_SHORT),
    )
    # UNKNOWN: don't orphan, stay at intermediate for Reconciler
    assert updated.status == PairStatus.ENTRY_MT5_FILLED
    print("[OK] apply_entry_results -- MT5 ok, exchange UNKNOWN -> ENTRY_MT5_FILLED")


def test_apply_entry_exchange_ok_mt5_unknown():
    pm, redis, repo, journal = _make_pair_manager()
    pm.bootstrap()
    pair = pm.create_entry(direction="LONG", spread=-0.20, conf_dev_entry=0.15, conf_dev_close=0.05)
    pm.mark_entry_dispatched(pair.pair_token)

    updated = pm.apply_entry_results(
        pair.pair_token,
        mt5_result=None,  # MT5 timeout = unknown
        exchange_result=_filled_result(),
        exchange_volume=1000,
        exchange_side=int(OrderSide.OPEN_SHORT),
    )
    assert updated.status == PairStatus.ENTRY_EXCHANGE_FILLED
    print("[OK] apply_entry_results -- exchange ok, MT5 unknown -> ENTRY_EXCHANGE_FILLED")


def test_begin_close():
    pm, redis, repo, journal = _make_pair_manager()
    pm.bootstrap()
    pair = _create_open_pair(pm)

    updated = pm.begin_close(
        pair.pair_token, close_reason=CloseReason.SIGNAL, close_spread=0.03,
    )
    assert updated.status == PairStatus.CLOSE_SENT
    assert updated.raw.get("close_reason") == CloseReason.SIGNAL.value

    events = journal.list_events(pair.pair_token)
    event_types = [e["event_type"] for e in events]
    assert PairEventType.CLOSE_DISPATCHED.value in event_types
    print("[OK] begin_close()")


def test_apply_close_both_success():
    pm, redis, repo, journal = _make_pair_manager()
    pm.bootstrap()
    pair = _create_open_pair(pm)
    pm.begin_close(pair.pair_token, close_reason=CloseReason.SIGNAL, close_spread=0.03)

    mt5_close = {"success": True, "close_price": 2951.0, "profit": 0.50, "latency_ms": 80.0}
    updated = pm.apply_close_results(
        pair.pair_token,
        mt5_result=mt5_close,
        exchange_result=_filled_result(),
    )
    assert updated.status == PairStatus.CLOSED
    assert pm.pair_count == 0  # terminal -> removed
    print("[OK] apply_close_results -- both close -> CLOSED")


def test_apply_close_mt5_ok_exchange_rejected():
    pm, redis, repo, journal = _make_pair_manager()
    pm.bootstrap()
    pair = _create_open_pair(pm)
    pm.begin_close(pair.pair_token, close_reason=CloseReason.SIGNAL, close_spread=0.03)

    mt5_close = {"success": True, "close_price": 2951.0, "profit": 0.50, "latency_ms": 80.0}
    updated = pm.apply_close_results(
        pair.pair_token,
        mt5_result=mt5_close,
        exchange_result=_rejected_result(),
    )
    # MT5 closed, exchange still has position -> ORPHAN_EXCHANGE (exchange survives)
    assert updated.status == PairStatus.ORPHAN_EXCHANGE
    print("[OK] apply_close_results -- MT5 ok, exchange REJECTED -> ORPHAN_EXCHANGE")


def test_apply_close_exchange_ok_mt5_rejected():
    pm, redis, repo, journal = _make_pair_manager()
    pm.bootstrap()
    pair = _create_open_pair(pm)
    pm.begin_close(pair.pair_token, close_reason=CloseReason.SIGNAL, close_spread=0.03)

    updated = pm.apply_close_results(
        pair.pair_token,
        mt5_result=_mt5_failed(),
        exchange_result=_filled_result(),
    )
    # Exchange closed, MT5 still has position -> ORPHAN_MT5 (MT5 survives)
    assert updated.status == PairStatus.ORPHAN_MT5
    print("[OK] apply_close_results -- exchange ok, MT5 rejected -> ORPHAN_MT5")


def test_apply_close_mt5_ok_exchange_unknown():
    pm, redis, repo, journal = _make_pair_manager()
    pm.bootstrap()
    pair = _create_open_pair(pm)
    pm.begin_close(pair.pair_token, close_reason=CloseReason.SIGNAL, close_spread=0.03)

    mt5_close = {"success": True, "close_price": 2951.0, "profit": 0.50, "latency_ms": 80.0}
    updated = pm.apply_close_results(
        pair.pair_token,
        mt5_result=mt5_close,
        exchange_result=_unknown_result(),
    )
    assert updated.status == PairStatus.CLOSE_MT5_DONE
    print("[OK] apply_close_results -- MT5 ok, exchange UNKNOWN -> CLOSE_MT5_DONE")


def test_mt5_position_gone_open_pair():
    pm, redis, repo, journal = _make_pair_manager()
    pm.bootstrap()
    pair = _create_open_pair(pm)
    ticket = pair.mt5_ticket

    updated = pm.mark_mt5_position_gone(ticket)
    assert updated is not None
    # MT5 gone -> Exchange survives -> ORPHAN_EXCHANGE
    assert updated.status == PairStatus.ORPHAN_EXCHANGE

    events = journal.list_events(pair.pair_token)
    event_types = [e["event_type"] for e in events]
    assert PairEventType.STOPOUT_DETECTED.value in event_types
    print("[OK] mark_mt5_position_gone() -> ORPHAN_EXCHANGE (surviving-side)")


def test_mt5_position_gone_non_open_ignored():
    pm, redis, repo, journal = _make_pair_manager()
    pm.bootstrap()
    pair = pm.create_entry(direction="LONG", spread=-0.20, conf_dev_entry=0.15, conf_dev_close=0.05)
    # Pair is PENDING, not OPEN
    result = pm.mark_mt5_position_gone(99999)
    assert result is None
    print("[OK] mark_mt5_position_gone() non-OPEN ignored")


def test_tracked_tickets_sync():
    pm, redis, repo, journal = _make_pair_manager()
    pm.bootstrap()
    pair = _create_open_pair(pm)

    tracked = redis.smembers("mt5:tracked_tickets")
    assert str(pair.mt5_ticket) in tracked

    # Close pair -> should be removed from tracked
    pm.begin_close(pair.pair_token, close_reason=CloseReason.SIGNAL, close_spread=0.03)
    mt5_close = {"success": True, "close_price": 2951.0, "profit": 0.50, "latency_ms": 80.0}
    pm.apply_close_results(pair.pair_token, mt5_result=mt5_close, exchange_result=_filled_result())

    tracked = redis.smembers("mt5:tracked_tickets")
    assert str(pair.mt5_ticket) not in tracked
    print("[OK] _sync_tracked_tickets() add + remove")


def test_terminal_pair_evicted_from_cache():
    pm, redis, repo, journal = _make_pair_manager()
    pm.bootstrap()
    pair = _create_open_pair(pm)
    assert pm.pair_count == 1

    pm.begin_close(pair.pair_token, close_reason=CloseReason.SIGNAL, close_spread=0.03)
    mt5_close = {"success": True, "close_price": 2951.0, "profit": 0.50, "latency_ms": 80.0}
    pm.apply_close_results(pair.pair_token, mt5_result=mt5_close, exchange_result=_filled_result())

    assert pm.pair_count == 0
    assert pm.get_pair(pair.pair_token) is None
    print("[OK] terminal pair evicted from cache")


def test_get_open_pair():
    pm, redis, repo, journal = _make_pair_manager()
    pm.bootstrap()
    assert pm.get_open_pair() is None

    pair = _create_open_pair(pm)
    result = pm.get_open_pair()
    assert result is not None
    assert result.pair_token == pair.pair_token
    print("[OK] get_open_pair()")


def test_get_pair_by_mt5_ticket():
    pm, redis, repo, journal = _make_pair_manager()
    pm.bootstrap()
    pair = _create_open_pair(pm)

    found = pm.get_pair_by_mt5_ticket(99999)
    assert found is not None
    assert found.pair_token == pair.pair_token

    assert pm.get_pair_by_mt5_ticket(11111) is None
    print("[OK] get_pair_by_mt5_ticket()")


def test_force_close_event():
    pm, redis, repo, journal = _make_pair_manager()
    pm.bootstrap()
    pair = _create_open_pair(pm)

    updated = pm.begin_close(
        pair.pair_token,
        close_reason=CloseReason.FORCE_SHUTDOWN,
        close_spread=None,
        force_terminal=True,
    )
    assert updated.status == PairStatus.CLOSE_SENT

    events = journal.list_events(pair.pair_token)
    event_types = [e["event_type"] for e in events]
    assert PairEventType.FORCE_CLOSE.value in event_types
    print("[OK] begin_close(force_terminal=True) -> FORCE_CLOSE event")


# ═══════════════════════════════════════════════════════════
# Group B: TradingBrain Tests
# ═══════════════════════════════════════════════════════════


def _make_brain(**config_overrides) -> tuple[TradingBrain, MockConfig, MockRedis, MockExchange, PairManager]:
    pm, redis, repo, journal = _make_pair_manager()
    pm.bootstrap()
    config = MockConfig(**config_overrides)
    exchange = MockExchange()
    shutdown = asyncio.Event()
    brain = TradingBrain(config, redis, exchange, pm, shutdown)
    brain._warmup_complete = True
    brain._pivot_ema = 0.0
    return brain, config, redis, exchange, pm


def test_calculate_spread():
    brain, *_ = _make_brain()
    mt5 = {"bid": 2950.00, "ask": 2951.00}
    exch = {"bid": 2949.00, "ask": 2950.00}
    spread = brain._calculate_spread(mt5, exch)
    # mt5_mid=2950.50, exch_mid=2949.50 -> spread=1.00
    assert abs(spread - 1.00) < 0.001
    print("[OK] _calculate_spread()")


def test_detect_signal_entry_long():
    brain, _, _, _, pm = _make_brain()
    # spread < -dev_entry (-0.15) -> ENTRY_LONG
    mt5 = {"bid": 2949.00, "ask": 2950.00}
    exch = {"bid": 2950.00, "ask": 2951.00}
    spread = brain._calculate_spread(mt5, exch)  # -1.00
    assert spread < -0.15
    signal = brain._detect_signal(spread)
    assert signal == "ENTRY_LONG"
    print("[OK] _detect_signal() -> ENTRY_LONG")


def test_detect_signal_entry_short():
    brain, _, _, _, pm = _make_brain()
    # spread > +dev_entry -> ENTRY_SHORT
    mt5 = {"bid": 2951.00, "ask": 2952.00}
    exch = {"bid": 2949.00, "ask": 2950.00}
    spread = brain._calculate_spread(mt5, exch)  # +2.00
    assert spread > 0.15
    signal = brain._detect_signal(spread)
    assert signal == "ENTRY_SHORT"
    print("[OK] _detect_signal() -> ENTRY_SHORT")


def test_detect_signal_close():
    brain, _, _, _, pm = _make_brain()
    _create_open_pair(pm)  # has open pair
    # |spread| < dev_close (0.05) -> CLOSE
    mt5 = {"bid": 2950.00, "ask": 2950.10}
    exch = {"bid": 2950.00, "ask": 2950.10}
    spread = brain._calculate_spread(mt5, exch)  # 0.0
    signal = brain._detect_signal(spread)
    assert signal == "CLOSE"
    print("[OK] _detect_signal() -> CLOSE")


def test_detect_signal_close_on_opposite_band():
    brain, _, _, _, pm = _make_brain(dev_close=-0.05)
    _create_open_pair(pm)
    mt5 = {"bid": 2950.00, "ask": 2950.10}
    exch = {"bid": 2949.90, "ask": 2950.00}
    spread = brain._calculate_spread(mt5, exch)  # 0.10
    signal = brain._detect_signal(spread)
    assert signal == "CLOSE"
    print("[OK] _detect_signal() -> CLOSE on opposite band")


def test_detect_signal_none():
    brain, _, _, _, _ = _make_brain()
    # Within thresholds, no pair -> NONE
    mt5 = {"bid": 2950.00, "ask": 2950.10}
    exch = {"bid": 2950.00, "ask": 2950.10}
    spread = brain._calculate_spread(mt5, exch)
    signal = brain._detect_signal(spread)
    assert signal == "NONE"
    print("[OK] _detect_signal() -> NONE")


def test_detect_signal_open_pair_blocks_entry():
    brain, _, _, _, pm = _make_brain()
    _create_open_pair(pm)
    # Even if spread > dev_entry, should NOT trigger entry (has pair)
    mt5 = {"bid": 2951.00, "ask": 2952.00}
    exch = {"bid": 2949.00, "ask": 2950.00}
    spread = brain._calculate_spread(mt5, exch)
    signal = brain._detect_signal(spread)
    assert signal == "NONE"  # not ENTRY_SHORT, because pair exists and spread > dev_close
    print("[OK] _detect_signal() -- open pair blocks new entry")


def test_execute_entry_auth_dead_blocks_order_dispatch():
    brain, _, redis, exchange, pm = _make_brain()
    exchange._auth_alive = False
    now = time.time()
    brain._latest_mt5 = {"bid": 2949.0, "ask": 2950.0, "local_ts": now, "time_msc": 1}
    brain._latest_exchange = {"bid": 2950.0, "ask": 2951.0, "local_ts": now, "ts": 1}

    async def _run():
        await brain._execute_entry(direction="LONG")

    asyncio.run(_run())

    assert pm.pair_count == 0
    assert redis._lists.get(REDIS_MT5_ORDER_QUEUE, []) == []
    assert brain._risk_flag is True
    assert brain._risk_reason == "exchange_auth_dead"
    print("[OK] _execute_entry() blocks new entry when exchange auth is dead")


def test_entry_auth_uses_fresh_cache_without_extra_ping():
    brain, _, _, exchange, _ = _make_brain()
    brain._record_auth_check(True)
    exchange._auth_alive = False

    async def _run():
        return await brain._ensure_exchange_auth_for_entry()

    assert asyncio.run(_run()) is True
    assert exchange.auth_check_calls == 0
    print("[OK] entry auth uses fresh cache without extra ping")


def test_is_data_fresh():
    brain, *_ = _make_brain()
    now = time.time()
    fresh = {"local_ts": now - 1.0}
    stale = {"local_ts": now - 15.0}

    assert brain._is_data_fresh(fresh, fresh) is True
    assert brain._is_data_fresh(stale, fresh) is False
    assert brain._is_data_fresh(fresh, stale) is False
    print("[OK] _is_data_fresh()")


def test_gate_abort_missing_ticks():
    brain, *_ = _make_brain()
    brain._latest_mt5 = None
    assert brain._gate_abort_reason("ENTRY_LONG") == "missing_ticks"
    print("[OK] _gate_abort_reason() -> missing_ticks")


def test_gate_abort_signal_changed():
    brain, _, _, _, pm = _make_brain()
    now = time.time()
    # Set ticks that produce spread=0 (NONE signal)
    brain._latest_mt5 = {"bid": 2950.0, "ask": 2950.1, "local_ts": now}
    brain._latest_exchange = {"bid": 2950.0, "ask": 2950.1, "local_ts": now}
    assert brain._gate_abort_reason("ENTRY_LONG") == "signal_changed"
    print("[OK] _gate_abort_reason() -> signal_changed")


def test_gate_abort_risk_flag():
    brain, _, _, _, pm = _make_brain()
    now = time.time()
    brain._latest_mt5 = {"bid": 2949.0, "ask": 2950.0, "local_ts": now}
    brain._latest_exchange = {"bid": 2950.5, "ask": 2951.0, "local_ts": now}
    brain._risk_flag = True
    assert brain._gate_abort_reason("ENTRY_LONG") == "risk_flag"
    print("[OK] _gate_abort_reason() -> risk_flag")


def test_gate_abort_clear():
    brain, _, _, _, pm = _make_brain(warmup_sec=0, warmup_min_ticks=0)
    brain._warmup_complete = True
    now = time.time()
    # Set ticks that produce ENTRY_LONG signal
    brain._latest_mt5 = {"bid": 2949.0, "ask": 2950.0, "local_ts": now}
    brain._latest_exchange = {"bid": 2950.5, "ask": 2951.0, "local_ts": now}
    # Check gates pass
    result = brain._gate_abort_reason("ENTRY_LONG")
    assert result is None, f"Expected None, got {result}"
    print("[OK] _gate_abort_reason() -> None (all gates pass)")


def test_maintenance_hold_timeout_forces_close():
    pm, redis, repo, journal = _make_pair_manager()
    pm.bootstrap()
    config = MockConfig(hold_time=10)
    exchange = MockExchange()
    brain = TradingBrain(config, redis, exchange, pm, asyncio.Event())
    brain._warmup_complete = True
    brain._pivot_ema = 0.0

    pair = _create_open_pair(pm)
    stale_row = dict(pair.raw)
    stale_row["entry_time"] = time.time() - 60
    pm._pairs[pair.pair_token] = PairRecord.from_row(stale_row)

    calls = []

    async def fake_execute_close(*, close_reason, success_status=PairStatus.CLOSED):
        calls.append((close_reason, success_status))

    brain._execute_close = fake_execute_close
    asyncio.run(brain._run_maintenance_checks())

    assert calls == [(CloseReason.HOLD_TIMEOUT, PairStatus.FORCE_CLOSED)]
    print("[OK] maintenance loop force-closes on hold timeout")


def test_maintenance_force_schedule_uses_utc():
    pm, redis, repo, journal = _make_pair_manager()
    pm.bootstrap()
    config = MockConfig(hold_time=0, schedule={"trading_hours": [], "force_close_hours": ["20:15-20:20"]})
    exchange = MockExchange()
    brain = TradingBrain(config, redis, exchange, pm, asyncio.Event())
    brain._warmup_complete = True
    brain._pivot_ema = 0.0
    brain._utc_now = lambda: datetime(2026, 4, 1, 20, 16, tzinfo=timezone.utc)
    _create_open_pair(pm)

    calls = []

    async def fake_execute_close(*, close_reason, success_status=PairStatus.CLOSED):
        calls.append((close_reason, success_status))

    brain._execute_close = fake_execute_close
    asyncio.run(brain._run_maintenance_checks())

    assert calls == [(CloseReason.FORCE_SCHEDULE, PairStatus.FORCE_CLOSED)]
    print("[OK] maintenance loop uses UTC force-close windows")


def test_maintenance_mt5_equity_threshold_trips_risk():
    brain, _, redis, _, _ = _make_brain(alert_equity_mt5=500.0)
    redis.set(
        REDIS_MT5_HEARTBEAT,
        orjson.dumps({"equity": 499.5, "ts": time.time()}).decode("utf-8"),
    )

    brain._enforce_mt5_equity_floor()

    assert brain._risk_flag is True
    assert "mt5_equity_below" in brain._risk_reason
    print("[OK] maintenance loop latches risk on low MT5 equity")


def test_orphan_limit_trips_risk_and_cooldown():
    brain, _, _, _, _ = _make_brain(orphan_max_count=1, orphan_cooldown_sec=900)
    pair = SimpleNamespace(pair_token="PAIR001", status=PairStatus.ORPHAN_MT5)

    brain._observe_pair_update(pair)

    assert brain._risk_flag is True
    assert "orphan_limit" in brain._risk_reason
    assert brain._orphan_cooldown_until > time.time()
    print("[OK] orphan enforcement arms cooldown and trips risk limit")


def test_handle_position_gone():
    brain, _, _, _, pm = _make_brain()
    pair = _create_open_pair(pm)

    async def _run():
        await brain._handle_position_gone({"ticket": pair.mt5_ticket})

    asyncio.run(_run())

    assert brain._risk_flag is True
    assert "mt5_position_gone" in brain._risk_reason
    updated = pm.get_pair(pair.pair_token)
    assert updated is not None
    assert updated.status == PairStatus.ORPHAN_EXCHANGE
    print("[OK] _handle_position_gone() -> risk_flag + ORPHAN_EXCHANGE")


def test_safe_exchange_order_exception():
    brain, _, _, exchange, _ = _make_brain()

    async def _crash(side, volume):
        raise ConnectionError("Network down")

    exchange.place_order = _crash

    async def _run():
        return await brain._safe_exchange_order(OrderSide.OPEN_SHORT, 1000)

    result = asyncio.run(_run())
    assert result.success is False
    assert result.order_status == ExchangeOrderStatus.UNKNOWN
    assert "Network down" in result.error
    print("[OK] _safe_exchange_order() exception -> UNKNOWN result")


def test_decode_payload():
    import orjson
    result = TradingBrain._decode_payload(orjson.dumps({"key": "value"}))
    assert result == {"key": "value"}

    result = TradingBrain._decode_payload(b'{"num": 42}')
    assert result == {"num": 42}

    result = TradingBrain._decode_payload(None)
    assert result == {}

    try:
        TradingBrain._decode_payload(b'[1,2,3]')
        assert False, "Should have raised ValueError"
    except ValueError:
        pass
    print("[OK] _decode_payload() bytes/str/None/non-dict")


# ═══════════════════════════════════════════════════════════
# Runner
# ═══════════════════════════════════════════════════════════


ALL_TESTS = [
    # Group A: PairManager
    test_bootstrap_empty,
    test_bootstrap_one_open,
    test_bootstrap_multiple_non_terminal,
    test_bootstrap_non_open_status,
    test_allows_new_entries,
    test_create_entry,
    test_mark_entry_dispatched,
    test_apply_entry_both_fill,
    test_apply_entry_mt5_ok_exchange_rejected,
    test_apply_entry_exchange_ok_mt5_rejected,
    test_apply_entry_both_rejected,
    test_apply_entry_mt5_ok_exchange_unknown,
    test_apply_entry_exchange_ok_mt5_unknown,
    test_begin_close,
    test_apply_close_both_success,
    test_apply_close_mt5_ok_exchange_rejected,
    test_apply_close_exchange_ok_mt5_rejected,
    test_apply_close_mt5_ok_exchange_unknown,
    test_mt5_position_gone_open_pair,
    test_mt5_position_gone_non_open_ignored,
    test_tracked_tickets_sync,
    test_terminal_pair_evicted_from_cache,
    test_get_open_pair,
    test_get_pair_by_mt5_ticket,
    test_force_close_event,
    # Group B: TradingBrain
    test_calculate_spread,
    test_detect_signal_entry_long,
    test_detect_signal_entry_short,
    test_detect_signal_close,
    test_detect_signal_none,
    test_detect_signal_open_pair_blocks_entry,
    test_execute_entry_auth_dead_blocks_order_dispatch,
    test_entry_auth_uses_fresh_cache_without_extra_ping,
    test_is_data_fresh,
    test_gate_abort_missing_ticks,
    test_gate_abort_signal_changed,
    test_gate_abort_risk_flag,
    test_gate_abort_clear,
    test_maintenance_hold_timeout_forces_close,
    test_maintenance_force_schedule_uses_utc,
    test_maintenance_mt5_equity_threshold_trips_risk,
    test_orphan_limit_trips_risk_and_cooldown,
    test_handle_position_gone,
    test_safe_exchange_order_exception,
    test_decode_payload,
]


if __name__ == "__main__":
    passed = 0
    failed = 0
    errors = []

    for test_fn in ALL_TESTS:
        try:
            test_fn()
            passed += 1
        except Exception as exc:
            failed += 1
            errors.append((test_fn.__name__, traceback.format_exc()))

    print(f"\n{'='*60}")
    print(f"Phase 3 Test Results: {passed} passed, {failed} failed out of {len(ALL_TESTS)}")
    if errors:
        print("\nFailed tests:")
        for name, err in errors:
            print(f"  x {name}: {err}")
    else:
        print("\n=== test_phase3_core (Phase 3): ALL PASSED ===")
    print(f"{'='*60}")

    if failed:
        sys.exit(1)
