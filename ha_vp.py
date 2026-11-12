"""
HA VP: the Auto trader's Heikin Ashi + volume profile strategy on the 2-hour candles of the
F&O stocks (auto_trader.py, enter_vp_signals and settle_vp), as the Pine script
"Volume Profile HA Strategy" (Desktop/Claude Pinescripts/Volume_Profile_HA_Strategy.pine),
but entering live, on the running candle, instead of at its close.

  * Trend: longs only when SMA 8 > SMA 20 > SMA 50 of the 2H closes and the close is above
    SMA 200; shorts only when SMA 8 < SMA 20 < SMA 50 and the close is below SMA 200.
  * Entry: long on a green running 2H HA candle with no lower wick (HA open = HA low) whose HA
    close is above the previous HA high; short on a red one with no upper wick (HA open = HA
    high) whose HA close is below the previous HA low. An opposite signal closes the position
    and enters the other way.
  * Target: the nearest POC, VAH or VAL of the volume profile (TARGET_PERIODS, volume_profile.py)
    at least MIN_TARGET_ATR x ATR above the entry (below for a short). HA VP takes it as profit when reached; HA VP TRAIL
    (SYSTEMS) does not, and trails TRAIL_PCT from there instead: the same signals, to compare.
  * SL: ATR_MULT x ATR(ATR_PERIOD) of the 2H candles below the highest high since entry (above
    the lowest low for a short), moved on every 5-minute bar, never back; the ATR is that of the
    last finished 2H candle. Once the target is reached (HA VP TRAIL), TRAIL_PCT behind
    the highest high (lowest low) instead. Hit when a 5-minute bar touches it.
  * Wick exit: in any 2H candle after the entry candle, as soon as its HA candle grows a wick
    against the trade: a lower wick for a long (price below that candle's HA open), an upper
    wick for a short (above its HA open). Out at the HA open, or the bar's open if it gapped past.
  * EMA exit: a finished 2H candle that closes across EMA(EMA_LEN) of the 2H closes against the
    trade (below for a long, above for a short), at that close.
  * Squared off at 3:15 PM: intraday only.

Also NIFTY and BANKNIFTY, in the same Chartink candles; their levels are the
"indices" of the file (volume_profile.py: their member stocks' turnover as the volume).

Candles are rows (start in seconds, close, high, low, HA open, HA high, HA low, HA close, volume), oldest
first (chartink_dashboard.fetch_vp_candles); 5-minute bars (start, open, high, low, close).
"""

from __future__ import annotations

from datetime import datetime, time

SYSTEM = "HA VP"
STYLE = "vp"
# The strategies trading these signals, and their exit style: "vp" takes profit at the target,
# "vp_trail" trails TRAIL_PCT once past it.
SYSTEMS = {"HA VP": "vp", "HA VP TRAIL": "vp_trail"}
TAKE_PROFIT_STYLES = ("vp",)
TIMEFRAME = "2 HOUR"
ATR_PERIOD = 14
ATR_MULT = 1.0
TRAIL_PCT = 1.5
EMA_LEN = 8
TARGET_PERIODS = ("Weekly", "Monthly", "3M", "1Y")  # as the Pine script's defaults (6M off)
MIN_TARGET_ATR = 1.0  # a level nearer the entry than this many ATRs is not a target: the next one is
SQUARE_OFF = time(15, 15)
BAR = 300  # seconds in a 5-minute bar
CLOSE, HIGH, LOW, HA_OPEN, HA_HIGH, HA_LOW, HA_CLOSE, VOLUME = range(1, 9)
SMAS = (8, 20, 50, 200)  # the trend filter: stacked SMAs of the 2H closes


def atr(rows: list[tuple]) -> list[float | None]:
    """Wilder's ATR(ATR_PERIOD) at each candle (None until there are enough)."""
    out, ranges, value = [], [], None
    for i, row in enumerate(rows):
        previous = rows[i - 1][CLOSE] if i else row[CLOSE]
        ranges.append(max(row[HIGH] - row[LOW], abs(row[HIGH] - previous), abs(row[LOW] - previous)))
        if len(ranges) == ATR_PERIOD:
            value = sum(ranges) / ATR_PERIOD
        elif len(ranges) > ATR_PERIOD:
            value = (value * (ATR_PERIOD - 1) + ranges[-1]) / ATR_PERIOD
        out.append(value)
    return out


def ema(rows: list[tuple]) -> list[float]:
    """EMA(EMA_LEN) of the closes at each candle, from the first close."""
    out, k = [], 2 / (EMA_LEN + 1)
    for row in rows:
        out.append(row[CLOSE] if not out else row[CLOSE] * k + out[-1] * (1 - k))
    return out


def sma(rows: list, length: int, column: int = CLOSE) -> float | None:
    """The SMA of the last `length` values of a column of `rows`, None if there are fewer."""
    if len(rows) < length:
        return None
    return sum(row[column] for row in rows[-length:]) / length


def trend(rows: list, column: int = CLOSE) -> str | None:
    """"BUY" when SMA 8 > SMA 20 > SMA 50 and the last close is above SMA 200 (SMAS), "SELL" the
    other way round, else None."""
    fast, mid, slow, long_ = (sma(rows, n, column) for n in SMAS)
    if long_ is None:
        return None
    close = rows[-1][column]
    if fast > mid > slow and close > long_:
        return "BUY"
    if fast < mid < slow and close < long_:
        return "SELL"
    return None


