# Config Specification — `config.json`

> File: `config.json` (project root)
> Hot-reload: Brain periodically checks config, thay đổi → auto apply
> Format: JSON, parsed bởi `Config` class (`src/utils/config.py`)

---

## Full Config Structure (v2)

```json
{
  "vps_name": "VPS-01",

  "redis": { ... },
  "mt5": { ... },
  "exchange": { ... },
  "arbitrage": { ... },
  "brain": { ... },
  "safety": { ... },
  "schedule": { ... },
  "database": { ... },
  "reconciler": { ... },
  "watchdog": { ... },
  "shutdown": { ... },
  "telegram": { ... }
}
```

---

## Sections chi tiết

### `vps_name`

| Key | Type | Default | Mô tả |
|---|---|---|---|
| `vps_name` | string | `"VPS-01"` | Tên VPS, dùng cho log + Telegram alert |

---

### `redis`

Kết nối Redis — IPC giữa processes.

| Key | Type | Default | Mô tả |
|---|---|---|---|
| `host` | string | `"localhost"` | Redis host |
| `port` | int | `6379` | Redis port |
| `db` | int | `0` | Redis database number |

```json
"redis": {
  "host": "localhost",
  "port": 6379,
  "db": 0
}
```

---

### `mt5`

MT5 terminal connection + trading config.

| Key | Type | Default | Mô tả | Hot-reload? |
|---|---|---|---|---|
| `terminal_path` | string | — | Đường dẫn đến `terminal64.exe` | ❌ |
| `login` | int | — | MT5 account number | ❌ |
| `password` | string | — | MT5 password | ❌ |
| `server` | string | — | MT5 broker server | ❌ |
| `symbol` | string | `"XAUUSD"` | Symbol trade trên MT5 | ❌ |
| `volume` | float | `0.01` | Volume per order (lot). Phải khớp với `exchange.volume` | ✅ |
| `position_check_interval_ms` | int | `30` | Interval (ms) giữa mỗi lần check MT5 positions. **30ms = ~33 checks/giây**, chỉ chạy khi có tracked tickets | ❌ |

```json
"mt5": {
  "terminal_path": "C:\\Program Files\\MetaTrader 5\\terminal64.exe",
  "login": 12345678,
  "password": "your_password",
  "server": "Broker-Server",
  "symbol": "XAUUSD",
  "volume": 0.01
}
```

> [!WARNING]
> `volume` MT5 và `volume` Exchange phải tương đương. Xem `exchange_standards.md` Section 6.

---

### `exchange`

Crypto exchange config. **Thay thế mục `mexc` cũ** — tổng quát cho mọi sàn.

| Key | Type | Default | Mô tả | Hot-reload? |
|---|---|---|---|---|
| `name` | string | `"mexc"` | Exchange identifier: `"mexc"`, `"okx"`, `"binance"` | ❌ |
| `symbol` | string | `"XAUT_USDT"` | Symbol trade trên exchange | ❌ |
| `volume` | int/float | `1000` | Volume per order (sàn unit). Phải khớp với `mt5.volume` | ✅ |
| `volume_min` | int/float | `1` | Volume tối thiểu sàn cho phép | ❌ |
| `volume_step` | int/float | `1` | Bước nhảy volume | ❌ |
| `leverage` | int | `10` | Đòn bẩy | ❌ |
| `web_token` | string | `""` | Auth token (MEXC WEB bypass) | ✅ |
| `api_key` | string | `""` | API key (OKX/Binance) | ❌ |
| `api_secret` | string | `""` | API secret | ❌ |
| `passphrase` | string | `""` | Passphrase (OKX) | ❌ |

```json
"exchange": {
  "name": "mexc",
  "symbol": "XAUT_USDT",
  "volume": 1000,
  "volume_min": 1,
  "volume_step": 1,
  "leverage": 10,
  "web_token": "WEB..."
}
```

> [!TIP]
> Chỉ cần `web_token` cho MEXC. Chỉ cần `api_key`/`api_secret`/`passphrase` cho OKX/Binance. Đặt field nào không dùng = `""`.

---

### `arbitrage`

Tham số chiến lược arbitrage.

