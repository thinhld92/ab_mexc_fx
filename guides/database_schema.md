# Database Schema â€” SQLite

> File: `data/arb.db` (relative to project root)
> Mode: WAL (Write-Ahead Logging) for concurrent read/write

## Table: `pairs` â€” Sá»• cÃ¡i chÃ­nh

Má»—i cáº·p arbitrage (MT5 + MEXC) = 1 row. Track toÃ n bá»™ lifecycle tá»« PENDING â†’ CLOSED.

```sql
CREATE TABLE IF NOT EXISTS pairs (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    pair_token      TEXT UNIQUE NOT NULL,
    direction       TEXT NOT NULL,             -- 'LONG' / 'SHORT'
    status          TEXT NOT NULL DEFAULT 'PENDING',
                    -- See Status Lifecycle below for full state machine

    -- Entry data (all times are UTC epoch)
    entry_time      REAL NOT NULL,              -- UTC epoch
    entry_spread    REAL,

    -- MT5 side
    mt5_ticket      INTEGER,
    mt5_action      TEXT,                      -- 'BUY' / 'SELL'
    mt5_volume      REAL,
    mt5_entry_price REAL,
    mt5_close_price REAL,
    mt5_profit      REAL DEFAULT 0,
    mt5_entry_latency_ms  REAL,
    mt5_close_latency_ms  REAL,

    -- Exchange side (generic)
    exchange_order_id        TEXT,
    exchange_close_order_id  TEXT,
    exchange_volume          INTEGER,
    exchange_side            INTEGER,
    exchange_entry_latency_ms REAL,
    exchange_close_latency_ms REAL,

    -- Close data (UTC epoch)
    close_time      REAL,                       -- UTC epoch
    close_spread    REAL,
    close_reason    TEXT,                       -- SIGNAL / HOLD_TIMEOUT / FORCE_SCHEDULE / FORCE_SHUTDOWN / HEAL_* / MANUAL

    -- Config snapshot at entry
    conf_dev_entry  REAL,
    conf_dev_close  REAL,

    -- Reconciliation
    recon_status    TEXT DEFAULT 'PENDING',     -- PENDING / OK / MISMATCH / ORPHAN_MT5 / ORPHAN_EXCHANGE
    recon_note      TEXT,
    last_recon_at   REAL,                       -- UTC epoch

    created_at      DATETIME DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    updated_at      DATETIME DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);
```

### Status Lifecycle (State Machine)

#### Entry Flow

```
PENDING â”€â”€â”€â”€â–¶ ENTRY_SENT â”€â”€â”¬â”€â”€ both fill â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â–¶ OPEN
                         â”‚
                         â”œâ”€â”€ MT5 fill first â”€â”€â”€â”€â”€â–¶ ENTRY_MT5_FILLED
                         â”‚                              â”œâ”€ Exchange fill â–¶ OPEN
                         â”‚                              â””â”€ Exchange fail â–¶ ORPHAN_MT5
                         â”‚
                         â”œâ”€â”€ Exchange fill first â”€â–¶ ENTRY_EXCHANGE_FILLED
                         â”‚                              â”œâ”€ MT5 fill â–¶ OPEN
                         â”‚                              â””â”€ MT5 fail â–¶ ORPHAN_EXCHANGE
                         â”‚
                         â””â”€â”€ both fail â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â–¶ FAILED
```

#### Close Flow