def signal(rows: list[tuple]) -> str | None:
    """"BUY", "SELL" or None for the last (running) candle of `rows`."""
    if len(rows) < 2:
        return None
    now, prev = rows[-1], rows[-2]
    side = trend(rows)
    if side == "BUY" and now[HA_CLOSE] > now[HA_OPEN] and now[HA_LOW] >= now[HA_OPEN] and now[HA_CLOSE] > prev[HA_HIGH]:
        return "BUY"
    if side == "SELL" and now[HA_CLOSE] < now[HA_OPEN] and now[HA_HIGH] <= now[HA_OPEN] and now[HA_CLOSE] < prev[HA_LOW]:
        return "SELL"
    return None

def target(levels: dict | None, entry: float, long: bool, gap: float = 0.0) -> float | None:
    """The nearest POC, VAH or VAL of TARGET_PERIODS at least `gap` beyond the entry (MIN_TARGET_ATR
    x ATR), from a stock's row of volume_profile_levels.json; None if there is none."""
    found = [v for period in TARGET_PERIODS for kind in ("POC", "VAH", "VAL")
             if isinstance(v := (levels or {}).get(f"{period} {kind}"), (int, float))
             and (v >= entry + gap if long else v <= entry - gap)]
    if not found:
        return None
    return min(found) if long else max(found)


def replay(position: dict, bars: list[tuple], rows: list[tuple], candle_end, now: datetime, tz) -> dict:
    """Where a position stands after the 5-minute bars since its entry: closed at its SL, target,
    an EMA cross at a 2H close, or at 3:15 PM, else open with its current SL ("stop").
    `rows` are the 2H candles, the running one included; `candle_end(start)` is when the 2H
    candle that started at `start` ends (seconds)."""
    long = position["side"] == "BUY"
    stop = float(position["sl"])
    goal = position.get("tp")
    take_profit = position.get("style", STYLE) in TAKE_PROFIT_STYLES
    value = position.get("atr")
    entered = datetime.fromtimestamp(position["time"] / 1000, tz)
    cutoff = datetime.combine(entered.date(), SQUARE_OFF, tz).timestamp()
    extreme = high = low = float(position["entry"])
    reached = moved = False
    # The 2H candles by their end: ATR, EMA and close at that end, and the EMA and close before.
    atrs, emas = atr(rows), ema(rows)
    spans = [(row[0], candle_end(row[0]), row[HA_OPEN]) for row in rows]
    entered_ts = entered.timestamp()
    entry_candle = next((s for s, e, _ in spans if s <= entered_ts < e), None)
    closes_at = {candle_end(row[0]): (atrs[i], emas[i], row[CLOSE], emas[i - 1] if i else None,
                                      rows[i - 1][CLOSE] if i else None) for i, row in enumerate(rows)}
    first = -(-int(entered.timestamp()) // BAR) * BAR

    def result(status, **extra):
        return {"id": position["id"], "status": status, "stop": round(stop, 2), "high": round(high, 2),
                "low": round(low, 2), **extra}

    for start, open_, bar_high, bar_low, close in (b for b in bars if first <= b[0] < cutoff):
        reason = f"Trail {TRAIL_PCT}%" if reached else "ATR trailing SL" if moved else "Stop loss"
        level = stop
        # A later 2H candle's HA open: the HA candle grows a wick against the trade past it.
        wick = next((o for s, e, o in spans if s <= start < e and s != entry_candle and s > (entry_candle or 0)), None)
        if wick is not None and (wick > stop if long else wick < stop):
            level, reason = wick, "HA lower wick" if long else "HA upper wick"
        gap = open_ < level if long else open_ > level
        if gap or (bar_low < level if long else bar_high > level) or (bar_low <= stop if long else bar_high >= stop):
            price = open_ if gap else level
            high, low = max(high, price), min(low, price)
            return result("closed", exit=round(price, 2), exit_time=start * 1000, reason=reason)
        if take_profit and goal is not None and (bar_high >= goal if long else bar_low <= goal):
            price = max(open_, goal) if long else min(open_, goal)
            high, low = max(high, price), min(low, price)
            return result("closed", exit=round(price, 2), exit_time=start * 1000, reason="VP target")
        high, low = max(high, bar_high), min(low, bar_low)
        extreme = max(extreme, bar_high) if long else min(extreme, bar_low)
        if goal is not None and (extreme >= goal if long else extreme <= goal):
            reached = True
        ended = closes_at.get(start + BAR)
        if ended:
            value, line, candle_close, line_before, close_before = ended
            if line_before is not None and (
                    candle_close < line and close_before >= line_before if long
                    else candle_close > line and close_before <= line_before):
                return result("closed", exit=round(close, 2), exit_time=(start + BAR) * 1000,
                              reason=f"EMA {EMA_LEN} cross")
        if value:
            level = (extreme * (1 - TRAIL_PCT / 100) if long else extreme * (1 + TRAIL_PCT / 100)) if reached else (
                extreme - ATR_MULT * value if long else extreme + ATR_MULT * value)
            if level > stop if long else level < stop:
                stop, moved = level, True
    if now.timestamp() >= cutoff:
        session = [b for b in bars if b[0] < cutoff and datetime.fromtimestamp(b[0], tz).date() == entered.date()]
        price = session[-1][4] if session else float(position["entry"])
        return result("closed", exit=round(price, 2), exit_time=int(cutoff * 1000), reason="EOD 3:15 PM",
                      high=round(max(high, price), 2), low=round(min(low, price), 2))
    return result("open")
