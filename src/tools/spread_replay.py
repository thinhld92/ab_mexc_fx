"""Offline spread replay tool for rolling-mean strategy experiments."""

from __future__ import annotations

import argparse
import csv
import math
import statistics
import sys
from collections import Counter
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Sequence


@dataclass(frozen=True)
class SpreadRow:
    """One recorded spread snapshot."""

    source: str
    timestamp: float
    time_label: str
    mt5_bid: float
    mt5_ask: float
    mexc_bid: float
    mexc_ask: float
    spread: float

    @property
    def mt5_mid(self) -> float:
        return (self.mt5_bid + self.mt5_ask) / 2.0

    @property
    def mexc_mid(self) -> float:
        return (self.mexc_bid + self.mexc_ask) / 2.0

    @property
    def quote_spread_sum(self) -> float:
        return (self.mt5_ask - self.mt5_bid) + (self.mexc_ask - self.mexc_bid)


@dataclass(frozen=True)
class StrategyParams:
    fast_tau_sec: float = 300.0
    slow_tau_sec: float = 900.0
    min_samples: int = 120
    entry_z_short: float = 2.5
    entry_z_long: float = 2.5
    exit_z: float = 0.5
    persist_sec: float = 1.0
    pullback_confirm_z: float = 0.30
    min_hold_sec: float = 180.0
    soft_timeout_sec: float = 30.0
    hard_timeout_sec: float = 60.0
    stop_z: float = 4.0
    regime_filter_sigma: float = 1.2
    min_sigma: float = 0.35
    max_quote_spread_sum: float = 0.51
    fee_points: float = 0.0
    slippage_points: float = 0.0

    @property
    def extra_cost_points(self) -> float:
        return self.fee_points + self.slippage_points


@dataclass(frozen=True)
class MetricSnapshot:
    fast_mean: float
    slow_mean: float
    sigma: float
    residual: float
    zscore: float


@dataclass
class EntrySetup:
    side: str
    start_time: float
    extreme_z: float


@dataclass
class OpenTrade:
    side: str
    entry_time: float
    entry_label: str
    entry_spread: float
    entry_z: float
    entry_fast_mean: float
    entry_slow_mean: float
    entry_sigma: float
    entry_effective_spread: float
    source: str
    best_gross_points: float = 0.0
    worst_gross_points: float = 0.0


@dataclass(frozen=True)
class CompletedTrade:
    side: str
    source: str
    entry_label: str
    exit_label: str
    entry_spread: float
    exit_spread: float
    entry_z: float
    exit_z: float
    entry_fast_mean: float
    exit_fast_mean: float
    entry_slow_mean: float
    exit_slow_mean: float
    sigma_at_entry: float
    sigma_at_exit: float
    entry_effective_spread: float
    exit_effective_spread: float
    gross_points: float
    net_points: float
    hold_sec: float
    exit_reason: str
    mae_points: float
    mfe_points: float


@dataclass(frozen=True)
class ReplayReport:
    params: StrategyParams
    rows: int
    trades: list[CompletedTrade]


class TimeEWMStats:
    """Time-based exponentially weighted mean and variance."""

    def __init__(self, tau_sec: float):
        self.tau_sec = float(tau_sec)
        self.mean: float | None = None
        self.var = 0.0
        self.last_timestamp: float | None = None
        self.count = 0

    def update(self, timestamp: float, value: float) -> tuple[float, float]:
        if self.mean is None:
            self.mean = value
            self.last_timestamp = timestamp
            self.count = 1
            return self.mean, 0.0

        dt = max(0.001, timestamp - float(self.last_timestamp))
        alpha = 1.0 - math.exp(-dt / self.tau_sec)
        diff = value - self.mean
        self.mean = self.mean + alpha * diff
        self.var = (1.0 - alpha) * (self.var + alpha * diff * diff)
        self.last_timestamp = timestamp
        self.count += 1
        return self.mean, math.sqrt(max(self.var, 0.0))


class RollingSpreadModel:
    """Realtime-friendly rolling mean model for spread and volatility."""

    def __init__(self, fast_tau_sec: float, slow_tau_sec: float, min_samples: int):
        self._fast = TimeEWMStats(fast_tau_sec)
        self._slow = TimeEWMStats(slow_tau_sec)
        self._min_samples = int(min_samples)

    def update(self, timestamp: float, spread: float) -> MetricSnapshot | None:
        fast_mean, sigma = self._fast.update(timestamp, spread)
        slow_mean, _ = self._slow.update(timestamp, spread)
        if self._fast.count < self._min_samples or sigma <= 1e-9:
            return None
        residual = spread - fast_mean
        return MetricSnapshot(
            fast_mean=fast_mean,
            slow_mean=slow_mean,
            sigma=sigma,
            residual=residual,
            zscore=residual / sigma,
        )


