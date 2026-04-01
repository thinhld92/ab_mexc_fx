"""Accurate simulation using ACTUAL bid/ask execution prices."""
import csv, os
from collections import defaultdict

DATA_DIR = r"d:\thinhld\python\botcryptoabv1\data"
COMMISSION = 1.51  # commission only (no bid-ask — we use real prices now)
MIN_HOLD = 180

def parse_time(ts, do=0):
    p=ts.split(":"); sp=p[2].split(".")
    return do*86400+int(p[0])*3600+int(p[1])*60+int(sp[0])+(int(sp[1])/1000 if len(sp)>1 else 0)

def load_all():
    ticks=[]
    for i,f in enumerate(sorted(x for x in os.listdir(DATA_DIR) if x.endswith(".csv"))):
        d=f.replace("spread_","").replace(".csv","")
        for row in csv.DictReader(open(os.path.join(DATA_DIR,f))):
            try:
                ticks.append({
                    "t": parse_time(row["time"],i),
                    "mt5_bid": float(row["mt5_bid"]),
                    "mt5_ask": float(row["mt5_ask"]),
                    "mt5_mid": float(row["mt5_mid"]),
                    "mexc_bid": float(row["mexc_bid"]),
                    "mexc_ask": float(row["mexc_ask"]),
                    "mexc_mid": float(row["mexc_mid"]),
                    "mt5_sp": float(row["mt5_sp"]),
                    "mexc_sp": float(row["mexc_sp"]),
                    "spread_mid": float(row["spread"]),
                    "ts": row["time"],
                    "day": d,
                })
            except: pass
    return ticks

def sim_accurate(ticks, ed, cd, ew, use_real_prices=True):
    """
    use_real_prices=True:  entry/close use actual bid/ask
    use_real_prices=False: entry/close use mid (old inaccurate way)
    """
    ema = ticks[0]["spread_mid"]; pt = ticks[0]["t"]
    wu = pt + ew; pos = 0; es = et = 0.0; trades = []

    for tk in ticks:
        t = tk["t"]; s_mid = tk["spread_mid"]
        # Execution spreads
        s_short = tk["mt5_bid"] - tk["mexc_ask"]  # SELL FX + BUY Crypto
        s_long  = tk["mt5_ask"] - tk["mexc_bid"]  # BUY FX + SELL Crypto

        e = t - pt
        if 0 < e < 60:
            a = 1-(1-2/(ew+1))**e; ema = a*s_mid+(1-a)*ema
        elif e >= 60: ema = s_mid
        pt = t
        if t < wu: continue

        dev_mid   = s_mid - ema
        dev_short = s_short - ema
        dev_long  = s_long - ema

        if pos == 0:
            if use_real_prices:
                # SHORT: use actual sell price (s_short)
                if dev_short >= ed:
                    pos, es, et = 1, s_short, t
                # LONG: use actual buy price (s_long)
                elif dev_long <= -ed:
                    pos, es, et = 2, s_long, t
            else:
                if dev_mid >= ed:
                    pos, es, et = 1, s_mid, t
                elif dev_mid <= -ed:
                    pos, es, et = 2, s_mid, t

        elif pos == 1:  # SHORT position
            hold = t - et
            if hold >= MIN_HOLD:
                if use_real_prices:
                    # Close SHORT = BUY FX + SELL Crypto = s_long
                    if dev_long <= cd:
                        pnl = es - s_long - COMMISSION
                        trades.append({"pnl":pnl,"hold":hold,"date":tk["day"],"end":False,
                            "ba_entry": s_mid - s_short, "ba_exit": s_long - s_mid})
                        pos = 0
                else:
                    if dev_mid <= cd:
                        pnl = es - s_mid - COMMISSION
                        trades.append({"pnl":pnl,"hold":hold,"date":tk["day"],"end":False,
                            "ba_entry":0, "ba_exit":0})
                        pos = 0

        elif pos == 2:  # LONG position
            hold = t - et
            if hold >= MIN_HOLD:
                if use_real_prices:
                    # Close LONG = SELL FX + BUY Crypto = s_short
                    if dev_short >= -cd:
                        pnl = s_short - es - COMMISSION
                        trades.append({"pnl":pnl,"hold":hold,"date":tk["day"],"end":False,
                            "ba_entry": s_long - s_mid, "ba_exit": s_mid - s_short})
                        pos = 0
                else:
                    if dev_mid >= -cd:
                        pnl = s_mid - es - COMMISSION
                        trades.append({"pnl":pnl,"hold":hold,"date":tk["day"],"end":False,
                            "ba_entry":0, "ba_exit":0})
                        pos = 0

    if pos:
        tk = ticks[-1]; s_mid = tk["spread_mid"]
        s_long = tk["mt5_ask"] - tk["mexc_bid"]
        s_short = tk["mt5_bid"] - tk["mexc_ask"]
        if pos == 1:
            pnl = es - (s_long if use_real_prices else s_mid) - COMMISSION
        else:
            pnl = (s_short if use_real_prices else s_mid) - es - COMMISSION
        trades.append({"pnl":pnl,"hold":ticks[-1]["t"]-et,"date":"END","end":True,"ba_entry":0,"ba_exit":0})
    return trades

