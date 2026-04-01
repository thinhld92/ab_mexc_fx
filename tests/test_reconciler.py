"""Unit tests for the Phase 4 Reconciler."""

from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path

import orjson

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.domain import PairCreate, PairStatus, ReconStatus
from src.exchanges.base import ExchangeOrderStatus, OrderResult, OrderSide
from src.reconciler import Reconciler
from src.storage import Database, JournalRepository, PairRepository
from src.utils.constants import (
    REDIS_BRAIN_HEARTBEAT,
    REDIS_MT5_HEARTBEAT,
    REDIS_MT5_ORDER_RESULT,
    REDIS_MT5_POSITIONS_RESPONSE,
    REDIS_MT5_QUERY_POSITIONS,
    REDIS_RECON_CMD_SHUTDOWN,
    REDIS_RECON_HEARTBEAT,
)


class MockConfig:
    def __init__(self, **overrides):
        self._overrides = overrides
        self.redis = {}

    def __getattr__(self, name):
        defaults = {
            "max_orders": 1,
            "reconciler_interval_sec": 1,
            "reconciler_auto_heal": True,
            "reconciler_ghost_action": "alert",
            "reconciler_alert_telegram": False,
            "reconciler_stale_entry_sec": 0,
            "reconciler_stale_close_sec": 0,
            "reconciler_stale_healing_sec": 0,
            "mexc_volume": 1000,
            "mt5_volume": 0.01,
            "mexc_symbol": "XAUT_USDT",
        }
        if name in self._overrides:
            return self._overrides[name]
        if name in defaults:
            return defaults[name]
        raise AttributeError(name)


class MockRedis:
    def __init__(self):
        self._data = {}
        self._lists = {}
        self.next_mt5_positions = []
        self.next_mt5_order_result = {"success": True, "close_price": 2501.0, "profit": 0.5}

    def get(self, key):
        return self._data.get(key)

    def set(self, key, value, ex=None):
        self._data[key] = value

    def lpush(self, key, *values):
        self._lists.setdefault(key, [])
        for value in values:
            self._lists[key].insert(0, value)
            if key == REDIS_MT5_QUERY_POSITIONS:
                payload = orjson.loads(value)
                response = {
                    "query_id": payload["query_id"],
                    "ts": time.time(),
                    "positions": list(self.next_mt5_positions),
                }
                self._lists.setdefault(REDIS_MT5_POSITIONS_RESPONSE, []).insert(
                    0,
                    orjson.dumps(response).decode("utf-8"),
                )
            elif key == "mt5:order:queue":
                payload = orjson.loads(value)
                result = dict(self.next_mt5_order_result or {})
                result.setdefault("success", True)
                result["job_id"] = payload["job_id"]
                result.setdefault("ticket", payload.get("ticket"))
                if result.get("success") and payload.get("action") == "CLOSE":
                    ticket = int(payload.get("ticket", 0) or 0)
                    self.next_mt5_positions = [
                        pos for pos in self.next_mt5_positions if int(pos.get("ticket", 0)) != ticket
                    ]
                self._lists.setdefault(REDIS_MT5_ORDER_RESULT, []).insert(
                    0,
                    orjson.dumps(result).decode("utf-8"),
                )

    def brpop(self, key, timeout=0):
        lst = self._lists.get(key, [])
        if lst:
            return (key, lst.pop())
        return None


class MockExchange:
    def __init__(self):
        self.positions = []
        self.next_result = OrderResult(
            success=True,
            order_status=ExchangeOrderStatus.FILLED,
            order_id="EX-CLOSE-1",
            latency_ms=12.0,
        )
        self.connected = False
        self.stop_called = False
        self.place_calls = []
        self.auth_alive = True

    async def connect(self):
        self.connected = True

    async def stop(self):
        self.stop_called = True

    async def check_auth_alive(self):
        return self.auth_alive

    async def get_positions(self):
        return list(self.positions)

    async def place_order(self, side, volume):
        self.place_calls.append((side, volume))
        if self.next_result.success:
            self.positions = []
        return self.next_result


def _make_pair(repo: PairRepository, token: str, *, direction: str = "LONG", entry_time: float | None = None):
    return repo.insert_pending_pair(
        PairCreate(
            pair_token=token,
            direction=direction,
            entry_time=time.time() - 120 if entry_time is None else entry_time,
            entry_spread=-0.4,
            conf_dev_entry=0.15,
            conf_dev_close=0.05,
        )
    )