```
OPEN â”€â”€â”€â”€â–¶ CLOSE_SENT â”€â”€â”¬â”€â”€ both close â”€â”€â”€â”€â”€â”€â”€â”€â–¶ CLOSED
                        â”‚
                        â”œâ”€â”€ MT5 close first â”€â”€â”€â–¶ CLOSE_MT5_DONE
                        â”‚                            â”œâ”€ Exchange close â–¶ CLOSED
                        â”‚                            â””â”€ Exchange fail â–¶ ORPHAN_EXCHANGE
                        â”‚
                        â”œâ”€â”€ Exchange close first â–¶ CLOSE_EXCHANGE_DONE
                        â”‚                            â”œâ”€ MT5 close â–¶ CLOSED
                        â”‚                            â””â”€ MT5 fail â–¶ ORPHAN_MT5
                        â”‚
                        â””â”€â”€ timeout â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â–¶ Reconciler xá»­ lÃ½

ORPHAN_* â”€â”€â–¶ HEALING â”€â”€â”¬â”€ OK â”€â–¶ HEALED
                       â””â”€ fail â–¶ ORPHAN_* (retry)

OPEN/CLOSE_SENT â”€â”€â–¶ FORCE_CLOSED  (blackout/shutdown)
```

### All Statuses (15)

| # | Status | Giai Ä‘oáº¡n | MÃ´ táº£ | Position? |
|---|---|---|---|---|
| 1 | `PENDING` | Entry | Pair táº¡o, chÆ°a gá»­i lá»‡nh | âŒ ChÆ°a |
| 2 | `ENTRY_SENT` | Entry | Orders dispatched cáº£ 2 bÃªn, chá» fill | â“ Äang chá» |
| 3 | `ENTRY_MT5_FILLED` | Entry | MT5 fill OK, chá» Exchange | âš ï¸ Chá»‰ MT5 |
| 4 | `ENTRY_EXCHANGE_FILLED` | Entry | Exchange fill OK, chá» MT5 | âš ï¸ Chá»‰ Exchange |
| 5 | `OPEN` | Active | Cáº£ 2 bÃªn fill, Ä‘ang active | âœ… Cáº£ 2 |
| 6 | `CLOSE_SENT` | Close | Close dispatched cáº£ 2 bÃªn | âœ… Äang Ä‘Ã³ng |
| 7 | `CLOSE_MT5_DONE` | Close | MT5 Ä‘Ã³ng OK, chá» Exchange | âš ï¸ Chá»‰ Exchange |
| 8 | `CLOSE_EXCHANGE_DONE` | Close | Exchange Ä‘Ã³ng OK, chá» MT5 | âš ï¸ Chá»‰ MT5 |
| 9 | `CLOSED` | Terminal | HoÃ n táº¥t cáº£ 2 bÃªn | âŒ KhÃ´ng |
| 10 | `FAILED` | Terminal | Entry fail cáº£ 2 bÃªn | âŒ KhÃ´ng |
| 11 | `ORPHAN_MT5` | Error | Chá»‰ MT5 cÃ³ position | âš ï¸ Chá»‰ MT5 |
| 12 | `ORPHAN_EXCHANGE` | Error | Chá»‰ Exchange cÃ³ position | âš ï¸ Chá»‰ Exchange |
| 13 | `HEALING` | Recovery | Reconciler Ä‘ang sá»­a | âš ï¸ Äang xá»­ lÃ½ |
| 14 | `HEALED` | Terminal | Orphan Ä‘Ã£ Ä‘Æ°á»£c Ä‘Ã³ng | âŒ KhÃ´ng |
| 15 | `FORCE_CLOSED` | Terminal | ÄÃ³ng kháº©n cáº¥p | âŒ KhÃ´ng |

### `close_reason` values

| Value | Nguá»“n Ä‘Ã³ng |
|---|---|
| `SIGNAL` | Brain detect close signal (spread há»™i tá»¥) |
| `HOLD_TIMEOUT` | Giá»¯ lá»‡nh quÃ¡ `hold_time_sec` |
| `FORCE_SCHEDULE` | Force close theo schedule |
| `FORCE_SHUTDOWN` | Force close khi shutdown |
| `HEAL_ORPHAN_MT5` | Reconciler Ä‘Ã³ng orphan MT5 |
| `HEAL_ORPHAN_EXCHANGE` | Reconciler Ä‘Ã³ng orphan Exchange |
| `HEAL_GHOST` | Reconciler Ä‘Ã³ng ghost position |
| `MANUAL` | ÄÃ³ng thá»§ cÃ´ng |

