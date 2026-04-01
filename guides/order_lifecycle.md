# Order Lifecycle Safety â€” Chi tiáº¿t & Rá»§i ro

> TÃ i liá»‡u phÃ¢n tÃ­ch chi tiáº¿t lifecycle lá»‡nh, rá»§i ro máº¥t lá»‡nh, vÃ  giáº£i phÃ¡p.

---

## 1. Rá»§i ro máº¥t lá»‡nh (Lost Order Analysis)

### Current Flow (cÃ³ lá»— há»•ng)

```
Brain detect signal
  â†’ INSERT pairs (PENDING)
  â†’ LPUSH mt5:order:queue       â† â¶
  â†’ await exchange.place_order() â† â·
  â†’ BRPOP mt5:order:result      â† â¸
  â†’ UPDATE pairs (OPEN)
```

### Táº¥t cáº£ crash points vÃ  rá»§i ro

| Crash Point | MÃ´ táº£ | Háº­u quáº£ | PhÃ¡t hiá»‡n? |
|---|---|---|---|
| **A** | Crash sau INSERT, trÆ°á»›c LPUSH | Pair PENDING, 0 position | âœ… Reconciler: STALE_ENTRY â†’ FAILED |
| **B** | LPUSH xong, MT5 BRPOPLPUSH xong, MT5 crash trÆ°á»›c execute | Command cÃ²n trong `mt5:order:processing` | âœ… MT5 restart: check processing list â†’ re-execute hoáº·c recover |
| **C** | MT5 execute xong, LPUSH result, Brain crash trÆ°á»›c Ä‘á»c result | MT5 cÃ³ position, DB váº«n PENDING | âœ… Reconciler: check MT5 positions â†’ recover |
| **D** | Exchange order sent, response tráº£ vá», Brain crash trÆ°á»›c write DB | Exchange cÃ³ position, DB váº«n PENDING | âœ… Reconciler: check Exchange positions â†’ recover |
| **E** | MT5 fill OK, Exchange fail, Brain crash trÆ°á»›c update ORPHAN | MT5 cÃ³ position, DB váº«n ENTRY_SENT | âœ… Reconciler: STALE_ENTRY + check MT5 â†’ ORPHAN_MT5 |
| **F** | Close: 1 bÃªn Ä‘Ã³ng OK, crash trÆ°á»›c Ä‘Ã³ng bÃªn kia | 1 position cÃ²n, DB váº«n CLOSE_SENT | âœ… Reconciler: STALE_CLOSE â†’ CLOSE_*_DONE â†’ ORPHAN_* |

### Giáº£i phÃ¡p: Write-Ahead + Intermediate States

**NguyÃªn táº¯c**: **Ghi DB trÆ°á»›c khi lÃ m báº¥t cá»© gÃ¬**. Má»—i bÆ°á»›c Ä‘á»u cÃ³ state riÃªng trong DB.

```
1. INSERT pairs (PENDING) + event CREATED         â† ghi trÆ°á»›c
2. UPDATE status = ENTRY_SENT                     â† Ä‘Ã¡nh dáº¥u Ä‘Ã£ gá»­i
3. LPUSH mt5:order:queue + exchange.place_order()
4. Nháº­n MT5 result â†’ UPDATE mt5 fields + event MT5_FILLED
5. Nháº­n Exchange result â†’ UPDATE exchange fields + event EXCHANGE_FILLED
6. UPDATE status = OPEN + event ENTRY_COMPLETE
```

â†’ Náº¿u crash á»Ÿ báº¥t ká»³ bÆ°á»›c nÃ o, DB luÃ´n biáº¿t "Ä‘ang á»Ÿ Ä‘Ã¢u" â†’ Reconciler biáº¿t cáº§n check gÃ¬.

---

## 2. PhÃ¢n biá»‡t nguá»“n Ä‘Ã³ng lá»‡nh

### Váº¥n Ä‘á»
Hiá»‡n táº¡i CLOSED/HEALED/FORCE_CLOSED phÃ¢n biá»‡t Ä‘Æ°á»£c. NhÆ°ng cáº§n rÃµ hÆ¡n **ai Ä‘Ã³ng, vÃ¬ lÃ½ do gÃ¬**.

### Giáº£i phÃ¡p: ThÃªm `close_reason` vÃ o báº£ng `pairs`