| Key | Type | Default | Mô tả | Hot-reload? |
|---|---|---|---|---|
| `deviation_entry` | float | `0.15` | Spread threshold để vào lệnh (USD). `|spread| > dev_entry` | ✅ |
| `deviation_close` | float | `0.05` | Ngưỡng đóng lệnh. Nếu dương: đóng khi spread co về gần pivot (`|gap| < dev_close`). Nếu âm: đóng khi spread đi sang biên đối diện (`LONG -> gap >= abs(dev_close)`, `SHORT -> gap <= -abs(dev_close)`) | ✅ |
| `stable_time_ms` | int | `300` | **Debounce timer**: signal phải giữ ổn định bao lâu trước khi execute (ms) | ✅ |
| `cooldown_entry_sec` | int | `60` | Thời gian chờ tối thiểu giữa 2 lệnh entry (giây) | ✅ |
| `cooldown_close_sec` | int | `3` | Thời gian chờ tối thiểu giữa 2 lệnh close (giây) | ✅ |
| `max_orders` | int | `1` | Số cặp position mở tối đa cùng lúc. **Default 1 để tránh gộp vị thế trên exchange** | ✅ |
| `hold_time_sec` | int | `240` | Thời gian giữ lệnh tối đa trước khi force close (giây). 0 = vô hạn | ✅ |

```json
"arbitrage": {
  "deviation_entry": 0.15,
  "deviation_close": 0.05,
  "stable_time_ms": 300,
  "cooldown_entry_sec": 60,
  "cooldown_close_sec": 3,
  "max_orders": 1,
  "hold_time_sec": 240
}
```

> [!TIP]
> `deviation_close` có 2 mode:
> `> 0`: chốt khi spread quay về gần EMA/pivot.
> `< 0`: chốt khi spread đi xuyên qua pivot và chạm biên phía đối diện.

---

### `brain`

Cấu hình Brain process.

| Key | Type | Default | Mô tả | Hot-reload? |
|---|---|---|---|---|
| `warmup_sec` | int | `300` | Thời gian warmup trước khi cho phép entry (giây) | ❌ |
| `warmup_min_ticks` | int | `500` | Số tick tối thiểu cần thu thập trong warmup | ❌ |
| `heartbeat_interval_sec` | int | `10` | Tần suất publish heartbeat | ❌ |
| `log_interval_sec` | int | `5` | Tần suất log spread ra console | ✅ |

```json
"brain": {
  "warmup_sec": 300,
  "warmup_min_ticks": 500,
  "heartbeat_interval_sec": 10,
  "log_interval_sec": 5
}
```

---

### `safety`

Kill switch và protection.

| Key | Type | Default | Mô tả | Hot-reload? |
|---|---|---|---|---|
| `max_tick_delay_sec` | float | `10.0` | Tick cũ hơn N giây → bỏ qua (data stale) | ✅ |
| `alert_equity_mt5` | float | `500` | Alert nếu MT5 equity dưới ngưỡng | ✅ |
| `orphan_max_count` | int | `3` | Số orphan tối đa trước khi stop trading | ✅ |
| `orphan_cooldown_sec` | int | `900` | Cooldown sau orphan (15 phút) | ✅ |
| `auth_cache_ttl_sec` | float | `2.0` | Cache rất ngắn cho auth status trước khi entry phải check lại exchange | ✅ |
| `auth_check_interval_sec` | int | `300` | Tần suất check exchange auth alive (giây) | ✅ |

```json
"safety": {
  "max_tick_delay_sec": 10.0,
  "alert_equity_mt5": 500,
  "orphan_max_count": 3,
  "orphan_cooldown_sec": 900,
  "auth_cache_ttl_sec": 2.0,
  "auth_check_interval_sec": 300
}
```

---

### `schedule`

Lịch trading + force close. Thời gian theo **UTC (GMT+0)**.

| Key | Type | Default | Mô tả |
|---|---|---|---|
| `trading_hours` | string[] | `["00:10-12:00", "16:02-19:30"]` | Khung giờ cho phép entry. Ngoài khung → chỉ close |
| `force_close_hours` | string[] | `["20:15-20:20"]` | Khung giờ force close tất cả positions |

```json
"schedule": {
  "trading_hours": [
    "00:10-12:00",
    "16:02-19:30"
  ],
  "force_close_hours": [
    "20:15-20:20"
  ]
}
```

> [!IMPORTANT]
> Thời gian phải là **UTC**. VD: 7:10 sáng VN (GMT+7) = `"00:10"` UTC.

---

### `database`

SQLite config.

| Key | Type | Default | Mô tả |
|---|---|---|---|
| `path` | string | `"data/arb.db"` | Đường dẫn file DB (relative to project root) |

```json
"database": {
  "path": "data/arb.db"
}
```

---

### `reconciler`

| Key | Type | Default | Mô tả |
|---|---|---|---|
| `interval_sec` | int | `30` | Tần suất reconcile (giây) |
| `auto_heal` | bool | `true` | Tự động đóng orphan positions |
| `ghost_action` | string | `"alert"` | Xử lý ghost: `"alert"` hoặc `"close"` |
| `alert_telegram` | bool | `true` | Gửi alert Telegram khi phát hiện bất thường |
| **`thresholds`** | object | | Nhóm các ngưỡng thời gian phát hiện stale |
| `thresholds.stale_entry_sec` | int | `60` | PENDING/ENTRY_* quá lâu → xử lý |
| `thresholds.stale_close_sec` | int | `120` | CLOSE_SENT/CLOSE_*_DONE quá lâu → xử lý |
| `thresholds.stale_healing_sec` | int | `300` | HEALING quá lâu → reset |

