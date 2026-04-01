"""CSV spread logger used by the Phase 3 trading brain."""

from __future__ import annotations

import csv
from datetime import datetime, timezone
from pathlib import Path

from .logger import log


class SpreadLogger:
    """Append spread snapshots to a daily CSV file."""

    FIELDNAMES = [
        "time",
        "mt5_bid",
        "mt5_ask",
        "mt5_mid",
        "mt5_sp",
        "exchange_bid",
        "exchange_ask",
        "exchange_mid",
        "exchange_sp",
        "spread",
        "pivot",
        "gap",
        "pivot_source",
        "signal",
        "pairs",
    ]

    def __init__(self, log_dir: str | Path | None = None) -> None:
        project_root = Path(__file__).resolve().parents[2]
        self._log_dir = Path(log_dir) if log_dir else project_root / "logs"
        self._log_dir.mkdir(parents=True, exist_ok=True)

        self._current_date: str | None = None
        self._csv_file = None
        self._writer = None
        self._row_count = 0
        self._last_mt5_ts = 0
        self._last_exchange_ts = 0

    def write(
        self,
        *,
        mt5_tick: dict,
        exchange_tick: dict,
        spread: float,
        pivot: float,
        gap: float,
        pivot_source: str,
        signal_name: str,
        pair_count: int,
    ) -> None:
        mt5_ts = int(mt5_tick.get("time_msc", 0))
        exchange_ts = int(exchange_tick.get("ts", 0))
        if mt5_ts == self._last_mt5_ts and exchange_ts == self._last_exchange_ts:
            return

        self._last_mt5_ts = mt5_ts
        self._last_exchange_ts = exchange_ts

        now_utc = datetime.now(timezone.utc)
        today = now_utc.strftime("%Y-%m-%d")
        if self._current_date != today:
            self._open_csv(today)

        mt5_bid = float(mt5_tick["bid"])
        mt5_ask = float(mt5_tick["ask"])
        exchange_bid = float(exchange_tick["bid"])
        exchange_ask = float(exchange_tick["ask"])

        row = {
            "time": now_utc.strftime("%H:%M:%S.%f")[:-3],
            "mt5_bid": f"{mt5_bid:.2f}",
            "mt5_ask": f"{mt5_ask:.2f}",
            "mt5_mid": f"{((mt5_bid + mt5_ask) / 2):.2f}",
            "mt5_sp": f"{(mt5_ask - mt5_bid):.2f}",
            "exchange_bid": f"{exchange_bid:.2f}",
            "exchange_ask": f"{exchange_ask:.2f}",
            "exchange_mid": f"{((exchange_bid + exchange_ask) / 2):.2f}",
            "exchange_sp": f"{(exchange_ask - exchange_bid):.2f}",
            "spread": f"{spread:+.4f}",
            "pivot": f"{pivot:+.4f}",
            "gap": f"{gap:+.4f}",
            "pivot_source": str(pivot_source),
            "signal": signal_name,
            "pairs": pair_count,
        }
        self._writer.writerow(row)
        self._row_count += 1
        if self._row_count % 100 == 0:
            self._csv_file.flush()

    def close(self) -> None:
        if self._csv_file and not self._csv_file.closed:
            self._csv_file.flush()
            self._csv_file.close()

    def _open_csv(self, date_str: str) -> None:
        self.close()

        filepath = self._resolve_filepath(date_str)
        file_exists = filepath.exists()
        self._csv_file = filepath.open("a", newline="", encoding="utf-8")
        self._writer = csv.DictWriter(
            self._csv_file,
            fieldnames=self.FIELDNAMES,
        )
        if not file_exists:
            self._writer.writeheader()

        self._current_date = date_str
        log("INFO", "BRAIN", f"Spread log: {filepath}")

    def _resolve_filepath(self, date_str: str) -> Path:
        primary = self._log_dir / f"spread_{date_str}.csv"
        if not primary.exists():
            return primary

        try:
            with primary.open("r", newline="", encoding="utf-8") as existing:
                header = next(csv.reader(existing), [])
        except Exception:
            header = []

        if header == self.FIELDNAMES:
            return primary
        return self._log_dir / f"spread_{date_str}_v2.csv"
