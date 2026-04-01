# Startup & Shutdown Sequence

> Quy trình khởi động và tắt hệ thống an toàn.

---

## Startup Sequence

### Phase 1: Watchdog khởi động

```
1. [WATCHDOG] Start
2. [WATCHDOG] Connect Redis, verify alive
3. [WATCHDOG] Clean stale Redis keys (mt5:tick, heartbeats, order queues)
4. [WATCHDOG] Spawn child processes theo thứ tự:
     a. MT5 Process (phải connect MT5 trước)
     b. Core Engine (cần MT5 tick để hoạt động)
     c. Reconciler (cần cả DB + MT5 + Exchange)
```

### Phase 2: MT5 Process khởi động

```
1. [MT5] mt5.initialize() → connect terminal
2. [MT5] Verify symbol available: mt5.symbol_info("XAUUSD")
3. [MT5] Create threading.Lock cho MT5 API (không thread-safe)
4. [MT5] Start Tick Thread → poll tick → SET mt5:tick + PUBLISH tick:mt5
5. [MT5] Tick Thread: position monitor mỗi `position_check_interval_ms` (default **30ms**), CHỈ khi tracked_tickets non-empty
6. [MT5] Start Order Thread → BRPOPLPUSH mt5:order:queue → mt5:order:processing (reliable queue)
7. [MT5] Publish heartbeat → SET mt5:heartbeat
8. [MT5] Log: "MT5 Process ready"
```

> [!NOTE]
> Tất cả MT5 API calls (`symbol_info_tick`, `positions_get`, `order_send`) dùng chung 1 `threading.Lock`.

### Phase 3: Core Engine khởi động

```
 1. [ENGINE] Connect Redis
 2. [ENGINE] Init Database (SQLite) → create tables if not exist
 3. [ENGINE] Create ExchangeHandler via factory: create_exchange(config)
 4. [ENGINE] Init PairManager → load state from DB (OPEN/CLOSE_SENT/ENTRY_SENT/ORPHAN_* pairs)
 5. [ENGINE] Wait MT5 ready: poll redis.get("mt5:heartbeat") — timeout 30s
 6. [ENGINE] Exchange connect: await exchange.connect()
 7. [ENGINE] Exchange WS: start exchange.run_ws() — tick stream + position events
 8. [ENGINE] Exchange auth check: await exchange.check_auth_alive()
 9. [ENGINE] Subscribe Redis PUB/SUB: tick:mt5, mt5:position:gone
10. [ENGINE] Init Brain (event-driven + debounce)
11. [ENGINE] Brain enters WARMUP phase (xem bên dưới)
```

### Phase 4: Brain WARMUP — Điều kiện bắt đầu phát tín hiệu

> [!IMPORTANT]
> **Brain KHÔNG được phát tín hiệu entry ngay khi khởi động.**
> Phải đợi đủ điều kiện warmup trước.

```
┌─────────────────────────────────────────────────────┐
│                  BRAIN WARMUP PHASE                  │
│                                                      │
│  Gate 1: MT5 tick available?          ❌ → wait      │
│  Gate 2: Exchange tick available?      ❌ → wait      │
│  Gate 3: Data fresh? (< max_delay)    ❌ → wait      │
│  Gate 4: Warmup ticks collected?      ❌ → wait      │
│  Gate 5: Trading time?                ❌ → wait      │
│  Gate 6: Exchange auth alive?         ❌ → wait      │
│                                                      │
│  ALL GATES ✅ → "BRAIN READY" → cho phép entry       │
│                                                      │
│  NOTE: Close signals LUÔN được phép (nếu có pair     │
│  đang OPEN từ session trước)                         │
└─────────────────────────────────────────────────────┘
```

#### Gate 4 chi tiết: Warmup Ticks

**Vấn đề**: Khi mới khởi động, spread chưa ổn định. Nếu phát tín hiệu ngay → có thể entry sai vì:
- Spread ban đầu bị lệch do tick chưa sync
- MT5 tick có thể là tick cũ (từ trước khi close market)
- Exchange WS chưa fill đủ orderbook

**Giải pháp**: Buffer warmup

