"""
OPT ATM HA: paper-trades the ATM call and put of NIFTY and BANKNIFTY (nearest expiry, Dhan's option
chain, dhan_data.py) on 15-minute candles, the CE and PE of each index apart, both only bought.

  * Entry: the Option chain page's star (option_chain_page.is_entry): a 15-minute Heikin Ashi candle
    that completes solid green (HA close above the HA open, no lower wick) right after a red one.
    Bought at the option's close at the end of that candle, 1 lot.
  * SL: the previous HA candle's low. After each later 15-minute candle completes, the SL moves up
    to that candle's HA low, never down.
  * Out at the SL (or the minute's open, when it opened below it), else squared off at 3:15 PM. No
    new entries from LAST_ENTRY.
  * One position at a time in each leg (NIFTY CE, NIFTY PE, BANKNIFTY CE, BANKNIFTY PE), and one
    entry per leg and candle.

A run (auto_trader.py) checks the 1-minute bars since the last one, so an entry or exit between runs
is found at the minute it happened. The ATM strike is the one at the time of the run.
"""

from __future__ import annotations

import time as _time
from datetime import datetime, time, timedelta

import dhan_data as dd

SYSTEM = "OPT ATM HA"
NAME = "ATM CE/PE · first solid green HA after red"
UNDERLYINGS = {"NIFTY": dd.NIFTY, "BANKNIFTY": dd.BANKNIFTY}
LOT_SIZE = {"NIFTY": 65, "BANKNIFTY": 30}  # NSE lot sizes ('futures stocks.csv')
LEGS = ("CE", "PE")
MINUTES = 15
SESSION_OPEN = time(9, 15)
LAST_ENTRY = time(15, 0)
SQUARE_OFF = time(15, 15)
LOOKBACK = timedelta(minutes=30)  # candles completed at most this long ago (GitHub can run late; no stale entries)
DAYS = 4  # days of 1-minute bars asked: the HA open needs the candles before today's


