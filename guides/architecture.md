# System Architecture — Arbitrage Bot v2

> Last updated: 2026-03-26

## Overview

Hệ thống arbitrage chênh lệch giá giữa **MT5 (FX broker)** và **Crypto Futures exchange**.
Mục tiêu: chạy 24/7 tự động, không cần giám sát.
Cùng 1 codebase, deploy trên nhiều VPS với exchange khác nhau (MEXC, OKX, Binance...).

## Process Architecture

```
┌──────────────────────────────────────────────────────────┐
│                    WATCHDOG (parent)                      │
│    Monitor heartbeat, auto-restart child processes        │
├──────────────────────────────────────────────────────────┤
│                                                           │
│  ┌────────────────┐  ┌──────────────────────────────┐    │
│  │  MT5 PROCESS   │  │        CORE ENGINE            │    │
│  │                │  │                               │    │
│  │  Tick Thread ──SET──▶ Brain (asyncio)              │    │
│  │  (poll 1ms)    │  │   - GET tick (Redis)          │    │
│  │                │  │   - Tính spread               │    │
│  │  Order Thread ◀LIST── Send order (Redis)          │    │
│  │  (execute)  ──LIST──▶ Get result (Redis)          │    │
│  │                │  │                               │    │
│  │  Heartbeat ──SET──▶                               │    │
│  └────────────────┘  │   ExchangeHandler (interface) │    │
│                      │   ┌─────────────────────────┐ │    │
│                      │   │ MEXC │ OKX │ Binance... │ │    │
│                      │   └─────────────────────────┘ │    │
│                      │   - WS tick                   │    │
│                      │   - REST order                │    │
│                      │                               │    │
│                      │   INSERT/UPDATE ▼             │    │
│                      │         SQLite.db              │    │
│                      └──────────────────────────────┘    │
│                                                           │
│  ┌──────────────────────────────────┐                    │
│  │         RECONCILER               │                    │
│  │   Mỗi 30-60s:                   │                    │
│  │   Query MT5 + Exchange + SQLite  │                    │
│  │   Diff → Auto-heal → Log        │                    │
│  └──────────────────────────────────┘                    │
│                                                           │
│  Telegram (alerts)                                        │
└──────────────────────────────────────────────────────────┘
```

## Component Responsibilities

| Component | Process | Nhiệm vụ |
|---|---|---|
| **Watchdog** | Parent | Monitor heartbeat, auto-restart processes đã chết |
| **MT5 Process** | Child 1 | 2 threads + `threading.Lock`: Tick polling (1ms) + Order execution + **Position monitor (30ms, chỉ khi có tracked tickets)** |
| **Core Engine** | Child 2 | Brain (asyncio): đọc tick, tính spread, phát hiện signal, đặt lệnh |
| **ExchangeHandler** | Trong Core | Abstract interface — MEXC/OKX/Binance implement riêng |
| **Reconciler** | Child 3 | ExchangeHandler riêng (REST-only). So khớp positions local vs sàn thật, auto-heal orphan |
| **SQLite DB** | File | Sổ cái, state, audit trail, analytics |

## Exchange Abstraction

```
src/exchanges/
  ├── base.py              ← Abstract ExchangeHandler interface
  ├── mexc_handler.py      ← MEXC Futures (MD5 signature bypass)
  ├── okx_handler.py       ← OKX Futures (future)
  └── binance_handler.py   ← Binance Futures (future)
```

**Interface methods:**
- `connect()` / `stop()` — lifecycle
- `run_ws()` — WebSocket tick stream
- `get_latest_tick() → {bid, ask, last, ts, local_ts}`
- `place_order(side, volume) → OrderResult` — xem `exchange_standards.md` Section 4
  - `{success, order_status, order_id, fill_price, fill_volume, error, error_category, latency_ms, raw_response}`
- `get_positions() → list[ExchangePosition]`
- `check_auth_alive() → bool`
- `validate_volume(volume) → bool`
- `get_volume_info() → {min, max, step, unit_name}`

