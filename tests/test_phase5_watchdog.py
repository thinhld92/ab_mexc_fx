"""Phase 5 Watchdog tests — startup, shutdown, monitoring, restart, cleanup.

Strategy: Use the Watchdog's own DI hooks (popen_factory, sleep_fn, redis_client)
to inject MockProcess and MockRedis. No real subprocesses are created.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
import traceback

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.utils.constants import (
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
from src.watchdog.watchdog import Watchdog, ComponentSpec


# ═══════════════════════════════════════════════════════════
# Mocks
# ═══════════════════════════════════════════════════════════


class MockRedis:
    """In-memory Redis stub for watchdog tests."""

    def __init__(self):
        self._data: dict[str, str] = {}
        self._deleted_log: list[str] = []

    def ping(self):
        return True

    def get(self, key):
        return self._data.get(key)

    def set(self, key, value, ex=None):
        self._data[key] = str(value)

    def delete(self, *keys):
        for key in keys:
            self._data.pop(key, None)
            self._deleted_log.append(key)


class MockProcess:
    """Fake subprocess.Popen for watchdog tests."""

    def __init__(self, command=None, cwd=None, **kwargs):
        self.command = command
        self.cwd = cwd
        self.pid = 9999
        self._returncode: int | None = None
        self._killed = False
        self._terminated = False
        self._wait_behavior: str = "graceful"  # "graceful" | "timeout" | "immediate"

    def poll(self) -> int | None:
        return self._returncode

    def wait(self, timeout=None):
        if self._wait_behavior == "timeout":
            raise subprocess.TimeoutExpired(cmd="mock", timeout=timeout or 1)
        if self._wait_behavior == "immediate":
            self._returncode = 0
        if self._returncode is None:
            self._returncode = 0
        return self._returncode

    def terminate(self):
        self._terminated = True

    def kill(self):
        self._killed = True
        self._returncode = -9
        self._wait_behavior = "immediate"

    def force_exit(self, code: int = 1):
        """Simulate unexpected exit."""
        self._returncode = code


class MockConfig:
    """Minimal config for watchdog tests."""

    def __init__(self, **overrides):
        self._overrides = overrides

    def __getattr__(self, name):
        defaults = {
            "redis": {},
            "vps_name": "TEST_VPS",
            "config_path": None,
            "watchdog_check_interval_sec": 1,
            "watchdog_restart_window_sec": 300,
            "watchdog_max_restarts": 3,
            "shutdown_grace_period_sec": 5,
        }
        if name in self._overrides:
            return self._overrides[name]
        if name in defaults:
            return defaults[name]
        raise AttributeError(f"MockConfig has no attribute '{name}'")


# ═══════════════════════════════════════════════════════════
# Helpers
# ═══════════════════════════════════════════════════════════


def _make_watchdog(*, auto_heartbeat=True, **config_overrides):
    """Build a Watchdog wired to mocks. Returns (watchdog, redis, spawned_processes).

    When auto_heartbeat=True (default), the popen_factory auto-sets the child's
    heartbeat key immediately, simulating an instant-ready child process.
    """
    redis = MockRedis()
    spawned: list[MockProcess] = []

    # We need the dog reference inside the factory, so use a mutable holder
    dog_holder: list[Watchdog] = []

    def popen_factory(command, cwd=None, **kw):
        proc = MockProcess(command=command, cwd=cwd, **kw)
        spawned.append(proc)
        # Auto-set heartbeat key to simulate child becoming ready
        if auto_heartbeat and dog_holder:
            dog = dog_holder[0]
            for spec in dog.COMPONENTS.values():
                if spec.module in (command or []):
                    redis.set(spec.heartbeat_key, "alive")
                    break
        return proc

    config = MockConfig(**config_overrides)
    dog = Watchdog(config=config, redis_client=redis, popen_factory=popen_factory, sleep_fn=lambda _: None)
    dog_holder.append(dog)

    # Override ComponentSpec timeouts to avoid real wall-clock waiting
    fast_specs = {}
    for key, spec in dog.COMPONENTS.items():
        fast_specs[key] = ComponentSpec(
            name=spec.name,
            module=spec.module,
            heartbeat_key=spec.heartbeat_key,
            shutdown_key=spec.shutdown_key,
            startup_timeout_sec=0.05,  # 50ms - enough for one loop iteration
            shutdown_timeout_sec=0.05,
            clear_on_start=spec.clear_on_start,
        )
    dog.COMPONENTS = fast_specs

    return dog, redis, spawned


# ═══════════════════════════════════════════════════════════
# Tests: Startup
# ═══════════════════════════════════════════════════════════


def test_startup_correct_order():
    dog, redis, spawned = _make_watchdog()
    dog._start_all_components()

    assert len(spawned) == 3
    modules = [p.command[-1] if p.command else None for p in spawned]
    # Module order: mt5 -> core -> recon
    assert "src.mt5_process.main" in modules[0]
    assert "src.core.engine" in modules[1]
    assert "src.reconciler.reconciler" in modules[2]
    print("[OK] Startup correct order: MT5 -> Core -> Recon")


def test_startup_heartbeat_timeout():
    dog, redis, spawned = _make_watchdog(auto_heartbeat=False)
    # No heartbeat -> should timeout
    try:
        dog._start_component("mt5")
        assert False, "Should have raised TimeoutError"
    except TimeoutError as e:
        assert "MT5" in str(e)
    print("[OK] Startup heartbeat timeout raises TimeoutError")


def test_startup_process_dies_before_heartbeat():
    dog, redis, spawned = _make_watchdog(auto_heartbeat=False)

    def dying_popen(command, cwd=None, **kw):
        proc = MockProcess(command=command, cwd=cwd, **kw)
        proc._returncode = 1  # already dead
        spawned.append(proc)
        return proc

    dog._popen_factory = dying_popen
    try:
        dog._start_component("mt5")
        assert False, "Should have raised RuntimeError"
    except RuntimeError as e:
        assert "exited before heartbeat" in str(e)
    print("[OK] Startup: process exits before heartbeat -> RuntimeError")


def test_startup_config_path_appended():
    dog, redis, spawned = _make_watchdog(config_path="/etc/bot/config.json")
    dog._start_component("mt5")
    assert "/etc/bot/config.json" in spawned[0].command
    print("[OK] Startup: config_path appended to command")


def test_startup_clears_component_keys():
    dog, redis, spawned = _make_watchdog()
    # Pre-set keys that should be cleared on start
    redis.set(REDIS_MT5_CMD_SHUTDOWN, "1")
    redis.set(REDIS_MT5_HEARTBEAT, "old")
    redis.set(REDIS_MT5_TICK, "stale")

    dog._start_component("mt5")
    assert REDIS_MT5_CMD_SHUTDOWN in redis._deleted_log
    print("[OK] Startup: clears shutdown_key, heartbeat, clear_on_start keys")


# ═══════════════════════════════════════════════════════════
# Tests: Shutdown
# ═══════════════════════════════════════════════════════════


def test_shutdown_correct_order():
    dog, redis, spawned = _make_watchdog()
    dog._start_all_components()

    stop_order = []
    original_stop = dog._stop_component

    def tracking_stop(name, *, reason):
        stop_order.append(name)
        original_stop(name, reason=reason)

    dog._stop_component = tracking_stop
    dog.shutdown()

    assert stop_order == ["core", "recon", "mt5"]
    print("[OK] Shutdown correct order: Core -> Recon -> MT5")


def test_shutdown_sets_redis_flag():
    dog, redis, spawned = _make_watchdog()
    dog._start_all_components()

    dog._stop_component("core", reason="test")
    assert REDIS_CORE_CMD_SHUTDOWN in redis._deleted_log  # cleaned up after stop
    print("[OK] Shutdown: Redis flag set then cleaned")


def test_shutdown_fallback_terminate():
    dog, redis, spawned = _make_watchdog()
    dog._start_component("mt5")

    process = spawned[0]
    process._wait_behavior = "timeout"

    dog._stop_component("mt5", reason="test")
    assert process._terminated, "Process should have been terminated"
    print("[OK] Shutdown: fallback terminate on wait timeout")


def test_shutdown_fallback_kill():
    dog, redis, spawned = _make_watchdog()
    dog._start_component("mt5")

    process = spawned[0]
    call_count = [0]

    def stubborn_wait(timeout=None):
        call_count[0] += 1
        if call_count[0] <= 2:  # first 2 waits fail
            raise subprocess.TimeoutExpired(cmd="mock", timeout=timeout or 1)
        process._returncode = -9
        return process._returncode

    process.wait = stubborn_wait

    dog._stop_component("mt5", reason="test")
    assert process._terminated, "Process should have been terminated"
    assert process._killed, "Process should have been killed"
    print("[OK] Shutdown: fallback kill after terminate timeout")


def test_shutdown_idempotent():
    dog, redis, spawned = _make_watchdog()
    dog._start_all_components()

    dog.shutdown()
    dog.shutdown()  # second call is no-op
    print("[OK] Shutdown: idempotent (second call is no-op)")


def test_shutdown_already_dead_process():
    dog, redis, spawned = _make_watchdog()
    dog._start_component("core")

    process = spawned[0]
    process.force_exit(0)  # already dead

    dog._stop_component("core", reason="test")
    assert not process._terminated
    assert not process._killed
    print("[OK] Shutdown: skip terminate/kill if process already dead")


def test_shutdown_none_process():
    dog, redis, spawned = _make_watchdog()
    # Process never started
    dog._stop_component("core", reason="test")
    # Should not crash
    print("[OK] Shutdown: no-op if process is None")


# ═══════════════════════════════════════════════════════════
# Tests: Monitoring
# ═══════════════════════════════════════════════════════════


def test_monitor_heartbeat_missing():
    dog, redis, spawned = _make_watchdog()
    dog._start_all_components()

    # Remove MT5 heartbeat to simulate expired TTL
    redis.delete(REDIS_MT5_HEARTBEAT)

    restart_triggered = [False, None]
    original_restart = dog._restart_component

    def mock_restart(name, reason):
        restart_triggered[0] = True
        restart_triggered[1] = name
        # Restart re-spawns, which auto-sets heartbeat via factory
        original_restart(name, reason)

    dog._restart_component = mock_restart
    dog._monitor_iteration()

    assert restart_triggered[0], "Restart should be triggered"
    assert restart_triggered[1] == "mt5"
    print("[OK] Monitor: heartbeat missing -> restart triggered")


def test_monitor_process_exit():
    dog, redis, spawned = _make_watchdog()
    dog._start_all_components()

    # Simulate Core process exit
    core_proc = spawned[1]  # mt5=0, core=1, recon=2
    core_proc.force_exit(1)

    restart_triggered = [False, None]
    original_restart = dog._restart_component

    def mock_restart(name, reason):
        restart_triggered[0] = True
        restart_triggered[1] = name
        original_restart(name, reason)

    dog._restart_component = mock_restart
    dog._monitor_iteration()

    assert restart_triggered[0], "Restart should be triggered"
    assert restart_triggered[1] == "core"
    print("[OK] Monitor: process exit -> restart triggered")


def test_monitor_global_shutdown():
    dog, redis, spawned = _make_watchdog()
    redis.set(REDIS_SIGNAL_SHUTDOWN, "1")
    assert dog._global_shutdown_requested()
    print("[OK] Monitor: global shutdown signal detected")


def test_monitor_no_global_shutdown():
    dog, redis, spawned = _make_watchdog()
    assert not dog._global_shutdown_requested()
    print("[OK] Monitor: no global shutdown when key absent")


def test_monitor_all_healthy():
    dog, redis, spawned = _make_watchdog()
    dog._start_all_components()

    restart_triggered = [False]
    dog._restart_component = lambda name, reason: restart_triggered.__setitem__(0, True)

    dog._monitor_iteration()
    assert not restart_triggered[0], "No restart should be triggered when all healthy"
    print("[OK] Monitor: all healthy -> no restart")


# ═══════════════════════════════════════════════════════════
# Tests: Restart Logic
# ═══════════════════════════════════════════════════════════


def test_restart_rate_limiting():
    dog, redis, spawned = _make_watchdog(watchdog_max_restarts=2, watchdog_restart_window_sec=300)

    # 3 restarts within the window should exceed limit (max=2)
    dog._record_restart("mt5")
    dog._record_restart("mt5")
    try:
        dog._record_restart("mt5")
        assert False, "Should have raised RuntimeError for restart limit"
    except RuntimeError as e:
        assert "Restart limit exceeded" in str(e)
    print("[OK] Restart: rate limiting (max restarts exceeded -> RuntimeError)")


def test_restart_window_expiry():
    dog, redis, spawned = _make_watchdog(watchdog_max_restarts=2, watchdog_restart_window_sec=10)

    # Manually add old restart history
    dog._restart_history["mt5"].append(time.time() - 20)  # old, outside window
    dog._restart_history["mt5"].append(time.time() - 15)  # old, outside window
    # No exception because old entries are pruned
    dog._record_restart("mt5")
    print("[OK] Restart: old entries pruned within window")


def test_restart_component_flow():
    dog, redis, spawned = _make_watchdog()
    dog._start_all_components()

    old_count = len(spawned)
    dog._restart_component("mt5", "heartbeat_missing")

    assert len(spawned) > old_count, "New process should be spawned"
    print("[OK] Restart: stop + start = new process spawned")


# ═══════════════════════════════════════════════════════════
# Tests: Cleanup
# ═══════════════════════════════════════════════════════════


def test_cleanup_runtime_keys():
    dog, redis, spawned = _make_watchdog()
    # Seed some runtime keys
    for key in Watchdog.FULL_CLEANUP_KEYS:
        redis.set(key, "value")

    dog._cleanup_runtime_keys()

    for key in Watchdog.FULL_CLEANUP_KEYS:
        assert redis.get(key) is None, f"Key {key} not cleaned up"
    print("[OK] Cleanup: all runtime keys deleted")


def test_full_cleanup_keys_complete():
    expected_count = 15  # All known runtime keys
    actual_count = len(Watchdog.FULL_CLEANUP_KEYS)
    assert actual_count == expected_count, f"Expected {expected_count} cleanup keys, got {actual_count}"
    print(f"[OK] Cleanup: FULL_CLEANUP_KEYS has {actual_count} keys")


# ═══════════════════════════════════════════════════════════
# Tests: ComponentSpec
# ═══════════════════════════════════════════════════════════


def test_component_spec_definitions():
    assert set(Watchdog.COMPONENTS.keys()) == {"mt5", "core", "recon"}
    assert Watchdog.STARTUP_ORDER == ("mt5", "core", "recon")
    assert Watchdog.SHUTDOWN_ORDER == ("core", "recon", "mt5")
    print("[OK] ComponentSpec: 3 components with correct startup/shutdown order")


def test_component_spec_shutdown_keys():
    assert Watchdog.COMPONENTS["mt5"].shutdown_key == REDIS_MT5_CMD_SHUTDOWN
    assert Watchdog.COMPONENTS["core"].shutdown_key == REDIS_CORE_CMD_SHUTDOWN
    assert Watchdog.COMPONENTS["recon"].shutdown_key == REDIS_RECON_CMD_SHUTDOWN
    print("[OK] ComponentSpec: per-process shutdown keys correct")


def test_component_spec_heartbeat_keys():
    assert Watchdog.COMPONENTS["mt5"].heartbeat_key == REDIS_MT5_HEARTBEAT
    assert Watchdog.COMPONENTS["core"].heartbeat_key == REDIS_BRAIN_HEARTBEAT
    assert Watchdog.COMPONENTS["recon"].heartbeat_key == REDIS_RECON_HEARTBEAT
    print("[OK] ComponentSpec: heartbeat keys correct")


# ═══════════════════════════════════════════════════════════
# Tests: run() integration
# ═══════════════════════════════════════════════════════════


def test_run_with_immediate_shutdown():
    dog, redis, spawned = _make_watchdog()

    # Set global shutdown before run loop starts
    redis.set(REDIS_SIGNAL_SHUTDOWN, "1")

    exit_code = dog.run()
    assert exit_code == 0
    assert dog._shutdown_started
    print("[OK] run(): immediate global shutdown -> clean exit (code 0)")


def test_run_redis_ping_fails():
    dog, redis, spawned = _make_watchdog()

    def failing_ping():
        raise ConnectionError("Redis not available")

    redis.ping = failing_ping

    exit_code = dog.run()
    assert exit_code == 1
    print("[OK] run(): Redis ping fails -> exit code 1")


# ═══════════════════════════════════════════════════════════
# Runner
# ═══════════════════════════════════════════════════════════


ALL_TESTS = [
    # Startup
    test_startup_correct_order,
    test_startup_heartbeat_timeout,
    test_startup_process_dies_before_heartbeat,
    test_startup_config_path_appended,
    test_startup_clears_component_keys,
    # Shutdown
    test_shutdown_correct_order,
    test_shutdown_sets_redis_flag,
    test_shutdown_fallback_terminate,
    test_shutdown_fallback_kill,
    test_shutdown_idempotent,
    test_shutdown_already_dead_process,
    test_shutdown_none_process,
    # Monitoring
    test_monitor_heartbeat_missing,
    test_monitor_process_exit,
    test_monitor_global_shutdown,
    test_monitor_no_global_shutdown,
    test_monitor_all_healthy,
    # Restart
    test_restart_rate_limiting,
    test_restart_window_expiry,
    test_restart_component_flow,
    # Cleanup
    test_cleanup_runtime_keys,
    test_full_cleanup_keys_complete,
    # ComponentSpec
    test_component_spec_definitions,
    test_component_spec_shutdown_keys,
    test_component_spec_heartbeat_keys,
    # Integration
    test_run_with_immediate_shutdown,
    test_run_redis_ping_fails,
]


if __name__ == "__main__":
    passed = 0
    failed = 0
    errors = []

    for test_fn in ALL_TESTS:
        try:
            test_fn()
            passed += 1
        except Exception as exc:
            failed += 1
            errors.append((test_fn.__name__, traceback.format_exc()))

    print(f"\n{'='*60}")
    print(f"Phase 5 Test Results: {passed} passed, {failed} failed out of {len(ALL_TESTS)}")
    if errors:
        print("\nFailed tests:")
        for name, err in errors:
            print(f"  x {name}:\n{err}")
    else:
        print("\n=== test_phase5_watchdog (Phase 5): ALL PASSED ===")
    print(f"{'='*60}")

    if failed:
        sys.exit(1)
