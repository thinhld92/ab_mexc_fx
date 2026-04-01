"""Unit tests for the Phase 3 PairManager."""

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.core.pair_manager import PairManager
from src.domain import CloseReason, PairCreate, PairStatus
from src.exchanges.base import ExchangeOrderStatus, OrderResult, OrderSide
from src.storage import Database, JournalRepository, PairRepository
from src.utils.constants import REDIS_MT5_TRACKED_TICKETS


class MockRedis:
    def __init__(self):
        self._sets = {}

    def delete(self, *keys):
        for key in keys:
            self._sets.pop(key, None)

    def sadd(self, key, *values):
        self._sets.setdefault(key, set()).update(values)

    def smembers(self, key):
        return self._sets.get(key, set())


def _make_manager():
    db = Database(":memory:")
    db.initialize()
    redis = MockRedis()
    repo = PairRepository(db)
    journal = JournalRepository(db)
    manager = PairManager(redis, repo, journal)
    return manager, repo, journal, redis
def test_bootstrap_rebuilds_tracked_tickets():
    manager, repo, _, redis = _make_manager()
    repo.insert_pending_pair(
        PairCreate(
            pair_token="BOOT_1",
            direction="LONG",
            entry_time=time.time(),
            entry_spread=-0.4,
            conf_dev_entry=0.15,
            conf_dev_close=0.05,
        )
    )
    repo.transition("BOOT_1", PairStatus.ENTRY_SENT)
    repo.transition(
        "BOOT_1",
        PairStatus.ENTRY_MT5_FILLED,
        fields={"mt5_ticket": 555, "mt5_action": "BUY", "mt5_volume": 0.01},
    )

    pairs = manager.bootstrap()
    assert len(pairs) == 1
    assert manager.safe_mode is True
    assert redis.smembers(REDIS_MT5_TRACKED_TICKETS) == {"555"}
    print("[OK] bootstrap rebuilds tracked tickets")


def test_entry_success_opens_pair():
    manager, _, _, redis = _make_manager()
    pair = manager.create_entry(direction="LONG", spread=-0.8, conf_dev_entry=0.15, conf_dev_close=0.05)
    manager.mark_entry_dispatched(pair.pair_token)

    updated = manager.apply_entry_results(
        pair.pair_token,
        mt5_result={"success": True, "ticket": 1001, "action": "BUY", "volume": 0.01, "price": 3245.5},
        exchange_result=OrderResult(
            success=True,
            order_status=ExchangeOrderStatus.FILLED,
            order_id="EX-1001",
            latency_ms=45.0,
        ),
        exchange_volume=1000,
        exchange_side=int(OrderSide.OPEN_SHORT),
    )
    assert updated.status == PairStatus.OPEN
    assert manager.get_open_pair() is not None
    assert redis.smembers(REDIS_MT5_TRACKED_TICKETS) == {"1001"}
    print("[OK] entry success -> OPEN")


def test_entry_unknown_keeps_intermediate_state():
    manager, _, _, redis = _make_manager()
    pair = manager.create_entry(direction="SHORT", spread=0.9, conf_dev_entry=0.15, conf_dev_close=0.05)
    manager.mark_entry_dispatched(pair.pair_token)

    updated = manager.apply_entry_results(
        pair.pair_token,
        mt5_result={"success": True, "ticket": 2002, "action": "SELL", "volume": 0.01, "price": 3245.1},
        exchange_result=OrderResult(
            success=False,
            order_status=ExchangeOrderStatus.UNKNOWN,
            error="timeout",
        ),
        exchange_volume=1000,
        exchange_side=int(OrderSide.OPEN_LONG),
    )
    assert updated.status == PairStatus.ENTRY_MT5_FILLED
    assert redis.smembers(REDIS_MT5_TRACKED_TICKETS) == {"2002"}
    print("[OK] mt5 success + exchange unknown -> ENTRY_MT5_FILLED")


def test_safe_mode_on_recovered_non_open_pair():
    manager, repo, _, _ = _make_manager()
    repo.insert_pending_pair(
        PairCreate(
            pair_token="SAFE_1",
            direction="LONG",
            entry_time=time.time(),
            entry_spread=-0.4,
            conf_dev_entry=0.15,
            conf_dev_close=0.05,
        )
    )
    repo.transition("SAFE_1", PairStatus.ENTRY_SENT)

    manager.bootstrap()
    assert manager.safe_mode is True
    assert manager.allows_new_entries is False
    print("[OK] recovered ENTRY_SENT enables safe mode")


def test_close_success_reaches_closed():
    manager, _, _, redis = _make_manager()
    pair = manager.create_entry(direction="LONG", spread=-0.8, conf_dev_entry=0.15, conf_dev_close=0.05)
    manager.mark_entry_dispatched(pair.pair_token)
    opened = manager.apply_entry_results(
        pair.pair_token,
        mt5_result={"success": True, "ticket": 4444, "action": "BUY", "volume": 0.01, "price": 3245.5},
        exchange_result=OrderResult(
            success=True,
            order_status=ExchangeOrderStatus.FILLED,
            order_id="EX-4444",
        ),
        exchange_volume=1000,
        exchange_side=int(OrderSide.OPEN_SHORT),
    )
    manager.begin_close(
        opened.pair_token,
        close_reason=CloseReason.SIGNAL,
        close_spread=0.01,
    )
    closed = manager.apply_close_results(
        opened.pair_token,
        mt5_result={"success": True, "ticket": 4444, "action": "CLOSE", "close_price": 3245.0, "profit": 1.5},
        exchange_result=OrderResult(
            success=True,
            order_status=ExchangeOrderStatus.FILLED,
            order_id="EX-CLOSE-4444",
        ),
    )
    assert closed.status == PairStatus.CLOSED
    assert manager.pair_count == 0
    assert redis.smembers(REDIS_MT5_TRACKED_TICKETS) == set()
    print("[OK] close success -> CLOSED")


def test_mt5_position_gone_maps_to_orphan_exchange():
    manager, _, _, redis = _make_manager()
    pair = manager.create_entry(direction="LONG", spread=-0.8, conf_dev_entry=0.15, conf_dev_close=0.05)
    manager.mark_entry_dispatched(pair.pair_token)
    manager.apply_entry_results(
        pair.pair_token,
        mt5_result={"success": True, "ticket": 3333, "action": "BUY", "volume": 0.01, "price": 3245.5},
        exchange_result=OrderResult(
            success=True,
            order_status=ExchangeOrderStatus.FILLED,
            order_id="EX-3333",
        ),
        exchange_volume=1000,
        exchange_side=int(OrderSide.OPEN_SHORT),
    )

    updated = manager.mark_mt5_position_gone(3333)
    assert updated is not None
    assert updated.status == PairStatus.ORPHAN_EXCHANGE
    assert redis.smembers(REDIS_MT5_TRACKED_TICKETS) == set()
    print("[OK] mt5 position gone -> ORPHAN_EXCHANGE")
if __name__ == "__main__":
    test_bootstrap_rebuilds_tracked_tickets()
    test_entry_success_opens_pair()
    test_entry_unknown_keeps_intermediate_state()
    test_safe_mode_on_recovered_non_open_pair()
    test_close_success_reaches_closed()
    test_mt5_position_gone_maps_to_orphan_exchange()
    print("\n=== test_pair_manager: ALL PASSED ===")