**Config chọn exchange:**
```json
{"exchange": {"name": "mexc", "symbol": "XAUT_USDT", ...}}
```

## Data Flow

### Tick Flow (Event-driven)

```
MT5 Terminal → mt5.symbol_info_tick() → Tick Thread (threading.Lock)
  → Redis SET "mt5:tick"      ← latest snapshot, overwrite mỗi tick mới
  → Redis PUBLISH "tick:mt5"  ← real-time event, push cho subscribers
  → Position monitor mỗi `position_check_interval_ms` (default **30ms**), CHỈ khi mt5:tracked_tickets non-empty:
    → mt5.positions_get(ticket=X) (threading.Lock)
    → Nếu position mất → PUBLISH "mt5:position:gone"

Exchange WS → push.ticker → ExchangeHandler
  → Redis SET "exchange:tick" ← latest snapshot
  → callback Brain.on_exchange_tick()  ← direct in-process, không qua PUB/SUB
```

#### SET vs PUBLISH — Tick Semantics

| Mechanism | Key | Semantic | Ai dùng | Đặc điểm |
|---|---|---|---|---|
| **SET** (overwrite) | `mt5:tick`, `exchange:tick` | **Latest snapshot** — luôn có giá trị mới nhất, đọc bất kỳ lúc nào | Spread Monitor (poll 5s), Reconciler (backup), Brain (fallback khi cần re-read) | Không mất data nếu reader chậm/offline. Reader luôn thấy tick gần nhất. |
| **PUBLISH** (fire-and-forget) | `tick:mt5` | **Real-time event** — "có tick mới, xử lý ngay" | Brain (SUB, event-driven loop) | Nếu không có subscriber → message mất. Không lưu history. Zero-latency push. |

> [!NOTE]
> **Tại sao cần cả hai?**
> - **SET alone** không đủ: Brain phải poll liên tục để biết có tick mới → tốn CPU, latency cao.
> - **PUBLISH alone** không đủ: Nếu Brain restart giữa chừng → mất tick cuối cùng, không có gì để đọc lại. Spread Monitor cần đọc tick bất kỳ lúc nào (poll 5s), không cần subscribe.
> - **Cả hai**: PUBLISH để Brain react ngay (event-driven), SET để mọi reader khác đọc khi cần (poll-based).
>
> **Exchange tick** chỉ dùng SET (không PUBLISH) vì ExchangeHandler chạy **trong cùng process** với Brain — gọi callback trực tiếp, không cần Redis PUB/SUB.

> [!NOTE]
> **MT5 API không thread-safe.** Tất cả MT5 API calls đều phải đi qua chung 1 `threading.Lock`.
>
> **Lock contention analysis:**
> | Call | Thread | Duration | Frequency |
> |---|---|---|---|
> | `symbol_info_tick()` | Tick | ~0.1ms | Mỗi 1ms |
> | `positions_get()` | Tick | ~0.1ms | Mỗi 30ms (khi có tracked) |
> | `order_send()` | Order | 100-500ms | Hiếm (chỉ khi entry/close) |
> | `positions_get()` | Order | ~0.5ms | Mỗi 30s (reconciler query) |
>
> Khi `order_send` giữ lock (100-500ms) → tick + position check bị delay. **Chấp nhận được** vì đang chủ động đặt lệnh. Không cần priority mechanism.
>
> **Chỉ MT5 Process được gọi MT5 API.** Brain/Reconciler/Watchdog KHÔNG bao giờ gọi trực tiếp — luôn qua Redis IPC:
> - Brain đặt lệnh → `LPUSH mt5:order:queue`
> - Reconciler query positions → `LPUSH mt5:cmd:query_positions` (request) → `BRPOP mt5:positions:response` (response, timeout 10s, verify `query_id`)

