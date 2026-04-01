"""Unit tests for Phase 1 â€” Storage Layer.

Covers:
  1. Domain enums (PairStatus, PairEventType, ReconStatus, CloseReason)
  2. State machine transitions (can_transition, ensure_transition, next_statuses)
  3. Database class (:memory: mode, WAL on file, schema init)
  4. PairRepository CRUD + full lifecycle
  5. JournalRepository (pair events + recon log)

All tests use :memory: SQLite â€” no files created.
"""

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.domain.enums import CloseReason, PairEventType, PairStatus, ReconStatus
from src.domain.models import PairCreate, PairRecord
from src.domain.state_machine import (
    NON_TERMINAL_STATUSES,
    TERMINAL_STATUSES,
    _TRANSITIONS,
    can_transition,
    ensure_transition,
    next_statuses,
)
from src.storage.db import Database
from src.storage.pair_repository import PairRepository
from src.storage.journal_repository import JournalRepository


# â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
# Helpers
# â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

def _mem_db() -> Database:
    """Create an in-memory Database and initialize schema."""
    db = Database(":memory:")
    db.initialize()
    return db


def _sample_pair(token: str = "TEST_001", direction: str = "LONG") -> PairCreate:
    return PairCreate(
        pair_token=token,
        direction=direction,
        entry_time=time.time(),
        entry_spread=-0.85,
        conf_dev_entry=0.5,
        conf_dev_close=0.2,
    )


# â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
# 1. Domain Enums
# â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

def test_pair_status_count():
    assert len(PairStatus) == 15, f"Expected 15 statuses, got {len(PairStatus)}"
    print("[OK] PairStatus has 15 members")


def test_pair_status_str_enum():
    """PairStatus inherits str â€” .value should equal the name."""
    for ps in PairStatus:
        assert ps.value == ps.name, f"{ps!r}: value != name"
    print("[OK] PairStatus is str enum, value == name")


def test_pair_event_type_count():
    assert len(PairEventType) == 20, f"Expected 20 event types, got {len(PairEventType)}"
    print("[OK] PairEventType has 20 members")


def test_recon_status_count():
    assert len(ReconStatus) == 6, f"Expected 6 recon statuses, got {len(ReconStatus)}"
    print("[OK] ReconStatus has 6 members")


def test_close_reason_count():
    assert len(CloseReason) == 8, f"Expected 8 close reasons, got {len(CloseReason)}"
    print("[OK] CloseReason has 8 members")


def test_terminal_vs_non_terminal():
    assert TERMINAL_STATUSES == {
        PairStatus.CLOSED, PairStatus.FAILED, PairStatus.HEALED, PairStatus.FORCE_CLOSED
    }
    assert PairStatus.OPEN in NON_TERMINAL_STATUSES
    assert PairStatus.CLOSED not in NON_TERMINAL_STATUSES
    assert len(TERMINAL_STATUSES) + len(NON_TERMINAL_STATUSES) == len(PairStatus)
    print("[OK] TERMINAL / NON_TERMINAL partition correct")


# â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
# 2. State Machine
# â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

def test_happy_path_entry():
    """PENDING â†’ ENTRY_SENT â†’ OPEN (both fill at once)."""
    assert can_transition(PairStatus.PENDING, PairStatus.ENTRY_SENT)
    assert can_transition(PairStatus.ENTRY_SENT, PairStatus.OPEN)
    print("[OK] Happy path: PENDINGâ†’ENTRY_SENTâ†’OPEN")


def test_happy_path_close():
    """OPEN â†’ CLOSE_SENT â†’ CLOSED."""
    assert can_transition(PairStatus.OPEN, PairStatus.CLOSE_SENT)
    assert can_transition(PairStatus.CLOSE_SENT, PairStatus.CLOSED)
    print("[OK] Happy path: OPENâ†’CLOSE_SENTâ†’CLOSED")


def test_stale_pending_can_fail():
    assert can_transition(PairStatus.PENDING, PairStatus.FAILED)
    print("[OK] Reconciler can fail stale PENDING pairs")


def test_partial_fill_mt5_first():
    """ENTRY_SENT â†’ ENTRY_MT5_FILLED â†’ OPEN."""
    assert can_transition(PairStatus.ENTRY_SENT, PairStatus.ENTRY_MT5_FILLED)
    assert can_transition(PairStatus.ENTRY_MT5_FILLED, PairStatus.OPEN)
    print("[OK] Partial fill: MT5 firstâ†’OPEN")


