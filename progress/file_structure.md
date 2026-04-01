# File Structure — Architecture v2

> Created: 2026-03-26
> Status: Phase 6 complete in codebase, live exchange validation pending

---

## Project Tree (sau khi upgrade)

```
botcryptoabv1/
├── config.json                          # ⚙️ Config duy nhất (12 sections, hot-reload)
├── start.bat                            # ▶️ Khởi động hệ thống
├── stop_bots.py                         # ⏹️ Gửi SIGNAL:SHUTDOWN qua Redis
├── stop_bots.bat                        # ⏹️ Wrapper cho stop_bots.py
│
├── data/
│   └── arb.db                           # 💾 SQLite DB (auto-created, WAL mode)
│
├── guides/                              # 📖 Tài liệu kiến trúc (9 docs)
│   ├── architecture.md                  #    Sơ đồ, data flows, Redis keys
│   ├── database_schema.md               #    15 states, 26 transitions, 20 events
│   ├── exchange_standards.md            #    Interface standards, OrderResult
│   ├── config_spec.md                   #    Config fields, types, defaults
│   ├── reconciler.md                    #    10 scenarios xử lý
│   ├── order_lifecycle.md               #    Write-ahead, crash recovery
│   ├── startup_shutdown.md              #    5-phase startup, shutdown order
│   ├── required.md                      #    Requirements tổng quan
│   └── rules.md                         #    10 dev rules
│
├── progress/                            # 📋 Tiến trình upgrade
│   └── file_structure.md                #    File này
│
├── src/
│   ├── __init__.py
│   ├── launcher.py                      # 🚀 Entry point — tạo Watchdog, chạy hệ thống
│   │
│   ├── exchanges/                       # 🔄 Exchange Abstraction Layer
│   │   ├── __init__.py                  #    Factory: create_exchange(config) → ExchangeHandler
│   │   ├── base.py                      #    ABC interface + enums + OrderResult dataclass
│   │   │                                #    Base nhận redis dep, tự SET exchange:tick
│   │   ├── mexc_handler.py              #    MEXC Futures implementation (WEB token)
│   │   ├── okx_handler.py               #    [future] OKX implementation
│   │   └── binance_handler.py           #    [future] Binance implementation
│   │
│   ├── mt5_process/                     # 📊 MT5 Process (OS process riêng)
│   │   ├── __init__.py
│   │   ├── main.py                      #    Entry point: mt5.initialize(), threading.Lock,
│   │   │                                #    spawn 2 threads, heartbeat, shutdown listener
│   │   ├── tick_worker.py               #    Thread A: tick poll 1ms + position monitor 30ms
│   │   │                                #    (conditional) → PUBLISH mt5:position:gone
│   │   └── order_worker.py              #    Thread B: BRPOPLPUSH reliable queue
│   │                                    #    + query_id position query + startup recovery
│   │
│   ├── core/                            # 🧠 Core Trading Engine
│   │   ├── __init__.py
│   │   ├── engine.py                    #    Bootstrap: DB, Factory, PUB/SUB, wait MT5
│   │   │                                #    heartbeat, pass deps to TradingBrain
│   │   ├── trading_brain.py             #    Event-driven signal detection + debounce +
│   │   │                                #    5-gate pre-execution check + write-ahead DB +
│   │   │                                #    order dispatch
│   │   └── pair_manager.py              #    Pair state management: SQLite source of truth
│   │                                    #    + in-memory cache + mt5:tracked_tickets
│   │
│   ├── reconciler/                      # 🔍 Reconciler (OS process riêng)
│   │   ├── __init__.py
│   │   └── reconciler.py               #    10 scenarios, query_id, own ExchangeHandler
│   │                                    #    stale_entry 60s, stale_close 120s, auto-heal
│   │
│   ├── watchdog/                        # 🐕 Watchdog
│   │   └── watchdog.py                  #    Watchdog class: spawn processes, monitor
│   │                                    #    heartbeats (MT5:15s, Brain:30s, Recon:120s)
│   │                                    #    auto-restart, max 3/5min
│   │
│   ├── tools/                           # 🛠️ Standalone Tools
│   │   └── spread_monitor.py            #    Terminal riêng: spread + signal mỗi 5s
│   │
│   ├── storage/                         # 💾 Data Layer
│   │   ├── __init__.py
│   │   └── database.py                  #    SQLite manager: WAL, busy_timeout=5000
│   │                                    #    3 tables (pairs, pair_events, recon_log)
│   │                                    #    CRUD: insert_pair, update_pair, add_event...
│   │
│   ├── utils/                           # 🔧 Shared Utilities
│   │   ├── __init__.py
│   │   ├── config.py                    #    Config loader + hot-reload
│   │   ├── constants.py                 #    14 Redis keys + enums
│   │   ├── logger.py                    #    Colored console logger
│   │   └── spread_logger.py             #    CSV spread logging
│   │
│   └── services/                        # 🌐 External Services
│       └── telegram_bot.py              #    Alert notifications
│
├── tests/                               # 🧪 Tests
│   ├── test_database.py                 #    Unit: CRUD, state transitions (:memory:)
│   ├── test_exchange_base.py            #    Unit: OrderResult, enums, validation
│   ├── test_signal_detection.py         #    Unit: spread → signal logic
│   ├── test_gate_check.py               #    Unit: 5 gates abort/pass
│   └── test_mt5_ipc.py                  #    Integration: MT5 process + Redis
```