### All Transitions

| From | To | Trigger |
|---|---|---|
| — | `PENDING` | Brain detect signal, INSERT pair (write-ahead) |
| `PENDING` | `ENTRY_SENT` | Brain dispatch orders cả 2 bên |
| `PENDING` | `FAILED` | Reconciler stale pending: không có position nào |
| `ENTRY_SENT` | `ENTRY_MT5_FILLED` | MT5 result: success |
| `ENTRY_SENT` | `ENTRY_EXCHANGE_FILLED` | Exchange result: FILLED |
| `ENTRY_SENT` | `OPEN` | Both fill gần đồng thời |
| `ENTRY_SENT` | `FAILED` | Both fail |
| `ENTRY_MT5_FILLED` | `OPEN` | Exchange fill OK |
| `ENTRY_MT5_FILLED` | `ORPHAN_MT5` | Exchange fail/REJECTED |
| `ENTRY_EXCHANGE_FILLED` | `OPEN` | MT5 fill OK |
| `ENTRY_EXCHANGE_FILLED` | `ORPHAN_EXCHANGE` | MT5 fail |
| `OPEN` | `CLOSE_SENT` | Brain detect close signal / hold_timeout / force |
| `OPEN` | `ORPHAN_EXCHANGE` | Position monitor: MT5 ticket gone (stop-out) â†’ Exchange survives |
| `OPEN` | `ORPHAN_MT5` | Exchange WS: position gone (liquidation) â†’ MT5 survives |
| `OPEN` | `FORCE_CLOSED` | Blackout / shutdown |
| `CLOSE_SENT` | `CLOSE_MT5_DONE` | MT5 close success |
| `CLOSE_SENT` | `CLOSE_EXCHANGE_DONE` | Exchange close success |
| `CLOSE_SENT` | `CLOSED` | Both close gáº§n Ä‘á»“ng thá»i |
| `CLOSE_SENT` | `FORCE_CLOSED` | Force close during closing |
| `CLOSE_MT5_DONE` | `CLOSED` | Exchange close OK |
| `CLOSE_MT5_DONE` | `ORPHAN_EXCHANGE` | Exchange close fail |
| `CLOSE_EXCHANGE_DONE` | `CLOSED` | MT5 close OK |
| `CLOSE_EXCHANGE_DONE` | `ORPHAN_MT5` | MT5 close fail |
| `ORPHAN_MT5` | `HEALING` | Reconciler báº¯t Ä‘áº§u auto-heal |
| `ORPHAN_EXCHANGE` | `HEALING` | Reconciler báº¯t Ä‘áº§u auto-heal |
| `HEALING` | `HEALED` | ÄÃ³ng orphan thÃ nh cÃ´ng |
| `HEALING` | `ORPHAN_*` | ÄÃ³ng orphan tháº¥t báº¡i |

## Table: `pair_events` â€” Audit Trail

Má»i thay Ä‘á»•i tráº¡ng thÃ¡i Ä‘Æ°á»£c ghi láº¡i. DÃ¹ng Ä‘á»ƒ trace láº¡i chuyá»‡n gÃ¬ xáº£y ra.

```sql
CREATE TABLE IF NOT EXISTS pair_events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    pair_token  TEXT NOT NULL,
    event_type  TEXT NOT NULL,
    event_data  TEXT,                           -- JSON chi tiáº¿t
    created_at  DATETIME DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);
```

### Event Types