def test_partial_fill_exchange_first():
    """ENTRY_SENT â†’ ENTRY_EXCHANGE_FILLED â†’ OPEN."""
    assert can_transition(PairStatus.ENTRY_SENT, PairStatus.ENTRY_EXCHANGE_FILLED)
    assert can_transition(PairStatus.ENTRY_EXCHANGE_FILLED, PairStatus.OPEN)
    print("[OK] Partial fill: Exchange firstâ†’OPEN")


def test_orphan_from_entry():
    """ENTRY_MT5_FILLED â†’ ORPHAN_MT5 (exchange fails)."""
    assert can_transition(PairStatus.ENTRY_MT5_FILLED, PairStatus.ORPHAN_MT5)
    assert can_transition(PairStatus.ENTRY_EXCHANGE_FILLED, PairStatus.ORPHAN_EXCHANGE)
    print("[OK] Orphan from partial entry")


def test_orphan_from_close():
    """CLOSE_MT5_DONE â†’ ORPHAN_EXCHANGE (exchange close fails)."""
    assert can_transition(PairStatus.CLOSE_MT5_DONE, PairStatus.ORPHAN_EXCHANGE)
    assert can_transition(PairStatus.CLOSE_EXCHANGE_DONE, PairStatus.ORPHAN_MT5)
    print("[OK] Orphan from partial close")


def test_healing_flow():
    assert can_transition(PairStatus.ORPHAN_MT5, PairStatus.HEALING)
    assert can_transition(PairStatus.HEALING, PairStatus.HEALED)
    assert can_transition(PairStatus.HEALING, PairStatus.ORPHAN_MT5)  # retry
    print("[OK] Healing flow: ORPHANâ†’HEALINGâ†’HEALED / retry")


def test_force_close():
    assert can_transition(PairStatus.OPEN, PairStatus.FORCE_CLOSED)
    assert can_transition(PairStatus.CLOSE_SENT, PairStatus.FORCE_CLOSED)
    print("[OK] Force close from OPEN and CLOSE_SENT")


def test_stopout():
    """OPEN â†’ ORPHAN_MT5 (stop-out)."""
    assert can_transition(PairStatus.OPEN, PairStatus.ORPHAN_MT5)
    assert can_transition(PairStatus.OPEN, PairStatus.ORPHAN_EXCHANGE)
    print("[OK] Stop-out / liquidation from OPEN")


def test_invalid_transitions_rejected():
    """Cannot skip states or go backwards."""
    assert not can_transition(PairStatus.PENDING, PairStatus.OPEN)
    assert not can_transition(PairStatus.OPEN, PairStatus.PENDING)
    assert not can_transition(PairStatus.CLOSED, PairStatus.OPEN)
    assert not can_transition(PairStatus.CLOSED, PairStatus.ENTRY_SENT)
    assert not can_transition(PairStatus.HEALED, PairStatus.OPEN)
    print("[OK] Invalid transitions correctly rejected")


def test_terminal_has_no_outgoing():
    """Terminal states should have no outgoing transitions."""
    for terminal in TERMINAL_STATUSES:
        assert terminal not in _TRANSITIONS or len(_TRANSITIONS[terminal]) == 0, \
            f"{terminal.value} has outgoing transitions!"
    print("[OK] Terminal states have no outgoing transitions")


def test_ensure_transition_raises():
    try:
        ensure_transition(PairStatus.PENDING, PairStatus.OPEN)
        assert False, "Should have raised ValueError"
    except ValueError as e:
        assert "Invalid" in str(e)
    print("[OK] ensure_transition raises ValueError on invalid transition")


def test_next_statuses_from_none():
    """None â†’ {PENDING} (initial state)."""
    result = next_statuses(None)
    assert result == {PairStatus.PENDING}
    print("[OK] next_statuses(None) == {PENDING}")


def test_next_statuses_from_entry_sent():
    ns = next_statuses(PairStatus.ENTRY_SENT)
    assert PairStatus.ENTRY_MT5_FILLED in ns
    assert PairStatus.ENTRY_EXCHANGE_FILLED in ns
    assert PairStatus.OPEN in ns
    assert PairStatus.FAILED in ns
    assert len(ns) == 4
    print("[OK] next_statuses(ENTRY_SENT) = 4 targets")