### Order Flow (Write-ahead + Reliable Queue)
```
Brain detect signal
  → INSERT pairs (PENDING) + event CREATED     ← write-ahead
  → UPDATE status = ENTRY_SENT
  → LPUSH "mt5:order:queue" (MT5 command)
  → await exchange.place_order() (Exchange REST)
  → await asyncio.to_thread(redis.brpop, "mt5:order:result")
  → UPDATE pairs (ENTRY_MT5_FILLED / ENTRY_EXCHANGE_FILLED / OPEN / FAILED)

MT5 Order Thread (reliable queue):
  → BRPOPLPUSH "mt5:order:queue" "mt5:order:processing" ← at-least-once
  → execute mt5.order_send()
  → LPUSH "mt5:order:result"
  → LREM "mt5:order:processing" (xóa sau khi xong)

MT5 restart recovery:
  → Check "mt5:order:processing" for unfinished commands
  → Verify MT5 positions → send result or re-execute
```

### Signal Detection (Debounce)
```
on_tick(mt5 or exchange):            ← được gọi mỗi khi BẤT KỲ bên nào có tick mới
  spread = calculate(latest_mt5, latest_exchange)
  signal = detect(spread)
  if signal (ENTRY/CLOSE) và cùng signal đang debounce:
    timer TIẾP TỤC (không reset)     ← chỉ cancel khi signal THAY ĐỔI
  if signal mới khác:
    cancel timer cũ, start timer mới (stable_time_ms)
  if signal NONE:
    cancel timer                       ← signal mất rồi
  if risk event (stop-out/liquidation):
    IMMEDIATE execute                  ← không debounce
```

### Pre-execution Gate Check (sau khi debounce timer fire)

> [!IMPORTANT]
> **Giữa 300ms debounce, mọi thứ có thể thay đổi** (stop-out, Reconciler đổi state, tick stale).
> PHẢI re-check TẤT CẢ điều kiện trước khi execute.

```
debounce_timer_fired:
  ── Gate 1: Signal còn đúng? ──
  recalculate spread → detect signal
  if signal ≠ original_signal → ABORT

  ── Gate 2: Tick còn fresh? ──
  if tick.ts quá cũ (> max_tick_delay) → ABORT

  ── Gate 3: Risk flag? ──
  if _risk_flag (set bởi position:gone event) → ABORT

  ── Gate 4: Pair state OK? ── (close only)
  if pair.status ≠ OPEN (Reconciler có thể đổi) → ABORT

  ── Gate 5: Concurrency guard? ──
  if _is_executing (entry/close khác đang chạy) → ABORT

  ── ALL GATES ✅ → EXECUTE ──
```

### Reconciliation Flow
```
Reconciler (mỗi 30s):
  → Query MT5 positions (via Redis)
  → Query Exchange positions (via ExchangeHandler riêng)
  → SELECT pairs WHERE status NOT IN ('CLOSED','FAILED','HEALED','FORCE_CLOSED')
  → Diff 3 nguồn → auto-heal/alert
  → INSERT recon_log (SQLite)
```

### Spread Monitor (standalone tool)
```
src/tools/spread_monitor.py (process riêng, terminal riêng)
  mỗi 5s:
    mt5_tick  = redis.GET("mt5:tick")
    exch_tick = redis.GET("exchange:tick")
    spread    = mt5_tick.bid - exch_tick.ask
    print(time, mt5, exchange, spread, signal)
```

> [!NOTE]
> **ExchangeHandler tự SET `exchange:tick` mỗi khi nhận WS tick mới** (nhất quán với MT5 Process SET `mt5:tick`). Spread Monitor chỉ đọc Redis, không connect WS/MT5.

## Redis IPC Keys