| Event | Khi nÃ o | Transition |
|---|---|---|
| `CREATED` | Pair vá»«a táº¡o | â†’ PENDING |
| `ENTRY_DISPATCHED` | Brain gá»­i orders cáº£ 2 bÃªn | PENDING â†’ ENTRY_SENT |
| `MT5_FILLED` | MT5 order fill thÃ nh cÃ´ng | ENTRY_SENT â†’ ENTRY_MT5_FILLED |
| `EXCHANGE_FILLED` | Exchange order fill thÃ nh cÃ´ng | ENTRY_SENT â†’ ENTRY_EXCHANGE_FILLED |
| `ENTRY_COMPLETE` | Cáº£ 2 bÃªn Ä‘Ã£ fill | â†’ OPEN |
| `ENTRY_FAILED` | Cáº£ 2 bÃªn fail | â†’ FAILED |
| `ENTRY_PARTIAL` | 1 bÃªn fill, 1 bÃªn fail | â†’ ORPHAN_* |
| `CLOSE_DISPATCHED` | Brain gá»­i close orders | OPEN â†’ CLOSE_SENT |
| `MT5_CLOSED` | MT5 position Ä‘Ã£ Ä‘Ã³ng | CLOSE_SENT â†’ CLOSE_MT5_DONE |
| `EXCHANGE_CLOSED` | Exchange position Ä‘Ã£ Ä‘Ã³ng | CLOSE_SENT â†’ CLOSE_EXCHANGE_DONE |
| `CLOSE_COMPLETE` | Cáº£ 2 bÃªn Ä‘Ã£ Ä‘Ã³ng | â†’ CLOSED |
| `CLOSE_PARTIAL_FAIL` | 1 bÃªn Ä‘Ã³ng OK, 1 bÃªn fail | CLOSE_*_DONE â†’ ORPHAN_* |
| `STOPOUT_DETECTED` | Position monitor: MT5 ticket gone | OPEN â†’ ORPHAN_EXCHANGE |
| `LIQUIDATION_DETECTED` | Exchange WS: position gone | OPEN â†’ ORPHAN_MT5 |
| `RECON_OK` | Reconciler verify OK | â€” |
| `RECON_MISMATCH` | Reconciler phÃ¡t hiá»‡n lá»‡ch | â†’ ORPHAN_* |
| `HEAL_START` | Reconciler báº¯t Ä‘áº§u auto-heal | â†’ HEALING |
| `HEAL_SUCCESS` | Auto-heal thÃ nh cÃ´ng | â†’ HEALED |
| `HEAL_FAILED` | Auto-heal tháº¥t báº¡i | â†’ ORPHAN_* |
| `FORCE_CLOSE` | Blackout/shutdown | â†’ FORCE_CLOSED |

## Table: `recon_log` â€” Reconciliation Log

Ghi láº¡i má»—i láº§n Reconciler cháº¡y, káº¿t quáº£ so khá»›p.

```sql
CREATE TABLE IF NOT EXISTS recon_log (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    run_at          REAL NOT NULL,              -- UTC epoch
    mt5_positions   INTEGER,
    exchange_positions INTEGER,
    local_pairs     INTEGER,
    mismatches      INTEGER DEFAULT 0,
    actions_taken   TEXT,                       -- JSON
    created_at      DATETIME DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);
```

## Indexes

```sql
CREATE INDEX IF NOT EXISTS idx_pairs_status ON pairs(status);
CREATE INDEX IF NOT EXISTS idx_pairs_token ON pairs(pair_token);
CREATE INDEX IF NOT EXISTS idx_events_token ON pair_events(pair_token);
CREATE INDEX IF NOT EXISTS idx_events_type ON pair_events(event_type);
```

## DB Write Ownership â€” Ai ghi gÃ¬?

> [!IMPORTANT]
> Chá»‰ process Ä‘Æ°á»£c chá»‰ Ä‘á»‹nh má»›i Ä‘Æ°á»£c write vÃ o table/event tÆ°Æ¡ng á»©ng. Vi pháº¡m = race condition.

### Báº£ng `pairs` â€” INSERT & UPDATE

