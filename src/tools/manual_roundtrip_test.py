"""Manual live order round-trip tester for MT5 + exchange."""

from __future__ import annotations

import argparse
import asyncio
import sys
import threading
import time
import uuid

try:
    import MetaTrader5 as mt5
except ImportError:  # pragma: no cover - depends on local MT5 installation
    mt5 = None

from ..exchanges import create_exchange
from ..exchanges.base import ExchangeOrderStatus, OrderSide
from ..mt5_process.order_worker import OrderWorker
from ..utils.config import Config
from ..utils.logger import log


class _NoopRedis:
    """Placeholder for OrderWorker methods that do not need Redis."""


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Open and close one live test pair on MT5 + exchange using current config.",
    )
    parser.add_argument(
        "direction",
        choices=("long", "short"),
        help="Pair direction: long = MT5 BUY + exchange SHORT, short = MT5 SELL + exchange LONG.",
    )
    parser.add_argument(
        "--wait-sec",
        type=float,
        default=5.0,
        help="How long to keep the test pair open before sending close orders.",
    )
    parser.add_argument(
        "--skip-close",
        action="store_true",
        help="Open the test pair and leave it open for manual inspection.",
    )
    parser.add_argument(
        "--yes-live",
        action="store_true",
        help="Required safety flag to acknowledge that this script places real orders.",
    )
    parser.add_argument(
        "--config",
        default=None,
        help="Optional path to config.json. Defaults to project config.json.",
    )
    return parser


def _connect_mt5(config: Config, api_lock: threading.Lock) -> bool:
    if mt5 is None:
        log("ERROR", "MAN", "MetaTrader5 package is not installed")
        return False

    kwargs: dict = {}
    terminal_path = config.mt5.get("terminal_path", "")
    if terminal_path:
        kwargs["path"] = terminal_path

    login = config.mt5.get("login", 0)
    if login:
        kwargs["login"] = login
        kwargs["password"] = config.mt5.get("password", "")
        kwargs["server"] = config.mt5.get("server", "")

    with api_lock:
        if not mt5.initialize(**kwargs):
            log("ERROR", "MAN", f"mt5.initialize() failed: {mt5.last_error()}")
            return False

        if not mt5.symbol_select(config.mt5_symbol, True):
            log("ERROR", "MAN", "MT5 symbol not found", symbol=config.mt5_symbol)
            mt5.shutdown()
            return False

        info = mt5.account_info()

    if info is not None:
        log(
            "SUCCESS",
            "MAN",
            "Connected to MT5",
            broker=getattr(info, "company", "?"),
            account=getattr(info, "login", "?"),
            balance=f"{float(getattr(info, 'balance', 0.0)):.2f}",
        )
    return True


def _make_mt5_worker(config: Config, api_lock: threading.Lock) -> OrderWorker:
    return OrderWorker(
        mt5_api=mt5,
        api_lock=api_lock,
        redis_client=_NoopRedis(),
        symbol=config.mt5_symbol,
        shutdown_event=threading.Event(),
    )


def _mt5_open(worker: OrderWorker, *, direction: str, volume: float, comment: str) -> dict:
    action = "BUY" if direction == "long" else "SELL"
    command = {
        "action": action,
        "volume": volume,
        "comment": comment,
        "job_id": uuid.uuid4().hex[:8],
    }
    return worker._execute_command(command)


def _mt5_close(worker: OrderWorker, *, ticket: int, volume: float) -> dict:
    command = {
        "action": "CLOSE",
        "ticket": ticket,
        "volume": volume,
        "job_id": uuid.uuid4().hex[:8],
    }
    return worker._execute_command(command)


def _exchange_sides(direction: str) -> tuple[OrderSide, OrderSide]:
    if direction == "long":
        return OrderSide.OPEN_SHORT, OrderSide.CLOSE_SHORT
    return OrderSide.OPEN_LONG, OrderSide.CLOSE_LONG


def _log_mt5_result(title: str, result: dict) -> None:
    level = "SUCCESS" if result.get("success") else "ERROR"
    log(
        level,
        "MT5T",
        title,
        success=result.get("success"),
        ticket=result.get("ticket"),
        action=result.get("action"),
        price=result.get("price") or result.get("close_price"),
        volume=result.get("volume"),
        latency_ms=result.get("latency_ms"),
        error=result.get("error"),
    )


