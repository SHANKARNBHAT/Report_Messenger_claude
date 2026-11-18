"""Backtest of HA WK BRK (ha_weekly.py) on the F&O stocks' last 60 days of Yahoo 5-minute bars.
Setup from last week's Weekly HA candle (Yahoo 1wk, 2y), armed when the running 4H HA close (built
from the 5-minute bars, live, bar by bar) crosses its HA high / low, entered on the first 5-minute bar
that reaches the 30-minute SMA 8 after that. SL variants; exits by ha_pure.replay (30m trail, expiry
3:15 PM). One position per stock at a time; 0.05% round-trip cost.
Usage: python wk_backtest.py [refresh]"""
import os
import pickle
import statistics as stx
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, time, timedelta

sys.path.insert(0, r"C:\Users\shank\Report_Messenger_claude")
import futures_simulator as fs  # noqa: E402
import ha_pure  # noqa: E402
import ha_weekly as hw  # noqa: E402

IST = fs.IST
COST = 0.05
CACHE = os.path.join(os.environ.get("TEMP", "."), "wk_data.pkl")
instruments = {i["symbol"]: i for i in fs.load_instruments() if i.get("yahoo")}

if os.path.exists(CACHE) and "refresh" not in sys.argv:
    data = pickle.load(open(CACHE, "rb"))
else:
    def get(sym):
        y = instruments[sym]["yahoo"]
        try:
            five = fs.yahoo_chart(y, "5m", "60d")
            q = five["indicators"]["quote"][0]
            bars = [r for r in zip(five["timestamp"], q["open"], q["high"], q["low"], q["close"]) if None not in r]
            wk = fs.yahoo_chart(y, "1wk", "2y")
            q = wk["indicators"]["quote"][0]
            weeks = [r for r in zip(wk["timestamp"], q["open"], q["high"], q["low"], q["close"]) if None not in r]
            return sym, (bars, weeks)
        except Exception as exc:  # noqa: BLE001
            return sym, str(exc)
    with ThreadPoolExecutor(8) as pool:
        data = dict(pool.map(get, sorted(instruments)))
    pickle.dump(data, open(CACHE, "wb"))


def heikin(rows):
    """rows (t, o, h, l, c) -> list of (t, ha_open, ha_high, ha_low, ha_close)."""
    out = []
    for i, (t, o, h, l, c) in enumerate(rows):
        hc = (o + h + l + c) / 4
        ho = (o + c) / 2 if i == 0 else (out[-1][1] + out[-1][4]) / 2
        out.append((t, ho, max(h, ho, hc), min(l, ho, hc), hc))
    return out


def four_key(ts):
    m = datetime.fromtimestamp(ts, IST)
    return m.date(), 0 if m.time() < time(13, 15) else 1


def atr30(candles, at, n=14):
    """ATR(n) of the 30-minute candles finished by `at`."""
    fin = [(s, b) for s, b in sorted(candles.items()) if ha_pure.trail_end(s, IST) <= at]
    trs, prev = [], None
    for _, b in fin:
        h, l, c = max(x[2] for x in b), min(x[3] for x in b), b[-1][4]
        trs.append(h - l if prev is None else max(h - l, abs(h - prev), abs(l - prev)))
        prev = c
    if len(trs) < n:
        return None
    a = sum(trs[:n]) / n
    for tr in trs[n:]:
        a = (a * (n - 1) + tr) / n
    return a


def stops(bars, candles, entry, long, bar):
    """SL variants for an entry on the bar starting at `bar`."""
    before = [b for b in bars if b[0] <= bar]
    fin = ha_pure.last_trail_candle(before, bar, IST)
    run = [b for b in before if ha_pure.trail_start(b[0], IST) == ha_pure.trail_start(bar, IST)]
    runx = (min(b[3] for b in run), max(b[2] for b in run)) if run else None
    a = hw.initial_stop(bars, [], entry, long, bar, IST)
    opts = [x for x in (fin, runx) if x]
    far = min(x[0] for x in opts) if long else max(x[1] for x in opts)
    if (far >= entry) if long else (far <= entry):
        far = a
    atr = atr30(candles, bar)
    out = {"A 30m candle (as built)": a, "B farther of last + running 30m": far}
    if far is not None and atr:
        for k in (1, 1.5, 2):
            d = max(abs(entry - far), k * atr)
            out[f"C B, at least {k} x 30m ATR"] = entry - d if long else entry + d
    for pct in (0.5, 1.0):
        d = max(abs(entry - far), entry * pct / 100) if far is not None else entry * pct / 100
        out[f"D B, at least {pct}%"] = entry - d if long else entry + d
    return out