| Thao tÃ¡c | Process | Khi nÃ o | Ghi gÃ¬ |
|---|---|---|---|
| `INSERT` | **Brain** | Detect entry signal | `pair_token`, `direction`, `status=PENDING`, `entry_time`, `entry_spread`, `conf_dev_*` |
| `UPDATE` â†’ ENTRY_SENT | **Brain** | Dispatch orders cáº£ 2 bÃªn | `status` |
| `UPDATE` â†’ ENTRY_MT5_FILLED | **Brain** | MT5 fill OK | `mt5_ticket`, `mt5_action`, `mt5_volume`, `mt5_entry_price`, `mt5_entry_latency_ms` |
| `UPDATE` â†’ ENTRY_EXCHANGE_FILLED | **Brain** | Exchange fill OK | `exchange_order_id`, `exchange_volume`, `exchange_side`, `exchange_entry_latency_ms` |
| `UPDATE` â†’ OPEN | **Brain** | Cáº£ 2 bÃªn fill | Remaining side fields |
| `UPDATE` → FAILED | **Brain** | Cả 2 bên fail | `status` |
| `UPDATE` → FAILED | **Reconciler** | Stale `PENDING`/`ENTRY_SENT` và không có position nào | `status`, `recon_status`, `recon_note`, `last_recon_at` |
| `UPDATE` → ORPHAN_* | **Brain** | 1 bên fill, 1 bên fail | Filled side fields + `status` |
| `UPDATE` â†’ CLOSE_SENT | **Brain** | Detect close signal | `status`, `close_reason` |
| `UPDATE` â†’ CLOSE_MT5_DONE | **Brain** | MT5 close OK | `mt5_close_price`, `mt5_profit`, `mt5_close_latency_ms` |
| `UPDATE` â†’ CLOSE_EXCHANGE_DONE | **Brain** | Exchange close OK | `exchange_close_order_id`, `exchange_close_latency_ms` |
| `UPDATE` â†’ CLOSED | **Brain** | Cáº£ 2 bÃªn close | `close_time`, `close_spread`, remaining close fields |
| `UPDATE` â†’ FORCE_CLOSED | **Brain** | Blackout/shutdown | `close_time`, `close_reason` |
| `UPDATE` â†’ ORPHAN_* | **Brain** | Stop-out/liquidation (OPEN) | `status` |
| `UPDATE` â†’ ORPHAN_* | **Reconciler** | Detect position mismatch | `status`, `recon_status`, `recon_note`, `last_recon_at` |
| `UPDATE` â†’ HEALING | **Reconciler** | Báº¯t Ä‘áº§u auto-heal | `status`, `recon_status`, `last_recon_at` |
| `UPDATE` â†’ HEALED | **Reconciler** | Heal thÃ nh cÃ´ng | `status`, `recon_status`, `recon_note`, `last_recon_at`, `close_reason` |
| `UPDATE` recon_status | **Reconciler** | Má»—i láº§n verify | `recon_status`, `recon_note`, `last_recon_at` |

### Báº£ng `pair_events` â€” INSERT only

| Event | Process | Khi nÃ o | `event_data` JSON |
|---|---|---|---|
| `CREATED` | **Brain** | INSERT pair vÃ o DB | `{spread, direction, conf_dev_entry, conf_dev_close}` |
| `ENTRY_DISPATCHED` | **Brain** | Dispatch orders | `{mt5_job_id}` |
| `MT5_FILLED` | **Brain** | MT5 order result success | `{ticket, price, volume, action, latency_ms}` |
| `EXCHANGE_FILLED` | **Brain** | Exchange order result success | `{order_id, order_status, fill_price, volume, side, latency_ms}` |
| `ENTRY_COMPLETE` | **Brain** | Cáº£ 2 bÃªn fill â†’ OPEN | `{mt5_ticket, exchange_order_id}` |

