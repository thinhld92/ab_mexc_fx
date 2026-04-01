# Reconciler â€” Chi tiáº¿t Scenarios & Xá»­ lÃ½

> Module Reconciler cháº¡y má»—i 30-60s, so khá»›p 3 nguá»“n:
> **MT5 tháº­t** â†” **Exchange tháº­t** â†” **DB local**

---

## Reconcile Cycle

```
1. Query MT5 positions      â†’ LPUSH request (query_id) â†’ BRPOP response (timeout 10s)
2. Query Exchange positions  â†’ list[ExchangePosition]
3. Query DB (non-terminal: PENDING/ENTRY_SENT/ENTRY_*_FILLED/OPEN/CLOSE_SENT/CLOSE_*_DONE/ORPHAN_*/HEALING) â†’ list[pair]
4. Diff 3 nguá»“n             â†’ phÃ¡t hiá»‡n báº¥t thÆ°á»ng
5. Auto-heal (náº¿u báº­t)      â†’ Ä‘Ã³ng orphan
6. Log káº¿t quáº£              â†’ INSERT recon_log
7. Heartbeat                â†’ SET recon:heartbeat
```

---

## Táº¥t cáº£ Scenarios cÃ³ thá»ƒ xáº£y ra

### Scenario 1: OK â€” KhÃ´ng cÃ³ váº¥n Ä‘á» gÃ¬

```
DB:       pair_token=ABC, status=OPEN, mt5_ticket=123, exchange_order_id=XYZ
MT5:      ticket=123 tá»“n táº¡i, Ä‘Ãºng volume, Ä‘Ãºng direction
Exchange: position LONG tá»“n táº¡i, Ä‘Ãºng volume
```

**Xá»­ lÃ½**: 
- `UPDATE pairs SET recon_status='OK', last_recon_at=now WHERE pair_token='ABC'`
- `INSERT pair_events (RECON_OK)`

---

### Scenario 2: ORPHAN_MT5 â€” Chá»‰ MT5 cÃ³, Exchange máº¥t

**Khi nÃ o xáº£y ra**:
- Exchange bá»‹ liquidation
- Exchange API lá»—i khi entry â†’ order khÃ´ng fill
- Ai Ä‘Ã³ manually close trÃªn exchange
- Exchange maintenance/downtime Ä‘Ã³ng position

```
DB:       pair_token=ABC, status=OPEN, mt5_ticket=123
MT5:      ticket=123 tá»“n táº¡i âœ…
Exchange: KHÃ”NG cÃ³ position matching âŒ
```

**Xá»­ lÃ½ (auto_heal = true)**:
1. `UPDATE pairs SET status='HEALING' WHERE pair_token='ABC'`
2. `INSERT pair_events (HEAL_START, {orphan_type: "ORPHAN_MT5", action: "close_mt5"})`
3. Gá»­i lá»‡nh close MT5 ticket 123 (via Redis `mt5:order:queue`)
4. Chá» result:
   - **ThÃ nh cÃ´ng**: 
     - `UPDATE pairs SET status='HEALED', recon_status='HEALED'`
     - `INSERT pair_events (HEAL_SUCCESS, {ticket: 123, profit: ...})`
   - **Tháº¥t báº¡i**: 
     - `UPDATE pairs SET status='ORPHAN_MT5'` (quay láº¡i, retry láº§n sau)
     - `INSERT pair_events (HEAL_FAILED, {error: "..."})`
     - Alert Telegram

**Xá»­ lÃ½ (auto_heal = false)**:
1. `UPDATE pairs SET status='ORPHAN_MT5', recon_status='ORPHAN_MT5'`
2. `INSERT pair_events (RECON_MISMATCH, {expected: "both", actual: "mt5_only"})`
3. Alert Telegram: "âš ï¸ ORPHAN_MT5: pair ABC, ticket 123 â€” Exchange position missing"

---

### Scenario 3: ORPHAN_EXCHANGE â€” Chá»‰ Exchange cÃ³, MT5 máº¥t

**Khi nÃ o xáº£y ra**:
- MT5 broker hit stop-loss / take-profit tá»± Ä‘Ã³ng
- MT5 terminal disconnect, broker auto-close margin call
- AI/quáº£n lÃ½ tÃ i khoáº£n Ä‘Ã³ng trÃªn MT5

```
DB:       pair_token=ABC, status=OPEN, exchange_order_id=XYZ
MT5:      KHÃ”NG cÃ³ ticket matching âŒ
Exchange: position LONG tá»“n táº¡i âœ…
```