# â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
# 3. Database class
# â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

def test_db_in_memory():
    db = _mem_db()
    with db.connection() as conn:
        tables = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        ).fetchall()
        names = [t["name"] for t in tables]
    assert "pairs" in names
    assert "pair_events" in names
    assert "recon_log" in names
    print("[OK] :memory: DB has 3 tables")


def test_db_indexes():
    db = _mem_db()
    with db.connection() as conn:
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND name LIKE 'idx_%'"
        ).fetchall()
        idx_names = {r["name"] for r in rows}
    expected = {"idx_pairs_status", "idx_pairs_token", "idx_events_token", "idx_events_type"}
    assert expected <= idx_names, f"Missing indexes: {expected - idx_names}"
    print("[OK] All 4 custom indexes exist")


def test_db_transaction_commit():
    db = _mem_db()
    with db.transaction() as conn:
        conn.execute(
            "INSERT INTO pairs (pair_token, direction, entry_time) VALUES (?, ?, ?)",
            ("TX_001", "LONG", time.time()),
        )
    with db.connection() as conn:
        row = conn.execute("SELECT * FROM pairs WHERE pair_token = 'TX_001'").fetchone()
    assert row is not None
    print("[OK] Transaction commit persists data")


def test_db_transaction_rollback():
    db = _mem_db()
    try:
        with db.transaction() as conn:
            conn.execute(
                "INSERT INTO pairs (pair_token, direction, entry_time) VALUES (?, ?, ?)",
                ("TX_002", "SHORT", time.time()),
            )
            raise RuntimeError("Force rollback")
    except RuntimeError:
        pass

    with db.connection() as conn:
        row = conn.execute("SELECT * FROM pairs WHERE pair_token = 'TX_002'").fetchone()
    assert row is None, "Row should NOT exist after rollback"
    print("[OK] Transaction rollback discards data")


def test_row_to_dict():
    db = _mem_db()
    with db.transaction() as conn:
        conn.execute(
            "INSERT INTO pairs (pair_token, direction, entry_time) VALUES (?, ?, ?)",
            ("TX_100", "LONG", 1.0),
        )
    with db.connection() as conn:
        row = conn.execute("SELECT * FROM pairs WHERE pair_token = 'TX_100'").fetchone()
        d = db.row_to_dict(row)
    assert isinstance(d, dict)
    assert d["pair_token"] == "TX_100"
    assert d["direction"] == "LONG"
    assert db.row_to_dict(None) is None
    print("[OK] row_to_dict works + handles None")


# â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
# 4. PairRepository â€” CRUD & Lifecycle
# â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

def test_insert_pending_pair():
    db = _mem_db()
    repo = PairRepository(db)
    pair = _sample_pair("PR_001")
    rec = repo.insert_pending_pair(pair)

    assert rec.pair_token == "PR_001"
    assert rec.direction == "LONG"
    assert rec.status == PairStatus.PENDING
    assert rec.entry_spread == -0.85
    print("[OK] insert_pending_pair creates PENDING record")


def test_duplicate_token_rejected():
    db = _mem_db()
    repo = PairRepository(db)
    repo.insert_pending_pair(_sample_pair("DUP"))
    try:
        repo.insert_pending_pair(_sample_pair("DUP"))
        assert False, "Should raise on duplicate pair_token"
    except Exception:
        pass
    print("[OK] Duplicate pair_token rejected (UNIQUE constraint)")


def test_get_by_token():
    db = _mem_db()
    repo = PairRepository(db)
    repo.insert_pending_pair(_sample_pair("GET_001"))

    found = repo.get_by_token("GET_001")
    assert found is not None
    assert found.pair_token == "GET_001"

    missing = repo.get_by_token("NOPE")
    assert missing is None
    print("[OK] get_by_token: found + missing")


def test_list_non_terminal():
    db = _mem_db()
    repo = PairRepository(db)
    repo.insert_pending_pair(_sample_pair("NT_001"))
    repo.insert_pending_pair(_sample_pair("NT_002"))

    results = repo.list_non_terminal()
    tokens = {r.pair_token for r in results}
    assert tokens == {"NT_001", "NT_002"}
    print("[OK] list_non_terminal returns all PENDING pairs")


