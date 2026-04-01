"""Phase 3 core engine bootstrap."""

from __future__ import annotations

import asyncio
import contextlib
import sys
import time

from ..exchanges import create_exchange
from ..storage import Database, JournalRepository, PairRepository
from ..utils.config import Config
from ..utils.constants import REDIS_CORE_CMD_SHUTDOWN, REDIS_MT5_HEARTBEAT
from ..utils.logger import log
from ..utils.redis_client import get_redis
from .pair_manager import PairManager
from .trading_brain import TradingBrain


class CoreEngine:
    """Bootstraps Redis, SQLite, ExchangeHandler, and TradingBrain."""

    def __init__(self, config_path: str | None = None):
        self.config = Config(config_path)
        self.redis = get_redis(self.config.redis)
        self.shutdown_event = asyncio.Event()

        self.db = Database(self.config)
        self.repo = PairRepository(self.db)
        self.journal = JournalRepository(self.db)
        self.pair_manager = PairManager(self.redis, self.repo, self.journal)
        self.exchange = create_exchange(self.config, self.shutdown_event, self.redis)
        self.brain = TradingBrain(self.config, self.redis, self.exchange, self.pair_manager, self.shutdown_event)

        self._exchange_task: asyncio.Task | None = None
        self._brain_task: asyncio.Task | None = None
        self._shutdown_monitor_task: asyncio.Task | None = None
        self._shutdown_started = False

    async def start(self) -> None:
        log("INFO", "ENG", "=" * 50)
        log("INFO", "ENG", "  PHASE 3 CORE ENGINE STARTING")
        log("INFO", "ENG", f"  MT5: {self.config.mt5_symbol}")
        log("INFO", "ENG", f"  EXCHANGE: {self.config.exchange_name}:{self.config.mexc_symbol}")
        log("INFO", "ENG", "=" * 50)

        try:
            self.db.initialize()
            self.pair_manager.bootstrap()

            await self._wait_for_mt5_ready()
            await self.exchange.connect()

            auth_ok = await self.exchange.check_auth_alive()
            if not auth_ok:
                raise RuntimeError("Exchange auth check failed during startup")
            self.brain._record_auth_check(auth_ok, checked_at=time.time())

            self._exchange_task = asyncio.create_task(self.exchange.run_ws(), name="exchange-ws")
            self._brain_task = asyncio.create_task(self.brain.run(), name="trading-brain")
            self._shutdown_monitor_task = asyncio.create_task(
                self._shutdown_monitor_loop(),
                name="core-shutdown-monitor",
            )

            done, pending = await asyncio.wait(
                [self._exchange_task, self._brain_task],
                return_when=asyncio.FIRST_EXCEPTION,
            )

            for task in done:
                if task.cancelled():
                    continue
                exc = task.exception()
                if exc is not None:
                    raise exc

            if pending and not self.shutdown_event.is_set():
                await asyncio.gather(*pending)
        finally:
            await self.shutdown()

    async def shutdown(self) -> None:
        if self._shutdown_started:
            return
        self._shutdown_started = True
        self.shutdown_event.set()

        log("WARN", "ENG", "Shutting down core engine")

        if self._brain_task and not self._brain_task.done():
            await self.brain.request_shutdown()

        with contextlib.suppress(Exception):
            await self.exchange.stop()

        tasks = [task for task in (self._brain_task, self._exchange_task) if task is not None]
        if self._shutdown_monitor_task is not None:
            tasks.append(self._shutdown_monitor_task)
        for task in tasks:
            if not task.done():
                task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

        log("INFO", "ENG", "Core engine stopped")

    async def _wait_for_mt5_ready(self) -> None:
        deadline = asyncio.get_running_loop().time() + 30
        while asyncio.get_running_loop().time() < deadline:
            raw = self.redis.get(REDIS_MT5_HEARTBEAT)
            if raw:
                log("SUCCESS", "ENG", "MT5 heartbeat detected")
                return
            await asyncio.sleep(0.5)
        raise TimeoutError("Timed out waiting for mt5:heartbeat")

    async def _shutdown_monitor_loop(self) -> None:
        try:
            while not self.shutdown_event.is_set():
                signal = self.redis.get(REDIS_CORE_CMD_SHUTDOWN)
                if signal == "1":
                    log("WARN", "ENG", "Watchdog shutdown flag received")
                    self.shutdown_event.set()
                    await self.brain.request_shutdown()
                    return
                await asyncio.sleep(0.5)
        except asyncio.CancelledError:
            pass


def main(config_path: str | None = None) -> None:
    engine = CoreEngine(config_path)
    try:
        asyncio.run(engine.start())
    except KeyboardInterrupt:
        log("WARN", "ENG", "Ctrl+C received")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else None)
