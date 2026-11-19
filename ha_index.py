"""
HA 15m index: the Auto trader's Heikin Ashi strategy on the 15-minute candles of NIFTY and
BANKNIFTY (auto_trader.py, enter_index_signals and settle), made from Yahoo Finance's
5-minute bars of the index (Chartink's index list has no BANKNIFTY data).

  * Entry, on the running 15-minute candle, as HA VP (ha_vp.signal): long on a green HA candle
    with no lower wick after a doji (wicks both sides; the new body at least the doji's) or after
    a red HA candle, closing above its body, when SMA 20 > SMA 50 of the 15-minute closes and the close is above SMA 200;
    short on a red one with no upper wick after a doji or a green one, the SMAs the other way.
  * SL ATR_MULT x ATR(ATR_PERIOD) of the 15-minute candles from the entry. After each
    15-minute candle closes, it moves to that close -/+ ATR_MULT x ATR when that is closer,
    never back. Hit when a 5-minute bar touches it (at the bar's open if it gapped past).
  * Doji exit: at the close of the first 15-minute HA candle, the entry candle included, with
    wicks on both sides (ha_vp.doji), at that close.
  * Squared off at 3:15 PM: intraday only.
  * 4H filter (since 19 Nov 2026, all three strategies): a long only when the previous (finished) 4H
    HA candle is solid green, with no lower wick; a short only when it is solid red, with no upper
    wick (htf_side). 4H candles run 9:15 AM to 1:15 PM and 1:15 PM to the close.

HA IDX BRK (rule "break"): ha_break's breakout entry on these 15-minute HA candles, SL at the
HA candle's low / high, trailed to each finished 15-minute HA candle's low / high (break_replay);
no ATR, no doji exit; squared off at 3:15 PM.
"""

from __future__ import annotations

from datetime import datetime, time, timedelta

import ha_break
import ha_vp

SYMBOLS = ("NIFTY", "BANKNIFTY")
TIMEFRAME = "15 MIN"
MINUTES = 15
HTF_MINUTES = 240  # the 4H filter (htf_side)
ATR_PERIOD = 14
ATR_MULT = 1.1
SESSION_OPEN, SESSION_END, SQUARE_OFF = time(9, 15), time(15, 30), time(15, 15)
BAR = 300  # seconds in a 5-minute bar