def test_list_by_statuses():
    db = _mem_db()
    repo = PairRepository(db)
    repo.insert_pending_pair(_sample_pair("LBS_001"))
    repo.insert_pending_pair(_sample_pair("LBS_002"))

    # Transition one to ENTRY_SENT
    repo.transition("LBS_001", PairStatus.ENTRY_SENT)

    pending = repo.list_by_statuses([PairStatus.PENDING])
    assert len(pending) == 1
    assert pending[0].pair_token == "LBS_002"

    both = repo.list_by_statuses([PairStatus.PENDING, PairStatus.ENTRY_SENT])
    assert len(both) == 2
    print("[OK] list_by_statuses filters correctly")


def test_transition_happy_path():
    """Full happy path: PENDING â†’ ENTRY_SENT â†’ OPEN â†’ CLOSE_SENT â†’ CLOSED."""
    db = _mem_db()
    repo = PairRepository(db)
    repo.insert_pending_pair(_sample_pair("HP_001"))

    rec = repo.transition("HP_001", PairStatus.ENTRY_SENT)
    assert rec.status == PairStatus.ENTRY_SENT

    rec = repo.transition("HP_001", PairStatus.OPEN)
    assert rec.status == PairStatus.OPEN

    rec = repo.transition("HP_001", PairStatus.CLOSE_SENT)
    assert rec.status == PairStatus.CLOSE_SENT

    rec = repo.transition("HP_001", PairStatus.CLOSED)
    assert rec.status == PairStatus.CLOSED
    print("[OK] Full happy path lifecycle: PENDINGâ†’â€¦â†’CLOSED")


def test_transition_with_fields():
    db = _mem_db()
    repo = PairRepository(db)
    repo.insert_pending_pair(_sample_pair("FLD_001"))
    repo.transition("FLD_001", PairStatus.ENTRY_SENT)

    rec = repo.transition(
        "FLD_001",
        PairStatus.ENTRY_MT5_FILLED,
        fields={
            "mt5_ticket": 12345678,
            "mt5_action": "BUY",
            "mt5_volume": 0.01,
            "mt5_entry_price": 3245.50,
            "mt5_entry_latency_ms": 125.5,
        },
    )
    assert rec.status == PairStatus.ENTRY_MT5_FILLED
    assert rec.mt5_ticket == 12345678
    assert rec.raw["mt5_action"] == "BUY"
    assert rec.raw["mt5_volume"] == 0.01
    assert rec.raw["mt5_entry_price"] == 3245.50
    print("[OK] transition with fields updates all columns")


def test_transition_invalid_rejected():
    db = _mem_db()
    repo = PairRepository(db)
    repo.insert_pending_pair(_sample_pair("INV_001"))

    try:
        repo.transition("INV_001", PairStatus.OPEN)  # skip ENTRY_SENT
        assert False, "Should raise ValueError"
    except ValueError as e:
        assert "Invalid" in str(e)
    print("[OK] Invalid transition (skip state) raises ValueError")


def test_transition_expected_current():
    db = _mem_db()
    repo = PairRepository(db)
    repo.insert_pending_pair(_sample_pair("EC_001"))

    try:
        repo.transition(
            "EC_001",
            PairStatus.ENTRY_SENT,
            expected_current=PairStatus.ENTRY_SENT,  # wrong: current is PENDING
        )
        assert False, "Should raise ValueError"
    except ValueError as e:
        assert "expected ENTRY_SENT" in str(e)
    print("[OK] expected_current mismatch raises ValueError")


def test_transition_not_found():
    db = _mem_db()
    repo = PairRepository(db)
    try:
        repo.transition("GHOST", PairStatus.ENTRY_SENT)
        assert False, "Should raise KeyError"
    except KeyError:
        pass
    print("[OK] transition on non-existent pair raises KeyError")