def _make_reconciler(**config_overrides):
    db = Database(":memory:")
    db.initialize()
    repo = PairRepository(db)
    journal = JournalRepository(db)
    redis = MockRedis()
    exchange = MockExchange()
    config = MockConfig(**config_overrides)
    reconciler = Reconciler(
        config=config,
        redis_client=redis,
        exchange=exchange,
        db=db,
        repo=repo,
        journal=journal,
        shutdown_event=asyncio.Event(),
    )
    return reconciler, repo, journal, redis, exchange


async def test_open_pair_ok_sets_recon_status():
    reconciler, repo, _, redis, exchange = _make_reconciler()
    token = "PAIR_OK"
    _make_pair(repo, token)
    repo.transition(token, PairStatus.ENTRY_SENT)
    repo.transition(
        token,
        PairStatus.OPEN,
        fields={
            "mt5_ticket": 111,
            "mt5_action": "BUY",
            "mt5_volume": 0.01,
            "exchange_order_id": "EX-111",
            "exchange_volume": 1000,
            "exchange_side": int(OrderSide.OPEN_SHORT),
        },
    )
    redis.next_mt5_positions = [
        {"ticket": 111, "type": "BUY", "volume": 0.01, "price": 2500.0, "comment": f"arb:{token}"}
    ]
    exchange.positions = [
        {"position_id": "POS-1", "symbol": "XAUT_USDT", "side": "SHORT", "volume": 1000, "volume_raw": 1000}
    ]

    await reconciler.reconcile_once()
    record = repo.get_by_token(token)
    assert record is not None
    assert record.recon_status == ReconStatus.OK
    print("[OK] OPEN pair -> recon_status OK")


async def test_stale_entry_recovers_open_from_comment_and_exchange_side():
    reconciler, repo, _, redis, exchange = _make_reconciler()
    token = "ENTRY_RECOVER"
    _make_pair(repo, token)
    repo.transition(token, PairStatus.ENTRY_SENT)
    redis.next_mt5_positions = [
        {"ticket": 222, "type": "BUY", "volume": 0.01, "price": 2502.0, "comment": f"arb:{token}"}
    ]
    exchange.positions = [
        {"position_id": "POS-2", "symbol": "XAUT_USDT", "side": "SHORT", "volume": 1000, "volume_raw": 1000}
    ]

    await reconciler.reconcile_once()
    record = repo.get_by_token(token)
    assert record is not None
    assert record.status == PairStatus.OPEN
    assert record.mt5_ticket == 222
    assert record.recon_status == ReconStatus.OK
    print("[OK] STALE_ENTRY -> OPEN recovery uses MT5 comment and exchange SHORT for LONG pair")


async def test_orphan_mt5_auto_heals_to_healed():
    reconciler, repo, _, redis, exchange = _make_reconciler()
    token = "ORPHAN_MT5_HEAL"
    _make_pair(repo, token)
    repo.transition(token, PairStatus.ENTRY_SENT)
    repo.mark_mt5_entry_filled(
        token,
        mt5_ticket=333,
        mt5_action="BUY",
        mt5_volume=0.01,
        mt5_entry_price=2503.0,
    )
    repo.transition(token, PairStatus.ORPHAN_MT5)
    redis.next_mt5_positions = [
        {"ticket": 333, "type": "BUY", "volume": 0.01, "price": 2503.0, "comment": f"arb:{token}"}
    ]
    exchange.positions = []

    await reconciler.reconcile_once()
    record = repo.get_by_token(token)
    assert record is not None
    assert record.status == PairStatus.HEALED
    assert record.recon_status == ReconStatus.HEALED
    print("[OK] ORPHAN_MT5 auto-heal -> HEALED")


async def test_orphan_exchange_already_gone_marks_healed():
    reconciler, repo, _, redis, exchange = _make_reconciler()
    token = "ORPHAN_EXCHANGE_GONE"
    _make_pair(repo, token)
    repo.transition(token, PairStatus.ENTRY_SENT)
    repo.mark_exchange_entry_filled(
        token,
        exchange_order_id="EX-777",
        exchange_volume=1000,
        exchange_side=int(OrderSide.OPEN_SHORT),
    )
    repo.transition(token, PairStatus.ORPHAN_EXCHANGE)
    redis.next_mt5_positions = []
    exchange.positions = []

    await reconciler.reconcile_once()
    record = repo.get_by_token(token)
    assert record is not None
    assert record.status == PairStatus.HEALED
    print("[OK] ORPHAN_EXCHANGE already gone -> HEALED")