```sql
close_reason TEXT,  -- Ai Ä‘Ã³ng + lÃ½ do
```

| close_reason | MÃ´ táº£ |
|---|---|
| `SIGNAL` | Brain detect close signal (spread há»™i tá»¥) |
| `HOLD_TIMEOUT` | Giá»¯ lá»‡nh quÃ¡ `hold_time_sec` |
| `FORCE_SCHEDULE` | Force close theo schedule |
| `FORCE_SHUTDOWN` | Force close khi shutdown |
| `HEAL_ORPHAN_MT5` | Reconciler Ä‘Ã³ng orphan MT5 |
| `HEAL_ORPHAN_EXCHANGE` | Reconciler Ä‘Ã³ng orphan Exchange |
| `HEAL_GHOST` | Reconciler Ä‘Ã³ng ghost position |
| `MANUAL` | ÄÃ³ng thá»§ cÃ´ng (qua web/command) |

â†’ Khi query "táº¡i sao lá»‡nh Ä‘Ã³ng", nhÃ¬n `close_reason` lÃ  biáº¿t ngay.

---

## 3. Intermediate States â€” Thiáº¿t káº¿ láº¡i

### Váº¥n Ä‘á»
`PENDING â†’ OPEN` nháº£y quÃ¡ xa. Giá»¯a 2 state nÃ y, ráº¥t nhiá»u thá»© xáº£y ra. Náº¿u crash giá»¯a chá»«ng â†’ khÃ´ng biáº¿t Ä‘Ã£ gá»­i lá»‡nh chÆ°a, bÃªn nÃ o fill rá»“i.

### State Machine má»›i (chi tiáº¿t hÆ¡n)

#### Entry Flow

```
PENDING           â†’ Pair táº¡o, chÆ°a gá»­i lá»‡nh nÃ o
    â”‚
    â–¼
ENTRY_SENT        â†’ ÄÃ£ dispatch orders (LPUSH mt5 + exchange REST)
    â”‚
    â”œâ”€â”€ MT5 fill trÆ°á»›c â”€â”€â–¶ ENTRY_MT5_FILLED
    â”‚                          â”‚
    â”‚                          â”œâ”€â”€ Exchange fill OK â”€â”€â–¶ OPEN âœ…
    â”‚                          â””â”€â”€ Exchange fail â”€â”€â–¶ ORPHAN_MT5
    â”‚
    â”œâ”€â”€ Exchange fill trÆ°á»›c â”€â”€â–¶ ENTRY_EXCHANGE_FILLED
    â”‚                              â”‚
    â”‚                              â”œâ”€â”€ MT5 fill OK â”€â”€â–¶ OPEN âœ…
    â”‚                              â””â”€â”€ MT5 fail â”€â”€â–¶ ORPHAN_EXCHANGE
    â”‚
    â”œâ”€â”€ Both fill (near-simultaneous) â”€â”€â–¶ OPEN âœ…
    â”‚
    â”œâ”€â”€ Both fail â”€â”€â–¶ FAILED
    â”‚
    â””â”€â”€ Timeout (> stale_entry_sec) â”€â”€â–¶ Reconciler xá»­ lÃ½
```

#### Close Flow

```
OPEN
    â”‚
    â–¼
CLOSE_SENT        â†’ ÄÃ£ dispatch close orders
    â”‚
    â”œâ”€â”€ MT5 close trÆ°á»›c â”€â”€â–¶ CLOSE_MT5_DONE
    â”‚                          â”‚
    â”‚                          â”œâ”€â”€ Exchange close OK â”€â”€â–¶ CLOSED âœ…
    â”‚                          â””â”€â”€ Exchange close fail â”€â”€â–¶ ORPHAN_EXCHANGE
    â”‚
    â”œâ”€â”€ Exchange close trÆ°á»›c â”€â”€â–¶ CLOSE_EXCHANGE_DONE
    â”‚                              â”‚
    â”‚                              â”œâ”€â”€ MT5 close OK â”€â”€â–¶ CLOSED âœ…
    â”‚                              â””â”€â”€ MT5 close fail â”€â”€â–¶ ORPHAN_MT5
    â”‚
    â”œâ”€â”€ Both close OK â”€â”€â–¶ CLOSED âœ…
    â”‚
    â””â”€â”€ Timeout â”€â”€â–¶ Reconciler xá»­ lÃ½
```

