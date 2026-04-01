"""Phase 4 reconciler tests — Reconciler matching, reconcile_once(), healing, ghosts.

Strategy: Test reconcile_once() end-to-end with controlled DB state + mock
MT5/Exchange positions.  Real :memory: SQLite, MockRedis, MockExchange.

IPC bypass: We monkey-patch _query_mt5_positions, _mt5_ticket_exists, and
_close_mt5_ticket to avoid Redis brpop blocking in tests.
"""

from __future__ import annotations

import asyncio
import sys
import os
import time
import traceback

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.domain import CloseReason, PairEventType, PairStatus, ReconStatus
from src.domain.state_machine import TERMINAL_STATUSES
from src.exchanges.base import (
    ErrorCategory,
    ExchangeOrderStatus,
    OrderResult,
    OrderSide,
)
from src.storage import Database, PairRepository, JournalRepository
from src.reconciler.reconciler import Reconciler, ExchangeMatch, PairObservation


# ═══════════════════════════════════════════════════════════
# Mocks
# ═══════════════════════════════════════════════════════════


class MockRedis:
    """In-memory Redis stub for reconciler tests."""

    def __init__(self):
        self._data = {}
        self._sets = {}
        self._lists = {}

    def get(self, key):
        return self._data.get(key)

    def set(self, key, value, ex=None):
        self._data[key] = value

    def delete(self, *keys):
        for key in keys:
            self._data.pop(key, None)

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
        if isinstance(key, (list, tuple)):
            for k in key:
                lst = self._lists.get(k, [])
                if lst:
                    return (k, lst.pop())
            return None
        lst = self._lists.get(key, [])
        if lst:
            return (key, lst.pop())
        return None

    def publish(self, channel, message):
        pass


class MockExchange:
    """Exchange stub returning pre-configured positions and order results."""

    def __init__(self):
        self._positions: list[dict] = []
        self._order_result = _filled_result()
        self._auth_alive = True

    async def connect(self):
        pass

    async def stop(self):
        pass

    async def check_auth_alive(self):
        return self._auth_alive

    async def get_positions(self):
        return list(self._positions)

    async def place_order(self, side, volume):
        return self._order_result


class MockConfig:
    """Minimal config for reconciler tests."""

    def __init__(self, **overrides):
        self._overrides = overrides

    def __getattr__(self, name):
        defaults = {
            "max_orders": 1,
            "mt5_volume": 0.01,
            "mexc_volume": 1000,
            "mexc_symbol": "XAUT_USDT",
            "reconciler_interval_sec": 30,
            "reconciler_stale_entry_sec": 0,
            "reconciler_stale_close_sec": 0,
            "reconciler_stale_healing_sec": 0,
            "reconciler_auto_heal": True,
            "reconciler_ghost_action": "alert",
            "reconciler_alert_telegram": False,
            "redis": {},
        }
        if name in self._overrides:
            return self._overrides[name]
        if name in defaults:
            return defaults[name]
        raise AttributeError(f"MockConfig has no attribute '{name}'")


# ═══════════════════════════════════════════════════════════
# Helpers
# ═══════════════════════════════════════════════════════════


