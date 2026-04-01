"""Unit tests for Phase 2 — MT5 Process IPC layer.

Covers:
  1. TickWorker: build_tick_payload, tick dedup, position monitoring, position:gone
  2. OrderWorker: serialize_positions, _decode_command, _execute_command,
     recovery (open/close), position query, live command dispatch
  3. MT5Process: heartbeat, shutdown signal, workers_alive check

All external deps (MT5 API, Redis) are mocked — no real connections needed.
"""

import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import orjson

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.utils.constants import (
    MT5_MAGIC,
    REDIS_MT5_CMD_SHUTDOWN,
    REDIS_MT5_ORDER_PROCESSING,
    REDIS_MT5_ORDER_QUEUE,
    REDIS_MT5_ORDER_RESULT,
    REDIS_MT5_POSITION_GONE,
    REDIS_MT5_POSITIONS_RESPONSE,
    REDIS_MT5_QUERY_POSITIONS,
    REDIS_MT5_TICK,
    REDIS_MT5_TICK_CHANNEL,
    REDIS_MT5_TRACKED_TICKETS,
)


# ────────────────────────────────────────────────────────────
# Mock MT5 API — simulates MetaTrader5 module
# ────────────────────────────────────────────────────────────

class MockMT5:
    """In-memory mock of the MetaTrader5 module."""

    ORDER_FILLING_FOK = 0
    ORDER_TYPE_BUY = 0
    ORDER_TYPE_SELL = 1
    TRADE_ACTION_DEAL = 1
    ORDER_TIME_GTC = 0
    ORDER_FILLING_IOC = 1
    ORDER_FILLING_RETURN = 2
    TRADE_RETCODE_DONE = 10009
    TRADE_RETCODE_INVALID_FILL = 10030
    SYMBOL_FILLING_FOK = 1
    SYMBOL_FILLING_IOC = 2
    SYMBOL_TRADE_EXECUTION_INSTANT = 0
    SYMBOL_TRADE_EXECUTION_REQUEST = 1
    SYMBOL_TRADE_EXECUTION_MARKET = 2
    SYMBOL_TRADE_EXECUTION_EXCHANGE = 3

    def __init__(self):
        self._positions: dict[int, SimpleNamespace] = {}
        self._tick = SimpleNamespace(bid=3245.50, ask=3245.80, last=3245.65, time_msc=1000)
        self._connected = True
        self._next_ticket = 100000
        self._symbol_info = SimpleNamespace(
            filling_mode=self.SYMBOL_FILLING_IOC,
            trade_exemode=self.SYMBOL_TRADE_EXECUTION_MARKET,
        )
        self._supported_filling_modes = {self.ORDER_FILLING_IOC}
        self.last_request = None

    def symbol_info_tick(self, symbol: str):
        return self._tick

    def symbol_info(self, symbol: str):
        return self._symbol_info

    def set_tick(self, bid, ask, last, time_msc):
        self._tick = SimpleNamespace(bid=bid, ask=ask, last=last, time_msc=time_msc)

    def set_symbol_info(self, *, filling_mode=None, trade_exemode=None):
        if filling_mode is not None:
            self._symbol_info.filling_mode = filling_mode
        if trade_exemode is not None:
            self._symbol_info.trade_exemode = trade_exemode

    def set_supported_filling_modes(self, *modes):
        self._supported_filling_modes = set(modes)

    def positions_get(self, *, ticket: int = None, symbol: str = None):
        if ticket is not None:
            pos = self._positions.get(ticket)
            return [pos] if pos else None
        if symbol is not None:
            return list(self._positions.values()) or None
        return list(self._positions.values()) or None

    def add_position(self, ticket, type_=0, volume=0.01, price=3245.0,
                     profit=0.0, magic=MT5_MAGIC, comment="arb"):
        self._positions[ticket] = SimpleNamespace(
            ticket=ticket, type=type_, volume=volume,
            price_open=price, profit=profit, magic=magic, comment=comment,
        )

    def remove_position(self, ticket):
        self._positions.pop(ticket, None)

    def order_send(self, request):
        self.last_request = dict(request)
        if request.get("type_filling") not in self._supported_filling_modes:
            return SimpleNamespace(
                retcode=self.TRADE_RETCODE_INVALID_FILL,
                order=0,
                price=request.get("price", 0.0),
                comment="Unsupported filling mode",
            )
        action_type = request.get("type")
        ticket = request.get("position")
        if ticket:
            # Close order
            self.remove_position(ticket)
            return SimpleNamespace(
                retcode=self.TRADE_RETCODE_DONE,
                order=ticket,
                price=request.get("price", 0.0),
                comment="Close OK",
            )
        # Open order
        self._next_ticket += 1
        new_ticket = self._next_ticket
        self.add_position(
            new_ticket,
            type_=request.get("type", 0),
            volume=request.get("volume", 0.01),
            price=request.get("price", 0.0),
            magic=request.get("magic", MT5_MAGIC),
            comment=request.get("comment", ""),
        )
        return SimpleNamespace(
            retcode=self.TRADE_RETCODE_DONE,
            order=new_ticket,
            price=request.get("price", 0.0),
            comment="Open OK",
        )

    def last_error(self):
        return (0, "No error")

    def history_deals_get(self, *, position=None):
        return [SimpleNamespace(profit=0.55)]

    def initialize(self, **kwargs):
        return True

    def shutdown(self):
        pass

    def symbol_select(self, symbol, enable):
        return True

    def account_info(self):
        return SimpleNamespace(company="Test", login=12345, balance=10000.0, leverage=100)

    def terminal_info(self):
        return SimpleNamespace(connected=self._connected)


