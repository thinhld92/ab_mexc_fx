# Exchange Data Standards

> Chuẩn chung cho tất cả exchange handler. Mỗi exchange phải convert dữ liệu riêng sang format chuẩn này.
> Brain, Reconciler, Database chỉ làm việc với format chuẩn — không biết exchange cụ thể.

---

## 1. Tick Model

Tick từ mọi exchange phải trả về cùng format:

```python
TickData = {
    "bid": float,        # Best bid price
    "ask": float,        # Best ask price
    "last": float,       # Last traded price
    "ts": int,           # Exchange server time (ms) — detect tick mới
    "local_ts": float,   # UTC epoch lúc nhận — check freshness
}
```

**Lưu ý**: `ts` là thời gian sàn (dùng để phát hiện tick mới/cũ), `local_ts` là `time.time()` UTC (dùng để check data freshness).

---

## 2. Position Model

Standardized position từ `get_positions()`:

```python
ExchangePosition = {
    "position_id": str,       # ID duy nhất trên sàn (string để tương thích mọi sàn)
    "symbol": str,            # Symbol trên sàn ("XAUT_USDT", "XAUT-USDT-SWAP"...)
    "side": str,              # "LONG" / "SHORT" (chuẩn hóa, không dùng int)
    "volume": float,          # Số lượng đã chuẩn hóa (xem Quantity Normalization)
    "volume_raw": int|float,  # Số lượng gốc của sàn (contracts, lots...)
    "entry_price": float,     # Giá vào trung bình
    "mark_price": float,      # Giá mark hiện tại
    "unrealized_pnl": float,  # PnL chưa hiện thực (USD)
    "leverage": int,          # Đòn bẩy
    "margin_mode": str,       # "CROSS" / "ISOLATED"
    "updated_at": float,      # UTC epoch
}
```

**Mỗi exchange handler chịu trách nhiệm convert:**
- MEXC: `holdVol` → `volume_raw`, side `1`/`2` → `"LONG"`/`"SHORT"`
- OKX: `pos` → `volume_raw`, `posSide` → `"LONG"`/`"SHORT"`
- Binance: `positionAmt` → `volume_raw`, sign determines side

---
## 3. Exchange Order Status

Trạng thái lệnh trên sàn — mỗi handler phải map về enum này:

```python
class ExchangeOrderStatus(str, Enum):
    # ─── Terminal (returned by place_order to Brain) ───
    FILLED = "FILLED"            # Fill hoàn toàn
    REJECTED = "REJECTED"        # Sàn từ chối
    UNKNOWN = "UNKNOWN"          # Không xác định (timeout, network error)

    # ─── Internal (handler-only, KHÔNG BAO GIỜ trả về Brain) ───
    ACCEPTED = "ACCEPTED"        # Sàn nhận lệnh, chưa fill
    PARTIALLY_FILLED = "PARTIAL" # Fill 1 phần
    CANCELLED = "CANCELLED"      # Bị hủy
    EXPIRED = "EXPIRED"          # Hết hạn
```

### place_order() Contract

> [!IMPORTANT]
> **`place_order()` chỉ được trả về 3 status terminal: `FILLED`, `REJECTED`, `UNKNOWN`.**
>
> Handler PHẢI tự xử lý các status non-terminal (ACCEPTED, PARTIAL, CANCELLED, EXPIRED)
> bên trong implementation bằng polling/retry trước khi return.

```
place_order() flow (bên trong handler):

  API call → response
    ├── FILLED    → return OrderResult(success=True, status=FILLED)
    ├── REJECTED  → return OrderResult(success=False, status=REJECTED)
    ├── ACCEPTED  → poll/wait cho đến terminal state → return FILLED/REJECTED/UNKNOWN
    ├── PARTIAL   → ❌ KHÔNG treat as FILLED (volume lệch = hedging sai)
    │               → retry/cancel rồi return REJECTED, hoặc return UNKNOWN
    ├── CANCELLED → return OrderResult(success=False, status=REJECTED)
    ├── EXPIRED   → return OrderResult(success=False, status=REJECTED)
    └── timeout   → return OrderResult(success=False, status=UNKNOWN)
```