def _log_exchange_result(title: str, result) -> None:
    payload = result.to_dict()
    level = "SUCCESS" if result.success else "ERROR"
    log(
        level,
        "EXT",
        title,
        success=result.success,
        status=payload.get("order_status"),
        order_id=result.order_id,
        fill_price=result.fill_price,
        fill_volume=result.fill_volume,
        latency_ms=result.latency_ms,
        error=result.error,
        error_category=result.error_category.value if result.error_category else None,
    )


async def _run_roundtrip(args: argparse.Namespace) -> int:
    if not args.yes_live:
        print("Refusing to place real orders without --yes-live.")
        return 2

    config = Config(args.config)
    direction = args.direction.lower()
    api_lock = threading.Lock()

    log(
        "INFO",
        "MAN",
        "Manual round-trip test starting",
        product=config.selected_product,
        direction=direction,
        mt5_symbol=config.mt5_symbol,
        mt5_volume=config.mt5_volume,
        exchange=f"{config.exchange_name}:{config.mexc_symbol}",
        exchange_volume=config.mexc_volume,
        wait_sec=args.wait_sec,
        skip_close=args.skip_close,
    )

    if not _connect_mt5(config, api_lock):
        return 1

    worker = _make_mt5_worker(config, api_lock)
    shutdown_event = asyncio.Event()
    exchange = create_exchange(config, shutdown_event)

    mt5_open_result: dict | None = None
    exchange_open_result = None
    exchange_close_result = None
    mt5_close_result: dict | None = None

    open_side, close_side = _exchange_sides(direction)
    comment = f"manual:{direction}:{uuid.uuid4().hex[:8]}"

    try:
        await exchange.connect()
        if not await exchange.check_auth_alive():
            log("ERROR", "MAN", "Exchange auth check failed before test order")
            return 1

        mt5_open_result = _mt5_open(worker, direction=direction, volume=config.mt5_volume, comment=comment)
        _log_mt5_result("MT5 open test order", mt5_open_result)
        if not mt5_open_result.get("success"):
            return 1

        exchange_open_result = await exchange.place_order(open_side, config.mexc_volume)
        _log_exchange_result("Exchange open test order", exchange_open_result)
        if not exchange_open_result.success or exchange_open_result.order_status != ExchangeOrderStatus.FILLED:
            log("WARN", "MAN", "Exchange open failed; attempting MT5 rollback")
            rollback = _mt5_close(
                worker,
                ticket=int(mt5_open_result.get("ticket", 0)),
                volume=float(mt5_open_result.get("volume", config.mt5_volume) or config.mt5_volume),
            )
            _log_mt5_result("MT5 rollback close", rollback)
            return 1

        if args.skip_close:
            log("WARN", "MAN", "Skipping close as requested; positions remain open for manual inspection")
            return 0

        if args.wait_sec > 0:
            log("INFO", "MAN", "Waiting before close", wait_sec=args.wait_sec)
            await asyncio.sleep(args.wait_sec)

        exchange_close_result = await exchange.place_order(close_side, config.mexc_volume)
        _log_exchange_result("Exchange close test order", exchange_close_result)

        mt5_close_result = _mt5_close(
            worker,
            ticket=int(mt5_open_result.get("ticket", 0)),
            volume=float(mt5_open_result.get("volume", config.mt5_volume) or config.mt5_volume),
        )
        _log_mt5_result("MT5 close test order", mt5_close_result)

        exchange_ok = (
            exchange_close_result.success
            and exchange_close_result.order_status == ExchangeOrderStatus.FILLED
        )
        mt5_ok = bool(mt5_close_result.get("success"))

        if exchange_ok and mt5_ok:
            log("SUCCESS", "MAN", "Manual round-trip completed cleanly")
            return 0

        log(
            "WARN",
            "MAN",
            "Manual round-trip finished with partial close; manual check required",
            mt5_closed=mt5_ok,
            exchange_closed=exchange_ok,
        )
        return 1
    finally:
        try:
            await exchange.stop()
        except Exception as exc:
            log("ERROR", "MAN", f"Exchange stop failed: {exc}")
        if mt5 is not None:
            with api_lock:
                mt5.shutdown()


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    try:
        return asyncio.run(_run_roundtrip(args))
    except KeyboardInterrupt:
        log("WARN", "MAN", "Manual round-trip interrupted")
        return 130


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