# ────────────────────────────────────────────────────────────
# Mock Redis — in-memory
# ────────────────────────────────────────────────────────────

class MockRedis:
    """Minimal in-memory Redis mock for testing IPC."""

    def __init__(self):
        self._data: dict[str, str] = {}
        self._lists: dict[str, list[str]] = {}
        self._sets: dict[str, set[str]] = {}
        self._published: list[tuple[str, str]] = []

    def ping(self):
        return True

    def set(self, key, value, ex=None):
        self._data[key] = value

    def get(self, key):
        return self._data.get(key)

    def lpush(self, key, value):
        self._lists.setdefault(key, []).insert(0, value)

    def rpush(self, key, value):
        self._lists.setdefault(key, []).append(value)

    def rpop(self, key):
        lst = self._lists.get(key, [])
        return lst.pop() if lst else None

    def lrange(self, key, start, end):
        lst = self._lists.get(key, [])
        if end == -1:
            return lst[start:]
        return lst[start:end + 1]

    def lrem(self, key, count, value):
        lst = self._lists.get(key, [])
        try:
            lst.remove(value)
        except ValueError:
            pass

    def brpoplpush(self, src, dst, timeout=0):
        lst = self._lists.get(src, [])
        if not lst:
            return None
        val = lst.pop()
        self._lists.setdefault(dst, []).insert(0, val)
        return val

    def publish(self, channel, message):
        self._published.append((channel, message))

    def sadd(self, key, *values):
        s = self._sets.setdefault(key, set())
        for v in values:
            s.add(v)

    def srem(self, key, *values):
        s = self._sets.get(key, set())
        for v in values:
            s.discard(v)

    def smembers(self, key):
        return self._sets.get(key, set())

    def delete(self, *keys):
        for key in keys:
            self._data.pop(key, None)
            self._lists.pop(key, None)
            self._sets.pop(key, None)


# ────────────────────────────────────────────────────────────
# Helper
# ────────────────────────────────────────────────────────────

def _make_tick_worker(**overrides):
    from src.mt5_process.tick_worker import TickWorker
    defaults = dict(
        mt5_api=MockMT5(),
        api_lock=threading.Lock(),
        redis_client=MockRedis(),
        symbol="XAUUSD",
        shutdown_event=threading.Event(),
        position_check_interval_ms=30,
    )
    defaults.update(overrides)
    return TickWorker(**defaults)


def _make_order_worker(**overrides):
    from src.mt5_process.order_worker import OrderWorker
    defaults = dict(
        mt5_api=MockMT5(),
        api_lock=threading.Lock(),
        redis_client=MockRedis(),
        symbol="XAUUSD",
        shutdown_event=threading.Event(),
    )
    defaults.update(overrides)
    return OrderWorker(**defaults)


# ════════════════════════════════════════════════════════════
# 1. TickWorker
# ════════════════════════════════════════════════════════════