def candle_start(ts: int) -> int:
    """Start (seconds) of the 15-minute candle a time is in, from 9:15 AM IST."""
    moment = datetime.fromtimestamp(ts, dd.IST)
    session = datetime.combine(moment.date(), SESSION_OPEN, dd.IST)
    step = timedelta(minutes=MINUTES)
    return int((session + (moment - session) // step * step).timestamp())


def group(bars: list[tuple]) -> list[dict]:
    """The 1-minute bars (start, open, high, low, close, ...) in 15-minute candles, oldest first:
    start, open, high, low, close and the bars, in market hours."""
    out: list[dict] = []
    for bar in bars:
        clock = datetime.fromtimestamp(bar[0], dd.IST).time()
        if not SESSION_OPEN <= clock < time(15, 30):
            continue
        start = candle_start(bar[0])
        if not out or out[-1]["start"] != start:
            out.append({"start": start, "open": bar[1], "high": bar[2], "low": bar[3], "close": bar[4], "bars": []})
        c = out[-1]
        c["high"], c["low"], c["close"] = max(c["high"], bar[2]), min(c["low"], bar[3]), bar[4]
        c["bars"].append(bar)
    return out


def heikin_ashi(candles: list[dict]) -> list[dict]:
    """The Heikin Ashi candle of each candle, chained from the first (as dhan_data.heikin_ashi): open,
    high, low, close, green, and solid (green with no lower wick)."""
    out: list[dict] = []
    for c in candles:
        ha_close = (c["open"] + c["high"] + c["low"] + c["close"]) / 4
        ha_open = (c["open"] + c["close"]) / 2 if not out else (out[-1]["open"] + out[-1]["close"]) / 2
        ha_low = min(c["low"], ha_open, ha_close)
        green = ha_close > ha_open
        out.append({"open": ha_open, "high": max(c["high"], ha_open, ha_close), "low": ha_low, "close": ha_close,
                    "green": green, "solid": green and ha_low >= ha_open})
    return out


def find_entry(bars: list[tuple], since: int, until: int) -> dict | None:
    """The first candle that completed in (since, until] (seconds), up to LAST_ENTRY, as the Option
    chain page's star: a solid green HA candle (no lower wick) right after a red one. With the entry
    (the option's close at that candle's end), its time and the SL (the previous HA candle's low).
    None if none did."""
    candles = group([b for b in bars if b[0] + 60 <= until])
    ha = heikin_ashi(candles)
    for k in range(1, len(candles)):
        end = candles[k]["start"] + MINUTES * 60
        if not since < end <= until or datetime.fromtimestamp(end, dd.IST).time() > LAST_ENTRY:
            continue
        if ha[k]["solid"] and not ha[k - 1]["green"]:
            return {"entry": candles[k]["close"], "time": end, "sl": ha[k - 1]["low"],
                    "candle": candles[k]["start"]}
    return None


def replay(position: dict, bars: list[tuple], now: datetime) -> dict:
    """A long option position from its entry on: SL trailed to each completed candle's HA low; closed at
    the SL or the 3:15 PM square-off. {"status": "open", "stop", "high", "low", "ltp"} or
    {"status": "closed", "exit", "exit_time" (ms), "reason", "high", "low"}."""
    entered = position["time"] // 1000  # the end of the entry candle
    day = datetime.fromtimestamp(entered - 60, dd.IST).date()
    stop, high, low, ltp = position["sl"], position["entry"], position["entry"], position.get("ltp")
    # Finished minutes only; the HA candles chained from the first bar, as at the entry.
    every = group([b for b in bars if b[0] + 60 <= now.timestamp()])
    ha = heikin_ashi(every)
    for c, h in zip(every, ha):
        if c["start"] + MINUTES * 60 <= entered:  # the entry candle and before
            continue
        for bar in c["bars"]:
            if bar[0] + 60 <= entered:  # the entry minute and before
                continue
            moment = datetime.fromtimestamp(bar[0], dd.IST)
            if moment.time() >= SQUARE_OFF or moment.date() > day:
                return {"status": "closed", "exit": bar[1], "exit_time": bar[0] * 1000,
                        "reason": "3:15 PM square-off", "high": high, "low": low}
            if bar[3] <= stop:
                price = min(bar[1], stop)
                return {"status": "closed", "exit": price, "exit_time": (bar[0] + 60) * 1000,
                        "reason": "SL" if stop <= position["sl"] else "Trailing SL",
                        "high": max(high, bar[2]), "low": min(low, price)}
            high, low, ltp = max(high, bar[2]), min(low, bar[3]), bar[4]
        if c["start"] + MINUTES * 60 <= now.timestamp():  # completed: trail to its HA low
            stop = max(stop, h["low"])
    if now.time() >= SQUARE_OFF and now.date() == day or now.date() > day:  # no bar after 3:15 (yet)
        return {"status": "closed", "exit": ltp or position["entry"], "exit_time": int(now.timestamp() * 1000),
                "reason": "3:15 PM square-off", "high": high, "low": low}
    return {"status": "open", "stop": stop, "high": high, "low": low, "ltp": ltp}


def charges(buy_value: float, sell_value: float) -> float:
    """Brokerage and taxes of an option round trip (premium values): ₹20 an order, STT 0.15% of
    the sell premium (from 1 April 2026), NSE 0.03503%, SEBI ₹10 a crore, stamp 0.003% on the buy,
    GST 18% on brokerage, exchange and SEBI charges."""
    brokerage = 20.0 * 2
    exchange = (buy_value + sell_value) * 0.0003503
    sebi = (buy_value + sell_value) * 0.000001
    return brokerage + exchange + sebi + sell_value * 0.0015 + buy_value * 0.00003 + \
        (brokerage + exchange + sebi) * 0.18


def atm_legs(name: str) -> dict:
    """The nearest expiry's ATM CE and PE of an index now: {"expiry", "spot", "strike",
    "CE": security id, "PE": security id}."""
    underlying = UNDERLYINGS[name]
    expiry = dd.expiries(underlying)[0]
    _time.sleep(3)  # Dhan allows one option chain request every 3 seconds
    spot, rows = dd.option_chain(expiry, underlying)
    strike = dd.atm_strike(spot, underlying)
    row = min(rows, key=lambda r: abs(r["strike"] - strike))
    return {"expiry": expiry, "spot": spot, "strike": row["strike"], "CE": row.get("ce_id"), "PE": row.get("pe_id")}


def symbol(name: str, expiry: str, strike: float, leg: str) -> str:
    return f"{name} {datetime.fromisoformat(expiry):%d%b}".upper() + f" {strike:g} {leg}"


def bars_of(security_id) -> list[tuple]:
    return dd.candles(int(security_id), minutes=1, days=DAYS, instrument="OPTIDX")