def _filled_result(**kwargs) -> OrderResult:
    defaults = dict(
        success=True,
        order_status=ExchangeOrderStatus.FILLED,
        order_id="EX_HEAL_001",
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


def _make_reconciler(
    mt5_positions: list[dict] | None = None,
    mt5_close_success: bool = True,
    mt5_after_close: list[dict] | None = None,
    **config_overrides,
) -> tuple[Reconciler, MockRedis, MockExchange, Database, PairRepository, JournalRepository]:
    """Build a Reconciler wired to in-memory DB + mocks.

    MT5 IPC is monkey-patched to avoid brpop blocking:
      - _query_mt5_positions -> returns mt5_positions list
      - _mt5_ticket_exists -> checks mt5_after_close list (post-heal)
      - _close_mt5_ticket -> returns synthetic result dict
    """
    db = Database("")
    db.initialize()
    repo = PairRepository(db)
    journal = JournalRepository(db)
    redis = MockRedis()
    exchange = MockExchange()
    config = MockConfig(**config_overrides)
    shutdown = asyncio.Event()

    recon = Reconciler(
        config=config,
        redis_client=redis,
        exchange=exchange,
        shutdown_event=shutdown,
        db=db,
        repo=repo,
        journal=journal,
    )

    # ── Monkey-patch MT5 IPC methods ──
    _mt5_pos = list(mt5_positions or [])
    _mt5_after = list(mt5_after_close) if mt5_after_close is not None else []
    _close_ok = mt5_close_success
    _query_count = [0]

    async def _fake_query_mt5():
        _query_count[0] += 1
        if _query_count[0] > 1 and mt5_after_close is not None:
            return list(_mt5_after)
        return list(_mt5_pos)

    async def _fake_mt5_exists(ticket, *, pair_token=None):
        check_list = _mt5_after if mt5_after_close is not None else _mt5_pos
        for p in check_list:
            if int(p.get("ticket", 0)) == ticket:
                return True
            if pair_token and pair_token in str(p.get("comment", "")):
                return True
        return False

    async def _fake_close_mt5(*, ticket, volume, pair_token):
        if _close_ok:
            return {"success": True, "close_price": 2951.0, "profit": 0.50, "latency_ms": 80.0, "job_id": "test"}
        return {"success": False, "error": "Close failed", "job_id": "test"}

    recon._query_mt5_positions = _fake_query_mt5
    recon._mt5_ticket_exists = _fake_mt5_exists
    recon._close_mt5_ticket = _fake_close_mt5

    return recon, redis, exchange, db, repo, journal


def _insert_pair(repo: PairRepository, journal: JournalRepository, token: str, **overrides) -> None:
    """Insert a pair and transition it to the desired status."""
    entry_time = overrides.pop("entry_time", time.time() - 120)
    direction = overrides.pop("direction", "LONG")
    target_status = overrides.pop("target_status", PairStatus.PENDING)

    from src.domain.models import PairCreate
    pair_create = PairCreate(
        pair_token=token,
        direction=direction,
        entry_time=entry_time,
        entry_spread=-0.20,
        conf_dev_entry=0.15,
        conf_dev_close=0.05,
    )
    repo.insert_pending_pair(pair_create)
    journal.add_event(token, PairEventType.CREATED, {"direction": direction})

    if target_status == PairStatus.PENDING:
        return

    # Walk through transitions
    need_entry_sent = target_status not in {PairStatus.PENDING}
    need_open = target_status in {
        PairStatus.OPEN, PairStatus.CLOSE_SENT, PairStatus.CLOSE_MT5_DONE,
        PairStatus.CLOSE_EXCHANGE_DONE, PairStatus.ORPHAN_MT5,
        PairStatus.ORPHAN_EXCHANGE, PairStatus.HEALING,
    }

    if need_entry_sent:
        repo.transition(token, PairStatus.ENTRY_SENT)

    if target_status == PairStatus.ENTRY_MT5_FILLED:
        repo.mark_mt5_entry_filled(
            token, mt5_ticket=overrides.get("mt5_ticket", 99999),
            mt5_action="BUY" if direction == "LONG" else "SELL", mt5_volume=0.01,
        )
        return

    if target_status == PairStatus.ENTRY_EXCHANGE_FILLED:
        repo.mark_exchange_entry_filled(
            token, exchange_order_id="EX001",
            exchange_volume=overrides.get("exchange_volume", 1000),
            exchange_side=int(OrderSide.OPEN_SHORT) if direction == "LONG" else int(OrderSide.OPEN_LONG),
        )
        return

    if target_status == PairStatus.ENTRY_SENT:
        return

    if need_open:
        repo.transition(
            token, PairStatus.OPEN,
            fields={
                "mt5_ticket": overrides.get("mt5_ticket", 99999),
                "mt5_action": "BUY" if direction == "LONG" else "SELL",
                "mt5_volume": 0.01,
                "mt5_entry_price": 2950.0,
                "exchange_order_id": "EX001",
                "exchange_volume": overrides.get("exchange_volume", 1000),
                "exchange_side": int(OrderSide.OPEN_SHORT) if direction == "LONG" else int(OrderSide.OPEN_LONG),
            },
        )

    if target_status == PairStatus.OPEN:
        return

    if target_status in {PairStatus.CLOSE_SENT, PairStatus.CLOSE_MT5_DONE, PairStatus.CLOSE_EXCHANGE_DONE}:
        repo.transition(token, PairStatus.CLOSE_SENT, fields={"close_reason": CloseReason.SIGNAL.value})
        if target_status == PairStatus.CLOSE_MT5_DONE:
            repo.mark_mt5_close_done(token, mt5_close_price=2951.0, mt5_profit=0.50, mt5_close_latency_ms=80.0)
        elif target_status == PairStatus.CLOSE_EXCHANGE_DONE:
            repo.mark_exchange_close_done(token, exchange_close_order_id="EX_CL_001", exchange_close_latency_ms=60.0)
        return

    if target_status in {PairStatus.ORPHAN_MT5, PairStatus.ORPHAN_EXCHANGE}:
        repo.transition(token, target_status)
        return

    if target_status == PairStatus.HEALING:
        repo.transition(token, PairStatus.ORPHAN_MT5)
        repo.transition(token, PairStatus.HEALING)
        return


def _mt5_position(ticket=99999, type_="BUY", volume=0.01, comment="arb:TEST001", **kw) -> dict:
    pos = {"ticket": ticket, "type": type_, "volume": volume, "comment": comment, "price": 2950.0}
    pos.update(kw)
    return pos


def _exchange_position(side="SHORT", volume=1000, symbol="XAUT_USDT", **kw) -> dict:
    pos = {"side": side, "volume": volume, "volume_raw": volume, "symbol": symbol}
    pos.update(kw)
    return pos


def _run(coro):
    return asyncio.run(coro)


# ═══════════════════════════════════════════════════════════
# Group A: Matching Logic (pure)
# ═══════════════════════════════════════════════════════════


def _make_pair_for_matching(token, target_status=PairStatus.OPEN, **kw):
    db = Database("")
    db.initialize()
    repo = PairRepository(db)
    journal = JournalRepository(db)
    _insert_pair(repo, journal, token, target_status=target_status, **kw)
    return repo.get_by_token(token)


def test_mt5_match_by_ticket():
    recon, *_ = _make_reconciler()
    pair = _make_pair_for_matching("T001", mt5_ticket=12345)
    positions = [_mt5_position(ticket=12345), _mt5_position(ticket=99999)]
    result = recon._match_mt5_position(pair, positions)
    assert result is not None and result["ticket"] == 12345
    print("[OK] MT5 match by ticket")


def test_mt5_match_by_comment():
    recon, *_ = _make_reconciler()
    pair = _make_pair_for_matching("COMM01", target_status=PairStatus.ENTRY_SENT)
    assert pair.mt5_ticket is None
    positions = [_mt5_position(ticket=77777, comment="arb:COMM01", type_="BUY", volume=0.01)]
    result = recon._match_mt5_position(pair, positions)
    assert result is not None and result["ticket"] == 77777
    print("[OK] MT5 match by comment+type+volume")


def test_mt5_no_match():
    recon, *_ = _make_reconciler()
    pair = _make_pair_for_matching("NOPE01", mt5_ticket=12345)
    positions = [_mt5_position(ticket=99999, comment="arb:OTHER")]
    result = recon._match_mt5_position(pair, positions)
    assert result is None
    print("[OK] MT5 no match")


def test_exchange_exact_match():
    recon, *_ = _make_reconciler()
    pair = _make_pair_for_matching("EX_MATCH", exchange_volume=1000)
    positions = [_exchange_position(side="SHORT", volume=1000)]
    result = recon._match_exchange_position(pair, positions)
    assert result.kind == "exact"
    print("[OK] Exchange exact match")


def test_exchange_volume_mismatch():
    recon, *_ = _make_reconciler()
    pair = _make_pair_for_matching("EX_VOL", exchange_volume=1000)
    positions = [_exchange_position(side="SHORT", volume=500)]
    result = recon._match_exchange_position(pair, positions)
    assert result.kind == "volume_mismatch"
    print("[OK] Exchange volume mismatch")


def test_exchange_missing():
    recon, *_ = _make_reconciler()
    pair = _make_pair_for_matching("EX_MISS")
    result = recon._match_exchange_position(pair, [])
    assert result.kind == "missing"
    print("[OK] Exchange missing")


# ═══════════════════════════════════════════════════════════
# Group B: reconcile_once() full cycles
# ═══════════════════════════════════════════════════════════


def test_empty_db_no_positions():
    recon, redis, exchange, db, repo, journal = _make_reconciler(mt5_positions=[])
    exchange._positions = []
    summary = _run(recon.reconcile_once())
    assert summary["local_pairs"] == 0
    assert summary["mismatches"] == 0
    print("[OK] Empty DB, no positions -> 0 mismatches")


def test_open_pair_both_present():
    recon, redis, exchange, db, repo, journal = _make_reconciler(
        mt5_positions=[_mt5_position(ticket=12345)],
    )
    _insert_pair(repo, journal, "OP_OK", target_status=PairStatus.OPEN, mt5_ticket=12345)
    exchange._positions = [_exchange_position(side="SHORT", volume=1000)]

    summary = _run(recon.reconcile_once())
    assert summary["mismatches"] == 0
    pair = repo.get_by_token("OP_OK")
    assert pair.raw.get("recon_status") == ReconStatus.OK.value
    print("[OK] OPEN pair, both present -> RECON_OK")


def test_open_pair_mt5_present_exchange_missing():
    recon, redis, exchange, db, repo, journal = _make_reconciler(
        mt5_positions=[_mt5_position(ticket=12345)],
        mt5_close_success=True,
        mt5_after_close=[],  # after heal, MT5 is gone
    )
    _insert_pair(repo, journal, "OP_M1", target_status=PairStatus.OPEN, mt5_ticket=12345)
    exchange._positions = []

    summary = _run(recon.reconcile_once())
    assert summary["mismatches"] >= 1
    pair = repo.get_by_token("OP_M1")
    assert pair.status == PairStatus.HEALED
    print("[OK] OPEN, MT5 present, exchange missing -> ORPHAN_MT5 -> HEALED")


def test_open_pair_mt5_missing_exchange_present():
    recon, redis, exchange, db, repo, journal = _make_reconciler(
        mt5_positions=[],
    )
    _insert_pair(repo, journal, "OP_M2", target_status=PairStatus.OPEN, mt5_ticket=12345)
    exchange._positions = [_exchange_position(side="SHORT", volume=1000)]
    exchange._order_result = _filled_result()

    # After exchange close, position list shrinks
    call_count = [0]
    original = exchange.get_positions
    async def shrinking():
        call_count[0] += 1
        if call_count[0] <= 1:
            return [_exchange_position(side="SHORT", volume=1000)]
        return []
    exchange.get_positions = shrinking

    summary = _run(recon.reconcile_once())
    pair = repo.get_by_token("OP_M2")
    assert pair.status == PairStatus.HEALED
    print("[OK] OPEN, MT5 missing, exchange present -> ORPHAN_EXCHANGE -> HEALED")


def test_open_pair_both_missing():
    recon, redis, exchange, db, repo, journal = _make_reconciler(mt5_positions=[])
    _insert_pair(repo, journal, "OP_NONE", target_status=PairStatus.OPEN, mt5_ticket=12345)
    exchange._positions = []

    summary = _run(recon.reconcile_once())
    assert summary["mismatches"] == 1
    pair = repo.get_by_token("OP_NONE")
    assert pair.raw.get("recon_status") == ReconStatus.MISMATCH.value
    print("[OK] OPEN, both missing -> MISMATCH")


def test_orphan_mt5_already_gone():
    recon, redis, exchange, db, repo, journal = _make_reconciler(mt5_positions=[])
    _insert_pair(repo, journal, "ORP_MT5_GONE", target_status=PairStatus.ORPHAN_MT5, mt5_ticket=12345)
    exchange._positions = []

    summary = _run(recon.reconcile_once())
    pair = repo.get_by_token("ORP_MT5_GONE")
    assert pair.status == PairStatus.HEALED
    print("[OK] ORPHAN_MT5, MT5 gone -> HEALED")


def test_orphan_exchange_already_gone():
    recon, redis, exchange, db, repo, journal = _make_reconciler(mt5_positions=[])
    _insert_pair(repo, journal, "ORP_EX_GONE", target_status=PairStatus.ORPHAN_EXCHANGE, mt5_ticket=12345)
    exchange._positions = []

    summary = _run(recon.reconcile_once())
    pair = repo.get_by_token("ORP_EX_GONE")
    assert pair.status == PairStatus.HEALED
    print("[OK] ORPHAN_EXCHANGE, exchange gone -> HEALED")


def test_orphan_mt5_heal_success():
    recon, redis, exchange, db, repo, journal = _make_reconciler(
        mt5_positions=[_mt5_position(ticket=12345)],
        mt5_close_success=True,
        mt5_after_close=[],
    )
    _insert_pair(repo, journal, "ORP_MT5_HEAL", target_status=PairStatus.ORPHAN_MT5, mt5_ticket=12345)
    exchange._positions = []

    summary = _run(recon.reconcile_once())
    pair = repo.get_by_token("ORP_MT5_HEAL")
    assert pair.status == PairStatus.HEALED
    assert pair.raw.get("close_reason") == CloseReason.HEAL_ORPHAN_MT5.value
    print("[OK] ORPHAN_MT5, MT5 present, heal success -> HEALED")


def test_orphan_mt5_heal_failure():
    recon, redis, exchange, db, repo, journal = _make_reconciler(
        mt5_positions=[_mt5_position(ticket=12345)],
        mt5_close_success=False,
        mt5_after_close=[_mt5_position(ticket=12345)],  # still there
    )
    _insert_pair(repo, journal, "ORP_MT5_FAIL", target_status=PairStatus.ORPHAN_MT5, mt5_ticket=12345)
    exchange._positions = []

    summary = _run(recon.reconcile_once())
    pair = repo.get_by_token("ORP_MT5_FAIL")
    assert pair.status == PairStatus.ORPHAN_MT5
    print("[OK] ORPHAN_MT5, heal failure -> back to ORPHAN_MT5")


def test_stale_pending_no_positions():
    recon, redis, exchange, db, repo, journal = _make_reconciler(mt5_positions=[])
    _insert_pair(repo, journal, "STALE_PEND", target_status=PairStatus.PENDING,
                 entry_time=time.time() - 300)
    exchange._positions = []

    summary = _run(recon.reconcile_once())
    pair = repo.get_by_token("STALE_PEND")
    assert pair.status == PairStatus.FAILED
    print("[OK] Stale PENDING, no positions -> FAILED")


def test_stale_entry_sent_both_present():
    recon, redis, exchange, db, repo, journal = _make_reconciler(
        mt5_positions=[_mt5_position(ticket=77777, comment="arb:STALE_ES_OK")],
    )
    _insert_pair(repo, journal, "STALE_ES_OK", target_status=PairStatus.ENTRY_SENT,
                 entry_time=time.time() - 300)
    exchange._positions = [_exchange_position(side="SHORT", volume=1000)]

    summary = _run(recon.reconcile_once())
    pair = repo.get_by_token("STALE_ES_OK")
    assert pair.status == PairStatus.OPEN
    assert pair.mt5_ticket == 77777
    print("[OK] Stale ENTRY_SENT, both present -> OPEN (recovered)")


def test_stale_entry_sent_mt5_only():
    recon, redis, exchange, db, repo, journal = _make_reconciler(
        mt5_positions=[_mt5_position(ticket=77777, comment="arb:STALE_ES_MT5")],
    )
    _insert_pair(repo, journal, "STALE_ES_MT5", target_status=PairStatus.ENTRY_SENT,
                 entry_time=time.time() - 300)
    exchange._positions = []

    summary = _run(recon.reconcile_once())
    pair = repo.get_by_token("STALE_ES_MT5")
    assert pair.status == PairStatus.ORPHAN_MT5
    print("[OK] Stale ENTRY_SENT, MT5 only -> ORPHAN_MT5")


def test_stale_entry_sent_exchange_only():
    recon, redis, exchange, db, repo, journal = _make_reconciler(mt5_positions=[])
    _insert_pair(repo, journal, "STALE_ES_EX", target_status=PairStatus.ENTRY_SENT,
                 entry_time=time.time() - 300)
    exchange._positions = [_exchange_position(side="SHORT", volume=1000)]

    summary = _run(recon.reconcile_once())
    pair = repo.get_by_token("STALE_ES_EX")
    assert pair.status == PairStatus.ORPHAN_EXCHANGE
    print("[OK] Stale ENTRY_SENT, exchange only -> ORPHAN_EXCHANGE")


def test_stale_entry_sent_both_missing():
    recon, redis, exchange, db, repo, journal = _make_reconciler(mt5_positions=[])
    _insert_pair(repo, journal, "STALE_ES_NONE", target_status=PairStatus.ENTRY_SENT,
                 entry_time=time.time() - 300)
    exchange._positions = []

    summary = _run(recon.reconcile_once())
    pair = repo.get_by_token("STALE_ES_NONE")
    assert pair.status == PairStatus.FAILED
    print("[OK] Stale ENTRY_SENT, both missing -> FAILED")


def test_stale_close_sent_both_gone():
    recon, redis, exchange, db, repo, journal = _make_reconciler(mt5_positions=[])
    _insert_pair(repo, journal, "STALE_CS_OK", target_status=PairStatus.CLOSE_SENT, mt5_ticket=12345)
    exchange._positions = []

    summary = _run(recon.reconcile_once())
    pair = repo.get_by_token("STALE_CS_OK")
    assert pair.status == PairStatus.CLOSED
    print("[OK] Stale CLOSE_SENT, both gone -> CLOSED")


def test_stale_close_sent_mt5_still_open():
    recon, redis, exchange, db, repo, journal = _make_reconciler(
        mt5_positions=[_mt5_position(ticket=12345)],
    )
    _insert_pair(repo, journal, "STALE_CS_MT5", target_status=PairStatus.CLOSE_SENT, mt5_ticket=12345)
    exchange._positions = []

    summary = _run(recon.reconcile_once())
    pair = repo.get_by_token("STALE_CS_MT5")
    assert pair.status == PairStatus.ORPHAN_MT5
    print("[OK] Stale CLOSE_SENT, MT5 still open -> ORPHAN_MT5")


def test_stale_close_sent_exchange_still_open():
    recon, redis, exchange, db, repo, journal = _make_reconciler(mt5_positions=[])
    _insert_pair(repo, journal, "STALE_CS_EX", target_status=PairStatus.CLOSE_SENT, mt5_ticket=12345)
    exchange._positions = [_exchange_position(side="SHORT", volume=1000)]

    summary = _run(recon.reconcile_once())
    pair = repo.get_by_token("STALE_CS_EX")
    assert pair.status == PairStatus.ORPHAN_EXCHANGE
    print("[OK] Stale CLOSE_SENT, exchange still open -> ORPHAN_EXCHANGE")


def test_stale_close_mt5_done_exchange_gone():
    recon, redis, exchange, db, repo, journal = _make_reconciler(mt5_positions=[])
    _insert_pair(repo, journal, "STALE_CMD", target_status=PairStatus.CLOSE_MT5_DONE, mt5_ticket=12345)
    exchange._positions = []

    summary = _run(recon.reconcile_once())
    pair = repo.get_by_token("STALE_CMD")
    assert pair.status == PairStatus.CLOSED
    print("[OK] Stale CLOSE_MT5_DONE, exchange gone -> CLOSED")


def test_stale_close_exchange_done_mt5_gone():
    recon, redis, exchange, db, repo, journal = _make_reconciler(mt5_positions=[])
    _insert_pair(repo, journal, "STALE_CED", target_status=PairStatus.CLOSE_EXCHANGE_DONE, mt5_ticket=12345)
    exchange._positions = []

    summary = _run(recon.reconcile_once())
    pair = repo.get_by_token("STALE_CED")
    assert pair.status == PairStatus.CLOSED
    print("[OK] Stale CLOSE_EXCHANGE_DONE, MT5 gone -> CLOSED")


def test_healing_stuck_both_gone():
    recon, redis, exchange, db, repo, journal = _make_reconciler(mt5_positions=[])
    _insert_pair(repo, journal, "HEAL_STUCK", target_status=PairStatus.HEALING, mt5_ticket=12345)
    exchange._positions = []

    summary = _run(recon.reconcile_once())
    pair = repo.get_by_token("HEAL_STUCK")
    assert pair.status == PairStatus.HEALED
    print("[OK] HEALING stuck, both gone -> HEALED")


def test_healing_stuck_mt5_present():
    recon, redis, exchange, db, repo, journal = _make_reconciler(
        mt5_positions=[_mt5_position(ticket=12345)],
    )
    _insert_pair(repo, journal, "HEAL_MT5", target_status=PairStatus.HEALING, mt5_ticket=12345)
    exchange._positions = []

    summary = _run(recon.reconcile_once())
    pair = repo.get_by_token("HEAL_MT5")
    assert pair.status == PairStatus.ORPHAN_MT5
    print("[OK] HEALING stuck, MT5 present -> reset ORPHAN_MT5")


def test_ghost_mt5_alert():
    recon, redis, exchange, db, repo, journal = _make_reconciler(
        mt5_positions=[_mt5_position(ticket=55555, comment="arb:GHOST")],
    )
    exchange._positions = []

    summary = _run(recon.reconcile_once())
    assert summary["mismatches"] == 1
    print("[OK] Ghost MT5 (alert mode)")


def test_ghost_exchange_alert():
    recon, redis, exchange, db, repo, journal = _make_reconciler(mt5_positions=[])
    exchange._positions = [_exchange_position(side="SHORT", volume=1000)]

    summary = _run(recon.reconcile_once())
    assert summary["mismatches"] == 1
    print("[OK] Ghost exchange (alert mode)")


def test_ghost_mt5_non_arb_skipped():
    recon, redis, exchange, db, repo, journal = _make_reconciler(
        mt5_positions=[_mt5_position(ticket=55555, comment="manual_trade")],
    )
    exchange._positions = []

    summary = _run(recon.reconcile_once())
    assert summary["mismatches"] == 0
    print("[OK] Ghost MT5 (non-arb skipped)")


# ═══════════════════════════════════════════════════════════
# Group C: Utilities
# ═══════════════════════════════════════════════════════════


def test_expected_exchange_side():
    recon, *_ = _make_reconciler()
    pair_l = _make_pair_for_matching("SIDE_L")
    assert recon._expected_exchange_side(pair_l) == "SHORT"
    pair_s = _make_pair_for_matching("SIDE_S", direction="SHORT")
    assert recon._expected_exchange_side(pair_s) == "LONG"
    print("[OK] _expected_exchange_side: LONG->SHORT, SHORT->LONG")


def test_expected_mt5_type():
    recon, *_ = _make_reconciler()
    pair_l = _make_pair_for_matching("TYPE_L")
    assert recon._expected_mt5_type(pair_l) == "BUY"
    pair_s = _make_pair_for_matching("TYPE_S", direction="SHORT")
    assert recon._expected_mt5_type(pair_s) == "SELL"
    print("[OK] _expected_mt5_type: LONG->BUY, SHORT->SELL")


def test_filter_exchange_positions():
    recon, *_ = _make_reconciler()
    positions = [
        _exchange_position(side="SHORT", volume=1000, symbol="XAUT_USDT"),
        _exchange_position(side="LONG", volume=500, symbol="BTC_USDT"),
    ]
    filtered = recon._filter_exchange_positions(positions)
    assert len(filtered) == 1
    assert filtered[0]["side"] == "SHORT"
    print("[OK] _filter_exchange_positions filters by symbol")


def test_parse_utc_text():
    ts = Reconciler._parse_utc_text("2026-03-27T00:00:00.000Z")
    assert isinstance(ts, float) and ts > 0
    ts2 = Reconciler._parse_utc_text("2026-03-27T00:00:00.000+00:00")
    assert abs(ts - ts2) < 0.001
    print("[OK] _parse_utc_text")


def test_float_or_none():
    assert Reconciler._float_or_none(None) is None
    assert Reconciler._float_or_none(42) == 42.0
    assert Reconciler._float_or_none("3.14") == 3.14
    assert Reconciler._float_or_none("invalid") is None
    print("[OK] _float_or_none")


# ═══════════════════════════════════════════════════════════
# Runner
# ═══════════════════════════════════════════════════════════


ALL_TESTS = [
    # Group A: Matching
    test_mt5_match_by_ticket,
    test_mt5_match_by_comment,
    test_mt5_no_match,
    test_exchange_exact_match,
    test_exchange_volume_mismatch,
    test_exchange_missing,
    # Group B: reconcile_once()
    test_empty_db_no_positions,
    test_open_pair_both_present,
    test_open_pair_mt5_present_exchange_missing,
    test_open_pair_mt5_missing_exchange_present,
    test_open_pair_both_missing,
    test_orphan_mt5_already_gone,
    test_orphan_exchange_already_gone,
    test_orphan_mt5_heal_success,
    test_orphan_mt5_heal_failure,
    test_stale_pending_no_positions,
    test_stale_entry_sent_both_present,
    test_stale_entry_sent_mt5_only,
    test_stale_entry_sent_exchange_only,
    test_stale_entry_sent_both_missing,
    test_stale_close_sent_both_gone,
    test_stale_close_sent_mt5_still_open,
    test_stale_close_sent_exchange_still_open,
    test_stale_close_mt5_done_exchange_gone,
    test_stale_close_exchange_done_mt5_gone,
    test_healing_stuck_both_gone,
    test_healing_stuck_mt5_present,
    test_ghost_mt5_alert,
    test_ghost_exchange_alert,
    test_ghost_mt5_non_arb_skipped,
    # Group C: Utilities
    test_expected_exchange_side,
    test_expected_mt5_type,
    test_filter_exchange_positions,
    test_parse_utc_text,
    test_float_or_none,
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
    print(f"Phase 4 Test Results: {passed} passed, {failed} failed out of {len(ALL_TESTS)}")
    if errors:
        print("\nFailed tests:")
        for name, err in errors:
            print(f"  x {name}:\n{err}")
    else:
        print("\n=== test_phase4_reconciler (Phase 4): ALL PASSED ===")
    print(f"{'='*60}")

    if failed:
        sys.exit(1)
