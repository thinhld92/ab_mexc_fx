# Requirements — Arbitrage Trading Bot

> Tổng hợp toàn bộ yêu cầu từ thảo luận kiến trúc (2026-03-26)

## Mục tiêu chính

Trade chênh lệch giá (arbitrage) giữa sàn FX (qua MT5) và sàn Crypto Futures.
Hệ thống chạy **24/7 tự động, không cần giám sát**.
Cùng 1 codebase có thể deploy trên nhiều VPS với exchange khác nhau (MEXC, OKX, Binance...).

## Yêu cầu chức năng

### 1. Core Trading
- Đọc tick real-time từ MT5 và Crypto exchange (qua Exchange Handler interface)
- Tính spread = MT5 mid - Crypto mid
- Phát hiện signal: ENTRY_LONG, ENTRY_SHORT, CLOSE (dựa trên deviation thresholds)
- Đặt lệnh đồng thời trên cả 2 sàn
- Brain chỉ gọi interface, không biết exchange cụ thể

### 2. Exchange Abstraction (Multi-Exchange)
- Abstract `ExchangeHandler` interface — tất cả exchange implement cùng 1 interface
- Brain/Engine/Reconciler gọi interface, không hardcode exchange nào
- Config chọn exchange: `"exchange": {"name": "mexc"}` hoặc `"name": "okx"`
- Mỗi VPS deploy với exchange khác nhau, chỉ đổi config
- Codebase structure: `src/exchanges/base.py` + `src/exchanges/mexc_handler.py` + ...
- Interface methods: `connect()`, `run_ws()`, `get_latest_tick()`, `place_order()`, `get_positions()`, `check_auth_alive()`, `stop()`

### 3. Database (SQLite) — 7 chức năng
1. **Sổ cái** — ghi lại mọi lệnh (thay CSV cũ)
2. **Lưu state** — trạng thái positions đang mở (thay Redis blob)
3. **Reconciliation** — log so khớp local vs sàn
4. **Crash recovery** — khôi phục state khi restart
5. **Audit trail** — lịch sử thay đổi trạng thái mỗi pair
6. **Analytics** — query profit, win rate, latency
7. **Web dashboard** (data source cho tương lai)

### 4. MT5 Process riêng
- Tách khỏi Brain, chạy process độc lập
- 2 thread: Tick polling (không bị block) + Order execution
- Giao tiếp qua Redis (SET cho tick, LIST cho order queue)
- Crash không ảnh hưởng Brain

### 5. Reconciler
- Chạy mỗi 30-60s, process riêng
- So khớp 3 nguồn: MT5 thật ↔ Exchange thật ↔ DB local
- Auto-detect: orphan, ghost, stale pending, volume mismatch
- Auto-heal: tự đóng orphan positions
- Ghi log mỗi lần chạy

### 6. Watchdog
- Monitor heartbeat tất cả processes
- Auto-restart nếu process chết
- Giới hạn: tối đa 3 restart trong 5 phút
- Alert qua Telegram nếu vượt giới hạn

### 7. Bỏ Accountant
- Module Accountant bị xóa hoàn toàn
- Database thay thế vai trò ghi sổ
- Theo dõi qua web dashboard hoặc query DB trực tiếp

## Yêu cầu kỹ thuật

### Timestamp
- **Tất cả timestamps lưu trữ phải là UTC (GMT+0)**
- Dùng `time.time()` (Unix epoch UTC) cho columns số
- Dùng `strftime('%Y-%m-%dT%H:%M:%fZ','now')` cho columns text
- Chỉ convert sang local time khi hiển thị trên console/UI

### Performance
- Brain: **event-driven** — react khi có tick mới (PUB/SUB + WS callback), không poll loop
- Signal detection: **debounce timer** (`stable_time_ms`) — chỉ entry/close khi signal ổn định
- Risk detection: **immediate** — stop-out/liquidation xử lý không qua debounce
- MT5 tick polling: ~1ms/cycle (Tick Thread)
- MT5 position monitor: ~30ms/cycle (detect stop-out nhanh)
- SQLite write: chỉ khi có lệnh (entry/close), **write-ahead** (INSERT trước khi gửi lệnh)
- Reconciler: async HTTP calls, không block Brain

### Safety
- 15-state lifecycle: track chính xác lệnh đang ở giai đoạn nào
- Write-ahead: INSERT DB trước khi gửi lệnh → không mất lệnh khi crash
- Mọi lệnh phải fill cả 2 bên, nếu 1 bên fail → ORPHAN
- Reconciler tự xử lý orphan + stale states
- `close_reason`: biết chính xác ai đóng, vì lý do gì
- Force close khi đến giờ blackout (configurable)
- Auth/token check exchange định kỳ (qua interface `check_auth_alive()`)