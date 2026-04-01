"""Main entry point for the dedicated MT5 process."""

from __future__ import annotations

import os
import sys
import threading
import time

import orjson

try:
    import MetaTrader5 as mt5
except ImportError:  # pragma: no cover - depends on local MT5 installation
    mt5 = None

from ..utils.config import Config
from ..utils.constants import REDIS_MT5_CMD_SHUTDOWN, REDIS_MT5_HEARTBEAT
from ..utils.logger import log
from ..utils.redis_client import get_redis
from .order_worker import OrderWorker
from .tick_worker import TickWorker


class MT5Process:
    """Owns the dedicated MT5 process and its worker threads."""

    HEARTBEAT_INTERVAL_SEC = 5
    HEARTBEAT_TTL_SEC = 15

    def __init__(self, config_path: str | None = None) -> None:
        self.config = Config(config_path)
        self.redis = get_redis(self.config.redis)
        self.shutdown_event = threading.Event()
        self.api_lock = threading.Lock()

        self._tick_worker: TickWorker | None = None
        self._order_worker: OrderWorker | None = None
        self._connected = False

    def run(self) -> int:
        """Run until a shutdown signal is received."""
        if mt5 is None:
            log("ERROR", "MT5P", "MetaTrader5 package is not installed")
            return 1

        try:
            self.redis.ping()
        except Exception as exc:
            log("ERROR", "MT5P", f"Redis unavailable: {exc}")
            return 1

        if not self._connect_mt5():
            return 1

        self._start_workers()
        log("SUCCESS", "MT5P", "MT5 process ready")

        exit_code = 0
        try:
            while not self.shutdown_event.is_set():
                if self._check_shutdown_signal():
                    break
                if not self._workers_alive():
                    log("ERROR", "MT5P", "A worker thread exited unexpectedly")
                    exit_code = 1
                    break

                self._connected = self._check_connection()
                if not self._connected:
                    log("WARN", "MT5P", "MT5 disconnected, attempting reconnect")
                    self._connected = self._connect_mt5()

                self._publish_heartbeat()
                time.sleep(self.HEARTBEAT_INTERVAL_SEC)
        except KeyboardInterrupt:
            log("WARN", "MT5P", "KeyboardInterrupt received")
        finally:
            self.shutdown_event.set()
            self._shutdown()

        return exit_code

    def _connect_mt5(self) -> bool:
        kwargs: dict = {}
        terminal_path = self.config.mt5.get("terminal_path", "")
        if terminal_path:
            kwargs["path"] = terminal_path

        login = self.config.mt5.get("login", 0)
        if login:
            kwargs["login"] = login
            kwargs["password"] = self.config.mt5.get("password", "")
            kwargs["server"] = self.config.mt5.get("server", "")

        with self.api_lock:
            if not mt5.initialize(**kwargs):
                log("ERROR", "MT5P", f"mt5.initialize() failed: {mt5.last_error()}")
                self._connected = False
                return False

            if not mt5.symbol_select(self.config.mt5_symbol, True):
                log("ERROR", "MT5P", f"Symbol not found: {self.config.mt5_symbol}")
                mt5.shutdown()
                self._connected = False
                return False

            info = mt5.account_info()

        if info is not None:
            log(
                "SUCCESS",
                "MT5P",
                "Connected to MT5",
                broker=getattr(info, "company", "?"),
                account=getattr(info, "login", "?"),
                balance=f"{float(getattr(info, 'balance', 0.0)):.2f}",
                leverage=f"1:{getattr(info, 'leverage', '?')}",
            )
        self._connected = True
        return True

    def _start_workers(self) -> None:
        if self._tick_worker is None:
            self._tick_worker = TickWorker(
                mt5_api=mt5,
                api_lock=self.api_lock,
                redis_client=self.redis,
                symbol=self.config.mt5_symbol,
                shutdown_event=self.shutdown_event,
                position_check_interval_ms=self.config.mt5_position_check_interval_ms,
            )
            self._tick_worker.start()

        if self._order_worker is None:
            self._order_worker = OrderWorker(
                mt5_api=mt5,
                api_lock=self.api_lock,
                redis_client=self.redis,
                symbol=self.config.mt5_symbol,
                shutdown_event=self.shutdown_event,
            )
            self._order_worker.start()

    def _workers_alive(self) -> bool:
        return bool(
            self._tick_worker
            and self._order_worker
            and self._tick_worker.is_alive()
            and self._order_worker.is_alive()
        )

    def _check_connection(self) -> bool:
        with self.api_lock:
            info = mt5.terminal_info()
        return bool(info and getattr(info, "connected", False))

    def _publish_heartbeat(self) -> None:
        heartbeat = {
            "ts": time.time(),
            "connected": self._connected,
            "tick_count": self._tick_worker.tick_count if self._tick_worker else 0,
            "order_count": self._order_worker.order_count if self._order_worker else 0,
            "pid": os.getpid(),
        }
        if mt5 is not None:
            try:
                with self.api_lock:
                    info = mt5.account_info()
                if info is not None:
                    heartbeat["equity"] = float(getattr(info, "equity", getattr(info, "balance", 0.0)))
            except Exception as exc:
                log("ERROR", "MT5P", f"Failed to read MT5 account info for heartbeat: {exc}")
        self.redis.set(
            REDIS_MT5_HEARTBEAT,
            orjson.dumps(heartbeat).decode("utf-8"),
            ex=self.HEARTBEAT_TTL_SEC,
        )

    def _check_shutdown_signal(self) -> bool:
        try:
            signal = self.redis.get(REDIS_MT5_CMD_SHUTDOWN)
        except Exception as exc:
            log("ERROR", "MT5P", f"Failed to read shutdown signal: {exc}")
            return False

        if signal == "1":
            log("WARN", "MT5P", "Shutdown signal received")
            self.shutdown_event.set()
            return True
        return False

    def _shutdown(self) -> None:
        log("WARN", "MT5P", "Stopping MT5 process")

        if self._tick_worker and self._tick_worker.is_alive():
            self._tick_worker.join(timeout=5)
        if self._order_worker and self._order_worker.is_alive():
            self._order_worker.join(timeout=10)

        if mt5 is not None:
            with self.api_lock:
                mt5.shutdown()

        log("INFO", "MT5P", "MT5 process stopped")


def main(config_path: str | None = None) -> int:
    """CLI entry point for `python -m src.mt5_process.main`."""
    process = MT5Process(config_path)
    return process.run()


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1] if len(sys.argv) > 1 else None))