def candle_start(ts: int, tz, minutes: int = MINUTES) -> int:
    """Start (seconds) of the 15-minute (or `minutes`) candle a time is in, from 9:15 AM."""
    moment = datetime.fromtimestamp(ts, tz)
    session = datetime.combine(moment.date(), SESSION_OPEN, tz)
    return int((session + (moment - session) // timedelta(minutes=minutes) * timedelta(minutes=minutes)).timestamp())


def candles(bars: list[tuple], tz, minutes: int = MINUTES) -> list[list]:
    """15-minute (or `minutes`) candles [start, open, high, low, close] from 5-minute bars (start, open,
    high, low, close), session hours only; the last one may still be running."""
    out: list[list] = []
    for start, open_, high, low, close in bars:
        moment = datetime.fromtimestamp(start, tz).time()
        if not SESSION_OPEN <= moment < SESSION_END:
            continue
        key = candle_start(start, tz, minutes)
        if out and out[-1][0] == key:
            out[-1][2], out[-1][3], out[-1][4] = max(out[-1][2], high), min(out[-1][3], low), close
        else:
            out.append([key, open_, high, low, close])
    return out


def heikin_ashi(rows: list[list]) -> list[tuple]:
    """(HA open, HA high, HA low, HA close) of each candle."""
    out = []
    for i, (_, open_, high, low, close) in enumerate(rows):
        ha_close = (open_ + high + low + close) / 4
        ha_open = (open_ + close) / 2 if i == 0 else (out[-1][0] + out[-1][3]) / 2
        out.append((ha_open, max(high, ha_open, ha_close), min(low, ha_open, ha_close), ha_close))
    return out


def htf_side(bars: list[tuple], tz, now_ts: int) -> str | None:
    """"BUY" if the previous (finished) 4H HA candle is green with no lower wick, "SELL" if red with
    no upper wick, else None: the 4H filter of the entries."""
    rows = candles([b for b in bars if b[0] <= now_ts], tz, HTF_MINUTES)
    done = [row for row in rows if row[0] < candle_start(now_ts, tz, HTF_MINUTES)]
    if not done:
        return None
    ha_open, ha_high, ha_low, ha_close = heikin_ashi(done)[-1]
    if ha_close > ha_open and ha_low >= ha_open:
        return "BUY"
    if ha_close < ha_open and ha_high <= ha_open:
        return "SELL"
    return None


def atr(rows: list[list]) -> list[float | None]:
    """Wilder's ATR(ATR_PERIOD) at each candle (None until there are enough)."""
    out, ranges = [], []
    value = None
    for i, (_, _, high, low, close) in enumerate(rows):
        previous = rows[i - 1][4] if i else close
        ranges.append(max(high - low, abs(high - previous), abs(low - previous)))
        if len(ranges) == ATR_PERIOD:
            value = sum(ranges) / ATR_PERIOD
        elif len(ranges) > ATR_PERIOD:
            value = (value * (ATR_PERIOD - 1) + ranges[-1]) / ATR_PERIOD
        out.append(value)
    return out


def signal(rows: list[list], use_trend: bool = True) -> str | None:
    """"BUY", "SELL" or None for the last (running) candle of `rows`, by ha_vp's rule: the SMA
    trend (ha_vp.trend) and the HA entry pattern (ha_vp.ha_entry). Without `use_trend` (HA IDX PURE),
    the HA entry pattern alone, either way."""
    if len(rows) < 2:
        return None
    ha = heikin_ashi(rows)
    if not use_trend:
        return next((side for side in ("BUY", "SELL") if ha_vp.ha_entry(ha[-1], ha[-2], side == "BUY")), None)
    side = ha_vp.trend(rows, 4)
    if side is None:
        return None
    return side if ha_vp.ha_entry(ha[-1], ha[-2], side == "BUY") else None


def rule_signal(rows: list[list], rule: str) -> str | None:
    """signal() by an INDEX_SYSTEMS rule: "trend" (HA IDX), "pure" (HA IDX PURE) or "break"
    (HA IDX BRK: ha_break's breakout entry on the 15-minute HA candles)."""
    if rule == "break":
        return ha_break.side_of(heikin_ashi(rows)) if len(rows) >= 2 else None
    return signal(rows, rule == "trend")


def initial_stop(rows: list[list], entry: float, long: bool, now_ts: int) -> float | None:
    """The SL of a new position: ATR_MULT x the ATR of the last finished candle away."""
    values = atr(rows)
    done = [v for row, v in zip(rows, values) if row[0] + MINUTES * 60 <= now_ts and v is not None]
    if not done:
        return None
    return entry - ATR_MULT * done[-1] if long else entry + ATR_MULT * done[-1]


def replay(position: dict, bars: list[tuple], now: datetime, tz) -> dict:
    """Where a position stands after the 5-minute bars since its entry: closed at its SL (hit
    by a bar) or at 3:15 PM, else open with its current SL ("stop"), as futures_simulator.replay."""
    long = position["side"] == "BUY"
    entry = float(position["entry"])
    stop = float(position["sl"])
    entered = datetime.fromtimestamp(position["time"] / 1000, tz)
    cutoff = datetime.combine(entered.date(), SQUARE_OFF, tz).timestamp()
    rows = candles([b for b in bars if b[0] < cutoff], tz)
    values = dict(zip((r[0] for r in rows), atr(rows)))
    dojis = {r[0]: ha_vp.doji(c) for r, c in zip(rows, heikin_ashi(rows))}
    first = -(-int(entered.timestamp()) // BAR) * BAR
    high = low = entry
    moved = False

    def result(status, **extra):
        return {"id": position["id"], "status": status, "stop": round(stop, 2), "high": round(high, 2),
                "low": round(low, 2), **extra}

    for start, open_, bar_high, bar_low, close in (b for b in bars if first <= b[0] < cutoff):
        gap = open_ <= stop if long else open_ >= stop
        if gap or (bar_low <= stop if long else bar_high >= stop):
            price = open_ if gap else stop
            high, low = max(high, price), min(low, price)
            return result("closed", exit=round(price, 2), exit_time=start * 1000,
                          reason="ATR trailing SL" if moved else "Stop loss")
        high, low = max(high, bar_high), min(low, bar_low)
        # The bar that ends a 15-minute candle: move the SL from that candle's close.
        candle = candle_start(start, tz)
        if start + BAR == candle + MINUTES * 60 and dojis.get(candle):
            return result("closed", exit=round(close, 2), exit_time=(start + BAR) * 1000, reason="HA doji")
        if start + BAR == candle + MINUTES * 60 and values.get(candle) is not None:
            level = close - ATR_MULT * values[candle] if long else close + ATR_MULT * values[candle]
            if level > stop if long else level < stop:
                stop, moved = level, True
    if now.timestamp() >= cutoff:
        session = [b for b in bars if b[0] < cutoff and datetime.fromtimestamp(b[0], tz).date() == entered.date()]
        price = session[-1][4] if session else entry
        return result("closed", exit=round(price, 2), exit_time=int(cutoff * 1000), reason="EOD 3:15 PM",
                      high=round(max(high, price), 2), low=round(min(low, price), 2))
    return result("open")


def break_stop(rows: list[list], entry: float, long: bool) -> float | None:
    """The SL of a new HA IDX BRK position: the running HA candle's low (high), else the previous one's."""
    return ha_break.stop_from(heikin_ashi(rows), entry, long)


def break_replay(position: dict, bars: list[tuple], now: datetime, tz) -> dict:
    """Where an HA IDX BRK position stands: SL trailed to each finished 15-minute HA candle's low
    (long) / high (short) after the entry (ha_break.trail), squared off at 3:15 PM that day."""
    entered = datetime.fromtimestamp(position["time"] / 1000, tz)
    cutoff = datetime.combine(entered.date(), SQUARE_OFF, tz).timestamp()
    session = [b for b in bars if b[0] < cutoff]
    rows = candles(session, tz)
    levels = {r[0] + MINUTES * 60: (c[2], c[1]) for r, c in zip(rows, heikin_ashi(rows))}
    return ha_break.trail(position, session, levels, cutoff, "EOD 3:15 PM", now)