**Xá»­ lÃ½ (auto_heal = true)**:
1. `UPDATE pairs SET status='HEALING'`
2. `INSERT pair_events (HEAL_START, {orphan_type: "ORPHAN_EXCHANGE", action: "close_exchange"})`
3. `await exchange.place_order(CLOSE_LONG/CLOSE_SHORT, volume)`
4. Chá» result:
   - **ThÃ nh cÃ´ng**:
     - `UPDATE pairs SET status='HEALED'`
     - `INSERT pair_events (HEAL_SUCCESS, {order_id: ...})`
   - **Tháº¥t báº¡i**:
     - `UPDATE pairs SET status='ORPHAN_EXCHANGE'` (retry láº§n sau)
     - `INSERT pair_events (HEAL_FAILED, {error: "..."})`
     - Alert Telegram

---

### Scenario 4: GHOST MT5 â€” MT5 cÃ³ position nhÆ°ng DB khÃ´ng cÃ³

**Khi nÃ o xáº£y ra**:
- Ai Ä‘Ã³ má»Ÿ position manual trÃªn MT5
- Bot bá»‹ crash GIá»®A lÃºc Ä‘áº·t lá»‡nh (MT5 fill nhÆ°ng chÆ°a ká»‹p INSERT DB)
- DB bá»‹ corrupt / restore tá»« backup cÅ©

```
DB:       KhÃ´ng cÃ³ pair nÃ o match ticket 555
MT5:      ticket=555 tá»“n táº¡i, comment="arb:{pair_token}" âœ… (hoáº·c khÃ´ng cÃ³ comment)
```

**Xá»­ lÃ½**: PhÃ¢n loáº¡i theo comment:

**a) Comment chá»©a "arb" (do bot má»Ÿ)**:
1. Log WARNING: "GHOST MT5: ticket 555, likely bot-opened, no DB record"
2. Náº¿u `ghost_action = "close"`:
   - Gá»­i close MT5 ticket 555
   - Log káº¿t quáº£
3. Náº¿u `ghost_action = "alert"`:
   - Alert Telegram: "ðŸ‘» GHOST MT5: ticket=555 â€” recommend manual check"
4. `INSERT recon_log (actions_taken: [{type: "ghost_mt5", ticket: 555, action: "..."}])`

**b) Comment KHÃ”NG chá»©a "arb" (manual hoáº·c EA khÃ¡c)**:
1. **IGNORE** â€” khÃ´ng pháº£i cá»§a bot, khÃ´ng xá»­ lÃ½
2. Log INFO: "Skipping non-bot MT5 position: ticket 555"

---

### Scenario 5: GHOST Exchange â€” Exchange cÃ³ position nhÆ°ng DB khÃ´ng cÃ³

**Khi nÃ o xáº£y ra**:
- Bot crash giá»¯a lÃºc entry (Exchange fill nhÆ°ng chÆ°a ká»‹p INSERT DB)
- Manually má»Ÿ trÃªn exchange (Ã­t xáº£y ra náº¿u dÃ¹ng WEB token)
- DB restore cÅ©

```
DB:       KhÃ´ng cÃ³ pair nÃ o match exchange position
Exchange: position LONG 1000 contracts tá»“n táº¡i
```

**Xá»­ lÃ½**:
1. Log WARNING: "GHOST EXCHANGE: position LONG 1000, no DB record"
2. Náº¿u `ghost_action = "close"`:
   - `await exchange.place_order(CLOSE_LONG, 1000)`
   - Log káº¿t quáº£
3. Náº¿u `ghost_action = "alert"`:
   - Alert Telegram: "ðŸ‘» GHOST EXCHANGE: LONG 1000 contracts â€” recommend manual check"

---

### Scenario 6: VOLUME_MISMATCH â€” Volume khÃ´ng khá»›p

**Khi nÃ o xáº£y ra**:
- Partial fill trÃªn exchange (Ã­t gáº·p vá»›i market order nhÆ°ng cÃ³ thá»ƒ)
- Config volume thay Ä‘á»•i giá»¯a chá»«ng
- Lá»—i rounding

```
DB:       pair_token=ABC, exchange_volume=1000
Exchange: position volume = 800 (thiáº¿u 200)
```

**Xá»­ lÃ½**:
1. `UPDATE pairs SET recon_status='MISMATCH', recon_note='volume: expected 1000, actual 800'`
2. `INSERT pair_events (RECON_MISMATCH, {type: "volume", expected: 1000, actual: 800})`
3. **KHÃ”NG auto-heal** â€” volume mismatch quÃ¡ phá»©c táº¡p Ä‘á»ƒ tá»± xá»­ lÃ½
4. Alert Telegram: "âš ï¸ Volume mismatch: pair ABC, expected 1000, actual 800"
5. Cáº§n manual intervention

---

### Scenario 7: DIRECTION_MISMATCH â€” Direction khÃ´ng khá»›p