### Báº£ng States hoÃ n chá»‰nh

| # | Status | Giai Ä‘oáº¡n | MÃ´ táº£ | Position? |
|---|---|---|---|---|
| 1 | `PENDING` | Entry | Pair táº¡o, chÆ°a gá»­i lá»‡nh | âŒ ChÆ°a |
| 2 | `ENTRY_SENT` | Entry | Orders dispatched cáº£ 2 bÃªn | â“ Äang chá» |
| 3 | `ENTRY_MT5_FILLED` | Entry | MT5 fill OK, chá» Exchange | âš ï¸ Chá»‰ MT5 |
| 4 | `ENTRY_EXCHANGE_FILLED` | Entry | Exchange fill OK, chá» MT5 | âš ï¸ Chá»‰ Exchange |
| 5 | `OPEN` | Active | Cáº£ 2 bÃªn fill, Ä‘ang active | âœ… Cáº£ 2 |
| 6 | `CLOSE_SENT` | Close | Close orders dispatched | âœ… Äang Ä‘Ã³ng |
| 7 | `CLOSE_MT5_DONE` | Close | MT5 Ä‘Ã³ng OK, chá» Exchange | âš ï¸ Chá»‰ Exchange |
| 8 | `CLOSE_EXCHANGE_DONE` | Close | Exchange Ä‘Ã³ng OK, chá» MT5 | âš ï¸ Chá»‰ MT5 |
| 9 | `CLOSED` | Terminal | HoÃ n táº¥t cáº£ 2 bÃªn | âŒ KhÃ´ng |
| 10 | `FAILED` | Terminal | Entry fail cáº£ 2 bÃªn | âŒ KhÃ´ng |
| 11 | `ORPHAN_MT5` | Error | Chá»‰ MT5 cÃ³ position | âš ï¸ Chá»‰ MT5 |
| 12 | `ORPHAN_EXCHANGE` | Error | Chá»‰ Exchange cÃ³ position | âš ï¸ Chá»‰ Exchange |
| 13 | `HEALING` | Recovery | Reconciler Ä‘ang sá»­a | âš ï¸ Äang xá»­ lÃ½ |
| 14 | `HEALED` | Terminal | Orphan Ä‘Ã£ Ä‘Æ°á»£c Ä‘Ã³ng | âŒ KhÃ´ng |
| 15 | `FORCE_CLOSED` | Terminal | ÄÃ³ng kháº©n cáº¥p | âŒ KhÃ´ng |

### So sÃ¡nh cÅ© vs má»›i

| CÅ© | Má»›i | Lá»£i Ã­ch |
|---|---|---|
| `PENDING` (quÃ¡ rá»™ng) | `PENDING` + `ENTRY_SENT` + `ENTRY_*_FILLED` | Biáº¿t chÃ­nh xÃ¡c crash á»Ÿ Ä‘Ã¢u |
| `CLOSING` (quÃ¡ rá»™ng) | `CLOSE_SENT` + `CLOSE_*_DONE` | Biáº¿t bÃªn nÃ o Ä‘Ã£ close |
| `CLOSE_PARTIAL` (mÆ¡ há»“) | `CLOSE_MT5_DONE` / `CLOSE_EXCHANGE_DONE` | Biáº¿t rÃµ bÃªn nÃ o xong |

---

## 4. Thá»i Ä‘iá»ƒm sinh `pair_token` vÃ  INSERT

### NguyÃªn táº¯c: **INSERT TRÆ¯á»šC khi gá»­i báº¥t ká»³ lá»‡nh nÃ o**

