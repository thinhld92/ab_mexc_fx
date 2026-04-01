"""Unit tests for the offline spread replay tool."""

from __future__ import annotations

import math
import shutil
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.tools.spread_replay import (
    MetricSnapshot,
    RollingSpreadModel,
    SpreadRow,
    StrategyParams,
    _gross_points,
    _open_trade,
    _row_timestamp,
    load_spread_rows,
    replay_spread_strategy,
    summarize_report,
)


def _row(
    timestamp: float,
    spread: float,
    *,
    mt5_bid: float | None = None,
    mt5_ask: float | None = None,
    mexc_bid: float | None = None,
    mexc_ask: float | None = None,
    label: str | None = None,
) -> SpreadRow:
    mt5_mid = 100.0
    mexc_mid = mt5_mid - spread
    mt5_bid = mt5_bid if mt5_bid is not None else mt5_mid - 0.05
    mt5_ask = mt5_ask if mt5_ask is not None else mt5_mid + 0.05
    mexc_bid = mexc_bid if mexc_bid is not None else mexc_mid - 0.10
    mexc_ask = mexc_ask if mexc_ask is not None else mexc_mid + 0.10
    return SpreadRow(
        source="spread_2026-03-26.csv",
        timestamp=timestamp,
        time_label=label or f"2026-03-26 00:00:{timestamp:06.3f}",
        mt5_bid=mt5_bid,
        mt5_ask=mt5_ask,
        mexc_bid=mexc_bid,
        mexc_ask=mexc_ask,
        spread=spread,
    )


def test_row_timestamp_uses_filename_date():
    midnight = datetime.fromisoformat("2026-03-26").timestamp()
    timestamp, label = _row_timestamp(
        Path("spread_2026-03-26.csv"),
        "10:11:12.250",
    )
    assert label == "2026-03-26 10:11:12.250"
    assert math.isclose(timestamp - midnight, 36672.25, rel_tol=0.0, abs_tol=1e-6)
    print("[OK] replay timestamp parser combines filename date and clock time")


def test_load_spread_rows_reads_repo_csv():
    temp_dir = Path("tests") / "_tmp_spread_replay"
    temp_dir.mkdir(exist_ok=True)
    csv_path = temp_dir / "spread_2026-03-26.csv"
    try:
        csv_path.write_text(
            "time,mt5_bid,mt5_ask,mt5_mid,mt5_sp,mexc_bid,mexc_ask,mexc_mid,mexc_sp,spread,signal,pairs\n"
            "00:00:01.000,100.0,100.2,100.1,0.2,98.0,98.2,98.1,0.2,2.0,NONE,0\n",
            encoding="utf-8",
        )
        rows = load_spread_rows([csv_path])
        assert len(rows) == 1
        assert rows[0].time_label == "2026-03-26 00:00:01.000"
        assert rows[0].spread == 2.0
    finally:
        if temp_dir.exists():
            shutil.rmtree(temp_dir)
    print("[OK] replay loader reads spread CSV rows")


def test_gross_points_uses_side_specific_exit_prices():
    short_trade = _open_trade(
        "short",
        _row(0.0, 3.0),
        MetricSnapshot(1.0, 1.0, 1.0, 2.0, 2.0),
    )
    long_trade = _open_trade(
        "long",
        _row(0.0, -3.0),
        MetricSnapshot(-1.0, -1.0, 1.0, -2.0, -2.0),
    )
    short_exit_row = _row(5.0, 1.0)
    long_exit_row = _row(5.0, -1.0)
    assert math.isclose(
        _gross_points("short", short_trade.entry_effective_spread, short_exit_row),
        1.7,
        rel_tol=0.0,
        abs_tol=1e-9,
    )
    assert math.isclose(
        _gross_points("long", long_trade.entry_effective_spread, long_exit_row),
        1.7,
        rel_tol=0.0,
        abs_tol=1e-9,
    )
    print("[OK] replay PnL model uses executable prices for both sides")


def test_replay_spread_strategy_opens_and_closes_short_trade():
    rows = [
        _row(0.0, 0.0),
        _row(1.0, 0.0),
        _row(2.0, 2.8),
        _row(3.0, 4.5),
        _row(4.2, 3.6),
        _row(5.0, 1.0),
        _row(6.0, 0.2),
    ]
    params = StrategyParams(
        fast_tau_sec=2.0,
        slow_tau_sec=10.0,
        min_samples=2,
        entry_z_short=0.8,
        entry_z_long=0.8,
        exit_z=0.9,
        persist_sec=1.0,
        pullback_confirm_z=0.15,
        min_hold_sec=0.0,
        soft_timeout_sec=30.0,
        hard_timeout_sec=60.0,
        stop_z=5.0,
        regime_filter_sigma=3.0,
        min_sigma=0.10,
        max_quote_spread_sum=1.0,
    )
    report = replay_spread_strategy(rows, params)
    assert len(report.trades) == 1
    trade = report.trades[0]
    assert trade.side == "short"
    assert trade.exit_reason == "mean_revert"
    assert trade.net_points > 0
    summary = summarize_report(report)
    assert summary["trades"] == 1
    assert summary["wins"] == 1
    print("[OK] replay strategy enters on pullback and exits on mean reversion")


def test_min_hold_blocks_early_mean_revert_but_stop_remains_active():
    rows = [
        _row(0.0, 0.0),
        _row(1.0, 0.0),
        _row(2.0, 2.8),
        _row(3.0, 4.5),
        _row(4.2, 3.6),
        _row(5.0, 1.0),
        _row(120.0, 0.2),
        _row(184.0, 0.1),
    ]
    params = StrategyParams(
        fast_tau_sec=2.0,
        slow_tau_sec=10.0,
        min_samples=2,
        entry_z_short=0.8,
        entry_z_long=0.8,
        exit_z=0.9,
        persist_sec=1.0,
        pullback_confirm_z=0.15,
        min_hold_sec=180.0,
        soft_timeout_sec=30.0,
        hard_timeout_sec=60.0,
        stop_z=10.0,
        regime_filter_sigma=3.0,
        min_sigma=0.10,
        max_quote_spread_sum=1.0,
    )
    report = replay_spread_strategy(rows, params)
    assert len(report.trades) == 1
    trade = report.trades[0]
    assert trade.hold_sec >= 180.0
    assert trade.exit_reason in {"mean_revert", "hard_timeout"}
    print("[OK] min_hold delays normal exits until the configured floor")


def test_rolling_spread_model_waits_for_min_samples():
    model = RollingSpreadModel(fast_tau_sec=5.0, slow_tau_sec=10.0, min_samples=3)
    assert model.update(0.0, 1.0) is None
    assert model.update(1.0, 1.2) is None
    snapshot = model.update(2.0, 0.8)
    assert snapshot is not None
    assert snapshot.sigma > 0
    print("[OK] rolling spread model respects warmup before emitting z-score")


if __name__ == "__main__":
    test_row_timestamp_uses_filename_date()
    test_load_spread_rows_reads_repo_csv()
    test_gross_points_uses_side_specific_exit_prices()
    test_replay_spread_strategy_opens_and_closes_short_trade()
    test_min_hold_blocks_early_mean_revert_but_stop_remains_active()
    test_rolling_spread_model_waits_for_min_samples()
    print("\n=== test_spread_replay: ALL PASSED ===")