**Khi nÃ o xáº£y ra**:
- Bug nghiÃªm trá»ng trong code
- DB bá»‹ corrupt

```
DB:       pair_token=ABC, direction=LONG
Exchange: position = SHORT
```

**Xá»­ lÃ½**:
1. **CRITICAL ALERT** â€” Ä‘Ã¢y lÃ  bug, khÃ´ng auto-heal
2. `UPDATE pairs SET recon_status='MISMATCH', recon_note='direction mismatch: DB=LONG, exchange=SHORT'`
3. Alert Telegram: "ðŸ”´ CRITICAL: Direction mismatch! pair ABC"
4. **KHUYáº¾N NGHá»Š**: Stop trading, manual check

---

### Scenario 8: STALE_ENTRY â€” Pair stuck á»Ÿ PENDING/ENTRY_SENT/ENTRY_*_FILLED quÃ¡ lÃ¢u

**Khi nÃ o xáº£y ra**:
- Bot crash sau INSERT pair nhÆ°ng trÆ°á»›c khi gá»­i lá»‡nh (PENDING)
- Bot crash sau dispatch nhÆ°ng chÆ°a nháº­n káº¿t quáº£ (ENTRY_SENT)
- Bot crash sau 1 bÃªn fill (ENTRY_MT5_FILLED / ENTRY_EXCHANGE_FILLED)
- Redis reliable queue recovery Ä‘Ã£ xá»­ lÃ½ command nhÆ°ng Brain chÆ°a nháº­n result

```
DB:       pair_token=ABC, status=ENTRY_SENT, entry_time=10 phÃºt trÆ°á»›c
```

**Xá»­ lÃ½** (threshold: `stale_entry_sec`, default 60s):

| DB status | MT5 position? | Exchange position? | Action |
|---|---|---|---|
| `PENDING` | âŒ | âŒ | â†’ `FAILED` (chÆ°a gá»­i lá»‡nh nÃ o) |
| `ENTRY_SENT` | âŒ | âŒ | â†’ `FAILED` (cáº£ 2 fail/timeout) |
| `ENTRY_SENT` | âœ… | âŒ | â†’ `ORPHAN_MT5` (update mt5 fields tá»« position tháº­t) |
| `ENTRY_SENT` | âŒ | âœ… | â†’ `ORPHAN_EXCHANGE` (update exchange fields) |
| `ENTRY_SENT` | âœ… | âœ… | â†’ `OPEN` (recover! ghi Ä‘áº§y Ä‘á»§ fields) |
| `ENTRY_MT5_FILLED` | âœ… | âŒ | â†’ `ORPHAN_MT5` |
| `ENTRY_MT5_FILLED` | âœ… | âœ… | â†’ `OPEN` (recover!) |
| `ENTRY_EXCHANGE_FILLED` | âŒ | âœ… | â†’ `ORPHAN_EXCHANGE` |
| `ENTRY_EXCHANGE_FILLED` | âœ… | âœ… | â†’ `OPEN` (recover!) |

---

### Scenario 9: STALE_CLOSE â€” Pair stuck á»Ÿ CLOSE_SENT/CLOSE_*_DONE quÃ¡ lÃ¢u

**Khi nÃ o xáº£y ra**:
- Bot crash giá»¯a lÃºc close
- Close 1 bÃªn OK nhÆ°ng chÆ°a ká»‹p update DB rá»“i crash

```
DB:       pair_token=ABC, status=CLOSE_SENT, close chÆ°a xong > 120s
```

**Xá»­ lÃ½** (threshold: `stale_close_sec`, default 120s):

| DB status | MT5 cÃ²n? | Exchange cÃ²n? | Action |
|---|---|---|---|
| `CLOSE_SENT` | âŒ | âŒ | â†’ `CLOSED` (cáº£ 2 Ä‘Ã£ Ä‘Ã³ng!) |
| `CLOSE_SENT` | âœ… | âŒ | â†’ `CLOSE_EXCHANGE_DONE`, sau Ä‘Ã³ â†’ `ORPHAN_MT5` |
| `CLOSE_SENT` | âŒ | âœ… | â†’ `CLOSE_MT5_DONE`, sau Ä‘Ã³ â†’ `ORPHAN_EXCHANGE` |
| `CLOSE_SENT` | âœ… | âœ… | Retry close cáº£ 2 bÃªn |
| `CLOSE_MT5_DONE` | â€” | âŒ | â†’ `CLOSED` (exchange cÅ©ng Ä‘Ã£ Ä‘Ã³ng!) |
| `CLOSE_MT5_DONE` | â€” | âœ… | Retry close exchange |
| `CLOSE_EXCHANGE_DONE` | âŒ | â€” | â†’ `CLOSED` (mt5 cÅ©ng Ä‘Ã£ Ä‘Ã³ng!) |
| `CLOSE_EXCHANGE_DONE` | âœ… | â€” | Retry close MT5 |