def test_build_tick_payload():
    """build_tick_payload should convert MT5 tick object to dict."""
    from src.mt5_process.tick_worker import TickWorker
    tick = SimpleNamespace(bid=3245.50, ask=3245.80, last=3245.65, time_msc=1711425600000)
    payload = TickWorker.build_tick_payload(tick, now=1711425600.123)
    assert payload["bid"] == 3245.50
    assert payload["ask"] == 3245.80
    assert payload["last"] == 3245.65
    assert payload["time_msc"] == 1711425600000
    assert payload["local_ts"] == 1711425600.123
    print("[OK] build_tick_payload")


def test_build_tick_payload_missing_fields():
    """Should handle missing tick fields gracefully."""
    from src.mt5_process.tick_worker import TickWorker
    tick = SimpleNamespace()
    payload = TickWorker.build_tick_payload(tick, now=1.0)
    assert payload["bid"] == 0.0
    assert payload["ask"] == 0.0
    assert payload["last"] == 0.0
    assert isinstance(payload["time_msc"], int)
    print("[OK] build_tick_payload with missing fields")


def test_tick_poll_dedup():
    """Same time_msc should not publish again."""
    mt5 = MockMT5()
    redis = MockRedis()
    worker = _make_tick_worker(mt5_api=mt5, redis_client=redis)

    # First poll — new tick
    worker._poll_tick()
    assert worker.tick_count == 1
    assert redis.get(REDIS_MT5_TICK) is not None

    # Second poll — same time_msc
    worker._poll_tick()
    assert worker.tick_count == 1  # no increment

    # Third poll — new time_msc
    mt5.set_tick(3246.0, 3246.50, 3246.25, 2000)
    worker._poll_tick()
    assert worker.tick_count == 2
    print("[OK] Tick dedup by time_msc")


def test_tick_publishes_to_channel():
    """_poll_tick should SET + PUBLISH."""
    mt5 = MockMT5()
    redis = MockRedis()
    worker = _make_tick_worker(mt5_api=mt5, redis_client=redis)

    worker._poll_tick()
    assert len(redis._published) == 1
    channel, msg = redis._published[0]
    assert channel == REDIS_MT5_TICK_CHANNEL
    data = orjson.loads(msg)
    assert data["bid"] == 3245.50
    print("[OK] Tick SET + PUBLISH")


def test_tick_none_ignored():
    """If symbol_info_tick returns None, no crash."""
    mt5 = MockMT5()
    mt5.symbol_info_tick = lambda s: None
    redis = MockRedis()
    worker = _make_tick_worker(mt5_api=mt5, redis_client=redis)

    worker._poll_tick()
    assert worker.tick_count == 0
    print("[OK] None tick ignored")


def test_position_monitoring_no_tracked():
    """No tracked tickets → skip position check."""
    redis = MockRedis()
    worker = _make_tick_worker(redis_client=redis)
    worker._next_position_check = 0  # force check
    worker._maybe_check_tracked_positions()
    assert len(redis._published) == 0
    print("[OK] No tracked tickets -> no check")


def test_position_monitoring_gone():
    """Tracked ticket missing → PUBLISH position:gone + remove from set."""
    mt5 = MockMT5()
    redis = MockRedis()
    redis.sadd(REDIS_MT5_TRACKED_TICKETS, "12345")
    # MT5 has NO position 12345

    worker = _make_tick_worker(mt5_api=mt5, redis_client=redis)
    worker._next_position_check = 0  # force check
    worker._maybe_check_tracked_positions()

    assert worker.position_gone_count == 1
    assert len(redis._published) == 1
    channel, msg = redis._published[0]
    assert channel == REDIS_MT5_POSITION_GONE
    data = orjson.loads(msg)
    assert data["ticket"] == 12345

    # Ticket removed from tracked set
    assert "12345" not in redis.smembers(REDIS_MT5_TRACKED_TICKETS)
    print("[OK] Position gone: published + removed from tracked")