```python
class TradingBrain:
    def __init__(self, ...):
        self._warmup_start = 0.0
        self._warmup_tick_count = 0
        self._is_warmed_up = False

    def _check_warmup(self, spread: float) -> bool:
        """Check if warmup conditions are met."""
        if self._is_warmed_up:
            return True

        if self._warmup_start == 0:
            self._warmup_start = time.time()
            log("INFO", "BRAIN", "Warmup started — collecting spread data...")

        self._warmup_tick_count += 1

        # Condition 1: Minimum time elapsed
        elapsed = time.time() - self._warmup_start
        if elapsed < self.config.warmup_sec:  # default: 300s (5 phút)
            return False

        # Condition 2: Minimum tick samples collected
        if self._warmup_tick_count < self.config.warmup_min_ticks:  # default: 500
            return False

        # All conditions met
        self._is_warmed_up = True
        log("INFO", "BRAIN",
            f"Warmup complete! {self._warmup_tick_count} ticks in {elapsed:.1f}s. "
            f"Signal detection enabled.")
        return True
```

**Config warmup:**
```json
{
  "brain": {
    "warmup_sec": 300,
    "warmup_min_ticks": 500
  }
}
```

| Param | Default | Mô tả |
|---|---|---|
| `warmup_sec` | 300 (5 phút) | Thời gian tối thiểu phải chờ (giây). Configurable. |
| `warmup_min_ticks` | 500 | Số tick tối thiểu cần thu thập |

**Trong warmup, Brain vẫn**:
- ✅ Đọc tick, tính spread, ghi CSV log
- ✅ Xử lý close signals (nếu có pairs OPEN từ session trước)
- ✅ Ghi heartbeat
- ❌ KHÔNG phát entry signals

#### Brain Event-driven (sau warmup)

```python
async def run(self):
    # Subscribe to tick events
    pubsub = self.redis.pubsub()
    await pubsub.subscribe("tick:mt5", "mt5:position:gone")
    self.exchange.on_tick = self._on_exchange_tick

    # Event loop: react khi có event
    async for message in pubsub.listen():
        if message["channel"] == "mt5:position:gone":
            # 🚨 IMMEDIATE: stop-out detected
            await self._handle_position_gone(message["data"])
            continue

        # Normal tick event
        self._on_tick(message)

def _on_tick(self, tick_source):
    mt5_tick = self._get_mt5_tick()
    exchange_tick = self._get_exchange_tick()
    if not mt5_tick or not exchange_tick:
        return
    if not self._is_data_fresh(mt5_tick, exchange_tick):
        return

    spread = self._calculate_spread(mt5_tick, exchange_tick)
    signal = self._detect_signal(spread)

    # CLOSE: debounce timer
    if signal == Signal.CLOSE and self.pos.has_positions:
        self._start_debounce(signal, spread)  # timer tiếp tục nếu cùng signal

    # ENTRY: debounce timer + warmup check
    elif signal in (Signal.ENTRY_LONG, Signal.ENTRY_SHORT):
        if self._check_warmup(spread) and self._is_trading_time():
            self._start_debounce(signal, spread)

    else:
        self._cancel_debounce()  # signal mất hoặc thay đổi → hủy timer

def _start_debounce(self, signal, spread):
    """Start hoặc tiếp tục debounce timer."""
    if self._current_signal == signal:
        return  # Cùng signal → timer TIẾP TỤC, không reset
    # Signal mới khác → cancel cũ, start mới
    self._cancel_debounce()
    self._current_signal = signal
    self._debounce_task = asyncio.create_task(
        self._debounce_execute(signal, spread)
    )

async def _debounce_execute(self, signal, spread):
    """Chờ stable_time_ms rồi RE-CHECK trước khi execute."""
    await asyncio.sleep(self.config.stable_time_ms / 1000)

    # ═══ PRE-EXECUTION GATE CHECK ═══
    # Gate 1: Signal còn đúng?
    current_spread = self._calculate_spread(self._latest_mt5, self._latest_exchange)
    current_signal = self._detect_signal(current_spread)
    if current_signal != signal:
        return  # signal thay đổi trong lúc debounce

    # Gate 2: Tick còn fresh?
    if not self._is_data_fresh(self._latest_mt5, self._latest_exchange):
        return  # tick quá cũ

    # Gate 3: Risk flag?
    if self._risk_flag:  # set bởi on_position_gone()
        return  # đang có sự cố

    # Gate 4: Pair state OK? (close only)
    if signal == Signal.CLOSE:
        pair = self.db.get_active_pair()
        if not pair or pair.status != "OPEN":
            return  # Reconciler đã đổi state

    # Gate 5: Concurrency guard
    if self._is_executing:
        return  # entry/close khác đang chạy

    # ═══ ALL GATES ✅ → EXECUTE ═══
    self._is_executing = True
    try:
        if signal == Signal.CLOSE:
            await self._execute_close(current_spread)
        else:
            await self._execute_entry(signal, current_spread)
    finally:
        self._is_executing = False
```