| Key | Type | Producer | Consumer |
|---|---|---|---|
| `mt5:tick` | SET | MT5 Tick Thread | Spread Monitor, Reconciler (backup) |
| `exchange:tick` | SET | ExchangeHandler (on WS tick) | Spread Monitor |
| `tick:mt5` | PUBLISH | MT5 Tick Thread | Brain (event SUB) |
| `mt5:order:queue` | LIST | Brain | MT5 Order Thread |
| `mt5:order:processing` | LIST | MT5 Order Thread (BRPOPLPUSH) | MT5 restart recovery |
| `mt5:order:result` | LIST | MT5 Order Thread | Brain |
| `mt5:heartbeat` | SET + EX 15s | MT5 Process | Watchdog |
| `mt5:cmd:query_positions` | LIST | Reconciler (LPUSH) | MT5 Order Thread (BRPOP) |
| `mt5:positions:response` | LIST | MT5 Order Thread (LPUSH) | Reconciler (BRPOP, timeout 10s) |
| `mt5:tracked_tickets` | SET (sadd/srem) | Brain | MT5 Tick Thread |
| `mt5:position:gone` | PUBLISH | MT5 Tick Thread | Brain (immediate) |
| `core:cmd:shutdown` | SET | Watchdog | Core Engine |
| `recon:cmd:shutdown` | SET | Watchdog | Reconciler |
| `mt5:cmd:shutdown` | SET | Watchdog | MT5 Process |
| `brain:heartbeat` | SET + EX 30s | Brain | Watchdog |
| `recon:heartbeat` | SET + EX 60s | Reconciler | Watchdog |
| `SIGNAL:SHUTDOWN` | SET | stop_bots.py | Watchdog (triggers phased shutdown) |

## SQLite Tables

| Table | Mục đích |
|---|---|
| `pairs` | Sổ cái chính — mỗi cặp arbitrage = 1 row, track full lifecycle (15 statuses) |
| `pair_events` | Audit trail — mọi thay đổi trạng thái được ghi lại |
| `recon_log` | Log mỗi lần reconcile chạy |

### Pair Status Lifecycle

