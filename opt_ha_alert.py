"""
ATM CE Heikin Ashi alert, 1H / 2H / 4H: after each 1-, 2- or 4-hour candle completes, check the ATM call
of NIFTY, BANKNIFTY and every F&O stock whose daily ATR covers more than the saved number of strikes
(futures_simulator.atr_strikes_above, the F&O ATR page's ratio), and alert on Telegram those whose
Heikin Ashi candle on that timeframe completed solid green (no lower wick) right after a red one.

  * Candles from 9:15 AM, as Chartink's: 1H ends 10:15 ... 3:15, 2H 11:15 / 1:15 / 3:15, 4H 1:15
    (the short 3:15-3:30 / 1:15-3:30 candles end at the close, too late, so they are not alerted).
  * The option: the nearest expiry (the next one on its expiry day), the strike nearest the price
    when scanned. Its 1H / 2H / 4H candles are made from Dhan's 15-minute ones, the HA chained over
    the last days. One download per contract serves every timeframe ending at that time.
  * Alerts only: no trades. Each candle end is scanned once (account["opt_ha"]).

Run from auto_trader.py (scan) in market hours. Dhan allows one option chain request every 3 seconds,
so a scan of ~25 underlyings takes a minute or two.
"""

from __future__ import annotations

import json
import sqlite3
import time as _time
from datetime import date, datetime, time, timedelta
from pathlib import Path

import dhan_data as dd

TIMEFRAMES = {"1H": 60, "2H": 120, "4H": 240}  # label -> minutes
SESSION_OPEN = time(9, 15)
SESSION_CLOSE = time(15, 30)
LAST_ALERT = time(15, 15)  # candles ending later (at the close) are not alerted
DAYS = 14  # calendar days of 15-minute bars: enough 4H candles to chain the HA
CHAIN_PAUSE = 3.1  # seconds between option chain requests (Dhan: one every 3 seconds)
HERE = Path(__file__).parent