### Phase 5: Reconciler khởi động

```
1. [RECON] Connect Redis
2. [RECON] Init Database (readonly for pairs, write for recon_log + recon events)
3. [RECON] Create ExchangeHandler riêng (REST-only, không WS) — tránh race condition với Brain
4. [RECON] Wait MT5 ready: poll mt5:heartbeat
5. [RECON] Wait Brain ready: poll brain:heartbeat
6. [RECON] Run first reconcile cycle
7. [RECON] Publish heartbeat → SET recon:heartbeat
8. [RECON] Loop mỗi interval_sec
```

---

## Shutdown Sequence

### Trigger: `stop_bots.py` / `Ctrl+C` / Watchdog internal

> [!IMPORTANT]
> **Watchdog điều phối shutdown theo thứ tự** bằng per-process Redis flag + `wait()`, fallback `terminate()/kill()` nếu child treo.
> Các process KHÔNG tự poll `SIGNAL:SHUTDOWN`. Chỉ Watchdog poll key global này, rồi set cờ riêng cho từng child khi cần tắt.

```
┌──────────────────────────────────────────────────────────┐
│              WATCHDOG-ORCHESTRATED SHUTDOWN               │
│                                                          │
│  Trigger: Ctrl+C / SIGNAL:SHUTDOWN / heartbeat failure   │
│                                                          │
│  ══ Phase 1: Stop Brain (không tạo pair mới) ══════════ │
│                                                          │
│  1. [WATCHDOG] terminate(brain_process)                  │
│     ┌─── BRAIN (on shutdown_event) ──────────────┐      │
│     │ a. Stop accepting entry signals             │      │
│     │ b. Wait current execution to finish         │      │
│     │ c. If has OPEN pairs:                       │      │
│     │    - config "force_close" → close all       │      │
│     │    - config "leave" → leave open            │      │
│     │ d. Flush SpreadLogger CSV                   │      │
│     │ e. Close DB connection                      │      │
│     │ f. Exit                                     │      │
│     └────────────────────────────────────────────┘      │
│  2. [WATCHDOG] wait(brain, timeout=grace_period_sec)     │
│  3. [WATCHDOG] Force kill if Brain still alive           │
│     ✅ Brain exited — no new orders possible             │
│                                                          │
│  ══ Phase 2: Stop Reconciler ══════════════════════════ │
│                                                          │
│  4. [WATCHDOG] terminate(reconciler_process)             │
│     ┌─── RECONCILER (on shutdown_event) ─────────┐      │
│     │ a. Finish current cycle (if running)        │      │
│     │ b. Close DB connection                      │      │
│     │ c. Exit                                     │      │
│     └────────────────────────────────────────────┘      │
│  5. [WATCHDOG] wait(reconciler, timeout=10s)             │
│  6. [WATCHDOG] Force kill if still alive                 │
│     ✅ Reconciler exited — no more heal attempts         │
│                                                          │
│  ══ Phase 3: Stop MT5 (xử lý nốt residual orders) ════ │
│                                                          │
│  7. [WATCHDOG] terminate(mt5_process)                    │
│     ┌─── MT5 PROCESS (on shutdown_event) ────────┐      │
│     │ a. Finish current order (if executing)      │      │
│     │ b. Stop tick thread                         │      │
│     │ c. Stop order thread                        │      │
│     │ d. mt5.shutdown()                           │      │
│     │ e. Exit                                     │      │
│     └────────────────────────────────────────────┘      │
│  8. [WATCHDOG] wait(mt5, timeout=15s)                    │
│  9. [WATCHDOG] Force kill if still alive                 │
│     ✅ MT5 exited — terminal released                    │
│                                                          │
│  ══ Phase 4: Watchdog cleanup ═════════════════════════ │
│                                                          │
│  10. [WATCHDOG] Clean Redis keys                         │
│  11. [WATCHDOG] Exit                                     │
└──────────────────────────────────────────────────────────┘
```