def parse_clock_time(value: str) -> tuple[int, int, float]:
    """Parse ``HH:MM:SS.mmm``."""

    hours, minutes, seconds = value.split(":")
    return int(hours), int(minutes), float(seconds)


def _date_from_filename(path: Path) -> str:
    stem = path.stem
    if stem.startswith("spread_"):
        return stem[len("spread_") :]
    raise ValueError(f"Cannot infer date from filename: {path}")


def _row_timestamp(path: Path, time_value: str) -> tuple[float, str]:
    date_part = _date_from_filename(path)
    hours, minutes, seconds = parse_clock_time(time_value)
    whole_seconds = int(seconds)
    microseconds = int(round((seconds - whole_seconds) * 1_000_000))
    stamp = datetime.fromisoformat(date_part).replace(
        hour=hours,
        minute=minutes,
        second=whole_seconds,
        microsecond=microseconds,
    )
    return stamp.timestamp(), f"{date_part} {time_value}"


def load_spread_rows(paths: Sequence[Path]) -> list[SpreadRow]:
    """Load one or more spread CSV files."""

    rows: list[SpreadRow] = []
    for path in sorted(paths):
        with path.open(newline="", encoding="utf-8") as handle:
            reader = csv.DictReader(handle)
            for raw in reader:
                timestamp, label = _row_timestamp(path, raw["time"])
                rows.append(
                    SpreadRow(
                        source=path.name,
                        timestamp=timestamp,
                        time_label=label,
                        mt5_bid=float(raw["mt5_bid"]),
                        mt5_ask=float(raw["mt5_ask"]),
                        mexc_bid=float(raw["mexc_bid"]),
                        mexc_ask=float(raw["mexc_ask"]),
                        spread=float(raw["spread"]),
                    )
                )
    rows.sort(key=lambda item: item.timestamp)
    return rows


def resolve_input_paths(raw_paths: Sequence[str] | None) -> list[Path]:
    """Expand paths or directories into replayable CSV files."""

    if raw_paths:
        candidates = [Path(value) for value in raw_paths]
    else:
        candidates = [Path("data")]

    files: list[Path] = []
    for candidate in candidates:
        if candidate.is_dir():
            files.extend(sorted(candidate.glob("spread_*.csv")))
        elif candidate.is_file():
            files.append(candidate)
    if not files:
        raise FileNotFoundError("No spread CSV files found")
    return files


def _short_entry_effective(row: SpreadRow) -> float:
    return row.mt5_bid - row.mexc_ask


def _short_exit_effective(row: SpreadRow) -> float:
    return row.mt5_ask - row.mexc_bid


def _long_entry_effective(row: SpreadRow) -> float:
    return row.mt5_ask - row.mexc_bid


def _long_exit_effective(row: SpreadRow) -> float:
    return row.mt5_bid - row.mexc_ask


def _gross_points(side: str, entry_effective: float, row: SpreadRow) -> float:
    if side == "short":
        return entry_effective - _short_exit_effective(row)
    return _long_exit_effective(row) - entry_effective


def _entry_effective_spread(side: str, row: SpreadRow) -> float:
    if side == "short":
        return _short_entry_effective(row)
    return _long_entry_effective(row)


def _regime_is_tradeable(snapshot: MetricSnapshot, params: StrategyParams) -> bool:
    return abs(snapshot.fast_mean - snapshot.slow_mean) <= (
        params.regime_filter_sigma * snapshot.sigma
    )


def _entry_threshold(side: str, params: StrategyParams) -> float:
    return params.entry_z_short if side == "short" else params.entry_z_long


def _maybe_start_or_update_setup(
    setup: EntrySetup | None,
    side: str,
    row: SpreadRow,
    snapshot: MetricSnapshot,
) -> EntrySetup:
    if setup is None or setup.side != side:
        return EntrySetup(side=side, start_time=row.timestamp, extreme_z=snapshot.zscore)
    if side == "short":
        setup.extreme_z = max(setup.extreme_z, snapshot.zscore)
    else:
        setup.extreme_z = min(setup.extreme_z, snapshot.zscore)
    return setup