See [database_schema.md](file:///d:/thinhld/python/botcryptoabv1/guides/database_schema.md) for full state machine (15 states) and transition table.

```
Entry:  PENDING → ENTRY_SENT → ENTRY_*_FILLED → OPEN
Close:  OPEN → CLOSE_SENT → CLOSE_*_DONE → CLOSED
Error:  ORPHAN_* → HEALING → HEALED
Terminal: CLOSED / FAILED / HEALED / FORCE_CLOSED
```

## Redis Payload Schemas

All payloads are JSON serialized via `orjson`. All timestamps are UTC epoch (`time.time()`).

### `mt5:tick` — Latest MT5 tick (SET, overwrite)

```json
{
  "bid": 3245.50,
  "ask": 3245.80,
  "last": 3245.65,
  "time_msc": 1711425600000,
  "local_ts": 1711425600.123
}
```

| Field | Type | Mô tả |
|---|---|---|
| `bid` | float | Giá bid MT5 |
| `ask` | float | Giá ask MT5 |
| `last` | float | Giá last MT5 |
| `time_msc` | int | MT5 server time (ms) — chỉ dùng để detect tick mới |
| `local_ts` | float | UTC epoch lúc poll — dùng để check freshness |

---

### `mt5:order:queue` — Order command (LIST, LPUSH by Brain, BRPOP by MT5)

**Entry order:**
```json
{
  "action": "BUY",
  "volume": 0.01,
  "job_id": "a1b2c3d4",
  "comment": "arb_long",
  "ts": 1711425600.123
}
```

**Close order:**
```json
{
  "action": "CLOSE",
  "ticket": 12345678,
  "volume": 0.01,
  "job_id": "e5f6g7h8",
  "ts": 1711425600.456
}
```

| Field | Type | Mô tả |
|---|---|---|
| `action` | string | `"BUY"` / `"SELL"` / `"CLOSE"` |
| `volume` | float | Khối lượng MT5 (lot) |
| `job_id` | string | ID để match result (uuid hex 8 chars) |
| `ticket` | int | MT5 ticket (chỉ cho CLOSE) |
| `comment` | string | Comment cho lệnh (chỉ cho BUY/SELL) |
| `ts` | float | UTC epoch lúc gửi |

---

### `mt5:order:result` — Order result (LIST, LPUSH by MT5, BRPOP by Brain)

**Success (entry):**
```json
{
  "success": true,
  "job_id": "a1b2c3d4",
  "ticket": 12345678,
  "price": 3245.60,
  "volume": 0.01,
  "action": "BUY",
  "latency_ms": 125.5
}
```

**Success (close):**
```json
{
  "success": true,
  "job_id": "e5f6g7h8",
  "ticket": 12345678,
  "close_price": 3246.10,
  "volume": 0.01,
  "action": "CLOSE",
  "profit": 0.50,
  "latency_ms": 98.3
}
```

**Failure:**
```json
{
  "success": false,
  "job_id": "a1b2c3d4",
  "error": "retcode=10004: Requote",
  "retcode": 10004,
  "latency_ms": 200.1
}
```

| Field | Type | Mô tả |
|---|---|---|
| `success` | bool | Thành công hay không |
| `job_id` | string | Match với command gửi đi |
| `ticket` | int | MT5 order ticket |
| `price` / `close_price` | float | Giá fill |
| `volume` | float | Volume fill thực tế |
| `action` | string | `"BUY"` / `"SELL"` / `"CLOSE"` |
| `profit` | float | P&L (chỉ cho CLOSE) |
| `latency_ms` | float | Thời gian execute (ms) |
| `error` | string | Lý do fail (chỉ khi success=false) |
| `retcode` | int | MT5 retcode (chỉ khi success=false) |

---

### `mt5:heartbeat` — MT5 process heartbeat (SET + EX 15s)

```json
{
  "ts": 1711425600.123,
  "connected": true,
  "tick_count": 52340,
  "order_count": 5,
  "pid": 12345
}
```

| Field | Type | Mô tả |
|---|---|---|
| `ts` | float | UTC epoch lần heartbeat gần nhất |
| `connected` | bool | MT5 terminal connected? |
| `tick_count` | int | Tổng tick đã poll |
| `order_count` | int | Tổng order đã execute |
| `pid` | int | Process ID |

---

### `brain:heartbeat` — Brain heartbeat (SET + EX 30s)

```json
{
  "ts": 1711425600.456,
  "loop_count": 1230456,
  "open_pairs": 2,
  "exchange_connected": true,
  "pid": 12346
}
```

---

### `recon:heartbeat` — Reconciler heartbeat (SET + EX 60s)

```json
{
  "ts": 1711425600.789,
  "run_count": 120,
  "last_mismatches": 0,
  "pid": 12347
}
```

---

### `mt5:cmd:query_positions` — Position query request (LIST, LPUSH by Reconciler)

```json
{
  "query_id": "a1b2c3d4",
  "ts": 1711425600.000
}
```

Reconciler LPUSH request → MT5 Order Thread BRPOP → query `mt5.positions_get()` → LPUSH response.

---

### `mt5:positions:response` — Position query response (LIST, LPUSH by MT5)

```json
{
  "query_id": "a1b2c3d4",
  "ts": 1711425600.123,
  "positions": [
    {
      "ticket": 12345678,
      "type": "BUY",
      "volume": 0.01,
      "price": 3245.60,
      "profit": 0.25,
      "magic": 123456,
      "comment": "arb_long"
    }
  ]
}
```

Reconciler BRPOP với timeout 10s. Verify `query_id` khớp trước khi dùng.

> [!WARNING]
> **Nếu timeout hoặc `query_id` không khớp → MT5 Process có vấn đề.** Alert + skip cycle này.

---

### `SIGNAL:SHUTDOWN` — Shutdown signal (SET by stop_bots.py)

```json
"1"
```

Simple string. **Chỉ Watchdog** poll key này. Khi detect `"1"`, Watchdog set cờ riêng cho child:
`core:cmd:shutdown` → `recon:cmd:shutdown` → `mt5:cmd:shutdown`
(xem `startup_shutdown.md`).

```