### Cơ chế terminate

Watchdog quản lý child processes bằng `subprocess.Popen`. Mỗi child process có `shutdown_event: threading.Event()` trong main loop. Khi Watchdog gọi `terminate()`:

| OS | Actual signal | Child nhận được |
|---|---|---|
| Windows | `TerminateProcess` | Process exit (cần dùng `CTRL_BREAK_EVENT` hoặc Redis flag cho graceful) |
| Linux | `SIGTERM` | Catch trong signal handler → `shutdown_event.set()` |

> [!NOTE]
> **Windows không có SIGTERM graceful.** Trên Windows, Watchdog dùng per-process Redis flag:
> `SET core:cmd:shutdown "1"` / `SET recon:cmd:shutdown "1"` / `SET mt5:cmd:shutdown "1"`
> → child poll flag riêng → `shutdown_event.set()` → graceful exit.
> Watchdog sau đó `wait(timeout)` → nếu không thoát → `kill()`.

### Shutdown Config

```json
{
  "shutdown": {
    "on_shutdown_positions": "leave",
    "grace_period_sec": 30
  }
}
```

| Param | Values | Mô tả |
|---|---|---|
| `on_shutdown_positions` | `"leave"` / `"force_close"` | Xử lý pairs đang OPEN khi shutdown |
| `grace_period_sec` | 30 | Timeout cho Brain (phase 1). Recon=10s, MT5=15s |

### Tại sao thứ tự này?

```
1. Brain tắt TRƯỚC   → không tạo pair mới, không gửi order mới
2. Reconciler tắt     → không heal nữa (Brain đã tắt, heal vô nghĩa)
3. MT5 tắt SAU CÙNG  → vẫn xử lý nốt order đang chạy
4. Watchdog tắt cuối  → đảm bảo mọi thứ clean
```

> [!WARNING]
> **KHÔNG được tắt MT5 Process trước Brain.** Nếu Brain đang đóng lệnh mà MT5 chết → orphan.
> Approach A (Watchdog điều phối) đảm bảo thứ tự này bằng sequential terminate + wait.

---

## Crash Recovery (khi restart sau crash)

Khi hệ thống restart sau crash (không có graceful shutdown):

```
1. [ENGINE] Load DB → tìm pairs status NOT IN ('CLOSED','FAILED','HEALED','FORCE_CLOSED')
2. [ENGINE] Log: "Recovering N pairs from previous session"
3. [BRAIN]  Warmup phase bình thường
4. [BRAIN]  Sau warmup → resume quản lý pairs đang OPEN (close khi signal)
5. [RECON]  First cycle → detect STALE states (ENTRY_SENT/CLOSE_SENT quá lâu) → auto-heal
```

**Pairs PENDING/ENTRY_SENT khi crash**: Reconciler check sàn thật → recover hoặc FAILED
**Pairs CLOSE_SENT khi crash**: Reconciler check → CLOSE_*_DONE hoặc CLOSED
**Pairs OPEN khi crash**: Brain quản lý bình thường sau warmup

---

## Startup Readiness Checklist

| # | Check | Verify bằng | Fail → |
|---|---|---|---|
| 1 | Redis alive | `redis.ping()` | Exit, cannot run |
| 2 | MT5 connected | `mt5:heartbeat` exists | Wait/retry |
| 3 | Exchange WS connected | `exchange.is_connected` | Wait/retry |
| 4 | Exchange auth alive | `exchange.check_auth_alive()` | Alert, exit |
| 5 | DB accessible | `Database.__init__` no error | Exit |
| 6 | Config valid | `Config.__init__` no error | Exit |
| 7 | Trading time | Schedule check | Wait (no exit) |
| 8 | Warmup complete | `_is_warmed_up` flag | Wait (no exit) |