> [!WARNING]
> **`UNKNOWN` là case nguy hiểm nhất.** Lệnh đã gửi nhưng không biết kết quả.
> Brain KHÔNG được assume fail — để Reconciler check sàn thật.

> [!NOTE]
> **Partial fill**: Nếu tương lai cần hỗ trợ partial fill thật sự, phải mở rộng interface thêm
> `get_order_status(order_id)` hoặc `wait_order_terminal(order_id, timeout)`. Không để lửng.

---

## 4. Order Result Model

Return từ `place_order()`:

```python
OrderResult = {
    "success": bool,
    "order_status": str,          # ExchangeOrderStatus value
    "order_id": str | None,       # ID lệnh trên sàn (luôn string)
    "fill_price": float | None,   # Giá fill thực tế (None nếu chưa fill)
    "fill_volume": float | None,  # Volume fill thực tế
    "fill_volume_raw": int | None,# Volume fill gốc của sàn
    "error": str | None,          # Mô tả lỗi (chỉ khi fail)
    "error_category": str | None, # Loại lỗi chuẩn (xem Error Categories)
    "error_code": str | None,     # Mã lỗi gốc từ sàn
    "latency_ms": float,
    "raw_response": dict,         # Response gốc từ sàn (debug)
}
```

**Brain xử lý `place_order()` result (chỉ 3 cases):**

| Status | `success` | Brain Action |
|---|---|---|
| `FILLED` | `true` | ✅ Ghi DB, update pair state |
| `REJECTED` | `false` | ❌ Ghi failed, check `error_category` để quyết định retry |
| `UNKNOWN` | `false` | ⚠️ KHÔNG ghi gì — để Reconciler check sàn thật |

> [!NOTE]
> Brain **không bao giờ** nhận ACCEPTED, PARTIAL, CANCELLED, EXPIRED từ `place_order()`.
> Handler tự resolve bên trong. Nếu nhận status lạ → treat as UNKNOWN + log error.

---

## 5. Order Side Enum

Chuẩn cho tất cả exchange, Brain dùng enum này:

```python
class OrderSide(IntEnum):
    OPEN_LONG = 1      # Mở Long (mua)
    CLOSE_SHORT = 2    # Đóng Short
    OPEN_SHORT = 3     # Mở Short (bán)
    CLOSE_LONG = 4     # Đóng Long
```

**Mapping mỗi sàn:**

| OrderSide | MEXC | OKX | Binance |
|---|---|---|---|
| `OPEN_LONG` | side=1 | side=buy, posSide=long | side=BUY, positionSide=LONG |
| `CLOSE_SHORT` | side=2 | side=buy, posSide=short | side=BUY, positionSide=SHORT |
| `OPEN_SHORT` | side=3 | side=sell, posSide=short | side=SELL, positionSide=SHORT |
| `CLOSE_LONG` | side=4 | side=sell, posSide=long | side=SELL, positionSide=LONG |

---

## 6. Margin Mode & Leverage

```python
class MarginMode(IntEnum):
    ISOLATED = 1
    CROSS = 2
```

**Quy tắc:**
- Config hien chi can `leverage`; margin mode dang dung mac dinh cua handler
- Exchange handler tự set leverage + margin mode khi connect
- Nếu sàn không hỗ trợ mode đã chọn → log warning, dùng default của sàn

---

## 7. Volume — Paired Config (Không dùng đơn vị chung)

> Mỗi sàn có unit khác nhau (lot, contracts, coin). Thay vì convert qua đơn vị chung (dễ sai), **dùng config paired volumes trực tiếp**.

### Cách hoạt động

Config set sẵn volume cho **cả 2 bên** — user tự tính cho khớp nhau:

```json
{
  "mt5": {
    "volume": 0.01
  },
  "exchange": {
    "name": "mexc",
    "symbol": "XAUT_USDT",
    "volume": 1000,
    "volume_min": 1,
    "volume_step": 1
  }
}
```

- Brain **không convert**: dùng `config.mt5.volume` cho MT5, `config.exchange.volume` cho Exchange
- User chịu trách nhiệm đảm bảo 2 volume tương đương

### Exchange Handler validate

