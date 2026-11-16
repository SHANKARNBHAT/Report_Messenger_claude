"""
HA BRK: the Auto trader's Heikin Ashi breakout strategy, as the TradingView script
(Volume_Profile_HA_Strategy.pine), without a trend filter. HA BRK 2H, 4H and Daily trade the F&O
stocks, NIFTY and BANKNIFTY on those timeframes, held overnight (auto_trader.py,
enter_pure_signals and settle_pure, with this module); HA IDX BRK trades NIFTY and BANKNIFTY on
15-minute candles, intraday (ha_index.py, auto_trader.enter_index_signals and settle_index).

  * Entry, live on the running candle (ha_vp.break_entry): long on a green HA candle with no
    lower wick whose HA close is above the previous HA candle's high; short on a red HA candle
    with no upper wick whose HA close is below the previous HA candle's low. An opposite signal
    closes a held position and enters the other way; no second entry on a candle.
  * SL: the HA low (long) / HA high (short) of the entry candle, else of the candle before it.
  * Trail: after each candle of the timeframe closes, the entry candle included, the SL moves to
    that candle's HA low (long) / HA high (short) when that is closer, never back: always the
    previous HA candle's low (high). Hit when a 5-minute bar touches it (at the bar's open if it
    gapped past). No other exit.
  * Held overnight and closed at 3:15 PM on the futures expiry day (HA BRK); HA IDX BRK is
    squared off at 3:15 PM every day.

Candles are rows as chartink_dashboard.fetch_vp_candles' (start in seconds, close, high, low,
HA open, HA high, HA low, HA close, volume); 5-minute bars (start, open, high, low, close).
"""

from __future__ import annotations

from datetime import datetime, time

import ha_vp
from ha_vp import HA_CLOSE, HA_HIGH, HA_LOW, HA_OPEN

SYSTEMS = {"HA BRK 2H": "2 HOUR", "HA BRK 4H": "4 HOUR", "HA BRK Daily": "DAILY"}
INDEX_SYSTEM = "HA IDX BRK"
STYLE, INDEX_STYLE = "ha_brk", "ha_brk_idx"
LABEL = "HA BRK"
SL_NOTE = "HA candle"
SQUARE_OFF = time(15, 15)
BAR = 300  # seconds in a 5-minute bar


def ha(row: tuple) -> tuple:
    """(HA open, HA high, HA low, HA close) of a candle row."""
    return row[HA_OPEN], row[HA_HIGH], row[HA_LOW], row[HA_CLOSE]


def side_of(candles: list[tuple]) -> str | None:
    """"BUY", "SELL" or None for the last of some HA candles (open, high, low, close)."""
    if len(candles) < 2:
        return None
    return next((side for side in ("BUY", "SELL") if ha_vp.break_entry(candles[-1], candles[-2], side == "BUY")),
                None)


def signal(rows: list[tuple]) -> str | None:
    """"BUY", "SELL" or None for the last (running) candle of `rows`."""
    return side_of([ha(r) for r in rows[-2:]])


def stop_from(candles: list[tuple], entry: float, long: bool) -> float | None:
    """The first SL: the HA low (high) of the last HA candle, else of the one before, that is on
    the right side of the entry; None if neither."""
    for candle in reversed(candles[-2:]):
        level = candle[2] if long else candle[1]
        if level < entry if long else level > entry:
            return level
    return None


def initial_stop(bars: list[tuple], rows: list[tuple], entry: float, long: bool, now: datetime, tz) -> float | None:
    """The SL of a new HA BRK position (the same call as ha_pure.initial_stop; no bars needed)."""
    return stop_from([ha(r) for r in rows[-2:]], entry, long)


def trail(position: dict, bars: list[tuple], levels: dict[int, tuple[float, float]], cutoff: float,
          cutoff_reason: str, now: datetime) -> dict:
    """Where a position stands after the 5-minute bars since its entry: closed at its SL or at
    `cutoff` (seconds), else open with its current SL ("stop"). `levels`: when each candle of the
    timeframe ends (seconds) -> its (HA low, HA high); at each one that ends after the entry the
    SL moves to the HA low (long) / HA high (short) when that is closer."""
    long = position["side"] == "BUY"
    stop = float(position["sl"])
    entry = float(position["entry"])
    entered = position["time"] / 1000
    high = low = entry
    moved = False
    first = -(-int(entered) // BAR) * BAR

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
                          reason="HA candle trail" if moved else "Stop loss")
        high, low = max(high, bar_high), min(low, bar_low)
        last = close
        end = start + BAR
        if end > now.timestamp() or end <= entered or end not in levels:
            continue
        level = levels[end][0] if long else levels[end][1]
        if level > stop if long else level < stop:
            stop, moved = level, True
    if now.timestamp() >= cutoff:
        price = last if last is not None else entry
        high, low = max(high, price), min(low, price)
        return result("closed", exit=round(price, 2), exit_time=int(cutoff * 1000), reason=cutoff_reason)
    return result("open")


def replay(position: dict, bars: list[tuple], rows: list[tuple], candle_end, cutoff: float, now: datetime,
           tz) -> dict:
    """trail() for an HA BRK position (the same call as ha_pure.replay): the candles of its
    timeframe, `candle_end(start)` being when the one that started at `start` ends; out at
    `cutoff`, 3:15 PM on expiry day."""
    levels = {candle_end(r[0]): (r[HA_LOW], r[HA_HIGH]) for r in rows}
    return trail(position, bars, levels, cutoff, "Expiry 3:15 PM", now)

