"""Unit tests for spread CSV logging."""

from __future__ import annotations

import csv
import shutil
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.utils.spread_logger import SpreadLogger


def _workspace_tempdir() -> Path:
    base = Path(__file__).resolve().parent.parent / ".tmp_tests"
    base.mkdir(exist_ok=True)
    path = base / f"spread_logger_{time.time_ns()}"
    path.mkdir()
    return path


def _sample_ticks() -> tuple[dict, dict]:
    mt5_tick = {"bid": 100.0, "ask": 100.2, "time_msc": 1111}
    exchange_tick = {"bid": 119.8, "ask": 120.0, "ts": 2222}
    return mt5_tick, exchange_tick


def test_spread_logger_writes_pivot_gap_and_source():
    tmpdir = _workspace_tempdir()
    try:
        logger = SpreadLogger(log_dir=tmpdir)
        mt5_tick, exchange_tick = _sample_ticks()
        logger.write(
            mt5_tick=mt5_tick,
            exchange_tick=exchange_tick,
            spread=-19.8000,
            pivot=-20.0000,
            gap=0.2000,
            pivot_source="warmup",
            signal_name="ENTRY_SHORT",
            pair_count=0,
        )
        logger.close()

        files = list(Path(tmpdir).glob("spread_*.csv"))
        assert len(files) == 1
        with files[0].open("r", newline="", encoding="utf-8") as handle:
            rows = list(csv.DictReader(handle))

        assert len(rows) == 1
        row = rows[0]
        assert row["spread"] == "-19.8000"
        assert row["pivot"] == "-20.0000"
        assert row["gap"] == "+0.2000"
        assert row["pivot_source"] == "warmup"
        assert row["signal"] == "ENTRY_SHORT"
        print("[OK] spread logger writes pivot/gap/pivot_source")
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def test_spread_logger_rotates_when_header_schema_changes():
    tmpdir = _workspace_tempdir()
    try:
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        legacy = Path(tmpdir) / f"spread_{today}.csv"
        legacy.write_text("time,spread,signal,pairs\n", encoding="utf-8")

        logger = SpreadLogger(log_dir=tmpdir)
        mt5_tick, exchange_tick = _sample_ticks()
        logger.write(
            mt5_tick=mt5_tick,
            exchange_tick=exchange_tick,
            spread=-19.7000,
            pivot=-20.0000,
            gap=0.3000,
            pivot_source="ema",
            signal_name="ENTRY_SHORT",
            pair_count=1,
        )
        logger.close()

        rotated = Path(tmpdir) / f"spread_{today}_v2.csv"
        assert rotated.exists() is True
        with rotated.open("r", newline="", encoding="utf-8") as handle:
            header = next(csv.reader(handle))

        assert header == SpreadLogger.FIELDNAMES
        print("[OK] spread logger rotates file on schema change")
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


if __name__ == "__main__":
    test_spread_logger_writes_pivot_gap_and_source()
    test_spread_logger_rotates_when_header_schema_changes()
    print("\n=== test_spread_logger: ALL PASSED ===")