def test_position_monitoring_still_exists():
    """Tracked ticket still exists → no event."""
    mt5 = MockMT5()
    mt5.add_position(12345)
    redis = MockRedis()
    redis.sadd(REDIS_MT5_TRACKED_TICKETS, "12345")

    worker = _make_tick_worker(mt5_api=mt5, redis_client=redis)
    worker._next_position_check = 0
    worker._maybe_check_tracked_positions()

    assert worker.position_gone_count == 0
    assert len(redis._published) == 0
    assert "12345" in redis.smembers(REDIS_MT5_TRACKED_TICKETS)
    print("[OK] Position still exists -> no event")


def test_tick_worker_thread_lifecycle():
    """TickWorker starts and stops cleanly."""
    shutdown = threading.Event()
    worker = _make_tick_worker(shutdown_event=shutdown)
    worker.start()
    time.sleep(0.05)
    assert worker.is_alive()
    shutdown.set()
    worker.join(timeout=3)
    assert not worker.is_alive()
    print("[OK] TickWorker thread lifecycle")


# ════════════════════════════════════════════════════════════
# 2. OrderWorker
# ════════════════════════════════════════════════════════════

def test_serialize_positions_filters_by_magic():
    """Only positions with matching magic are included."""
    from src.mt5_process.order_worker import OrderWorker
    positions = [
        SimpleNamespace(ticket=1, type=0, volume=0.01, price_open=3000.0, profit=1.0, magic=MT5_MAGIC, comment="arb"),
        SimpleNamespace(ticket=2, type=1, volume=0.02, price_open=3100.0, profit=-0.5, magic=99999, comment="other"),
        SimpleNamespace(ticket=3, type=0, volume=0.03, price_open=3200.0, profit=2.0, magic=MT5_MAGIC, comment="arb_long"),
    ]
    result = OrderWorker.serialize_positions(positions, magic=MT5_MAGIC)
    assert len(result) == 2
    assert result[0]["ticket"] == 1
    assert result[0]["type"] == "BUY"
    assert result[1]["ticket"] == 3
    assert result[1]["type"] == "BUY"
    print("[OK] serialize_positions filters by magic")


def test_serialize_positions_sell_type():
    """type=1 should serialize as 'SELL'."""
    from src.mt5_process.order_worker import OrderWorker
    positions = [
        SimpleNamespace(ticket=10, type=1, volume=0.05, price_open=3000.0, profit=0.0, magic=MT5_MAGIC, comment=""),
    ]
    result = OrderWorker.serialize_positions(positions, magic=MT5_MAGIC)
    assert result[0]["type"] == "SELL"
    print("[OK] serialize_positions: type=1 -> SELL")


def test_serialize_positions_empty():
    from src.mt5_process.order_worker import OrderWorker
    assert OrderWorker.serialize_positions(None) == []
    assert OrderWorker.serialize_positions([]) == []
    print("[OK] serialize_positions: None/[] -> []")


def test_decode_command():
    """_decode_command should parse JSON string."""
    worker = _make_order_worker()
    cmd = worker._decode_command('{"action": "BUY", "volume": 0.01, "job_id": "abc"}')
    assert cmd["action"] == "BUY"
    assert cmd["job_id"] == "abc"
    print("[OK] _decode_command parses JSON")


def test_decode_command_invalid():
    """Non-dict JSON should raise ValueError."""
    worker = _make_order_worker()
    try:
        worker._decode_command('"just a string"')
        assert False, "Should raise"
    except ValueError as e:
        assert "Expected JSON object" in str(e)
    print("[OK] _decode_command rejects non-dict")


def test_execute_open_buy():
    mt5 = MockMT5()
    redis = MockRedis()
    worker = _make_order_worker(mt5_api=mt5, redis_client=redis)
    command = {"action": "BUY", "volume": 0.01, "job_id": "j1", "comment": "arb_long"}

    result = worker._execute_command(command)
    assert result["success"] is True
    assert result["job_id"] == "j1"
    assert result["action"] == "BUY"
    assert result["ticket"] > 0
    assert result["volume"] == 0.01
    assert result["latency_ms"] >= 0
    print("[OK] _execute_command BUY success")


def test_execute_open_sell():
    mt5 = MockMT5()
    worker = _make_order_worker(mt5_api=mt5)
    command = {"action": "SELL", "volume": 0.02, "job_id": "j2", "comment": "arb_short"}
    result = worker._execute_command(command)
    assert result["success"] is True
    assert result["action"] == "SELL"
    print("[OK] _execute_command SELL success")