---

## Process Map

```
┌──────────────────────────────────────────────────────────────────┐
│ launcher.py → Watchdog (Parent)                                  │
│                                                                  │
│   ┌── MT5 Process ───┐  ┌── Core Engine ──┐  ┌── Reconciler ──┐│
│   │ mt5_process/      │  │ core/           │  │ reconciler/     ││
│   │ • 2 threads+Lock  │  │ • engine.py     │  │ • 10 scenarios  ││
│   │ • tick 1ms        │  │ • trading_brain │  │ • query_id      ││
│   │ • monitor 30ms    │  │ • pair_manager  │  │ • own Exchange  ││
│   │ • BRPOPLPUSH      │  │ • storage/db    │  │ • 30s cycle     ││
│   └──────────────────┘  └──────────────────┘  └───────────────┘│
│            │                      │                    │         │
│            └──────── Redis IPC ───┘────────────────────┘         │
└──────────────────────────────────────────────────────────────────┘

  ┌── Spread Monitor ──┐
  │ tools/              │  ← standalone, terminal riêng
  │ spread_monitor.py   │     python -m src.tools.spread_monitor
  └─────────────────────┘
```

---

## File Changes Summary

| Phase | Action | File | Purpose |
|---|---|---|---|
| 0 | NEW | `src/exchanges/base.py` | ABC + enums + OrderResult |
| 0 | NEW | `src/exchanges/__init__.py` | Factory `create_exchange()` |
| 0 | MOVE | `src/exchanges/mexc_handler.py` | MEXC implement interface |
| 1 | NEW | `src/storage/database.py` | SQLite 15-state, CRUD |
| 1 | NEW | `src/storage/__init__.py` | Package init |
| 1 | MODIFY | `src/utils/constants.py` | 14 Redis keys |
| 2 | NEW | `src/mt5_process/__init__.py` | Package init |
| 2 | NEW | `src/mt5_process/main.py` | MT5 entry + Lock + threads |
| 2 | NEW | `src/mt5_process/tick_worker.py` | Tick + position monitor |
| 2 | NEW | `src/mt5_process/order_worker.py` | Reliable queue + query |
| 3 | MODIFY | `src/core/engine.py` | Factory + DB + PUB/SUB |
| 3 | NEW | `src/core/trading_brain.py` | Event-driven + gates |
| 3 | NEW | `src/core/pair_manager.py` | SQLite + cache |
| 3 | DELETE | `src/core/brain.py` | Replaced by trading_brain |
| 3 | DELETE | `src/core/position_manager.py` | Replaced by pair_manager |
| 4 | NEW | `src/reconciler/__init__.py` | Package init |
| 4 | NEW | `src/reconciler/reconciler.py` | 10 scenarios + heal |
| 5 | NEW | `src/watchdog/watchdog.py` | Watchdog class |
| 5 | NEW | `src/tools/spread_monitor.py` | Spread terminal tool |
| 5 | MODIFY | `src/launcher.py` | Entry point → creates Watchdog |
| 5 | MODIFY | `config.json` | Add new sections |
| 6 | DELETE | `src/accountant/` | Replaced by storage/ |
| 6 | DELETE | `src/core/mexc_handler.py` | Moved to exchanges/ |
| 6 | DELETE | `src/core/mt5_handler.py` | Replaced by mt5_process/ |

**Total: 13 new, 4 modify, 7 delete**