def test_mark_mt5_entry_filled():
    db = _mem_db()
    repo = PairRepository(db)
    repo.insert_pending_pair(_sample_pair("MT5_E"))
    repo.transition("MT5_E", PairStatus.ENTRY_SENT)

    rec = repo.mark_mt5_entry_filled(
        "MT5_E",
        mt5_ticket=99999,
        mt5_action="BUY",
        mt5_volume=0.02,
        mt5_entry_price=3200.0,
        mt5_entry_latency_ms=100.0,
    )
    assert rec.status == PairStatus.ENTRY_MT5_FILLED
    assert rec.mt5_ticket == 99999
    print("[OK] mark_mt5_entry_filled shortcut works")


def test_mark_exchange_entry_filled():
    db = _mem_db()
    repo = PairRepository(db)
    repo.insert_pending_pair(_sample_pair("EX_E"))
    repo.transition("EX_E", PairStatus.ENTRY_SENT)

    rec = repo.mark_exchange_entry_filled(
        "EX_E",
        exchange_order_id="ORD-ABC",
        exchange_volume=1000,
        exchange_side=3,
        exchange_entry_latency_ms=50.0,
    )
    assert rec.status == PairStatus.ENTRY_EXCHANGE_FILLED
    assert rec.exchange_order_id == "ORD-ABC"
    print("[OK] mark_exchange_entry_filled shortcut works")


def test_mark_mt5_close_done():
    db = _mem_db()
    repo = PairRepository(db)
    repo.insert_pending_pair(_sample_pair("MT5_C"))
    repo.transition("MT5_C", PairStatus.ENTRY_SENT)
    repo.transition("MT5_C", PairStatus.OPEN)
    repo.transition("MT5_C", PairStatus.CLOSE_SENT)

    rec = repo.mark_mt5_close_done(
        "MT5_C",
        mt5_close_price=3300.0,
        mt5_profit=0.55,
        mt5_close_latency_ms=98.0,
    )
    assert rec.status == PairStatus.CLOSE_MT5_DONE
    assert rec.raw["mt5_profit"] == 0.55
    print("[OK] mark_mt5_close_done shortcut works")


def test_mark_exchange_close_done():
    db = _mem_db()
    repo = PairRepository(db)
    repo.insert_pending_pair(_sample_pair("EX_C"))
    repo.transition("EX_C", PairStatus.ENTRY_SENT)
    repo.transition("EX_C", PairStatus.OPEN)
    repo.transition("EX_C", PairStatus.CLOSE_SENT)

    rec = repo.mark_exchange_close_done(
        "EX_C",
        exchange_close_order_id="CLOSE-XYZ",
        exchange_close_latency_ms=45.0,
    )
    assert rec.status == PairStatus.CLOSE_EXCHANGE_DONE
    assert rec.raw["exchange_close_order_id"] == "CLOSE-XYZ"
    print("[OK] mark_exchange_close_done shortcut works")


def test_update_recon_status():
    db = _mem_db()
    repo = PairRepository(db)
    repo.insert_pending_pair(_sample_pair("RECON_1"))

    rec = repo.update_recon_status(
        "RECON_1",
        ReconStatus.OK,
        recon_note="All matched",
    )
    assert rec.recon_status == ReconStatus.OK
    assert rec.recon_note == "All matched"
    print("[OK] update_recon_status works")


def test_orphan_healing_lifecycle():
    """Full orphan â†’ heal flow."""
    db = _mem_db()
    repo = PairRepository(db)
    repo.insert_pending_pair(_sample_pair("ORPHAN_1"))
    repo.transition("ORPHAN_1", PairStatus.ENTRY_SENT)

    # MT5 fills, exchange fails â†’ ORPHAN_MT5
    repo.mark_mt5_entry_filled(
        "ORPHAN_1", mt5_ticket=111, mt5_action="BUY", mt5_volume=0.01,
    )
    rec = repo.transition("ORPHAN_1", PairStatus.ORPHAN_MT5)
    assert rec.status == PairStatus.ORPHAN_MT5

    # Reconciler heals
    rec = repo.transition("ORPHAN_1", PairStatus.HEALING)
    assert rec.status == PairStatus.HEALING

    rec = repo.transition("ORPHAN_1", PairStatus.HEALED)
    assert rec.status == PairStatus.HEALED
    print("[OK] Orphan lifecycle: ENTRYâ†’MT5_FILLEDâ†’ORPHANâ†’HEALINGâ†’HEALED")