def _setup_ready(
    setup: EntrySetup | None,
    side: str,
    row: SpreadRow,
    snapshot: MetricSnapshot,
    params: StrategyParams,
) -> bool:
    if setup is None or setup.side != side:
        return False
    if (row.timestamp - setup.start_time) < params.persist_sec:
        return False
    threshold = _entry_threshold(side, params)
    if side == "short":
        if snapshot.zscore < threshold:
            return False
        return (setup.extreme_z - snapshot.zscore) >= params.pullback_confirm_z
    if snapshot.zscore > -threshold:
        return False
    return (snapshot.zscore - setup.extreme_z) >= params.pullback_confirm_z


def _open_trade(
    side: str,
    row: SpreadRow,
    snapshot: MetricSnapshot,
) -> OpenTrade:
    return OpenTrade(
        side=side,
        entry_time=row.timestamp,
        entry_label=row.time_label,
        entry_spread=row.spread,
        entry_z=snapshot.zscore,
        entry_fast_mean=snapshot.fast_mean,
        entry_slow_mean=snapshot.slow_mean,
        entry_sigma=snapshot.sigma,
        entry_effective_spread=_entry_effective_spread(side, row),
        source=row.source,
    )


def _exit_reason(
    trade: OpenTrade,
    row: SpreadRow,
    snapshot: MetricSnapshot,
    params: StrategyParams,
) -> str | None:
    hold_sec = row.timestamp - trade.entry_time
    if trade.side == "short" and snapshot.zscore >= params.stop_z:
        return "stop_z"
    if trade.side == "long" and snapshot.zscore <= -params.stop_z:
        return "stop_z"
    if hold_sec < params.min_hold_sec:
        return None
    if abs(snapshot.zscore) <= params.exit_z:
        return "mean_revert"
    if hold_sec >= max(params.hard_timeout_sec, params.min_hold_sec):
        return "hard_timeout"
    current_gross = _gross_points(trade.side, trade.entry_effective_spread, row)
    if hold_sec >= max(params.soft_timeout_sec, params.min_hold_sec) and current_gross > 0:
        return "soft_timeout"
    return None


def _close_trade(
    trade: OpenTrade,
    row: SpreadRow,
    snapshot: MetricSnapshot,
    params: StrategyParams,
    reason: str,
) -> CompletedTrade:
    if trade.side == "short":
        exit_effective = _short_exit_effective(row)
    else:
        exit_effective = _long_exit_effective(row)
    gross_points = _gross_points(trade.side, trade.entry_effective_spread, row)
    net_points = gross_points - params.extra_cost_points
    hold_sec = row.timestamp - trade.entry_time
    return CompletedTrade(
        side=trade.side,
        source=trade.source,
        entry_label=trade.entry_label,
        exit_label=row.time_label,
        entry_spread=trade.entry_spread,
        exit_spread=row.spread,
        entry_z=trade.entry_z,
        exit_z=snapshot.zscore,
        entry_fast_mean=trade.entry_fast_mean,
        exit_fast_mean=snapshot.fast_mean,
        entry_slow_mean=trade.entry_slow_mean,
        exit_slow_mean=snapshot.slow_mean,
        sigma_at_entry=trade.entry_sigma,
        sigma_at_exit=snapshot.sigma,
        entry_effective_spread=trade.entry_effective_spread,
        exit_effective_spread=exit_effective,
        gross_points=gross_points,
        net_points=net_points,
        hold_sec=hold_sec,
        exit_reason=reason,
        mae_points=trade.worst_gross_points,
        mfe_points=trade.best_gross_points,
    )