```python
async def _execute_entry(self, signal, spread, mt5_tick, exchange_tick):
    # â‘  Sinh token TRÆ¯á»šC
    pair_token = uuid.uuid4().hex[:16]

    # â‘¡ INSERT DB TRÆ¯á»šC (write-ahead)
    self.db.insert_pair({
        "pair_token": pair_token,
        "direction": "LONG" if signal == Signal.ENTRY_LONG else "SHORT",
        "status": "PENDING",
        "entry_time": time.time(),
        "entry_spread": spread,
        "conf_dev_entry": self.config.dev_entry,
        "conf_dev_close": self.config.dev_close,
    })
    self.db.add_event(pair_token, "CREATED", {
        "spread": spread, "signal": signal.name
    })

    # â‘¢ Update status TRÆ¯á»šC khi gá»­i lá»‡nh
    self.db.update_pair(pair_token, status="ENTRY_SENT")

    # â‘£ Gá»­i MT5 command vÃ o queue (kÃ¨m pair_token)
    job_id = uuid.uuid4().hex[:8]
    self.redis.lpush(REDIS_MT5_ORDER_QUEUE, orjson.dumps({
        "action": "BUY",
        "volume": self.config.mt5_volume,
        "job_id": job_id,
        "pair_token": pair_token,  # â† link láº¡i DB
        "ts": time.time(),
    }))

    # â‘¤ Gá»­i Exchange order
    exchange_result = await self.exchange.place_order(...)

    # â‘¥ Chá» MT5 result
    mt5_result = await asyncio.to_thread(self.redis.brpop, ...)

    # â‘¦ Update DB based on results...
```

### Táº¡i sao INSERT trÆ°á»›c?

| Náº¿u crash á»Ÿ bÆ°á»›c... | DB cÃ³ gÃ¬? | Reconciler biáº¿t gÃ¬? |
|---|---|---|
| â‘¡ (INSERT) | Pair PENDING | KhÃ´ng cÃ³ positions â†’ FAILED |
| â‘¢ (ENTRY_SENT) | Pair ENTRY_SENT | Check cáº£ 2 sÃ n xem cÃ³ fill khÃ´ng |
| â‘£ (LPUSH) | Pair ENTRY_SENT | Check MT5 positions |
| â‘¤ (Exchange) | Pair ENTRY_SENT | Check Exchange positions |
| â‘¥ (wait result) | Pair ENTRY_SENT | Check cáº£ 2 sÃ n |

â†’ **KhÃ´ng bao giá» máº¥t lá»‡nh** â€” DB luÃ´n biáº¿t pair tá»“n táº¡i.

---

## 5. Reconciliation Flow dá»±a trÃªn gÃ¬?

### 3 nguá»“n dá»¯ liá»‡u

```
â”Œâ”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”    â”Œâ”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”    â”Œâ”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”
â”‚  MT5 THáº¬T       â”‚    â”‚  EXCHANGE THáº¬T  â”‚    â”‚  DB LOCAL       â”‚
â”‚  (positions via â”‚    â”‚  (positions via â”‚    â”‚  (pairs table)  â”‚
â”‚   Redis query)  â”‚    â”‚   REST API)     â”‚    â”‚                 â”‚
â””â”€â”€â”€â”€â”€â”€â”€â”€â”¬â”€â”€â”€â”€â”€â”€â”€â”€â”˜    â””â”€â”€â”€â”€â”€â”€â”€â”€â”¬â”€â”€â”€â”€â”€â”€â”€â”€â”˜    â””â”€â”€â”€â”€â”€â”€â”€â”€â”¬â”€â”€â”€â”€â”€â”€â”€â”€â”˜
         â”‚                      â”‚                      â”‚
         â””â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”¬â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”˜                      â”‚
                    â”‚                                  â”‚
                    â–¼                                  â–¼
            â”Œâ”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”                â”Œâ”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”
            â”‚ ACTUAL STATE  â”‚    compare     â”‚ EXPECTED STATE  â”‚
            â”‚ (sÃ n tháº­t cÃ³  â”‚ â—„â•â•â•â•â•â•â•â•â•â•â•â•â–º â”‚ (DB nghÄ© lÃ  cÃ³  â”‚
            â”‚  position gÃ¬) â”‚                â”‚  position gÃ¬)   â”‚
            â””â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”˜                â””â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”˜
```

### Matching keys

| Source | Match báº±ng | LÆ°u Ã½ |
|---|---|---|
| **MT5 ↔ DB** | `mt5_ticket` (primary) / `comment` (ENTRY_SENT recovery) | Ticket là primary key. Khi DB chưa có ticket, match `comment="arb:{pair_token}"` + `type` + `volume` |
| **Exchange ↔ DB** | `expected_exchange_side(pair.direction)` + `exchange_volume` | `pair.direction=LONG` -> Exchange `SHORT`; `pair.direction=SHORT` -> Exchange `LONG` |
| **Pair completeness** | `status` column | Non-terminal pairs Cáº¦N cÃ³ positions trÃªn sÃ n |