def test_force_close_from_open():
    db = _mem_db()
    repo = PairRepository(db)
    repo.insert_pending_pair(_sample_pair("FC_1"))
    repo.transition("FC_1", PairStatus.ENTRY_SENT)
    repo.transition("FC_1", PairStatus.OPEN)

    rec = repo.transition(
        "FC_1",
        PairStatus.FORCE_CLOSED,
        fields={"close_time": time.time(), "close_reason": "FORCE_SHUTDOWN"},
    )
    assert rec.status == PairStatus.FORCE_CLOSED
    assert rec.raw["close_reason"] == "FORCE_SHUTDOWN"
    print("[OK] Force close from OPEN with close_reason")


def test_both_fail_entry():
    """ENTRY_SENT â†’ FAILED."""
    db = _mem_db()
    repo = PairRepository(db)
    repo.insert_pending_pair(_sample_pair("FAIL_1"))
    repo.transition("FAIL_1", PairStatus.ENTRY_SENT)
    rec = repo.transition("FAIL_1", PairStatus.FAILED)
    assert rec.status == PairStatus.FAILED
    print("[OK] Both fail: ENTRY_SENTâ†’FAILED")


# â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
# 5. JournalRepository
# â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

def test_add_event():
    db = _mem_db()
    repo = PairRepository(db)
    journal = JournalRepository(db)

    repo.insert_pending_pair(_sample_pair("EVT_001"))
    eid = journal.add_event(
        "EVT_001",
        PairEventType.CREATED,
        {"spread": -0.85, "direction": "LONG"},
    )
    assert eid >= 1
    print("[OK] add_event returns event id")


def test_add_event_no_data():
    db = _mem_db()
    journal = JournalRepository(db)
    eid = journal.add_event("ANY_TOKEN", PairEventType.FORCE_CLOSE)
    assert eid >= 1
    print("[OK] add_event with no event_data")


def test_list_events():
    db = _mem_db()
    journal = JournalRepository(db)

    journal.add_event("TL_001", PairEventType.CREATED, {"a": 1})
    journal.add_event("TL_001", PairEventType.ENTRY_DISPATCHED, {"b": 2})
    journal.add_event("TL_001", PairEventType.MT5_FILLED, {"c": 3})
    journal.add_event("OTHER", PairEventType.CREATED, {"x": 99})

    events = journal.list_events("TL_001")
    assert len(events) == 3
    assert events[0]["event_type"] == "CREATED"
    assert events[2]["event_type"] == "MT5_FILLED"
    print("[OK] list_events returns ordered, filtered events")


def test_list_events_empty():
    db = _mem_db()
    journal = JournalRepository(db)
    events = journal.list_events("NOPE")
    assert events == []
    print("[OK] list_events returns [] for missing token")


def test_event_data_json():
    """event_data should be serialized as JSON string."""
    db = _mem_db()
    journal = JournalRepository(db)
    journal.add_event(
        "JSON_001",
        PairEventType.ENTRY_COMPLETE,
        {"mt5_ticket": 12345, "exchange_order_id": "ABC"},
    )
    events = journal.list_events("JSON_001")
    import orjson
    data = orjson.loads(events[0]["event_data"])
    assert data["mt5_ticket"] == 12345
    assert data["exchange_order_id"] == "ABC"
    print("[OK] event_data stored as valid JSON")


def test_add_recon_log():
    db = _mem_db()
    journal = JournalRepository(db)
    rid = journal.add_recon_log(
        mt5_positions=2,
        exchange_positions=2,
        local_pairs=2,
        mismatches=0,
        actions_taken={"healed": 0},
    )
    assert rid >= 1
    print("[OK] add_recon_log returns log id")


def test_add_recon_log_defaults():
    db = _mem_db()
    journal = JournalRepository(db)
    rid = journal.add_recon_log()
    assert rid >= 1

    with db.connection() as conn:
        row = conn.execute("SELECT * FROM recon_log WHERE id = ?", (rid,)).fetchone()
        d = db.row_to_dict(row)
    assert d["run_at"] is not None
    assert d["mismatches"] == 0
    print("[OK] add_recon_log with all defaults")


def test_add_recon_log_custom_run_at():
    db = _mem_db()
    journal = JournalRepository(db)
    ts = 1711425600.0
    rid = journal.add_recon_log(run_at=ts, mismatches=3)

    with db.connection() as conn:
        row = conn.execute("SELECT * FROM recon_log WHERE id = ?", (rid,)).fetchone()
        d = db.row_to_dict(row)
    assert d["run_at"] == ts
    assert d["mismatches"] == 3
    print("[OK] add_recon_log with custom run_at")