def replay_spread_strategy(
    rows: Sequence[SpreadRow],
    params: StrategyParams,
) -> ReplayReport:
    """Replay spread snapshots with a rolling-mean entry/exit model."""

    if not rows:
        return ReplayReport(params=params, rows=0, trades=[])

    model = RollingSpreadModel(
        fast_tau_sec=params.fast_tau_sec,
        slow_tau_sec=params.slow_tau_sec,
        min_samples=params.min_samples,
    )
    trades: list[CompletedTrade] = []
    setup: EntrySetup | None = None
    open_trade: OpenTrade | None = None
    last_snapshot: MetricSnapshot | None = None

    for row in rows:
        snapshot = model.update(row.timestamp, row.spread)
        if snapshot is None:
            continue
        last_snapshot = snapshot

        if open_trade is not None:
            current_gross = _gross_points(
                open_trade.side,
                open_trade.entry_effective_spread,
                row,
            )
            open_trade.best_gross_points = max(open_trade.best_gross_points, current_gross)
            open_trade.worst_gross_points = min(open_trade.worst_gross_points, current_gross)
            reason = _exit_reason(open_trade, row, snapshot, params)
            if reason is not None:
                trades.append(_close_trade(open_trade, row, snapshot, params, reason))
                open_trade = None
                setup = None
            continue

        if row.quote_spread_sum > params.max_quote_spread_sum:
            setup = None
            continue
        if not _regime_is_tradeable(snapshot, params):
            setup = None
            continue
        if snapshot.sigma < params.min_sigma:
            setup = None
            continue

        side: str | None = None
        if snapshot.zscore >= params.entry_z_short:
            side = "short"
        elif snapshot.zscore <= -params.entry_z_long:
            side = "long"

        if side is None:
            setup = None
            continue

        setup = _maybe_start_or_update_setup(setup, side, row, snapshot)
        if _setup_ready(setup, side, row, snapshot, params):
            open_trade = _open_trade(side, row, snapshot)
            setup = None

    if open_trade is not None:
        last_row = rows[-1]
        snapshot = last_snapshot or MetricSnapshot(
            fast_mean=open_trade.entry_fast_mean,
            slow_mean=open_trade.entry_slow_mean,
            sigma=open_trade.entry_sigma,
            residual=last_row.spread - open_trade.entry_fast_mean,
            zscore=open_trade.entry_z,
        )
        trades.append(_close_trade(open_trade, last_row, snapshot, params, "end_of_data"))

    return ReplayReport(params=params, rows=len(rows), trades=trades)


def summarize_report(report: ReplayReport) -> dict[str, float | int]:
    """Build aggregate metrics for a replay run."""

    trades = report.trades
    summary: dict[str, float | int] = {
        "rows": report.rows,
        "trades": len(trades),
        "wins": 0,
        "losses": 0,
        "gross_points": 0.0,
        "net_points": 0.0,
        "avg_hold_sec": 0.0,
        "median_hold_sec": 0.0,
        "win_rate": 0.0,
        "profit_factor": 0.0,
        "max_drawdown": 0.0,
    }
    if not trades:
        return summary

    net_points = [trade.net_points for trade in trades]
    wins = [value for value in net_points if value > 0]
    losses = [value for value in net_points if value <= 0]

    summary["wins"] = len(wins)
    summary["losses"] = len(losses)
    summary["gross_points"] = round(sum(trade.gross_points for trade in trades), 4)
    summary["net_points"] = round(sum(net_points), 4)
    summary["avg_hold_sec"] = round(statistics.mean(trade.hold_sec for trade in trades), 3)
    summary["median_hold_sec"] = round(
        statistics.median(trade.hold_sec for trade in trades), 3
    )
    summary["win_rate"] = round(len(wins) / len(trades), 4)
    total_loss = abs(sum(losses))
    summary["profit_factor"] = (
        round(sum(wins) / total_loss, 4) if total_loss > 0 else float("inf")
    )

    equity = 0.0
    peak = 0.0
    max_drawdown = 0.0
    for value in net_points:
        equity += value
        peak = max(peak, equity)
        max_drawdown = max(max_drawdown, peak - equity)
    summary["max_drawdown"] = round(max_drawdown, 4)
    return summary


def write_trades_csv(trades: Sequence[CompletedTrade], output_path: Path) -> None:
    """Persist trades for further analysis."""

    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=[
                "side",
                "source",
                "entry_label",
                "exit_label",
                "entry_spread",
                "exit_spread",
                "entry_z",
                "exit_z",
                "gross_points",
                "net_points",
                "hold_sec",
                "exit_reason",
                "mfe_points",
                "mae_points",
            ],
        )
        writer.writeheader()
        for trade in trades:
            writer.writerow(
                {
                    "side": trade.side,
                    "source": trade.source,
                    "entry_label": trade.entry_label,
                    "exit_label": trade.exit_label,
                    "entry_spread": f"{trade.entry_spread:.6f}",
                    "exit_spread": f"{trade.exit_spread:.6f}",
                    "entry_z": f"{trade.entry_z:.6f}",
                    "exit_z": f"{trade.exit_z:.6f}",
                    "gross_points": f"{trade.gross_points:.6f}",
                    "net_points": f"{trade.net_points:.6f}",
                    "hold_sec": f"{trade.hold_sec:.6f}",
                    "exit_reason": trade.exit_reason,
                    "mfe_points": f"{trade.mfe_points:.6f}",
                    "mae_points": f"{trade.mae_points:.6f}",
                }
            )


