"""Phase 5 watchdog parent process."""

from __future__ import annotations

import contextlib
import subprocess
import sys
import time
from collections import defaultdict, deque
from dataclasses import dataclass
from pathlib import Path

from ..utils.config import Config
from ..utils.constants import (
    REDIS_BRAIN_HEARTBEAT,
    REDIS_CORE_CMD_SHUTDOWN,
    REDIS_EXCHANGE_TICK,
    REDIS_MT5_CMD_SHUTDOWN,
    REDIS_MT5_HEARTBEAT,
    REDIS_MT5_ORDER_PROCESSING,
    REDIS_MT5_ORDER_QUEUE,
    REDIS_MT5_ORDER_RESULT,
    REDIS_MT5_POSITIONS_RESPONSE,
    REDIS_MT5_QUERY_POSITIONS,
    REDIS_MT5_TICK,
    REDIS_MT5_TRACKED_TICKETS,
    REDIS_RECON_CMD_SHUTDOWN,
    REDIS_RECON_HEARTBEAT,
    REDIS_SIGNAL_SHUTDOWN,
)
from ..utils.logger import log
from ..utils.redis_client import get_redis


@dataclass(frozen=True)
class ComponentSpec:
    name: str
    module: str
    heartbeat_key: str
    shutdown_key: str
    startup_timeout_sec: float
    shutdown_timeout_sec: float
    clear_on_start: tuple[str, ...]


