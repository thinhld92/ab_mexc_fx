# Reading Guide - Doc Hieu Project Nhanh

> Muc tieu cua file nay: giup doc code khong bi ngop.
> Khong can doc toan bo project ngay tu dau.

---

## 1. Nhin Tong The Truoc

Neu bo het chi tiet di, project nay chi dang lam 4 viec:

1. Lay gia tu MT5 va Exchange
2. Tinh spread, pivot, signal
3. Dat 2 lenh doi ung va luu state vao DB
4. Neu bi crash, lech state, orphan thi tu kiem tra va sua

No to khong phai vi strategy phuc tap.
No to vi phan van hanh duoc tach rieng de bot song sot khi chay live.

---

## 2. Cach Nho He Thong

Hay nho nhu sau:

- `MT5 Process`: tay chan ben MT5
- `Exchange Handler`: tay chan ben san crypto
- `Trading Brain`: bo nao ra quyet dinh
- `PairManager`: so cai ghi nho lenh dang o trang thai nao
- `Reconciler`: doi hau kiem, chuyen di kiem tra sai lech
- `Watchdog`: ong giam sat, thay process chet thi bat lai
- `SQLite + Redis`: mot ben la nho dai han, mot ben la giao tiep real-time

Neu nho duoc 7 vai tro nay thi da hieu hon 70% project.

---

## 3. Luong Chay That Su

Luong chay co the hieu bang 1 cau:

`Watchdog bat he thong -> MT5/Exchange day tick -> Brain tinh signal -> PairManager ghi state -> Reconciler kiem tra lech -> Watchdog giu process song`

Chi tiet hon:

1. `Watchdog` spawn 3 khoi chinh:
   - MT5 process
   - Core engine
   - Reconciler
2. `MT5 process` poll tick va nhan lenh dat/close ben MT5
3. `Exchange handler` nhan tick ben san va dat/close lenh crypto
4. `TradingBrain` nhan 2 ben tick, tinh:
   - `spread = MT5 - Exchange`
   - `pivot`
   - `gap = spread - pivot`
   - `signal = ENTRY_LONG / ENTRY_SHORT / CLOSE / NONE`
5. Khi co signal hop le, Brain dispatch lenh 2 ben
6. `PairManager` ghi DB xem pair dang:
   - `PENDING`
   - `ENTRY_SENT`
   - `OPEN`
   - `CLOSE_SENT`
   - `CLOSED`
   - hoac `ORPHAN_*`
7. `Reconciler` dinh ky so MT5 / Exchange / DB co khop nhau khong
8. `Watchdog` theo doi heartbeat, process nao chet thi restart

---

## 4. Neu Chi Doc 5 File Thi Doc File Nao

Day la thu tu doc de hieu nhanh nhat:

1. `config.json`
   - Xem bot dang trade product nao
   - Symbol nao
   - Pivot, spread band, volume la bao nhieu

2. `src/core/trading_brain.py`
   - Day la noi strategy song that
   - Muon hieu bot vao/ra lenh the nao, doc file nay dau tien

3. `src/core/pair_manager.py`
   - Day la noi giu trang thai pair
   - Muon hieu lenh dang o buoc nao, doc file nay

4. `src/exchanges/mexc_handler.py`
   - Day la noi dat lenh ben san crypto
   - Muon hieu exchange duoc goi ra sao, doc file nay

5. `src/reconciler/reconciler.py`
   - Day la noi "cua hau"
   - Muon hieu neu lech state/crash/orphan thi he thong sua the nao, doc file nay

Doc xong 5 file nay la da nam duoc project o muc thuc chien.

---

## 5. Tung Module Lam Gi

### `config.json`

Noi chon:

- product dang trade
- MT5 symbol
- exchange symbol
- mt5 volume
- exchange volume
- EMA pivot
- warmup pivot
- deviation entry/close
- trading hours
- force close hours

Hay xem file nay la "bang dieu khien" cua bot.

### `src/watchdog/watchdog.py`

Nhiem vu:

- bat MT5, Core, Reconciler
- theo doi heartbeat
- restart process loi
- shutdown dung thu tu

File nay khong trade.
No chi giu he thong song.

### `src/mt5_process/main.py`

Nhiem vu:

- ket noi MetaTrader 5
- bat worker lay tick
- bat worker dat lenh
- gui heartbeat

Day la lop cach ly MT5 ra khoi Brain.
Ly do: MT5 API de treo va khong thread-safe.

### `src/mt5_process/tick_worker.py`

Nhiem vu:

- poll tick MT5
- day tick vao Redis
- theo doi tracked ticket
- neu position MT5 bien mat thi bao ngay