def test_execute_close():
    mt5 = MockMT5()
    mt5.add_position(55555, type_=0, volume=0.01, price=3245.0)
    worker = _make_order_worker(mt5_api=mt5)
    command = {"action": "CLOSE", "ticket": 55555, "volume": 0.01, "job_id": "j3"}

    result = worker._execute_command(command)
    assert result["success"] is True
    assert result["action"] == "CLOSE"
    assert result["ticket"] == 55555
    assert "profit" in result
    print("[OK] _execute_command CLOSE success")


def test_execute_open_selects_supported_market_filling_mode():
    mt5 = MockMT5()
    mt5.set_symbol_info(
        filling_mode=mt5.SYMBOL_FILLING_FOK,
        trade_exemode=mt5.SYMBOL_TRADE_EXECUTION_MARKET,
    )
    mt5.set_supported_filling_modes(mt5.ORDER_FILLING_FOK)
    worker = _make_order_worker(mt5_api=mt5)

    result = worker._execute_command({"action": "BUY", "volume": 0.01, "job_id": "j_fill_open"})

    assert result["success"] is True
    assert mt5.last_request["type_filling"] == mt5.ORDER_FILLING_FOK
    print("[OK] _execute_command BUY picks broker-supported market fill mode")


def test_execute_close_uses_return_when_exchange_execution_allows_it():
    mt5 = MockMT5()
    mt5.add_position(66666, type_=0, volume=0.01, price=3245.0)
    mt5.set_symbol_info(
        filling_mode=0,
        trade_exemode=mt5.SYMBOL_TRADE_EXECUTION_EXCHANGE,
    )
    mt5.set_supported_filling_modes(mt5.ORDER_FILLING_RETURN)
    worker = _make_order_worker(mt5_api=mt5)

    result = worker._execute_command({"action": "CLOSE", "ticket": 66666, "volume": 0.01, "job_id": "j_fill_close"})

    assert result["success"] is True
    assert mt5.last_request["type_filling"] == mt5.ORDER_FILLING_RETURN
    print("[OK] _execute_command CLOSE reuses supported fill mode")


def test_execute_close_missing_position():
    mt5 = MockMT5()
    worker = _make_order_worker(mt5_api=mt5)
    command = {"action": "CLOSE", "ticket": 99999, "job_id": "j4"}
    result = worker._execute_command(command)
    assert result["success"] is False
    assert "not found" in result["error"]
    print("[OK] _execute_command CLOSE missing position fails")


def test_execute_close_no_ticket():
    worker = _make_order_worker()
    command = {"action": "CLOSE", "job_id": "j5"}
    result = worker._execute_command(command)
    assert result["success"] is False
    assert "ticket" in result["error"].lower()
    print("[OK] _execute_command CLOSE without ticket fails")


def test_execute_unknown_action():
    worker = _make_order_worker()
    command = {"action": "HEDGE", "job_id": "j6"}
    result = worker._execute_command(command)
    assert result["success"] is False
    assert "Unknown action" in result["error"]
    print("[OK] _execute_command unknown action fails")


def test_execute_invalid_volume():
    worker = _make_order_worker()
    command = {"action": "BUY", "volume": "xyz", "job_id": "j7"}
    result = worker._execute_command(command)
    assert result["success"] is False
    print("[OK] _execute_command invalid volume fails")


def test_recover_open_found():
    """Recovery: BUY command found matching position → success result."""
    mt5 = MockMT5()
    mt5.add_position(77777, type_=0, volume=0.01, magic=MT5_MAGIC, comment="arb_long")
    worker = _make_order_worker(mt5_api=mt5)

    command = {"action": "BUY", "volume": 0.01, "job_id": "r1", "comment": "arb_long"}
    result = worker._recover_command(command)
    assert result is not None
    assert result["success"] is True
    assert result["ticket"] == 77777
    assert result["recovered"] is True
    print("[OK] Recover open: found matching position")


def test_recover_open_not_found():
    """Recovery: BUY command with no matching position → None (requeue)."""
    mt5 = MockMT5()
    worker = _make_order_worker(mt5_api=mt5)

    command = {"action": "BUY", "volume": 0.01, "job_id": "r2", "comment": "arb_long"}
    result = worker._recover_command(command)
    assert result is None
    print("[OK] Recover open: no match -> None (requeue)")


