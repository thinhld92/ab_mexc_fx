"""Unit tests for the Phase 5 watchdog."""

from __future__ import annotations

import subprocess
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.utils.constants import (
    REDIS_BRAIN_HEARTBEAT,
    REDIS_CORE_CMD_SHUTDOWN,
    REDIS_MT5_CMD_SHUTDOWN,
    REDIS_MT5_HEARTBEAT,
    REDIS_RECON_CMD_SHUTDOWN,
    REDIS_RECON_HEARTBEAT,
)
from src.watchdog import Watchdog


class MockConfig:
    def __init__(self, **overrides):
        self.redis = {}
        self.vps_name = "TEST-VPS"
        self.config_path = "config.json"
        self.watchdog_check_interval_sec = overrides.get("watchdog_check_interval_sec", 1)
        self.watchdog_max_restarts = overrides.get("watchdog_max_restarts", 3)
        self.watchdog_restart_window_sec = overrides.get("watchdog_restart_window_sec", 300)
        self.shutdown_grace_period_sec = overrides.get("shutdown_grace_period_sec", 30)


class MockRedis:
    def __init__(self):
        self._data = {}
        self.set_calls = []

    def ping(self):
        return True

    def get(self, key):
        return self._data.get(key)

    def set(self, key, value, ex=None):
        self._data[key] = value
        self.set_calls.append((key, value, ex))

    def delete(self, *keys):
        deleted = 0
        for key in keys:
            if key in self._data:
                deleted += 1
                del self._data[key]
        return deleted


class FakeProcess:
    def __init__(self, pid: int, redis_client: MockRedis, shutdown_key: str):
        self.pid = pid
        self._redis = redis_client
        self._shutdown_key = shutdown_key
        self.returncode = None
        self.terminate_calls = 0
        self.kill_calls = 0

    def poll(self):
        return self.returncode

    def wait(self, timeout=None):
        if self.returncode is None and self._redis.get(self._shutdown_key) == "1":
            self.returncode = 0
        if self.returncode is None:
            raise subprocess.TimeoutExpired("fake", timeout)
        return self.returncode

    def terminate(self):
        self.terminate_calls += 1
        self.returncode = 0

    def kill(self):
        self.kill_calls += 1
        self.returncode = -9


class FakePopenFactory:
    HEARTBEATS = {
        "src.mt5_process.main": REDIS_MT5_HEARTBEAT,
        "src.core.engine": REDIS_BRAIN_HEARTBEAT,
        "src.reconciler.reconciler": REDIS_RECON_HEARTBEAT,
    }

    SHUTDOWN_KEYS = {
        "src.mt5_process.main": REDIS_MT5_CMD_SHUTDOWN,
        "src.core.engine": REDIS_CORE_CMD_SHUTDOWN,
        "src.reconciler.reconciler": REDIS_RECON_CMD_SHUTDOWN,
    }

    def __init__(self, redis_client: MockRedis):
        self.redis = redis_client
        self.calls = []
        self.processes = defaultdict(list)
        self._next_pid = 1000

    def __call__(self, command, cwd=None):
        module = command[2]
        process = FakeProcess(self._next_pid, self.redis, self.SHUTDOWN_KEYS[module])
        self._next_pid += 1
        self.calls.append((module, cwd))
        self.processes[module].append(process)
        self.redis.set(self.HEARTBEATS[module], f"ready:{process.pid}", ex=30)
        return process


def test_watchdog_startup_and_shutdown_order():
    redis = MockRedis()
    popen_factory = FakePopenFactory(redis)
    watchdog = Watchdog(
        config=MockConfig(),
        redis_client=redis,
        popen_factory=popen_factory,
        sleep_fn=lambda _: None,
    )

    watchdog._start_all_components()
    assert [module for module, _ in popen_factory.calls] == [
        "src.mt5_process.main",
        "src.core.engine",
        "src.reconciler.reconciler",
    ]

    watchdog.shutdown()
    shutdown_keys = [key for key, value, _ in redis.set_calls if value == "1"]
    assert shutdown_keys[-3:] == [
        REDIS_CORE_CMD_SHUTDOWN,
        REDIS_RECON_CMD_SHUTDOWN,
        REDIS_MT5_CMD_SHUTDOWN,
    ]
    print("[OK] Watchdog startup order and phased shutdown order")


def test_watchdog_restarts_component_when_heartbeat_disappears():
    redis = MockRedis()
    popen_factory = FakePopenFactory(redis)
    watchdog = Watchdog(
        config=MockConfig(),
        redis_client=redis,
        popen_factory=popen_factory,
        sleep_fn=lambda _: None,
    )

    watchdog._start_all_components()
    redis.delete(REDIS_BRAIN_HEARTBEAT)
    watchdog._monitor_iteration()

    assert len(popen_factory.processes["src.core.engine"]) == 2
    assert popen_factory.processes["src.core.engine"][0].returncode == 0
    print("[OK] Watchdog restarts Core when brain heartbeat disappears")


def test_watchdog_enforces_restart_limit():
    redis = MockRedis()
    popen_factory = FakePopenFactory(redis)
    watchdog = Watchdog(
        config=MockConfig(watchdog_max_restarts=1),
        redis_client=redis,
        popen_factory=popen_factory,
        sleep_fn=lambda _: None,
    )

    watchdog._start_all_components()
    redis.delete(REDIS_BRAIN_HEARTBEAT)
    watchdog._monitor_iteration()
    redis.delete(REDIS_BRAIN_HEARTBEAT)

    try:
        watchdog._monitor_iteration()
    except RuntimeError as exc:
        assert "Restart limit exceeded" in str(exc)
        print("[OK] Watchdog enforces restart limit")
        return

    raise AssertionError("Expected RuntimeError when restart limit is exceeded")


if __name__ == "__main__":
    test_watchdog_startup_and_shutdown_order()
    test_watchdog_restarts_component_when_heartbeat_disappears()
    test_watchdog_enforces_restart_limit()
    print("\n=== test_watchdog: ALL PASSED ===")
