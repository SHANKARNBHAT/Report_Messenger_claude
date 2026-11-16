"""
HA PURE: the Auto trader's Heikin Ashi strategy without a trend filter, on the 2H, 4H and Daily
candles of the F&O stocks, NIFTY and BANKNIFTY (auto_trader.py, enter_pure_signals and
settle_pure), one strategy per timeframe (SYSTEMS). HA IDX PURE (ha_index.py) is the same on
15-minute index candles, intraday.

  * Entry, live on the running candle (ha_vp.ha_entry): long on a green HA candle with no lower
    wick after a doji (wicks on both sides; the new body at least the doji's) or after a red HA
    candle, closing above its body; short on a red one with no upper wick after a doji or a
    green one, closing below its body. An opposite signal closes the position and enters the
    other way; no second entry on a candle.
  * SL: the low (long) or high (short) of the last finished 30-minute candle (from 9:15 AM)
    before the entry, else ATR_MULT x ATR(14) of the timeframe's candles away. Trailed: after each
    30-minute candle closes, moved to its low (long) / high (short) when that is closer, never
    back. Hit when a 5-minute bar touches it (at the bar's open if it gapped past).
  * Doji exit: at the close of the first finished HA candle of the timeframe, the entry candle
    included, with wicks on both sides (ha_vp.doji), at that close.
  * Held overnight; closed at 3:15 PM on the futures expiry day (futures_simulator.expiry_after).

Candles are rows as chartink_dashboard.fetch_vp_candles' (start in seconds, close, high, low,
HA open, HA high, HA low, HA close, volume); 5-minute bars (start, open, high, low, close).
"""

from __future__ import annotations

from datetime import datetime, time, timedelta

import ha_vp
from ha_vp import CLOSE, HA_CLOSE, HA_HIGH, HA_LOW, HA_OPEN

SYSTEMS = {"HA PURE 2H": "2 HOUR", "HA PURE 4H": "4 HOUR", "HA PURE Daily": "DAILY"}
STYLE = "ha_pure"
ATR_MULT = 1.1  # the SL when no 30-minute candle is known
TRAIL_MINUTES = 30
SESSION_OPEN, SESSION_CLOSE, SQUARE_OFF = time(9, 15), time(15, 30), time(15, 15)
BAR = 300  # seconds in a 5-minute bar


def ha(row: tuple) -> tuple:
    """(HA open, HA high, HA low, HA close) of a candle row."""
    return row[HA_OPEN], row[HA_HIGH], row[HA_LOW], row[HA_CLOSE]


def signal(rows: list[tuple]) -> str | None:
    """"BUY", "SELL" or None for the last (running) candle of `rows`: the HA entry pattern alone."""
    if len(rows) < 2:
        return None
    return next((side for side in ("BUY", "SELL") if ha_vp.ha_entry(ha(rows[-1]), ha(rows[-2]), side == "BUY")),
                None)


def trail_start(ts: int, tz) -> int:
    """Start (seconds) of the 30-minute candle, from 9:15 AM, that a time is in."""
    moment = datetime.fromtimestamp(ts, tz)
    session = datetime.combine(moment.date(), SESSION_OPEN, tz)
    step = timedelta(minutes=TRAIL_MINUTES)
    return int((session + max(moment - session, timedelta(0)) // step * step).timestamp())


def trail_end(start: int, tz) -> int:
    """When the 30-minute candle that started at `start` ends: the last one of a day at the close."""
    moment = datetime.fromtimestamp(start, tz)
    close = datetime.combine(moment.date(), SESSION_CLOSE, tz).timestamp()
    return int(min(start + TRAIL_MINUTES * 60, close))


def last_trail_candle(bars: list[tuple], before: float, tz) -> tuple[float, float] | None:
    """Low and high of the last 30-minute candle finished by `before` (seconds), from 5-minute bars;
    None if the bars do not reach back to one."""
    candles: dict[int, list[tuple]] = {}
    for bar in bars:
        start = trail_start(bar[0], tz)
        if trail_end(start, tz) <= before:
            candles.setdefault(start, []).append(bar)
    if not candles:
        return None
    found = candles[max(candles)]
    return min(b[3] for b in found), max(b[2] for b in found)


def initial_stop(bars: list[tuple], rows: list[tuple], entry: float, long: bool, now: datetime, tz) -> float | None:
    """The SL of a new position: the last finished 30-minute candle's low (long) / high (short),
    else ATR_MULT x the ATR of the last finished candle of the timeframe away; None if neither."""
    found = last_trail_candle(bars, now.timestamp(), tz)
    if found and (found[0] < entry if long else found[1] > entry):
        return found[0] if long else found[1]
    values = ha_vp.atr(rows[:-1])
    if values and values[-1] is not None:
        return entry - ATR_MULT * values[-1] if long else entry + ATR_MULT * values[-1]
    return None


def replay(position: dict, bars: list[tuple], rows: list[tuple], candle_end, cutoff: float, now: datetime,
           tz) -> dict:
    """Where a position stands after the 5-minute bars since its entry: closed at its SL, at the
    close of a doji candle or at `cutoff` (3:15 PM on expiry day, seconds), else open with its
    current SL ("stop"). `candle_end(start)` is when the candle of the timeframe that started at
    `start` ends."""
    long = position["side"] == "BUY"
    stop = float(position["sl"])
    entry = float(position["entry"])
    entered = position["time"] / 1000
    high = low = entry
    moved = False
    # The candles of the timeframe that end after the entry, by their end: is it a doji.
    dojis = {candle_end(row[0]): ha_vp.doji(ha(row)) for row in rows if candle_end(row[0]) > entered}
    first = -(-int(entered) // BAR) * BAR
    # The 30-minute candle being built: its start, low and high.
    trail = None

    def result(status, **extra):
        return {"id": position["id"], "status": status, "stop": round(stop, 2), "high": round(high, 2),
                "low": round(low, 2), **extra}

    last = None
    for start, open_, bar_high, bar_low, close in (b for b in bars if first <= b[0] < cutoff):
        gap = open_ <= stop if long else open_ >= stop
        if gap or (bar_low <= stop if long else bar_high >= stop):
            price = open_ if gap else stop
            high, low = max(high, price), min(low, price)
            return result("closed", exit=round(price, 2), exit_time=start * 1000,
                          reason="30m trailing SL" if moved else "Stop loss")
        high, low = max(high, bar_high), min(low, bar_low)
        last = close
        end = start + BAR
        if end > now.timestamp():
            continue
        if dojis.get(end):
            return result("closed", exit=round(close, 2), exit_time=end * 1000, reason="HA doji")
        # The 30-minute candles that end after the entry: trail the SL to each one's low (high).
        candle = trail_start(start, tz)
        if trail is None or trail[0] != candle:
            trail = [candle, bar_low, bar_high]
        else:
            trail[1], trail[2] = min(trail[1], bar_low), max(trail[2], bar_high)
        if end == trail_end(candle, tz) and candle >= trail_start(int(entered), tz):
            level = trail[1] if long else trail[2]
            if level > stop if long else level < stop:
                stop, moved = level, True
    if now.timestamp() >= cutoff:
        price = last if last is not None else entry
        return result("closed", exit=round(price, 2), exit_time=int(cutoff * 1000), reason="Expiry 3:15 PM")
    return result("open")