def test_recover_close_already_closed():
    """Recovery: CLOSE command, position gone → success (already closed)."""
    mt5 = MockMT5()
    worker = _make_order_worker(mt5_api=mt5)

    command = {"action": "CLOSE", "ticket": 88888, "volume": 0.01, "job_id": "r3"}
    result = worker._recover_command(command)
    assert result is not None
    assert result["success"] is True
    assert result["recovered"] is True
    print("[OK] Recover close: position already gone -> success")


def test_recover_close_still_open():
    """Recovery: CLOSE command, position still exists → None (requeue to re-execute)."""
    mt5 = MockMT5()
    mt5.add_position(88888, volume=0.01)
    worker = _make_order_worker(mt5_api=mt5)

    command = {"action": "CLOSE", "ticket": 88888, "volume": 0.01, "job_id": "r4"}
    result = worker._recover_command(command)
    assert result is None
    print("[OK] Recover close: position still open -> None (requeue)")


def test_recover_close_no_ticket():
    """Recovery: CLOSE without ticket → failure."""
    worker = _make_order_worker()
    command = {"action": "CLOSE", "ticket": 0, "job_id": "r5"}
    result = worker._recover_command(command)
    assert result is not None
    assert result["success"] is False
    assert "Invalid CLOSE" in result["error"]
    print("[OK] Recover close without ticket -> failure")


def test_recover_unknown_action():
    worker = _make_order_worker()
    command = {"action": "HEDGE", "job_id": "r6"}
    result = worker._recover_command(command)
    assert result is not None
    assert result["success"] is False
    assert "Unknown action" in result["error"]
    print("[OK] Recover unknown action -> failure")


def test_position_query():
    """_handle_position_query should respond with serialized positions."""
    mt5 = MockMT5()
    mt5.add_position(11111, type_=0, volume=0.01, magic=MT5_MAGIC, comment="arb")
    mt5.add_position(22222, type_=1, volume=0.02, magic=MT5_MAGIC, comment="arb")
    redis = MockRedis()

    query = orjson.dumps({"query_id": "q1", "ts": time.time()}).decode()
    redis.lpush(REDIS_MT5_QUERY_POSITIONS, query)

    worker = _make_order_worker(mt5_api=mt5, redis_client=redis)
    handled = worker._handle_position_query()
    assert handled is True
    assert worker.position_query_count == 1

    # Check response
    raw = redis.rpop(REDIS_MT5_POSITIONS_RESPONSE)
    assert raw is not None
    response = orjson.loads(raw)
    assert response["query_id"] == "q1"
    assert len(response["positions"]) == 2
    print("[OK] Position query: response with 2 positions")


def test_position_query_no_pending():
    """No pending query → returns False."""
    redis = MockRedis()
    worker = _make_order_worker(redis_client=redis)
    assert worker._handle_position_query() is False
    print("[OK] No pending position query -> False")


def test_live_command_dispatch():
    """Full flow: push command → worker picks up → result in result queue."""
    mt5 = MockMT5()
    redis = MockRedis()
    shutdown = threading.Event()

    buy_cmd = orjson.dumps({
        "action": "BUY", "volume": 0.01, "job_id": "live1", "comment": "arb_long"
    }).decode()
    redis.rpush(REDIS_MT5_ORDER_QUEUE, buy_cmd)

    worker = _make_order_worker(mt5_api=mt5, redis_client=redis, shutdown_event=shutdown)

    # Simulate one iteration
    raw = redis.brpoplpush(REDIS_MT5_ORDER_QUEUE, REDIS_MT5_ORDER_PROCESSING, timeout=1)
    assert raw is not None
    worker._process_live_command(raw)

    # Check result
    raw_result = redis.rpop(REDIS_MT5_ORDER_RESULT)
    assert raw_result is not None
    result = orjson.loads(raw_result)
    assert result["success"] is True
    assert result["job_id"] == "live1"
    assert result["action"] == "BUY"
    assert worker.order_count == 1

    # Processing queue should be clean
    assert redis.lrange(REDIS_MT5_ORDER_PROCESSING, 0, -1) == []
    print("[OK] Live command dispatch: BUY -> result + cleanup")