---

### Scenario 10: HEALING stuck â€” Pair stuck á»Ÿ HEALING quÃ¡ lÃ¢u

**Khi nÃ o xáº£y ra**:
- Reconciler crash giá»¯a lÃºc heal
- Heal order timeout

```
DB:       pair_token=ABC, status=HEALING, > 300s
```

**Xá»­ lÃ½** (threshold: `stale_healing_sec`, default 300s):
1. Check position cÃ²n tá»“n táº¡i khÃ´ng?
2. **CÃ²n** â†’ `UPDATE status='ORPHAN_*'` (quay láº¡i, heal láº¡i)
3. **ÄÃ£ Ä‘Ã³ng** â†’ `UPDATE status='HEALED'`

---

## Decision Matrix tá»•ng há»£p

| # | Scenario | PhÃ¡t hiá»‡n báº±ng | Auto-heal? | Má»©c nguy hiá»ƒm |
|---|---|---|---|---|
| 1 | OK | Táº¥t cáº£ khá»›p | â€” | ðŸŸ¢ Safe |
| 2 | ORPHAN_MT5 | MT5 cÃ³, Exchange khÃ´ng | âœ… Close MT5 | ðŸŸ¡ Medium |
| 3 | ORPHAN_EXCHANGE | Exchange cÃ³, MT5 khÃ´ng | âœ… Close Exchange | ðŸŸ¡ Medium |
| 4 | GHOST MT5 | MT5 cÃ³, DB khÃ´ng | âš™ï¸ Configurable | ðŸŸ¡ Medium |
| 5 | GHOST Exchange | Exchange cÃ³, DB khÃ´ng | âš™ï¸ Configurable | ðŸŸ¡ Medium |
| 6 | Volume mismatch | Volume khÃ¡c nhau | âŒ Alert only | ðŸŸ  High |
| 7 | Direction mismatch | Direction khÃ¡c nhau | âŒ Alert only | ðŸ”´ Critical |
| 8 | STALE_ENTRY | PENDING/ENTRY_* > 60s | âœ… Recover/Fail | ðŸŸ¡ Medium |
| 9 | STALE_CLOSE | CLOSE_SENT/CLOSE_*_DONE > 120s | âœ… Recover/Retry | ðŸŸ¡ Medium |
| 10 | HEALING stuck | HEALING > 300s | âœ… Reset to ORPHAN | ðŸŸ¡ Medium |

---

## Config Reconciler

```json
{
  "reconciler": {
    "interval_sec": 30,
    "auto_heal": true,
    "ghost_action": "alert",
    "thresholds": {
      "stale_entry_sec": 60,
      "stale_close_sec": 120,
      "stale_healing_sec": 300
    },
    "alert_telegram": true
  }
}
```

---

## Matching Logic

### LÃ m sao match MT5 position vá»›i DB pair?

Primary key:
```
MT5 ticket == pairs.mt5_ticket
```

Fallback cho `ENTRY_SENT` / `PENDING` recovery khi DB chưa kịp ghi ticket:
- tìm position có `comment` chứa `pair_token` (Brain gửi `comment = "arb:{pair_token}"`)
- verify thêm `type` + `volume` để tránh match nhầm

â†’ DÃ¹ng ticket number (unique trÃªn MT5). Bot ghi ticket vÃ o DB khi fill.

### LÃ m sao match Exchange position vá»›i DB pair?

**KHÃ”NG dÃ¹ng order_id** (vÃ¬ 1 position cÃ³ thá»ƒ tá»« nhiá»u order).

DÃ¹ng **direction + volume**:
```
exchange_position.side == expected_exchange_side(pair.direction)
AND exchange_position.volume_raw == pair.exchange_volume
```

Trong đó:
- `pair.direction = LONG` -> Exchange position phải là `SHORT`
- `pair.direction = SHORT` -> Exchange position phải là `LONG`

> [!WARNING]
> Náº¿u cÃ³ nhiá»u pairs cÃ¹ng direction â†’ cáº§n careful matching. Phase hiá»‡n táº¡i khÃ³a **max_orders = 1 toÃ n há»‡ thá»‘ng**, nÃªn matching 1:1.

### Náº¿u khÃ´ng match Ä‘Æ°á»£c?

- MT5 position khÃ´ng match: â†’ GHOST MT5
- Exchange position khÃ´ng match: â†’ GHOST Exchange
- DB pair khÃ´ng tÃ¬m tháº¥y position: â†’ ORPHAN