results = {}
skipped = 0
for sym, got in data.items():
    if isinstance(got, str) or not got[0] or not got[1]:
        skipped += 1
        continue
    bars, weeks = got
    bars = [b for b in bars if time(9, 15) <= datetime.fromtimestamp(b[0], IST).time() < time(15, 30)]
    if len(bars) < 500:
        skipped += 1
        continue
    end_now = datetime.fromtimestamp(bars[-1][0] + 300, IST)
    wha = heikin(weeks)
    # Weekly setups by the Monday of the week they trade in.
    setups = {}
    for i in range(1, len(wha)):
        prev, cur = wha[i - 1], wha[i]
        monday = hw.monday_of(datetime.fromtimestamp(cur[0], IST).date())
        if prev[4] < prev[1]:
            setups[monday] = ("BUY", prev[2])
        elif prev[4] > prev[1]:
            setups[monday] = ("SELL", prev[3])
    # 4H candles from the 5-minute bars, HA of the finished ones; running HA close bar by bar.
    groups = {}
    for b in bars:
        groups.setdefault(four_key(b[0]), []).append(b)
    keys = sorted(groups)
    fh = heikin([(groups[k][0][0], groups[k][0][1], max(x[2] for x in groups[k]), min(x[3] for x in groups[k]),
                  groups[k][-1][4]) for k in keys])
    ha_open = {k: fh[i][1] for i, k in enumerate(keys)}
    candles = hw.thirty_minute(bars, IST)
    first_day = datetime.fromtimestamp(bars[0][0], IST).date() + timedelta(days=10)  # let HA / SMA settle
    open_until = 0
    by_week = {}
    for b in bars:
        d = datetime.fromtimestamp(b[0], IST).date()
        if d >= first_day:
            by_week.setdefault(hw.monday_of(d), []).append(b)
    lot = instruments[sym]["lot"]
    for monday, wbars in sorted(by_week.items()):
        setup = setups.get(monday)
        if not setup:
            continue
        side, level = setup
        long = side == "BUY"
        armed = None
        hi = lo = None
        cur = None
        for b in wbars:
            k = four_key(b[0])
            if k != cur:
                cur, o4, hi, lo = k, b[1], b[2], b[3]
            hi, lo = max(hi, b[2]), min(lo, b[3])
            hc = (o4 + hi + lo + b[4]) / 4
            if hc > level if long else hc < level:
                armed = b[0] + 300
                break
        if armed is None:
            continue
        for b in wbars:
            if b[0] < armed or datetime.fromtimestamp(b[0] + 300, IST).time() > time(15, 0):
                continue
            sma = hw.sma_before(candles, b[0], IST)
            if sma is None or not (b[3] <= sma if long else b[2] >= sma):
                continue
            entry = min(b[1], sma) if long else max(b[1], sma)
            at = b[0] + 300
            if at < open_until:
                break
            sl = hw.initial_stop(bars, [], entry, long, b[0], IST)
            pos = {"id": "x", "side": side, "sl": sl, "stop": sl, "entry": entry, "time": at * 1000}
            cutoff = datetime.combine(fs.expiry_after(datetime.fromtimestamp(at, IST).date()), time(15, 15), IST).timestamp()
            r = hw.replay(pos, bars, [], None, cutoff, end_now, IST)
            exitp = r.get("exit", bars[-1][4])
            pct = ((exitp - entry) if long else (entry - exitp)) / entry * 100 - COST
            name = "LIVE CODE (ha_weekly)"
            results.setdefault(name, []).append({
                "sym": sym, "side": side, "pct": pct, "rs": pct / 100 * entry * lot, "risk": abs(entry - sl) / entry * 100,
                "open": r["status"] == "open", "reason": r.get("reason", "open"),
                "held": ((r.get("exit_time", end_now.timestamp() * 1000) / 1000) - at) / 3600, "week": monday})
            open_until = r.get("exit_time", 1e15) / 1000
            break

print(f"{len(data) - skipped} stocks, 5m bars ~60 days; skipped {skipped}")
for name, xs in results.items():
    w = [x for x in xs if x["pct"] > 0]
    gains, losses = sum(x["pct"] for x in w), -sum(x["pct"] for x in xs if x["pct"] <= 0)
    longs = [x for x in xs if x["side"] == "BUY"]
    print(f"{name:34s} n={len(xs):4d} (long {len(longs)}) win={100 * len(w) / len(xs):3.0f}% "
          f"avg={stx.mean(x['pct'] for x in xs):+.2f}% PF={gains / losses if losses else 0:4.2f} "
          f"net Rs/lot={sum(x['rs'] for x in xs):+,.0f} risk med={stx.median(x['risk'] for x in xs):.2f}% "
          f"held med={stx.median(x['held'] for x in xs):.1f}h reasons={dict(__import__('collections').Counter(x['reason'] for x in xs))}")
    if name.startswith("LIVE"):
        for side in ("BUY", "SELL"):
            s = [x for x in xs if x["side"] == side]
            if s:
                print(f"    {side}: n={len(s)} win={100 * sum(x['pct'] > 0 for x in s) / len(s):.0f}% avg={stx.mean(x['pct'] for x in s):+.2f}%")
        weeks = {}
        for x in xs:
            weeks.setdefault(x["week"], []).append(x["pct"])
        print("    by week:", ", ".join(f"{w:%d %b} {len(v)}:{sum(v):+.0f}%" for w, v in sorted(weeks.items())))