def test_recovery_on_startup():
    """Processing queue items should be recovered on startup."""
    mt5 = MockMT5()
    mt5.add_position(44444, type_=0, volume=0.01, magic=MT5_MAGIC, comment="arb_long")
    redis = MockRedis()

    stale_cmd = orjson.dumps({
        "action": "BUY", "volume": 0.01, "job_id": "stale1", "comment": "arb_long",
    }).decode()
    redis.lpush(REDIS_MT5_ORDER_PROCESSING, stale_cmd)

    worker = _make_order_worker(mt5_api=mt5, redis_client=redis)
    worker._recover_processing_queue()

    assert worker.recovered_count == 1

    # Result should be in result queue
    raw = redis.rpop(REDIS_MT5_ORDER_RESULT)
    assert raw is not None
    result = orjson.loads(raw)
    assert result["success"] is True
    assert result["recovered"] is True
    print("[OK] Recovery on startup: matched position -> result")


def test_recovery_requeue():
    """Unmatched recovery item should be requeued."""
    mt5 = MockMT5()
    redis = MockRedis()

    stale_cmd = orjson.dumps({
        "action": "BUY", "volume": 0.01, "job_id": "stale2", "comment": "arb_long",
    }).decode()
    redis.lpush(REDIS_MT5_ORDER_PROCESSING, stale_cmd)

    worker = _make_order_worker(mt5_api=mt5, redis_client=redis)
    worker._recover_processing_queue()

    assert worker.recovered_count == 0
    # Should be back in main queue
    pending = redis.lrange(REDIS_MT5_ORDER_QUEUE, 0, -1)
    assert len(pending) == 1
    print("[OK] Recovery: no match -> requeued to main queue")


def test_order_worker_thread_lifecycle():
    """OrderWorker starts and stops cleanly."""
    mt5 = MockMT5()
    redis = MockRedis()
    shutdown = threading.Event()

    # Override brpoplpush to respect shutdown
    original_brpoplpush = redis.brpoplpush
    def blocking_brpoplpush(src, dst, timeout=0):
        if shutdown.is_set():
            return None
        time.sleep(0.01)
        return original_brpoplpush(src, dst, timeout)
    redis.brpoplpush = blocking_brpoplpush

    worker = _make_order_worker(mt5_api=mt5, redis_client=redis, shutdown_event=shutdown)
    worker.start()
    time.sleep(0.1)
    assert worker.is_alive()
    shutdown.set()
    worker.join(timeout=5)
    assert not worker.is_alive()
    print("[OK] OrderWorker thread lifecycle")


# ════════════════════════════════════════════════════════════
# 3. MT5Process (unit-level, no real connections)
# ════════════════════════════════════════════════════════════

def test_heartbeat_payload():
    """_publish_heartbeat should write proper JSON to Redis."""
    from src.mt5_process.main import MT5Process

    mt5_mock = MockMT5()
    redis = MockRedis()

    with patch("src.mt5_process.main.get_redis", return_value=redis), \
         patch("src.mt5_process.main.Config") as MockConfig:
        cfg = MockConfig.return_value
        cfg.redis = {}
        cfg.mt5 = {}
        cfg.mt5_symbol = "XAUUSD"
        cfg.mt5_position_check_interval_ms = 30

        proc = MT5Process.__new__(MT5Process)
        proc.config = cfg
        proc.redis = redis
        proc.shutdown_event = threading.Event()
        proc.api_lock = threading.Lock()
        proc._connected = True
        proc._tick_worker = SimpleNamespace(tick_count=100)
        proc._order_worker = SimpleNamespace(order_count=5)

        proc._publish_heartbeat()

    raw = redis.get("mt5:heartbeat")
    assert raw is not None
    hb = orjson.loads(raw)
    assert hb["connected"] is True
    assert hb["tick_count"] == 100
    assert hb["order_count"] == 5
    assert "pid" in hb
    assert "ts" in hb
    print("[OK] Heartbeat payload correct")


def test_shutdown_signal_detected():
    """_check_shutdown_signal should return True when mt5:cmd:shutdown = '1'."""
    from src.mt5_process.main import MT5Process

    redis = MockRedis()

    proc = MT5Process.__new__(MT5Process)
    proc.redis = redis
    proc.shutdown_event = threading.Event()

    assert proc._check_shutdown_signal() is False

    redis.set(REDIS_MT5_CMD_SHUTDOWN, "1")
    assert proc._check_shutdown_signal() is True
    assert proc.shutdown_event.is_set()
    print("[OK] Shutdown signal detection")


