"""
The regular option strike interval of each NSE F&O stock, for the "F&O ATR" page
(atr_strikes_page.py), saved to strike_steps.json.

Source: Dhan's public instrument list (no login), every listed stock option. The interval is
measured on the nearest expiry around the stock's latest close (stock_market_data.db): the
largest standard NSE step whose strikes form a full ladder there, with no finer ladder listed
beside it. That skips the adjusted strikes NSE adds after a dividend or other corporate action
(e.g. 364.75 next to 365), which would otherwise make the interval look tiny.

NSE revises strike intervals now and then, so a GitHub Actions job (strike_steps.yml) runs this
monthly.   python strike_steps.py [out.json]
"""

from __future__ import annotations

import csv
import io
import json
import math
import os
import sqlite3
import sys
from collections import defaultdict
from datetime import datetime
from zoneinfo import ZoneInfo

import requests

try:  # trust the Windows certificate store too (antivirus HTTPS scanning on the home PC)
    import truststore

    truststore.inject_into_ssl()
except ImportError:
    pass

MASTER = "https://images.dhan.co/api-data/api-scrip-master.csv"
HERE = os.path.dirname(os.path.abspath(__file__))
DB = os.path.join(HERE, "stock_market_data.db")
OUT = os.path.join(HERE, "strike_steps.json")
STEPS = [500, 250, 100, 50, 20, 10, 5, 2.5, 1, 0.5]  # NSE's stock option strike intervals


def option_strikes() -> tuple[dict[str, dict[str, set[float]]], dict[str, int]]:
    """(underlying -> expiry (YYYY-MM-DD) -> its listed strikes, for NSE stock options;
    NSE equity symbol -> Dhan security id, which Dhan's option chain asks for)."""
    reply = requests.get(MASTER, timeout=120)
    reply.raise_for_status()
    strikes: dict[str, dict[str, set[float]]] = defaultdict(lambda: defaultdict(set))
    ids: dict[str, int] = {}
    for row in csv.DictReader(io.StringIO(reply.text)):
        if row["SEM_EXM_EXCH_ID"] != "NSE":
            continue
        if row["SEM_INSTRUMENT_NAME"] == "OPTSTK":
            # e.g. BAJAJ-AUTO-Nov2026-9000-CE: the name may hold dashes itself
            underlying = row["SEM_TRADING_SYMBOL"].rsplit("-", 3)[0]
            strikes[underlying][row["SEM_EXPIRY_DATE"][:10]].add(round(float(row["SEM_STRIKE_PRICE"]), 2))
        elif row["SEM_INSTRUMENT_NAME"] == "EQUITY" and row["SEM_SERIES"] == "EQ":
            ids[row["SEM_TRADING_SYMBOL"]] = int(row["SEM_SMST_SECURITY_ID"])
    return strikes, ids


def _multiples(step: float, low: float, high: float) -> list[float]:
    return [round(k * step, 2) for k in range(math.ceil(low / step - 1e-9), math.floor(high / step + 1e-9) + 1)]


def _share(listed: set[float], wanted: list[float]) -> float:
    return sum(w in listed for w in wanted) / len(wanted) if wanted else 0.0


def strike_step(listed: set[float], price: float) -> float | None:
    """The regular strike interval around `price` (see the module notes), or None."""
    for i, step in enumerate(STEPS):
        reach = max(price * 0.06, 3 * step)
        low, high = price - reach, price + reach
        ladder = _multiples(step, low, high)
        if len(ladder) < 3 or _share(listed, ladder) < 0.9:
            continue
        finer = any(_share(listed, [m for m in _multiples(t, low, high) if abs(m / step - round(m / step)) > 1e-6]) >= 0.8
                    for t in STEPS[i + 1:])
        if not finer:
            return step
    return None


def latest_closes() -> dict[str, float]:
    with sqlite3.connect(f"file:{DB}?mode=ro", uri=True) as db:
        rows = db.execute("SELECT symbol, close FROM stock_ohlc_indicators "
                          "WHERE date = (SELECT MAX(date) FROM stock_ohlc_indicators)").fetchall()
    return {symbol.removesuffix(".NS"): close for symbol, close in rows if close}


def build() -> dict:
    (strikes, ids), closes = option_strikes(), latest_closes()
    steps, missing = {}, []
    for symbol, close in sorted(closes.items()):
        if symbol not in strikes:
            continue  # an index, or not in F&O any more
        step = strike_step(strikes[symbol][min(strikes[symbol])], close)
        if step:
            steps[symbol] = step
        else:
            missing.append(symbol)
    return {"updated": datetime.now(ZoneInfo("Asia/Kolkata")).strftime("%Y-%m-%d"),
            "steps": steps, "ids": {s: ids[s] for s in steps if s in ids}, "not_found": missing}


def fno_atr():
    """The latest day's F&O stocks with ATR (14), close, lot, strike interval, ATR ÷ interval and
    Dhan security id, as a DataFrame sorted by ATR ÷ interval (biggest first), and the ATR date
    and the strike intervals' date. For the F&O ATR and Option chain pages."""
    import pandas as pd  # the monthly job only needs requests

    with sqlite3.connect(f"file:{DB}?mode=ro", uri=True) as db:
        frame = pd.read_sql_query(
            "SELECT symbol, date, close, atr_14, lot_size FROM stock_ohlc_indicators "
            "WHERE date = (SELECT MAX(date) FROM stock_ohlc_indicators) AND atr_14 IS NOT NULL", db)
    with open(OUT, encoding="utf-8") as f:
        saved = json.load(f)
    frame["symbol"] = frame["symbol"].str.removesuffix(".NS")
    frame["step"] = frame["symbol"].map(saved["steps"])
    frame["dhan_id"] = frame["symbol"].map(saved.get("ids", {}))
    frame = frame.dropna(subset=["step"])
    frame["ratio"] = frame["atr_14"] / frame["step"]
    frame = frame.sort_values("ratio", ascending=False).reset_index(drop=True)
    return frame, (frame["date"].max() if not frame.empty else "–"), saved.get("updated", "–")


if __name__ == "__main__":
    out = sys.argv[1] if len(sys.argv) > 1 else OUT
    data = build()
    with open(out, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=1, sort_keys=True)
    print(f"{len(data['steps'])} strike intervals saved to {out}; not found: {data['not_found']}")