```json
"reconciler": {
  "interval_sec": 30,
  "auto_heal": true,
  "ghost_action": "alert",
  "alert_telegram": true,
  "thresholds": {
    "stale_entry_sec": 60,
    "stale_close_sec": 120,
    "stale_healing_sec": 300
  }
}
```

---

### `watchdog`

| Key | Type | Default | Mô tả |
|---|---|---|---|
| `check_interval_sec` | int | `5` | Tần suất check heartbeat |
| `max_restarts` | int | `3` | Số lần restart tối đa trong `restart_window_sec` |
| `restart_window_sec` | int | `300` | Cửa sổ tính restart (5 phút) |

```json
"watchdog": {
  "check_interval_sec": 5,
  "max_restarts": 3,
  "restart_window_sec": 300
}
```

---

### `shutdown`

| Key | Type | Default | Mô tả |
|---|---|---|---|
| `on_shutdown_positions` | string | `"leave"` | `"leave"` = giữ pairs, `"force_close"` = đóng hết |
| `grace_period_sec` | int | `30` | Chờ processes tự exit trước force kill |

```json
"shutdown": {
  "on_shutdown_positions": "leave",
  "grace_period_sec": 30
}
```

---

### `telegram`

| Key | Type | Default | Mô tả |
|---|---|---|---|
| `enable` | bool | `false` | Bật/tắt Telegram alerts |
| `bot_token` | string | `""` | Bot token từ @BotFather |
| `chat_id` | string | `""` | Chat ID nhận alert |

```json
"telegram": {
  "enable": false,
  "bot_token": "",
  "chat_id": ""
}
```

---

## Hot-reload Rules

| Reload được (✅) | Không reload được (❌) |
|---|---|
| `arbitrage.*` (tất cả) | `mt5.login/password/server` |
| `safety.*` (tất cả) | `exchange.name/symbol` |
| `mt5.volume` | `redis.*` |
| `exchange.volume` | `database.path` |
| `exchange.web_token` | `reconciler.*` |
| `brain.log_interval_sec` | `watchdog.*` |

> [!TIP]
> Để thay đổi config không reload được → phải restart hệ thống.

---

## Example: Full config.json v2

```json
{
  "vps_name": "VPS-01",

  "redis": {
    "host": "localhost",
    "port": 6379,
    "db": 0
  },

  "mt5": {
    "terminal_path": "C:\\Program Files\\MetaTrader 5\\terminal64.exe",
    "login": 12345678,
    "password": "your_password",
    "server": "Broker-Server",
    "symbol": "XAUUSD",
    "volume": 0.01
  },

  "exchange": {
    "name": "mexc",
    "symbol": "XAUT_USDT",
    "volume": 1000,
    "volume_min": 1,
    "volume_step": 1,
    "leverage": 10,
    "web_token": "WEB..."
  },

  "arbitrage": {
    "deviation_entry": 0.15,
    "deviation_close": 0.05,
    "stable_time_ms": 300,
    "cooldown_entry_sec": 60,
    "cooldown_close_sec": 3,
    "max_orders": 1,
    "hold_time_sec": 240
  },

  "brain": {
    "warmup_sec": 300,
    "warmup_min_ticks": 500,
    "heartbeat_interval_sec": 10,
    "log_interval_sec": 5
  },

  "safety": {
    "max_tick_delay_sec": 10.0,
    "alert_equity_mt5": 500,
    "orphan_max_count": 3,
    "orphan_cooldown_sec": 900,
    "auth_check_interval_sec": 300
  },

  "schedule": {
    "trading_hours": ["00:10-12:00", "16:02-19:30"],
    "force_close_hours": ["20:15-20:20"]
  },

  "database": {
    "path": "data/arb.db"
  },

  "reconciler": {
    "interval_sec": 30,
    "auto_heal": true,
    "ghost_action": "alert",
    "alert_telegram": true,
    "thresholds": {
      "stale_entry_sec": 60,
      "stale_close_sec": 120,
      "stale_healing_sec": 300
    }
  },

  "watchdog": {
    "check_interval_sec": 5,
    "max_restarts": 3,
    "restart_window_sec": 300
  },

  "shutdown": {
    "on_shutdown_positions": "leave",
    "grace_period_sec": 30
  },

  "telegram": {
    "enable": false,
    "bot_token": "",
    "chat_id": ""
  }
}
```