def stats(trades, nd):
    real=[t for t in trades if not t["end"]]
    n=len(real)
    if n==0: return None
    w=sum(1 for t in real if t["pnl"]>0); tot=sum(t["pnl"] for t in real)
    ah=sum(t["hold"] for t in real)/n; worst=min(t["pnl"] for t in real)
    avg_ba = sum(t["ba_entry"]+t["ba_exit"] for t in real)/n if n > 0 else 0
    bd=defaultdict(list)
    for t in real: bd[t["date"]].append(t)
    dc="/".join(str(len(bd[d])) for d in sorted(bd))
    return {"n":n,"tpd":n/nd,"w":w,"wr":w/n*100,"tot":tot,"avg":tot/n,"worst":worst,"ah":ah,"dc":dc,"avg_ba":avg_ba}

ticks = load_all()
nd = len(set(tk["day"] for tk in ticks))
print(f"Loaded {len(ticks):,} ticks | {nd} days")

# Compute average bid-ask spreads
mt5_sps = [tk["mt5_sp"] for tk in ticks]
mexc_sps = [tk["mexc_sp"] for tk in ticks]
print(f"\nBid-Ask Spread Stats:")
print(f"  MT5:  avg={sum(mt5_sps)/len(mt5_sps):.4f}  min={min(mt5_sps):.4f}  max={max(mt5_sps):.4f}")
print(f"  MEXC: avg={sum(mexc_sps)/len(mexc_sps):.4f}  min={min(mexc_sps):.4f}  max={max(mexc_sps):.4f}")
total_ba = sum(mt5_sps)/len(mt5_sps) + sum(mexc_sps)/len(mexc_sps)
print(f"  Total BA cost: ~{total_ba:.4f} per side, ~{total_ba*2:.4f} per round trip")

# =====================================================================
print(f"\n{'='*120}")
print(f"  HEAD-TO-HEAD: Mid (old) vs Real Bid/Ask (new)")
print(f"{'='*120}")

hdr = f"  {'Config':<40} | {'Tr':>4} {'~/d':>4} {'Win%':>5} | {'Total':>8} {'Avg':>7} {'Worst':>7} {'':>2} | {'Hold':>5} {'AvgBA':>6} | Day"
sep = f"  {'─'*40} | {'─'*4} {'─'*4} {'─'*5} | {'─'*8} {'─'*7} {'─'*7} {'─'*2} | {'─'*5} {'─'*6} | {'─'*12}"

def prow(label, s):
    if not s: return
    sf="ok" if s["worst"]>=0 else "!!"
    print(f"  {label:<40} | {s['n']:>4} {s['tpd']:>4.0f} {s['wr']:>4.0f}% | {s['tot']:>+8.1f} {s['avg']:>+7.2f} {s['worst']:>+7.2f} {sf} | {s['ah']/60:>4.1f}m {s['avg_ba']:>5.2f} | {s['dc']}")

for ew in [900]:
    for ed in [2.0, 2.5, 3.0]:
        for cd in [0.00, -0.50, -1.00, -1.50, -1.75]:
            print(hdr); print(sep)
            s_mid = stats(sim_accurate(ticks, ed, cd, ew, False), nd)
            s_real = stats(sim_accurate(ticks, ed, cd, ew, True), nd)
            prow(f"MID  EMA15 | {ed:.1f}/{cd:+.2f}", s_mid)
            prow(f"REAL EMA15 | {ed:.1f}/{cd:+.2f}", s_real)
            if s_mid and s_real:
                diff = s_real["tot"] - s_mid["tot"]
                diff_avg = s_real["avg"] - s_mid["avg"] if s_real["n"] > 0 else 0
                print(f"  {'DIFF':>40} |      {'':>4} {'':>5} | {diff:>+8.1f} {diff_avg:>+7.2f} {'':>7}    |")
            print()

# =====================================================================
print(f"\n{'='*120}")
print(f"  REAL BID/ASK ONLY — Best configs (50-100 trades/day, sorted by PnL)")
print(f"{'='*120}")
print(hdr); print(sep)

results = []
for ed in [2.0, 2.5, 3.0]:
    for cd in [0.00, -0.25, -0.50, -0.75, -1.00, -1.25, -1.50, -1.75, -2.00]:
        s = stats(sim_accurate(ticks, ed, cd, 900, True), nd)
        if s and 40 <= s["tpd"] <= 120:
            results.append((f"REAL | {ed:.1f}/{cd:+.2f}", s))

results.sort(key=lambda x: x[1]["tot"], reverse=True)
for label, s in results[:20]:
    prow(label, s)

# Safe only
safe = [(l,s) for l,s in results if s["worst"] >= 0]
print(f"\n  SAFE ONLY (worst >= $0): {len(safe)} configs")
print(hdr); print(sep)
for label, s in safe[:15]:
    prow(label, s)

# Per-day for top 3 safe
print(f"\n  PER-DAY — Top 3 Safe (Real)")
for i, (label, s) in enumerate(safe[:3]):
    real = [t for t in sim_accurate(ticks, float(label.split("|")[1].split("/")[0]), float(label.split("/")[1]), 900, True) if not t["end"]]
    bd = defaultdict(list)
    for t in real: bd[t["date"]].append(t)
    print(f"\n  #{i+1} {label} | {s['n']} trades ({s['tpd']:.0f}/day) | PnL: {s['tot']:+.1f} | Worst: {s['worst']:+.2f}")
    for day in sorted(bd):
        dt=bd[day]; dn=len(dt); dw=sum(1 for t in dt if t["pnl"]>0); dp=sum(t["pnl"] for t in dt)
        print(f"    {day}: {dn} trades, win={dw}/{dn} ({dw/dn*100:.0f}%), pnl={dp:+.1f}, worst={min(t['pnl'] for t in dt):+.2f}")

print("\nDone!")
