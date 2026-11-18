"""
HA WK BRK: the Auto trader's weekly Heikin Ashi breakout, timed on 4H candles and entered on a
pullback to the 30-minute SMA 8, for the F&O stocks, NIFTY and BANKNIFTY (auto_trader.py,
enter_weekly_signals; exits by settle_pure).

  * Setup: last week's Weekly HA candle is red (long) / green (short). The level: its HA high
    (long) / HA low (short).
  * Armed: the first time this week a 4H HA close, the running candle's included (live), is above
    the level (long) / below it (short). Kept in the account ("wk_armed") for the rest of the week.
  * Entry: after it is armed, a 5-minute bar whose low reaches the 30-minute SMA 8 (long; its high,
    short): in at the SMA, or at the bar's open if it opened past it. SMA 8: of the closes of the
    last 8 finished 30-minute candles (from 9:15 AM) before the bar. One entry per stock and week.
  * SL: the farther of the low (long) / high (short) of the last finished 30-minute candle and of
    the running one up to the entry, but at least MIN_SL_PCT from the entry. It stays put until the
    trade is BREAKEVEN_R x that risk in profit; then, at each 30-minute close, to the entry and on to
    that candle's low / high, never back. (A 30-minute trail from the start, the backtest showed,
    stops nearly every trade out within the hour: the entry is at the pullback, beside those lows.)
  * No other exit: held overnight, closed at 3:15 PM on the futures expiry day.

Candles are fetch_vp_candles' rows (start in seconds, close, high, low, HA open, HA high, HA low,
HA close, volume); 5-minute bars (start, open, high, low, close).
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta

import ha_pure
from ha_vp import HA_CLOSE, HA_HIGH, HA_LOW, HA_OPEN

SYSTEM = "HA WK BRK"
SYSTEMS = {SYSTEM: "4 HOUR"}
STYLE = "ha_wk"
LABEL = "HA WK BRK"
SL_NOTE = "30m candle"
SQUARE_OFF = time(15, 15)
SMA = 8
MIN_SL_PCT = 1.0  # the SL at least this % from the entry
BREAKEVEN_R = 1.0  # the SL moves (to the entry, then the 30-minute trail) once this many risks in profit
FRESH = 15 * 60  # seconds: a pullback older than this, found late (a missed run), is not traded
BAR = 300


def monday_of(day: date) -> date:
    return day - timedelta(days=day.weekday())


def setup(weekly: list[tuple], now: datetime, tz) -> tuple[str, float] | None:
    """("BUY", HA high) when last week's Weekly HA candle is red, ("SELL", HA low) when green, else
    None (no candle for last week, or a flat one)."""
    monday = monday_of(now.date())
    before = [row for row in weekly if datetime.fromtimestamp(row[0], tz).date() < monday]
    if not before or datetime.fromtimestamp(before[-1][0], tz).date() < monday - timedelta(days=7):
        return None
    row = before[-1]
    if row[HA_CLOSE] < row[HA_OPEN]:
        return "BUY", row[HA_HIGH]
    if row[HA_CLOSE] > row[HA_OPEN]:
        return "SELL", row[HA_LOW]
    return None


def crossed(rows: list[tuple], side: str, level: float, now: datetime, tz) -> int | None:
    """Start (seconds) of the first 4H candle of this week whose HA close is past the level: above
    it for a long, below it for a short; None if none."""
    monday = monday_of(now.date())
    for row in rows:
        if datetime.fromtimestamp(row[0], tz).date() >= monday and (
                row[HA_CLOSE] > level if side == "BUY" else row[HA_CLOSE] < level):
            return row[0]
    return None


def thirty_minute(bars: list[tuple], tz) -> dict[int, list[tuple]]:
    """30-minute candle start -> its 5-minute bars."""
    candles: dict[int, list[tuple]] = {}
    for bar in bars:
        candles.setdefault(ha_pure.trail_start(bar[0], tz), []).append(bar)
    return candles


def sma_before(candles: dict[int, list[tuple]], at: int, tz) -> float | None:
    """SMA of the closes of the last SMA 30-minute candles finished by `at` (seconds)."""
    closes = [bars[-1][4] for start, bars in sorted(candles.items()) if ha_pure.trail_end(start, tz) <= at]
    if len(closes) < SMA:
        return None
    return sum(closes[-SMA:]) / SMA


def pullback(bars: list[tuple], side: str, armed: int, now: datetime, tz) -> tuple[int, float, float] | None:
    """The latest 5-minute bar from `armed` on (seconds), finished, no older than FRESH, that reached
    the 30-minute SMA: (its start, the fill, the SMA); None if none."""
    long = side == "BUY"
    candles = thirty_minute(bars, tz)
    found = None
    for start, open_, high, low, _ in bars:
        if start < armed - BAR or start + BAR > now.timestamp() or start + BAR < now.timestamp() - FRESH:
            continue
        sma = sma_before(candles, start, tz)
        if sma is None:
            continue
        if low <= sma if long else high >= sma:
            fill = min(open_, sma) if long else max(open_, sma)
            found = (start, fill, sma)
    return found


def initial_stop(bars: list[tuple], rows: list[tuple], entry: float, long: bool, at: int, tz) -> float | None:
    """The SL of a position entered on the bar starting at `at`: the farther of the last finished
    30-minute candle's low (long) / high (short) and the running one's up to that bar, but at least
    MIN_SL_PCT from the entry (the 30-minute candles alone sit at the pullback, too close)."""
    before = [b for b in bars if b[0] <= at]
    finished = ha_pure.last_trail_candle(before, at, tz)
    running = [b for b in before if ha_pure.trail_start(b[0], tz) == ha_pure.trail_start(at, tz)]
    options = [finished] if finished else []
    if running:
        options.append((min(b[3] for b in running), max(b[2] for b in running)))
    floor = entry * MIN_SL_PCT / 100
    far = max([entry - low for low, _ in options] + [floor]) if long else max([high - entry for _, high in options] + [floor])
    return entry - far if long else entry + far


def replay(position: dict, bars: list[tuple], rows: list[tuple], candle_end, cutoff: float, now: datetime,
           tz) -> dict:
    """Where a position stands after the 5-minute bars since its entry: closed at its SL or at
    `cutoff` (3:15 PM on expiry day, seconds), else open with its current SL ("stop"). The SL stays
    put until the trade has been BREAKEVEN_R x its first risk in profit; from then on, at each
    30-minute candle's close, it moves to the entry and then to that candle's low (long) / high
    (short), never back. `rows` and `candle_end` are unused (settle_pure's call)."""
    long = position["side"] == "BUY"
    first_stop = stop = float(position["sl"])
    entry = float(position["entry"])
    risk = abs(entry - first_stop)
    entered = position["time"] / 1000
    high = low = entry
    moved = False
    trail = None  # the 30-minute candle being built from the bars since the entry: start, low, high

    def result(status, **extra):
        return {"id": position["id"], "status": status, "stop": round(stop, 2), "high": round(high, 2),
                "low": round(low, 2), **extra}

    last = None
    for start, open_, bar_high, bar_low, close in (b for b in bars if entered <= b[0] < cutoff):
        if start + BAR > now.timestamp():
            break
        gap = open_ <= stop if long else open_ >= stop
        if gap or (bar_low <= stop if long else bar_high >= stop):
            price = open_ if gap else stop
            high, low = max(high, price), min(low, price)
            return result("closed", exit=round(price, 2), exit_time=start * 1000,
                          reason="30m trailing SL" if moved else "Stop loss")
        high, low = max(high, bar_high), min(low, bar_low)
        last = close
        candle = ha_pure.trail_start(start, tz)
        if trail is None or trail[0] != candle:
            trail = [candle, bar_low, bar_high]
        else:
            trail[1], trail[2] = min(trail[1], bar_low), max(trail[2], bar_high)
        if start + BAR != ha_pure.trail_end(candle, tz):
            continue
        if (high - entry if long else entry - low) < BREAKEVEN_R * risk:
            continue
        level = max(trail[1], entry) if long else min(trail[2], entry)
        if level > stop if long else level < stop:
            stop, moved = level, True
    if now.timestamp() >= cutoff:
        price = last if last is not None else entry
        return result("closed", exit=round(price, 2), exit_time=int(cutoff * 1000), reason="Expiry 3:15 PM")
    return result("open")