async def test_stale_close_with_no_positions_closes_pair():
    reconciler, repo, _, redis, exchange = _make_reconciler()
    token = "CLOSE_RECOVER"
    _make_pair(repo, token)
    repo.transition(token, PairStatus.ENTRY_SENT)
    repo.transition(
        token,
        PairStatus.OPEN,
        fields={
            "mt5_ticket": 444,
            "mt5_action": "BUY",
            "mt5_volume": 0.01,
            "exchange_order_id": "EX-444",
            "exchange_volume": 1000,
            "exchange_side": int(OrderSide.OPEN_SHORT),
        },
    )
    repo.transition(token, PairStatus.CLOSE_SENT, fields={"close_reason": "SIGNAL", "close_spread": 0.0})
    redis.next_mt5_positions = []
    exchange.positions = []

    await reconciler.reconcile_once()
    record = repo.get_by_token(token)
    assert record is not None
    assert record.status == PairStatus.CLOSED
    print("[OK] STALE_CLOSE with no live positions -> CLOSED")


async def test_healing_stuck_without_positions_marks_healed():
    reconciler, repo, _, redis, exchange = _make_reconciler()
    token = "HEALING_STUCK"
    _make_pair(repo, token)
    repo.transition(token, PairStatus.ENTRY_SENT)
    repo.mark_exchange_entry_filled(
        token,
        exchange_order_id="EX-555",
        exchange_volume=1000,
        exchange_side=int(OrderSide.OPEN_SHORT),
    )
    repo.transition(token, PairStatus.ORPHAN_EXCHANGE)
    repo.transition(token, PairStatus.HEALING, fields={"recon_status": ReconStatus.ORPHAN_EXCHANGE.value})
    redis.next_mt5_positions = []
    exchange.positions = []

    await reconciler.reconcile_once()
    record = repo.get_by_token(token)
    assert record is not None
    assert record.status == PairStatus.HEALED
    print("[OK] HEALING stuck with no positions -> HEALED")


async def test_reconciler_startup_waits_for_heartbeats_and_publishes_heartbeat():
    reconciler, _, _, redis, exchange = _make_reconciler(reconciler_interval_sec=5)
    redis.set(REDIS_MT5_HEARTBEAT, orjson.dumps({"ts": time.time()}).decode("utf-8"))
    redis.set(REDIS_BRAIN_HEARTBEAT, orjson.dumps({"ts": time.time()}).decode("utf-8"))
    redis.next_mt5_positions = []
    exchange.positions = []

    task = asyncio.create_task(reconciler.start())
    await asyncio.sleep(0.2)
    assert exchange.connected is True
    assert redis.get(REDIS_RECON_HEARTBEAT) is not None
    await reconciler.request_shutdown()
    await task
    print("[OK] Reconciler startup waits for heartbeats and publishes recon heartbeat")


async def test_reconciler_shutdown_monitor_reacts_to_flag():
    reconciler, _, _, redis, _ = _make_reconciler()
    task = asyncio.create_task(reconciler._shutdown_monitor_loop())
    await asyncio.sleep(0.05)
    redis.set(REDIS_RECON_CMD_SHUTDOWN, "1")
    await asyncio.sleep(0.6)
    assert reconciler.shutdown_event.is_set() is True
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    print("[OK] Reconciler shutdown monitor reacts to recon:cmd:shutdown")


async def _main():
    await test_open_pair_ok_sets_recon_status()
    await test_stale_entry_recovers_open_from_comment_and_exchange_side()
    await test_orphan_mt5_auto_heals_to_healed()
    await test_orphan_exchange_already_gone_marks_healed()
    await test_stale_close_with_no_positions_closes_pair()
    await test_healing_stuck_without_positions_marks_healed()
    await test_reconciler_startup_waits_for_heartbeats_and_publishes_heartbeat()
    await test_reconciler_shutdown_monitor_reacts_to_flag()
    print("\n=== test_reconciler: ALL PASSED ===")


if __name__ == "__main__":
    asyncio.run(_main())