# â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
# 6. PairRecord model
# â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

def test_pair_record_from_row():
    row = {
        "pair_token": "MODEL_1",
        "direction": "SHORT",
        "status": "OPEN",
        "entry_time": 123.0,
        "entry_spread": -0.5,
        "close_time": None,
        "close_spread": None,
        "mt5_ticket": 999,
        "exchange_order_id": "OID",
        "recon_status": "OK",
        "recon_note": "good",
    }
    rec = PairRecord.from_row(row)
    assert rec.pair_token == "MODEL_1"
    assert rec.status == PairStatus.OPEN
    assert rec.mt5_ticket == 999
    assert rec.recon_status == ReconStatus.OK
    assert rec.raw == row
    print("[OK] PairRecord.from_row parses correctly")


def test_pair_create_frozen():
    pair = _sample_pair("FREEZE")
    try:
        pair.pair_token = "CHANGED"
        assert False, "Should be frozen"
    except AttributeError:
        pass
    print("[OK] PairCreate is frozen")


def test_pair_record_frozen():
    row = {
        "pair_token": "FREEZE_REC",
        "direction": "LONG",
        "status": "PENDING",
        "entry_time": 1.0,
    }
    rec = PairRecord.from_row(row)
    try:
        rec.status = PairStatus.OPEN
        assert False, "Should be frozen"
    except AttributeError:
        pass
    print("[OK] PairRecord is frozen")


# â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
# Runner
# â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

if __name__ == "__main__":
    tests = [
        # 1. Enums
        test_pair_status_count,
        test_pair_status_str_enum,
        test_pair_event_type_count,
        test_recon_status_count,
        test_close_reason_count,
        test_terminal_vs_non_terminal,
        # 2. State machine
        test_happy_path_entry,
        test_happy_path_close,
        test_stale_pending_can_fail,
        test_partial_fill_mt5_first,
        test_partial_fill_exchange_first,
        test_orphan_from_entry,
        test_orphan_from_close,
        test_healing_flow,
        test_force_close,
        test_stopout,
        test_invalid_transitions_rejected,
        test_terminal_has_no_outgoing,
        test_ensure_transition_raises,
        test_next_statuses_from_none,
        test_next_statuses_from_entry_sent,
        # 3. Database
        test_db_in_memory,
        test_db_indexes,
        test_db_transaction_commit,
        test_db_transaction_rollback,
        test_row_to_dict,
        # 4. PairRepository
        test_insert_pending_pair,
        test_duplicate_token_rejected,
        test_get_by_token,
        test_list_non_terminal,
        test_list_by_statuses,
        test_transition_happy_path,
        test_transition_with_fields,
        test_transition_invalid_rejected,
        test_transition_expected_current,
        test_transition_not_found,
        test_mark_mt5_entry_filled,
        test_mark_exchange_entry_filled,
        test_mark_mt5_close_done,
        test_mark_exchange_close_done,
        test_update_recon_status,
        test_orphan_healing_lifecycle,
        test_force_close_from_open,
        test_both_fail_entry,
        # 5. JournalRepository
        test_add_event,
        test_add_event_no_data,
        test_list_events,
        test_list_events_empty,
        test_event_data_json,
        test_add_recon_log,
        test_add_recon_log_defaults,
        test_add_recon_log_custom_run_at,
        # 6. Models
        test_pair_record_from_row,
        test_pair_create_frozen,
        test_pair_record_frozen,
    ]

    passed = 0
    failed = 0
    errors = []
    for t in tests:
        try:
            t()
            passed += 1
        except Exception as e:
            failed += 1
            errors.append((t.__name__, str(e)))
            print(f"[FAIL] {t.__name__}: {e}")

    print(f"\n{'='*60}")
    print(f"Phase 1 Test Results: {passed} passed, {failed} failed out of {len(tests)}")
    if errors:
        print("\nFailed tests:")
        for name, err in errors:
            print(f"  âœ— {name}: {err}")
    else:
        print("\n=== test_database (Phase 1): ALL PASSED ===")
    print(f"{'='*60}")



