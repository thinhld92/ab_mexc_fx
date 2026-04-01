"""Unit tests for the Phase 5 spread monitor helper logic."""

from __future__ import annotations

import sys
from pathlib import Path

import orjson

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.tools.spread_monitor import _calculate_spread, _detect_raw_signal, _load_tick


def test_load_tick_decodes_json_object():
    payload = _load_tick(orjson.dumps({"bid": 10.0, "ask": 12.0}).decode("utf-8"))
    assert payload == {"bid": 10.0, "ask": 12.0}
    print("[OK] spread monitor decodes Redis tick payload")


def test_spread_calculation_uses_mid_prices():
    mt5_tick = {"bid": 100.0, "ask": 102.0}
    exchange_tick = {"bid": 98.0, "ask": 100.0}
    assert _calculate_spread(mt5_tick, exchange_tick) == 2.0
    print("[OK] spread monitor calculates spread from mids")


def test_raw_signal_detection():
    assert _detect_raw_signal(0.20, 0.15, 0.05) == "ENTRY_SHORT"
    assert _detect_raw_signal(-0.20, 0.15, 0.05) == "ENTRY_LONG"
    assert _detect_raw_signal(0.01, 0.15, 0.05) == "CLOSE_ZONE"
    assert _detect_raw_signal(0.08, 0.15, 0.05) == "NONE"
    print("[OK] spread monitor maps spread to raw signal zones")


if __name__ == "__main__":
    test_load_tick_decodes_json_object()
    test_spread_calculation_uses_mid_prices()
    test_raw_signal_detection()
    print("\n=== test_spread_monitor: ALL PASSED ===")