def window_start(ts: int, minutes: int) -> int:
    """Start (seconds) of the `minutes` candle a time is in, from 9:15 AM IST."""
    moment = datetime.fromtimestamp(ts, dd.IST)
    session = datetime.combine(moment.date(), SESSION_OPEN, dd.IST)
    step = timedelta(minutes=minutes)
    return int((session + (moment - session) // step * step).timestamp())


def window_end(start: int, minutes: int) -> int:
    moment = datetime.fromtimestamp(start, dd.IST)
    close = datetime.combine(moment.date(), SESSION_CLOSE, dd.IST)
    return int(min(moment + timedelta(minutes=minutes), close).timestamp())


def ends_on(day: date, minutes: int) -> list[datetime]:
    """The alerted candle ends of a timeframe on a day (9:15 AM + n candles, up to 3:15 PM)."""
    out, moment = [], datetime.combine(day, SESSION_OPEN, dd.IST) + timedelta(minutes=minutes)
    while moment.time() <= LAST_ALERT:
        out.append(moment)
        moment += timedelta(minutes=minutes)
    return out


def last_end(now: datetime) -> tuple[int, list[str]] | None:
    """(the latest candle end today, as seconds; the timeframes ending then), or None before 10:15 AM."""
    ends: dict[datetime, list[str]] = {}
    for name, minutes in TIMEFRAMES.items():
        for end in ends_on(now.date(), minutes):
            if end <= now:
                ends.setdefault(end, []).append(name)
    if not ends:
        return None
    end = max(ends)
    return int(end.timestamp()), ends[end]


def resample(bars: list[tuple], minutes: int, now: float) -> list[dict]:
    """Finished 15-minute bars (start, open, high, low, close, volume) in `minutes` candles, oldest first."""
    out: list[dict] = []
    for bar in bars:
        clock = datetime.fromtimestamp(bar[0], dd.IST).time()
        if not SESSION_OPEN <= clock < SESSION_CLOSE or bar[0] + 900 > now:
            continue
        start = window_start(bar[0], minutes)
        if not out or out[-1]["start"] != start:
            out.append({"start": start, "open": bar[1], "high": bar[2], "low": bar[3], "close": bar[4]})
        c = out[-1]
        c["high"], c["low"], c["close"] = max(c["high"], bar[2]), min(c["low"], bar[3]), bar[4]
    return out


def heikin_ashi(candles: list[dict]) -> list[dict]:
    out: list[dict] = []
    for c in candles:
        ha_close = (c["open"] + c["high"] + c["low"] + c["close"]) / 4
        ha_open = (c["open"] + c["close"]) / 2 if not out else (out[-1]["open"] + out[-1]["close"]) / 2
        ha_low = min(c["low"], ha_open, ha_close)
        green = ha_close > ha_open
        out.append({"open": ha_open, "close": ha_close, "low": ha_low, "green": green,
                    "solid": green and ha_low >= ha_open})
    return out


def turned(bars: list[tuple], minutes: int, end: int, now: float) -> dict | None:
    """The `minutes` candle ending at `end`, if its HA candle is solid green right after a red one."""
    candles = resample(bars, minutes, now)
    ha = heikin_ashi(candles)
    for k in range(1, len(candles)):
        if window_end(candles[k]["start"], minutes) == end and ha[k]["solid"] and not ha[k - 1]["green"]:
            return {**candles[k], "ha_open": ha[k]["open"], "ha_close": ha[k]["close"]}
    return None


def underlyings(ratio: float) -> list[dict]:
    """NIFTY, BANKNIFTY, then the F&O stocks with ATR (14) > `ratio` strike intervals, biggest ratio first."""
    out = [{**u, "ratio": None} for u in dd.INDICES.values()]
    with open(HERE / "strike_steps.json", encoding="utf-8") as f:
        saved = json.load(f)
    steps, ids = saved["steps"], saved["ids"]
    with sqlite3.connect(f"file:{HERE / 'stock_market_data.db'}?mode=ro", uri=True) as db:
        rows = db.execute("SELECT symbol, atr_14 FROM stock_ohlc_indicators WHERE date = (SELECT MAX(date) FROM "
                          "stock_ohlc_indicators WHERE atr_14 IS NOT NULL) AND atr_14 IS NOT NULL").fetchall()
    stocks = []
    for symbol, atr in rows:
        symbol = symbol.removesuffix(".NS")
        if symbol in steps and symbol in ids and atr / steps[symbol] > ratio:
            stocks.append({**dd.stock(symbol, ids[symbol], steps[symbol]), "ratio": atr / steps[symbol]})
    return out + sorted(stocks, key=lambda u: -u["ratio"])


def nearest_expiry(expiries: list[str], today: date) -> str:
    """The nearest expiry; the next one on its expiry day (the contract ends that afternoon)."""
    later = [e for e in expiries if e > today.isoformat()]
    return later[0] if later else expiries[-1]


def chain(underlying: dict, expiry: str) -> tuple[float, list[dict]]:
    """dd.option_chain after the 3-second pause, once more after a longer one if Dhan says too many requests."""
    for wait in (CHAIN_PAUSE, 10):
        _time.sleep(wait)
        try:
            return dd.option_chain(expiry, underlying)
        except dd.DhanError as exc:
            if "429" not in str(exc) or wait == 10:
                raise
    raise AssertionError


def expiries(underlying: dict) -> list[str]:
    _time.sleep(CHAIN_PAUSE)  # the expiry list counts as an option chain request
    return dd.expiries(underlying)


def atm_call(underlying: dict, expiry: str) -> dict:
    """{"spot", "strike", "id", "ltp"} of the call nearest the price."""
    spot, rows = chain(underlying, expiry)
    rows = [r for r in rows if r.get("ce_id")]
    if not rows:
        raise IndexError("no calls in the chain")
    row = min(rows, key=lambda r: abs(r["strike"] - spot))
    return {"spot": spot, "strike": row["strike"], "id": int(row["ce_id"]), "ltp": row.get("ce_ltp")}


def label(name: str, expiry: str, strike: float) -> str:
    return f"{name} {datetime.fromisoformat(expiry):%d%b}".upper() + f" {strike:g} CE"


def scan(now: datetime, ratio: float, lots: dict[str, int], note, skip: set[str] = frozenset()
         ) -> tuple[int, list[str], list[dict], list[str], int] | None:
    """(the candle end, the timeframes ending then, the hits, the names checked, how many underlyings
    in all) for the latest completed candle end, leaving out `skip` (checked already); None before
    the first one today. Each hit: timeframe, name, option, expiry, strike, spot, ltp, lot, ratio and
    the candle. A name Dhan failed for is not in the checked ones, so the next run tries it again."""
    latest = last_end(now)
    if latest is None:
        return None
    end, frames = latest
    found, checked = [], []
    every = underlyings(ratio)
    stock_expiry = None
    for u in [u for u in every if u["name"] not in skip]:
        try:
            if u["instrument"] == "OPTSTK":
                if stock_expiry is None:  # every stock option shares the monthly expiry
                    stock_expiry = nearest_expiry(expiries(u), now.date())
                expiry = stock_expiry
            else:
                expiry = nearest_expiry(expiries(u), now.date())
            call = atm_call(u, expiry)
            bars = dd.candles(call["id"], minutes=15, days=DAYS, instrument=u["instrument"])
        except (dd.DhanError, IndexError, ValueError, KeyError) as exc:
            note(f"HA CE alert: {u['name']}: {exc}")
            if "renewed somewhere else" in str(exc) or "token" in str(exc).lower():
                break  # no token now: the rest next run
            continue
        checked.append(u["name"])
        for frame in frames:
            hit = turned(bars, TIMEFRAMES[frame], end, now.timestamp())
            if hit:
                found.append({"timeframe": frame, "name": u["name"], "option": label(u["name"], expiry, call["strike"]),
                              "expiry": expiry, "strike": call["strike"], "spot": call["spot"], "ltp": call["ltp"],
                              "lot": lots.get(u["name"]), "ratio": u["ratio"], "candle": hit})
    return end, frames, found, checked, len(every)
