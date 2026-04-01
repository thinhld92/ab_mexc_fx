# Development Rules & Conventions

> Tất cả developer (và AI) phải tuân theo các quy tắc này.

## 1. Timestamp — UTC Only

> **Mọi timestamp lưu trữ phải là UTC (GMT+0). Không bao giờ lưu local time.**

| Nơi lưu | Format | Cách dùng |
|---|---|---|
| DB columns (`entry_time`, `close_time`, `run_at`, ...) | Unix epoch UTC | `time.time()` |
| DB `created_at` / `updated_at` | ISO string UTC | `strftime('%Y-%m-%dT%H:%M:%fZ','now')` |
| Event JSON data | Unix epoch UTC | `time.time()` |
| Redis tick `local_ts` | Unix epoch UTC | `time.time()` |
| Redis heartbeat | Unix epoch UTC | `time.time()` |
| SpreadLogger CSV `time` column | UTC `HH:MM:SS.fff` | `datetime.utcnow()` |
| Console log | **Local time (chỉ display)** | Logger tự convert |

**Lý do**: Hệ thống có thể chạy trên VPS ở nhiều timezone khác nhau. UTC đảm bảo dữ liệu nhất quán. Frontend/web dashboard sẽ convert sang local khi hiển thị.

## 2. Language — English Codebase

- Tất cả code, comments, variable names, function names bằng **tiếng Anh**
- Tài liệu trong `guides/` có thể dùng tiếng Việt
- Log messages bằng tiếng Anh
- **Pre-execution Gate Check**: sau debounce timer fire, PHẢI re-check 5 gates trước khi execute: (1) signal còn đúng, (2) tick còn fresh, (3) risk flag, (4) pair state chưa bị đổi, (5) concurrency guard. Xem `architecture.md` → Pre-execution Gate Check

## 3. Database Rules

- **SQLite WAL mode** — cho phép concurrent read/write
- **Không dùng ORM** — raw SQL cho performance, dễ debug
- **Mọi state thay đổi phải ghi `pair_events`** — audit trail bắt buộc
- **DB file path**: `data/arb.db` (relative to project root)
- **`PRAGMA busy_timeout = 5000`** — Brain + Reconciler cùng write, cần timeout đủ lớn
- **Backup**: copy file DB định kỳ (future: automated)

## 4. Redis Rules

- Redis chỉ dùng cho **real-time IPC** (tick, order queue, heartbeat, shutdown signal)
- **Không dùng Redis cho persistent state** — đã chuyển sang SQLite
- Tick dùng `SET` + `PUBLISH`: `SET` là latest snapshot, `PUBLISH` là real-time wake-up event. Không dùng `PUBLISH` làm source of truth.
- Order queue: **Reliable queue** (`BRPOPLPUSH`) — command chuyển từ `mt5:order:queue` sang `mt5:order:processing`, chỉ xóa sau khi xong. MT5 restart phải check processing list trước.
- Heartbeat dùng `SET` + `EX` (TTL tự expire)

## 5. Process Isolation

- **MT5 = process riêng** — crash không ảnh hưởng Brain. **Chỉ MT5 Process được gọi MT5 API** — Brain/Reconciler/Watchdog KHÔNG bao giờ gọi trực tiếp, luôn qua Redis.
- **Brain = process riêng** — crash có thể restart, state trong DB
- **Tối đa 1 pair mở cùng lúc** (`max_orders = 1`) — tránh bị gộp vị thế (position merge) trên sàn crypto. Sàn crypto gộp nhiều order cùng direction thành 1 position → khó match khi reconcile.
- **Reconciler = process riêng** — không block Brain
- Giao tiếp giữa processes **chỉ qua Redis**
- Mỗi process phải publish **heartbeat** định kỳ
- **Reconciler tạo ExchangeHandler instance riêng** (chỉ REST, không WS) — tránh race condition với Brain
- **MT5 API không thread-safe** — tất cả MT5 calls phải đi qua chung 1 `threading.Lock`
- **Position Monitor (30ms) ≠ Reconciler (30s)**: Monitor = phát hiện mất position nhanh (chỉ tracked tickets), Reconciler = audit toàn diện 3 nguồn. Hai tầng bổ trợ.
- Position Monitor **chỉ chạy khi có tracked_tickets** — không cần check khi không có lệnh

## 6. Error Handling

- Tất cả vòng lặp chính phải có `try/except` bọc ngoài
- Không để exception chết âm thầm — phải log
- Orphan position = **highest priority** — phải detect và xử lý
- Watchdog auto-restart, tối đa 3 lần trong 5 phút

## 7. Order Safety

- **Cả 2 sàn phải được đặt lệnh** trước khi coi là thành công
- Nếu 1 bên fail → đánh dấu ORPHAN → Reconciler xử lý
- Timeout chờ MT5 result: 5 giây
- Cooldown giữa các lệnh entry: config `cooldown_entry_sec`
- Cooldown giữa các lệnh close: config `cooldown_close_sec`

## 8. Reconciliation

- Chạy mỗi **30-60 giây** (configurable)
- So sánh 3 nguồn: MT5 actual ↔ Exchange actual ↔ DB local
- Auto-heal orphan positions (configurable on/off)
- Ghost positions (sàn có, local không): alert hoặc auto-close (configurable)
- Log mỗi lần chạy vào `recon_log` table

## 9. Exchange Abstraction

- Tất cả exchange (MEXC, OKX, Binance...) phải implement `ExchangeHandler` interface
- **Brain, Engine, Reconciler không được import trực tiếp** `MEXCHandler` hay `OKXHandler`
- Chỉ import `ExchangeHandler` (abstract) + dùng factory `create_exchange(config)`
- Tick format chuẩn: `{bid, ask, last, ts, local_ts}` — mọi exchange trả cùng format
- Order result chuẩn: `OrderResult` — xem `exchange_standards.md` Section 4 (`order_status`, `fill_price`, `fill_volume`, `error_category`...)
- Sàn mới = chỉ thêm 1 file trong `src/exchanges/`, không sửa Brain/Engine