def print_report(report: ReplayReport, max_trades: int = 10) -> None:
    """Human-readable replay output."""

    summary = summarize_report(report)
    reason_counts = Counter(trade.exit_reason for trade in report.trades)
    side_counts = Counter(trade.side for trade in report.trades)

    print("Spread replay summary")
    print(f"rows={summary['rows']} trades={summary['trades']} win_rate={summary['win_rate']}")
    print(
        "gross_points={gross_points} net_points={net_points} profit_factor={profit_factor} "
        "max_drawdown={max_drawdown}".format(**summary)
    )
    print(
        "avg_hold_sec={avg_hold_sec} median_hold_sec={median_hold_sec}".format(
            **summary
        )
    )
    print(
        "params="
        f"fast={report.params.fast_tau_sec}s slow={report.params.slow_tau_sec}s "
        f"entry_short={report.params.entry_z_short} entry_long={report.params.entry_z_long} "
        f"exit={report.params.exit_z} persist={report.params.persist_sec}s "
        f"min_hold={report.params.min_hold_sec}s "
        f"pullback={report.params.pullback_confirm_z} stop_z={report.params.stop_z}"
    )
    if side_counts:
        print("side_counts=" + ", ".join(f"{key}:{value}" for key, value in sorted(side_counts.items())))
    if reason_counts:
        print(
            "exit_reasons="
            + ", ".join(f"{key}:{value}" for key, value in sorted(reason_counts.items()))
        )

    if not report.trades or max_trades <= 0:
        return

    print("\nSample trades")
    for trade in report.trades[:max_trades]:
        print(
            f"{trade.entry_label} -> {trade.exit_label} | {trade.side.upper()} "
            f"gross={trade.gross_points:+.4f} net={trade.net_points:+.4f} "
            f"hold={trade.hold_sec:.3f}s reason={trade.exit_reason} "
            f"entry_z={trade.entry_z:+.2f} exit_z={trade.exit_z:+.2f}"
        )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("paths", nargs="*", help="CSV files or directories to replay")
    parser.add_argument("--fast-ema-sec", type=float, default=300.0)
    parser.add_argument("--slow-ema-sec", type=float, default=900.0)
    parser.add_argument("--min-samples", type=int, default=120)
    parser.add_argument("--entry-z-short", type=float, default=2.5)
    parser.add_argument("--entry-z-long", type=float, default=2.5)
    parser.add_argument("--exit-z", type=float, default=0.5)
    parser.add_argument("--persist-sec", type=float, default=1.0)
    parser.add_argument("--pullback-z", type=float, default=0.30)
    parser.add_argument("--min-hold-sec", type=float, default=180.0)
    parser.add_argument("--soft-timeout-sec", type=float, default=30.0)
    parser.add_argument("--hard-timeout-sec", type=float, default=60.0)
    parser.add_argument("--stop-z", type=float, default=4.0)
    parser.add_argument("--regime-filter-sigma", type=float, default=1.2)
    parser.add_argument("--min-sigma", type=float, default=0.35)
    parser.add_argument("--max-quote-spread-sum", type=float, default=0.51)
    parser.add_argument("--fee-points", type=float, default=0.0)
    parser.add_argument("--slippage-points", type=float, default=0.0)
    parser.add_argument("--max-trades", type=int, default=10)
    parser.add_argument("--output-csv", type=Path)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    try:
        rows = load_spread_rows(resolve_input_paths(args.paths))
    except FileNotFoundError as exc:
        parser.error(str(exc))

    params = StrategyParams(
        fast_tau_sec=args.fast_ema_sec,
        slow_tau_sec=args.slow_ema_sec,
        min_samples=args.min_samples,
        entry_z_short=args.entry_z_short,
        entry_z_long=args.entry_z_long,
        exit_z=args.exit_z,
        persist_sec=args.persist_sec,
        pullback_confirm_z=args.pullback_z,
        min_hold_sec=args.min_hold_sec,
        soft_timeout_sec=args.soft_timeout_sec,
        hard_timeout_sec=args.hard_timeout_sec,
        stop_z=args.stop_z,
        regime_filter_sigma=args.regime_filter_sigma,
        min_sigma=args.min_sigma,
        max_quote_spread_sum=args.max_quote_spread_sum,
        fee_points=args.fee_points,
        slippage_points=args.slippage_points,
    )
    report = replay_spread_strategy(rows, params)
    print_report(report, max_trades=args.max_trades)
    if args.output_csv is not None:
        write_trades_csv(report.trades, args.output_csv)
        print(f"\nTrades written to {args.output_csv}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
