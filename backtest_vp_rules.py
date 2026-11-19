"""Backtest of three volume profile rules on NIFTY, BANKNIFTY and the NIFTY 50 stocks, over about
two years of Yahoo 60-minute bars (volume_profile.py's bars and levels: an index's volume is its
member stocks' turnover). Each session is judged against the levels as they stood at the previous
close, for four profiles: Weekly and Monthly (the last finished week / month), 3M and 1Y (rolling).

  Rule 1/2 Breakout: a close outside the value area after a close inside it (above VAH: long,
           below VAL: short). "Ahead" is the zone the move would cross next, from the edge to the
           carry-on target, read on the 1Y profile: HV ahead when that zone's volume per bin is at
           least the 1Y profile's average, else LV ahead (beyond the 1Y range counts as LV).
           Carried on: price reaches the edge + one value-area width, at most MAX_MOVE (long; short mirrored)
           before a close back inside the value area, within CONT_DAYS sessions.
  Rule 3   Rotation: a session that came from inside the value area, touched the VAH (VAL)
           and closed back inside, above (below) the POC. Win: the POC is reached before the
           rejection session's high (low) is taken out, within ROT_DAYS sessions, bar by bar on
           the 60-minute bars (a bar touching both counts as a loss). R: the distance to the POC
           over the distance to the rejection high (low), from the rejection close.

Usage: python backtest_vp_rules.py [refresh]      (bars are cached in %TEMP%)"""
from __future__ import annotations

import os
import pickle
import sys
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import volume_profile as vp  # noqa: E402

HISTORY = 725       # days of 60-minute bars (Yahoo keeps about 730)
CONT_DAYS = 10      # sessions a breakout gets to carry on
MAX_MOVE = 0.03     # the carry-on target: one value-area width past the edge, at most 3% of the price
ROT_DAYS = 20       # sessions a rejection gets to reach the POC
PROFILES = ("Weekly", "Monthly", "3M", "1Y")
CACHE = os.path.join(os.environ.get("TEMP", "."), "vp_rules_bars.pkl")


def load() -> dict[str, pd.DataFrame]:
    if os.path.exists(CACHE) and "refresh" not in sys.argv:
        return pickle.load(open(CACHE, "rb"))
    vp.HISTORY_DAYS = HISTORY
    now = datetime.now(vp.IST)
    names = sorted({s for _, stocks in vp.INDEX_MEMBERS.values() for s in stocks})

    def one(symbol):
        try:
            return symbol, vp.stock_bars(symbol, now)
        except Exception as exc:  # noqa: BLE001
            print(f"{symbol}: {type(exc).__name__}: {exc}")
            return symbol, None

    with ThreadPoolExecutor(vp.WORKERS) as pool:
        stocks = {s: b for s, b in pool.map(one, names) if b is not None}
    data = {}
    for index, (yahoo, members) in vp.INDEX_MEMBERS.items():
        found = {s: stocks[s] for s in members if s in stocks}
        data[index] = vp.index_bars(yahoo, found, now)
        print(f"{index}: {len(found)} of {len(members)} members, {len(data[index])} bars")
    nifty_members = set(vp.INDEX_MEMBERS["NIFTY"][1])
    data |= {s: b for s, b in stocks.items() if s in nifty_members}
    pickle.dump(data, open(CACHE, "wb"))
    return data


def profile_of(bars: pd.DataFrame):
    """(levels, bin edges, bin volumes) of some bars, or None."""
    bars = bars[bars["volume"] > 0]
    if len(bars) < 5:
        return None
    step = max(float(bars["close"].iloc[-1]) * vp.BIN_PCT / 100, vp.MIN_BIN)
    edges, bins = vp.profile(bars["high"].to_numpy(float), bars["low"].to_numpy(float),
                             bars["volume"].to_numpy(float), step)
    poc, bottom, top = vp.value_area(bins)
    return {"POC": (edges[poc] + edges[poc + 1]) / 2, "VAH": edges[top + 1], "VAL": edges[bottom]}, edges, bins


def zone_ahead(year, start: float, end: float) -> str:
    """"HV" when the 1Y profile's volume per bin from `start` to `end` is at least its average."""
    if year is None:
        return "?"
    _, edges, bins = year
    lo, hi = min(start, end), max(start, end)
    mids = (edges[:-1] + edges[1:]) / 2
    inside = (mids >= lo) & (mids <= hi)
    covered = (hi - lo) / (edges[1] - edges[0])
    if covered <= 0:
        return "?"
    # Bins past the 1Y range hold nothing: they count in the average as empty.
    per_bin = bins[inside].sum() / max(covered, inside.sum())
    return "HV" if per_bin >= bins.mean() else "LV"