### `src/mt5_process/order_worker.py`

Nhiem vu:

- nhan lenh tu Redis queue
- goi MT5 dat BUY/SELL/CLOSE
- tra result ve Redis
- recover command dang do khi restart

### `src/core/engine.py`

Nhiem vu:

- bootstrap phan core
- mo DB
- tao exchange handler
- tao PairManager
- tao TradingBrain

Co the xem day la file "lap rap bo nao".

### `src/core/trading_brain.py`

Day la file quan trong nhat.

No lam cac viec:

- nhan tick MT5 + Exchange
- tinh spread
- cap nhat pivot EMA
- tim signal
- debounce signal
- check gate an toan truoc khi vao lenh
- dat entry/close
- enforce safety runtime

Neu dai ca chi muon biet "logic trade o dau", thi cau tra loi la file nay.

### `src/core/pair_manager.py`

Nhiem vu:

- tao pair moi
- update state pair
- giu cache in-memory
- dong bo tracked MT5 tickets
- phan biet OPEN / CLOSE_SENT / ORPHAN / CLOSED ...

No khong tinh signal.
No chi giu su that cua pair.

### `src/exchanges/base.py`

Nhiem vu:

- dinh nghia interface chung cho san
- enum status
- `OrderResult`

No ton tai de sau nay co the doi MEXC sang san khac ma it sua Brain.

### `src/exchanges/mexc_handler.py`

Nhiem vu:

- websocket ticker MEXC
- dat lenh market
- check auth
- lay position
- resolve terminal state cua lenh

Day la lop noi chuyen truc tiep voi san.

### `src/reconciler/reconciler.py`

Nhiem vu:

- chay dinh ky
- so DB voi MT5 va Exchange
- phat hien stale state
- phat hien orphan
- auto-heal neu duoc phep
- ghi recon log

File nay khong phai strategy.
No la lop "kiem toan va cuu ho".

### `src/storage/`

Nhiem vu:

- quan ly SQLite
- repository cho `pairs`, `pair_events`, `recon_log`

Co the xem day la data layer.

### `src/utils/`

Nhiem vu:

- `config.py`: doc config
- `constants.py`: Redis keys, constants
- `logger.py`: log console
- `spread_logger.py`: ghi CSV spread

Phan nay la utilities, khong can doc ky tu dau.

---

## 6. Nen Doc Theo Thu Tu Nao

Neu dai ca dang bi roi, doc theo dung thu tu nay:

1. `config.json`
2. `src/core/trading_brain.py`
3. `src/core/pair_manager.py`
4. `src/exchanges/mexc_handler.py`
5. `src/mt5_process/main.py`
6. `src/reconciler/reconciler.py`
7. `src/watchdog/watchdog.py`

Thu tu nay di tu "bot trade gi" -> "bot dat lenh the nao" -> "bot tu cuu ra sao".

---

## 7. Nhung Phan Co The Bo Qua Luc Dau

Luc doc lan dau, co the tam bo qua:

- chi tiet toan bo `tests/`
- het cac file trong `guides/`
- toan bo repository/storage implementation chi tiet
- cac helper nho trong `utils/`

Doc som qua nhung phan nay se rat de bi "loan thong tin".

---

## 8. 3 Cau Hoi De Tu Kiem Tra Da Hieu Chua

Neu tra loi duoc 3 cau nay la dai ca da vao flow:

1. Tick MT5 va tick Exchange di vao dau?
   - vao `TradingBrain`

2. Signal trade duoc tinh o dau?
   - o `TradingBrain`

3. Neu 1 ben khop, 1 ben khong khop thi ai xu ly?
   - `PairManager` ghi state, `Reconciler` theo sau de heal

---

## 9. Cach Nghi Don Gian Hon

Dung nghi day la 1 project gom 50 file.
Hay nghi day la 3 tang:

- Tang 1: `lay gia + dat lenh`
- Tang 2: `ra quyet dinh + luu state`
- Tang 3: `kiem tra loi + restart + heal`

Moi file trong project chi la mot manh cua 3 tang nay.

---

## 10. Ket Luan

Neu dai ca moi gap kieu kien truc nay lan dau thi bi ngop la chuyen binh thuong.
Project nay khong kho vi cong thuc trade kho.
No kho vi phan van hanh duoc tach thanh nhieu lop.

Chi can nho:

- `TradingBrain` = logic trade
- `PairManager` = state pair
- `MEXCHandler` = lenh san
- `MT5 Process` = lenh MT5
- `Reconciler` = hau kiem
- `Watchdog` = giam sat

Nam 6 cai ten nay la dai ca da co ban do trong dau roi.