class Watchdog:
    """Spawn and monitor the MT5, Core, and Reconciler child processes."""

    STARTUP_ORDER = ("mt5", "core", "recon")
    SHUTDOWN_ORDER = ("core", "recon", "mt5")

    COMPONENTS = {
        "mt5": ComponentSpec(
            name="MT5",
            module="src.mt5_process.main",
            heartbeat_key=REDIS_MT5_HEARTBEAT,
            shutdown_key=REDIS_MT5_CMD_SHUTDOWN,
            startup_timeout_sec=30.0,
            shutdown_timeout_sec=15.0,
            clear_on_start=(REDIS_MT5_TICK,),
        ),
        "core": ComponentSpec(
            name="CORE",
            module="src.core.engine",
            heartbeat_key=REDIS_BRAIN_HEARTBEAT,
            shutdown_key=REDIS_CORE_CMD_SHUTDOWN,
            startup_timeout_sec=30.0,
            shutdown_timeout_sec=30.0,
            clear_on_start=(REDIS_EXCHANGE_TICK,),
        ),
        "recon": ComponentSpec(
            name="RECON",
            module="src.reconciler.reconciler",
            heartbeat_key=REDIS_RECON_HEARTBEAT,
            shutdown_key=REDIS_RECON_CMD_SHUTDOWN,
            startup_timeout_sec=30.0,
            shutdown_timeout_sec=10.0,
            clear_on_start=(),
        ),
    }

    FULL_CLEANUP_KEYS = (
        REDIS_SIGNAL_SHUTDOWN,
        REDIS_MT5_CMD_SHUTDOWN,
        REDIS_CORE_CMD_SHUTDOWN,
        REDIS_RECON_CMD_SHUTDOWN,
        REDIS_MT5_HEARTBEAT,
        REDIS_BRAIN_HEARTBEAT,
        REDIS_RECON_HEARTBEAT,
        REDIS_MT5_TICK,
        REDIS_EXCHANGE_TICK,
        REDIS_MT5_ORDER_QUEUE,
        REDIS_MT5_ORDER_PROCESSING,
        REDIS_MT5_ORDER_RESULT,
        REDIS_MT5_QUERY_POSITIONS,
        REDIS_MT5_POSITIONS_RESPONSE,
        REDIS_MT5_TRACKED_TICKETS,
    )

    def __init__(
        self,
        config_path: str | None = None,
        *,
        config: Config | None = None,
        redis_client=None,
        popen_factory=None,
        sleep_fn=None,
    ) -> None:
        self.config = config or Config(config_path)
        self.redis = redis_client or get_redis(self.config.redis)
        self._popen_factory = popen_factory or subprocess.Popen
        self._sleep = sleep_fn or time.sleep
        self._project_root = Path(__file__).resolve().parents[2]

        self._processes: dict[str, subprocess.Popen | None] = {}
        self._restart_history: dict[str, deque[float]] = defaultdict(deque)
        self._shutdown_requested = False
        self._shutdown_started = False

    def run(self) -> int:
        exit_code = 0
        log("INFO", "DOG", "=" * 50)
        log("INFO", "DOG", "  PHASE 5 WATCHDOG STARTING")
        log("INFO", "DOG", f"  VPS: {self.config.vps_name}")
        log("INFO", "DOG", "=" * 50)

        try:
            self.redis.ping()
            if self._global_shutdown_requested():
                log("WARN", "DOG", "Global shutdown signal detected before startup")
                self._shutdown_requested = True
            else:
                self._cleanup_runtime_keys(include_shutdown_signal=False)
                self._start_all_components()

            while not self._shutdown_requested:
                if self._global_shutdown_requested():
                    log("WARN", "DOG", "Global shutdown signal detected")
                    self._shutdown_requested = True
                    break

                self._monitor_iteration()
                self._sleep(max(self.config.watchdog_check_interval_sec, 1))
        except KeyboardInterrupt:
            log("WARN", "DOG", "Ctrl+C received")
            self._shutdown_requested = True
        except Exception as exc:
            log("ERROR", "DOG", f"Watchdog failed: {exc}")
            exit_code = 1
        finally:
            self.shutdown()

        return exit_code

    def shutdown(self) -> None:
        if self._shutdown_started:
            return
        self._shutdown_started = True

        for name in self.SHUTDOWN_ORDER:
            self._stop_component(name, reason="watchdog_shutdown")

        self._cleanup_runtime_keys()
        log("INFO", "DOG", "Watchdog stopped")

    def _start_all_components(self) -> None:
        for name in self.STARTUP_ORDER:
            self._start_component(name)

    def _start_component(self, name: str) -> None:
        spec = self.COMPONENTS[name]
        self.redis.delete(spec.shutdown_key, spec.heartbeat_key, *spec.clear_on_start)

        command = [sys.executable, "-m", spec.module]
        if self.config.config_path:
            command.append(self.config.config_path)

        process = self._popen_factory(command, cwd=str(self._project_root))
        self._processes[name] = process
        log("INFO", "DOG", f"Spawned {spec.name}", pid=getattr(process, "pid", "?"))
        self._wait_for_heartbeat(name)

    def _wait_for_heartbeat(self, name: str) -> None:
        spec = self.COMPONENTS[name]
        process = self._processes.get(name)
        deadline = time.monotonic() + spec.startup_timeout_sec

        while time.monotonic() < deadline:
            if process is not None and process.poll() is not None:
                raise RuntimeError(f"{spec.name} exited before heartbeat")
            if self.redis.get(spec.heartbeat_key):
                log("SUCCESS", "DOG", f"{spec.name} ready")
                return
            self._sleep(0.2)

        raise TimeoutError(f"Timed out waiting for {spec.name} heartbeat")

    def _monitor_iteration(self) -> None:
        for name in self.STARTUP_ORDER:
            spec = self.COMPONENTS[name]
            process = self._processes.get(name)
            if process is None:
                continue

            returncode = process.poll()
            if returncode is not None:
                self._restart_component(name, f"process_exit:{returncode}")
                return

            if not self.redis.get(spec.heartbeat_key):
                self._restart_component(name, "heartbeat_missing")
                return

    def _restart_component(self, name: str, reason: str) -> None:
        self._record_restart(name)
        spec = self.COMPONENTS[name]
        log("WARN", "DOG", f"Restarting {spec.name}", reason=reason)
        self._stop_component(name, reason=f"restart:{reason}")
        self._start_component(name)

    def _record_restart(self, name: str) -> None:
        now = time.time()
        history = self._restart_history[name]
        window = max(self.config.watchdog_restart_window_sec, 1)
        while history and now - history[0] > window:
            history.popleft()
        history.append(now)

        if len(history) > max(self.config.watchdog_max_restarts, 0):
            raise RuntimeError(f"Restart limit exceeded for {self.COMPONENTS[name].name}")

    def _stop_component(self, name: str, *, reason: str) -> None:
        spec = self.COMPONENTS[name]
        process = self._processes.get(name)
        timeout_sec = (
            max(self.config.shutdown_grace_period_sec, 1)
            if name == "core"
            else spec.shutdown_timeout_sec
        )
        if process is None:
            self.redis.delete(spec.shutdown_key, spec.heartbeat_key)
            return

        if process.poll() is None:
            try:
                self.redis.set(spec.shutdown_key, "1", ex=int(timeout_sec) + 5)
            except Exception as exc:
                log("ERROR", "DOG", f"Failed to set shutdown flag for {spec.name}: {exc}")

            log("WARN", "DOG", f"Stopping {spec.name}", reason=reason)

            if not self._wait_process(process, timeout_sec):
                with contextlib.suppress(Exception):
                    process.terminate()
                if not self._wait_process(process, 2.0):
                    with contextlib.suppress(Exception):
                        process.kill()
                    self._wait_process(process, 2.0)

        self._processes[name] = None
        self.redis.delete(spec.shutdown_key, spec.heartbeat_key)

    @staticmethod
    def _wait_process(process, timeout_sec: float) -> bool:
        try:
            process.wait(timeout=max(timeout_sec, 0.1))
            return True
        except subprocess.TimeoutExpired:
            return False

    def _global_shutdown_requested(self) -> bool:
        return self.redis.get(REDIS_SIGNAL_SHUTDOWN) == "1"

    def _cleanup_runtime_keys(self, *, include_shutdown_signal: bool = True) -> None:
        keys = self.FULL_CLEANUP_KEYS
        if not include_shutdown_signal:
            keys = tuple(key for key in keys if key != REDIS_SIGNAL_SHUTDOWN)
        self.redis.delete(*keys)


def main(config_path: str | None = None) -> int:
    watchdog = Watchdog(config_path)
    return watchdog.run()


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1] if len(sys.argv) > 1 else None))