```python
class ExchangeHandler(ABC):
    @abstractmethod
    def validate_volume(self, volume: int|float) -> bool:
        """Check volume hợp lệ (min/max/step). Gọi trước khi đặt lệnh."""
        ...

    @abstractmethod
    def get_volume_info(self) -> dict:
        """Return {min, max, step, unit_name} — dùng cho validation + logging."""
        ...
```

### Ví dụ paired volumes cho 1 oz vàng

| MT5 | MEXC | OKX | Binance |
|---|---|---|---|
| 0.01 lot | 1000 contracts | 100 contracts | 1 coin |

> [!TIP]
> Khi thêm sàn mới, chỉ cần check trên sàn 1 contract = bao nhiêu → tính volume cho khớp với MT5 → ghi vào config.

---

## 8. Error Categories

Chuẩn hóa lỗi để Brain/Reconciler xử lý đúng, không cần biết lỗi cụ thể từ sàn:

```python
class ErrorCategory(str, Enum):
    # ─── Retryable (có thể retry ngay) ───
    TIMEOUT = "TIMEOUT"                # Request timeout
    RATE_LIMIT = "RATE_LIMIT"          # Bị rate limit, chờ rồi retry
    NETWORK = "NETWORK"                # Network error
    REQUOTE = "REQUOTE"                # Giá thay đổi, retry với giá mới
    BUSY = "BUSY"                      # Sàn đang bận

    # ─── Non-retryable (không retry, cần xử lý khác) ───
    AUTH_EXPIRED = "AUTH_EXPIRED"       # Token/API key hết hạn → alert
    INSUFFICIENT_BALANCE = "BALANCE"   # Không đủ margin → stop trading
    INVALID_VOLUME = "INVALID_VOL"     # Volume sai → check config
    SYMBOL_NOT_FOUND = "NO_SYMBOL"     # Symbol không tồn tại
    POSITION_NOT_FOUND = "NO_POS"      # Position không tồn tại (đã đóng?)
    MARKET_CLOSED = "MKT_CLOSED"       # Thị trường đóng cửa

    # ─── Critical (cần alert ngay) ───
    LIQUIDATION = "LIQUIDATION"        # Bị thanh lý
    ACCOUNT_FROZEN = "FROZEN"          # Tài khoản bị khóa
    UNKNOWN = "UNKNOWN"                # Lỗi không xác định
```

**Mỗi exchange handler phải map lỗi gốc → ErrorCategory:**

```python
# Trong MEXCHandler:
def _categorize_error(self, error_code: int, message: str) -> ErrorCategory:
    if error_code == 602:
        return ErrorCategory.INSUFFICIENT_BALANCE
    if "token" in message.lower() or error_code == 401:
        return ErrorCategory.AUTH_EXPIRED
    if error_code == 429:
        return ErrorCategory.RATE_LIMIT
    ...
    return ErrorCategory.UNKNOWN
```

**Brain xử lý theo category:**

| Category | Hành động |
|---|---|
| `TIMEOUT`, `NETWORK`, `REQUOTE`, `BUSY` | Retry tối đa 3 lần |
| `RATE_LIMIT` | Chờ 1-5s rồi retry |
| `AUTH_EXPIRED` | Alert Telegram, stop trading |
| `BALANCE` | Alert, stop entry (vẫn cho close) |
| `NO_POS` | Position đã đóng → update DB |
| `LIQUIDATION` | Alert khẩn cấp |
| `UNKNOWN` | Log, alert, không retry |

---

## Tóm tắt: Exchange Handler phải implement gì?

```python
class ExchangeHandler(ABC):
    # Lifecycle
    async def connect(self) -> None
    async def run_ws(self) -> None
    async def stop(self) -> None

    # Data (chuẩn hóa)
    def get_latest_tick(self) -> TickData | None
    async def get_positions(self) -> list[ExchangePosition]
    async def check_auth_alive(self) -> bool

    # Trading
    async def place_order(self, side: OrderSide, volume: int|float) -> OrderResult
    # volume = giá trị gốc từ config (e.g. 1000 contracts), KHÔNG convert

    # Volume validation
    def validate_volume(self, volume: int|float) -> bool
    def get_volume_info(self) -> dict  # {min, max, step, unit_name}

    # Properties
    @property
    def is_connected(self) -> bool
    @property
    def exchange_name(self) -> str  # "mexc", "okx", "binance"
```