### Terminal vs Non-terminal

| Terminal (khÃ´ng cáº§n position) | Non-terminal (Cáº¦N position) |
|---|---|
| `CLOSED`, `FAILED`, `HEALED`, `FORCE_CLOSED` | `OPEN`, `ENTRY_SENT`, `ENTRY_*_FILLED`, `CLOSE_SENT`, `CLOSE_*_DONE`, `ORPHAN_*`, `HEALING` |

### Reconcile Logic (pseudocode)

```python
# 1. Láº¥y data
mt5_positions = query_mt5()        # {ticket: position_info} + search by comment for ENTRY_SENT recovery
exchange_positions = query_exchange()  # {(exchange_side, volume): position_info}
db_pairs = db.get_non_terminal()   # list[pair]

matched_mt5_tickets = set()
matched_exchange_keys = set()

# 2. Check tá»«ng DB pair
for pair in db_pairs:
    mt5_pos = match_mt5(pair, mt5_positions)
    exch_key = (expected_exchange_side(pair.direction), pair.exchange_volume)
    mt5_ok = mt5_pos is not None
    exch_ok = exch_key in exchange_positions

    if mt5_ok:
        matched_mt5_tickets.add(mt5_pos.ticket)
    if exch_ok:
        matched_exchange_keys.add(exch_key)

    # Xá»­ lÃ½ theo status + actual positions...
    if pair.status == "OPEN":
        if mt5_ok and exch_ok:     # OK
        elif mt5_ok and not exch_ok:  # ORPHAN_MT5 (MT5 survives)
        elif not mt5_ok and exch_ok:  # ORPHAN_EXCHANGE (Exchange survives)
    elif pair.status == "ENTRY_SENT":
        # stale check + recover...

# 3. Check Ghost (positions khÃ´ng ai nháº­n)
for ticket, pos in mt5_positions.items():
    if ticket not in matched_mt5_tickets:
        # GHOST MT5
for key, pos in exchange_positions.items():
    if key not in matched_exchange_keys:
        # GHOST Exchange
```

---

## 6. Exchange Order Status

### Váº¥n Ä‘á»
Hiá»‡n táº¡i `OrderResult.success = True/False` quÃ¡ Ä‘Æ¡n giáº£n. Cáº§n biáº¿t order á»Ÿ tráº¡ng thÃ¡i nÃ o trÃªn sÃ n.

### Exchange Order Status Enum

```python
class ExchangeOrderStatus(str, Enum):
    # â”€â”€â”€ Terminal (returned by place_order to Brain) â”€â”€â”€
    FILLED = "FILLED"            # Fill hoÃ n toÃ n
    REJECTED = "REJECTED"        # SÃ n tá»« chá»‘i
    UNKNOWN = "UNKNOWN"          # KhÃ´ng xÃ¡c Ä‘á»‹nh (timeout, network error)

    # â”€â”€â”€ Internal (handler-only, KHÃ”NG tráº£ vá» Brain) â”€â”€â”€
    ACCEPTED = "ACCEPTED"
    PARTIALLY_FILLED = "PARTIAL"
    CANCELLED = "CANCELLED"
    EXPIRED = "EXPIRED"
```

### Brain xá»­ lÃ½ `place_order()` result (chá»‰ 3 cases)

| Order Status | `success` | Brain action |
|---|---|---|
| `FILLED` | `true` | âœ… Ghi DB, update state |
| `REJECTED` | `false` | âŒ Ghi failed, check `error_category` |
| `UNKNOWN` | `false` | âš ï¸ KHÃ”NG ghi gÃ¬ â€” Ä‘á»ƒ Reconciler check sÃ n tháº­t |

> [!IMPORTANT]
> **`UNKNOWN` lÃ  case nguy hiá»ƒm nháº¥t.** Lá»‡nh Ä‘Ã£ gá»­i nhÆ°ng khÃ´ng biáº¿t káº¿t quáº£ (network timeout).
> Brain KHÃ”NG Ä‘Æ°á»£c assume fail. Pháº£i Ä‘á»ƒ Reconciler check sÃ n tháº­t.