def test_workers_alive_check():
    """_workers_alive returns True only when both workers are alive threads."""
    from src.mt5_process.main import MT5Process

    proc = MT5Process.__new__(MT5Process)

    proc._tick_worker = None
    proc._order_worker = None
    assert proc._workers_alive() is False

    proc._tick_worker = SimpleNamespace(is_alive=lambda: True)
    proc._order_worker = SimpleNamespace(is_alive=lambda: True)
    assert proc._workers_alive() is True

    proc._tick_worker = SimpleNamespace(is_alive=lambda: False)
    assert proc._workers_alive() is False
    print("[OK] _workers_alive logic")


# ════════════════════════════════════════════════════════════
# 4. Redis Key Constants
# ════════════════════════════════════════════════════════════

def test_redis_keys_not_empty():
    """All v2 Redis keys should be non-empty strings."""
    keys = [
        REDIS_MT5_TICK, REDIS_MT5_TICK_CHANNEL, REDIS_MT5_ORDER_QUEUE,
        REDIS_MT5_ORDER_PROCESSING, REDIS_MT5_ORDER_RESULT,
        REDIS_MT5_TRACKED_TICKETS, REDIS_MT5_POSITION_GONE,
        REDIS_MT5_QUERY_POSITIONS, REDIS_MT5_POSITIONS_RESPONSE,
        REDIS_MT5_CMD_SHUTDOWN,
    ]
    for k in keys:
        assert isinstance(k, str) and len(k) > 0, f"Bad key: {k!r}"
    print("[OK] All Redis key constants defined")


def test_magic_constant():
    assert isinstance(MT5_MAGIC, int)
    assert MT5_MAGIC > 0
    print("[OK] MT5_MAGIC constant")


# ════════════════════════════════════════════════════════════
# Runner
# ════════════════════════════════════════════════════════════

if __name__ == "__main__":
    tests = [
        # 1. TickWorker
        test_build_tick_payload,
        test_build_tick_payload_missing_fields,
        test_tick_poll_dedup,
        test_tick_publishes_to_channel,
        test_tick_none_ignored,
        test_position_monitoring_no_tracked,
        test_position_monitoring_gone,
        test_position_monitoring_still_exists,
        test_tick_worker_thread_lifecycle,
        # 2. OrderWorker
        test_serialize_positions_filters_by_magic,
        test_serialize_positions_sell_type,
        test_serialize_positions_empty,
        test_decode_command,
        test_decode_command_invalid,
        test_execute_open_buy,
        test_execute_open_sell,
        test_execute_close,
        test_execute_open_selects_supported_market_filling_mode,
        test_execute_close_uses_return_when_exchange_execution_allows_it,
        test_execute_close_missing_position,
        test_execute_close_no_ticket,
        test_execute_unknown_action,
        test_execute_invalid_volume,
        test_recover_open_found,
        test_recover_open_not_found,
        test_recover_close_already_closed,
        test_recover_close_still_open,
        test_recover_close_no_ticket,
        test_recover_unknown_action,
        test_position_query,
        test_position_query_no_pending,
        test_live_command_dispatch,
        test_recovery_on_startup,
        test_recovery_requeue,
        test_order_worker_thread_lifecycle,
        # 3. MT5Process
        test_heartbeat_payload,
        test_shutdown_signal_detected,
        test_workers_alive_check,
        # 4. Constants
        test_redis_keys_not_empty,
        test_magic_constant,
    ]

    passed = 0
    failed = 0
    errors = []
    for t in tests:
        try:
            t()
            passed += 1
        except Exception as e:
            failed += 1
            errors.append((t.__name__, str(e)))
            print(f"[FAIL] {t.__name__}: {e}")

    print(f"\n{'='*60}")
    print(f"Phase 2 Test Results: {passed} passed, {failed} failed out of {len(tests)}")
    if errors:
        print("\nFailed tests:")
        for name, err in errors:
            print(f"  x {name}: {err}")
    else:
        print("\n=== test_mt5_ipc (Phase 2): ALL PASSED ===")
    print(f"{'='*60}")