| UPDATE → FAILED | **Reconciler** | Stale PENDING/ENTRY_SENT và không có position nào | status, 
econ_status, 
econ_note, last_recon_at |
| `ENTRY_PARTIAL` | **Brain** | 1 bÃªn fail â†’ ORPHAN_* | `{filled_side, error_side, error, error_category}` |
| `CLOSE_DISPATCHED` | **Brain** | Dispatch close | `{spread, close_reason}` |
| `MT5_CLOSED` | **Brain** | MT5 close success | `{ticket, close_price, profit, latency_ms}` |
| `EXCHANGE_CLOSED` | **Brain** | Exchange close success | `{order_id, order_status, latency_ms}` |
| `CLOSE_COMPLETE` | **Brain** | Cáº£ 2 bÃªn close â†’ CLOSED | `{mt5_profit, close_spread}` |
| `CLOSE_PARTIAL_FAIL` | **Brain** | 1 bÃªn close fail â†’ ORPHAN_* | `{closed_side, error_side, error, error_category}` |
| `STOPOUT_DETECTED` | **Brain** | Position monitor: ticket gone | `{mt5_ticket}` |
| `LIQUIDATION_DETECTED` | **Brain** | Exchange WS: position gone | `{direction}` |
| `FORCE_CLOSE` | **Brain** | Blackout/shutdown | `{close_reason}` |
| `RECON_OK` | **Reconciler** | Verify pair OK | `{run_id}` |
| `RECON_MISMATCH` | **Reconciler** | PhÃ¡t hiá»‡n lá»‡ch | `{expected, actual, detail}` |
| `HEAL_START` | **Reconciler** | Báº¯t Ä‘áº§u auto-heal | `{orphan_type, action}` |
| `HEAL_SUCCESS` | **Reconciler** | Heal thÃ nh cÃ´ng | `{orphan_type, closed_ticket_or_order_id}` |
| `HEAL_FAILED` | **Reconciler** | Heal tháº¥t báº¡i | `{orphan_type, error}` |

### Báº£ng `recon_log` â€” INSERT only

| Process | Khi nÃ o | Ghi gÃ¬ |
|---|---|---|
| **Reconciler** | Má»—i cycle (30-60s) | `run_at`, `mt5_positions`, `exchange_positions`, `local_pairs`, `mismatches`, `actions_taken` |

### TÃ³m táº¯t Process â†’ Table

| Process | `pairs` | `pair_events` | `recon_log` |
|---|---|---|---|
| **Brain** | INSERT + UPDATE (entry/close/force/stopout) | INSERT (15 event types) | âŒ KhÃ´ng |
| **Reconciler** | UPDATE (recon/heal status) | INSERT (5 event types) | INSERT |
| **MT5 Process** | âŒ KhÃ´ng | âŒ KhÃ´ng | âŒ KhÃ´ng |
| **Watchdog** | âŒ KhÃ´ng | âŒ KhÃ´ng | âŒ KhÃ´ng |

> [!WARNING]
> **MT5 Process vÃ  Watchdog KHÃ”NG BAO GIá»œ write vÃ o DB.** Chá»‰ Brain + Reconciler cÃ³ DB access.

---

## Useful Queries

```sql
-- Pairs Ä‘ang má»Ÿ
SELECT * FROM pairs WHERE status = 'OPEN';

-- Profit theo ngÃ y
SELECT date(entry_time, 'unixepoch') as day, 
       SUM(mt5_profit) as total_profit, 
       COUNT(*) as trades
FROM pairs WHERE status = 'CLOSED' GROUP BY 1 ORDER BY 1 DESC;

-- Win rate
SELECT 
  COUNT(CASE WHEN mt5_profit > 0 THEN 1 END) * 100.0 / COUNT(*) as win_rate
FROM pairs WHERE status = 'CLOSED';

-- Orphan history
SELECT * FROM pairs WHERE status LIKE 'ORPHAN%';

-- Timeline cá»§a 1 pair
SELECT * FROM pair_events WHERE pair_token = 'xxx' ORDER BY created_at;

-- Reconciliation history
SELECT * FROM recon_log ORDER BY run_at DESC LIMIT 20;

-- Average latency
SELECT 
  AVG(mt5_entry_latency_ms) as avg_mt5_lat,
  AVG(exchange_entry_latency_ms) as avg_exchange_lat
FROM pairs WHERE status = 'CLOSED';
```