def study(symbol: str, bars: pd.DataFrame) -> tuple[list, list]:
    days = bars.index.tz_localize(None).normalize()
    sessions = sorted(set(days))
    daily = bars.groupby(days).agg(high=("high", "max"), low=("low", "min"), close=("close", "last"))
    first_day = sessions[0]
    by_day = {d: bars[days == d] for d in sessions}
    cache: dict = {}

    def levels_at(end: pd.Timestamp):
        """Profile -> (levels, edges, bins) as at the close of session `end`."""
        out = {}
        for period, (a, b) in vp.period_spans(end).items():
            if period not in PROFILES:
                continue
            if a < first_day + pd.Timedelta(days=vp.LATE_START_DAYS):
                out[period] = None
                continue
            key = (period, a, b)
            if key not in cache:
                cache[key] = profile_of(bars[(days >= a) & (days <= b)])
            out[period] = cache[key]
        return out

    breakouts, rotations = [], []
    for i in range(2, len(sessions) - 1):
        prev, day = sessions[i - 1], sessions[i]
        found = levels_at(prev)
        year = found.get("1Y")
        p_close, d = daily.loc[sessions[i - 1], "close"], daily.loc[day]
        for period, lv in found.items():
            if lv is None:
                continue
            L = lv[0]
            vah, val, poc = L["VAH"], L["VAL"], L["POC"]
            width = vah - val
            if width <= 0:
                continue
            inside_before = val <= p_close <= vah
            # Rule 1/2: breakout close
            for long in (True, False):
                edge = vah if long else val
                if not inside_before or not (d["close"] > vah if long else d["close"] < val):
                    continue
                move = min(width, MAX_MOVE * edge)
                target = edge + move if long else edge - move
                ahead = zone_ahead(year, edge, target)
                result = "neither"
                for later in sessions[i + 1: i + 1 + CONT_DAYS]:
                    x = daily.loc[later]
                    if (x["high"] >= target) if long else (x["low"] <= target):
                        result = "carried"
                        break
                    if (x["close"] < edge) if long else (x["close"] > edge):
                        result = "back inside"
                        break
                breakouts.append({"symbol": symbol, "day": day, "profile": period, "side": "long" if long else "short",
                                  "ahead": ahead, "result": result})
            # Rule 3: rejection at the edge
            for top in (True, False):
                edge = vah if top else val
                touched = d["high"] >= edge if top else d["low"] <= edge
                closed_in = (poc < d["close"] < edge) if top else (edge < d["close"] < poc)
                came_from_inside = (p_close < edge) if top else (p_close > edge)
                if not (touched and closed_in and came_from_inside):
                    continue
                stop = d["high"] if top else d["low"]
                risk, reward = abs(stop - d["close"]), abs(d["close"] - poc)
                if risk <= 0:
                    continue
                result, took = "neither", None
                for n, later in enumerate(sessions[i + 1: i + 1 + ROT_DAYS], 1):
                    for _, b in by_day[later].iterrows():
                        hit_poc = b["low"] <= poc if top else b["high"] >= poc
                        hit_stop = b["high"] > stop if top else b["low"] < stop
                        if hit_stop:
                            result = "stopped"
                        elif hit_poc:
                            result = "POC"
                        if result != "neither":
                            break
                    if result != "neither":
                        took = n
                        break
                rotations.append({"symbol": symbol, "day": day, "profile": period, "edge": "VAH" if top else "VAL",
                                  "result": result, "sessions": took, "rr": reward / risk})
    return breakouts, rotations


def pct(part: int, whole: int) -> str:
    return f"{100 * part / whole:5.1f}%" if whole else "    –"


def report(title: str, breakouts: pd.DataFrame, rotations: pd.DataFrame) -> None:
    print(f"\n=== {title} ===")
    print(f"Rule 1/2 breakouts: carried on (one VA width, at most {MAX_MOVE:.0%}) before a close back inside, within "
          f"{CONT_DAYS} sessions")
    print(f"  {'profile':8} {'ahead':5} {'n':>5} {'carried':>8} {'back in':>8} {'neither':>8}")
    for period in PROFILES:
        for ahead in ("HV", "LV"):
            g = breakouts[(breakouts.profile == period) & (breakouts.ahead == ahead)] if len(breakouts) else breakouts
            n = len(g)
            if not n:
                continue
            c = (g.result == "carried").sum()
            b = (g.result == "back inside").sum()
            print(f"  {period:8} {ahead:5} {n:5} {pct(c, n):>8} {pct(b, n):>8} {pct(n - c - b, n):>8}")
    print(f"Rule 3 rejections at the VAH / VAL: POC reached before the rejection high/low, within {ROT_DAYS} sessions")
    print(f"  {'profile':8} {'edge':4} {'n':>5} {'to POC':>8} {'stopped':>8} {'neither':>8} {'avg R':>6} "
          f"{'exp R':>6} {'med days':>8}")
    for period in PROFILES:
        for edge in ("VAH", "VAL"):
            g = rotations[(rotations.profile == period) & (rotations.edge == edge)] if len(rotations) else rotations
            n = len(g)
            if not n:
                continue
            w, s = (g.result == "POC"), (g.result == "stopped")
            rr = g["rr"].clip(upper=10)
            exp = (rr[w].sum() - s.sum()) / n  # in R, open ones at 0
            days = g.loc[w, "sessions"].median() if w.any() else float("nan")
            print(f"  {period:8} {edge:4} {n:5} {pct(w.sum(), n):>8} {pct(s.sum(), n):>8} "
                  f"{pct(n - w.sum() - s.sum(), n):>8} {rr.median():6.2f} {exp:6.2f} {days:8.0f}")


def main() -> None:
    data = load()
    results = {}
    for symbol, bars in data.items():
        results[symbol] = study(symbol, bars)
    for name, group in (("NIFTY", ["NIFTY"]), ("BANKNIFTY", ["BANKNIFTY"]),
                        ("Both indices", ["NIFTY", "BANKNIFTY"]),
                        ("NIFTY 50 stocks", [s for s in results if s not in vp.INDEX_MEMBERS])):
        b = pd.DataFrame([r for s in group for r in results[s][0]])
        r = pd.DataFrame([r for s in group for r in results[s][1]])
        report(name, b, r)
    span = data["NIFTY"].index
    print(f"\nBars {span[0]:%d %b %Y} to {span[-1]:%d %b %Y}; 1Y levels need a year of bars first.")
    out = os.path.join(os.environ.get("TEMP", "."), "vp_rules_events.csv")
    pd.concat([pd.DataFrame([x for s in results for x in results[s][k]]).assign(rule=name)
               for k, name in ((0, "breakout"), (1, "rotation"))]).to_csv(out, index=False)
    print(f"Every event: {out}")


if __name__ == "__main__":
    main()
