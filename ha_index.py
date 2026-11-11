"""
HA 15m index: the Auto trader's Heikin Ashi strategy on the 15-minute candles of NIFTY and
BANKNIFTY (auto_trader.py, enter_index_signals and settle), made from Yahoo Finance's
5-minute bars of the index (Chartink's index list has no BANKNIFTY data).

  * Entry, on the running 15-minute candle: long when the close is above the previous HA high
    and that HA candle was red (HA close below HA open); short when the close is below the
    previous HA low and that HA candle was green. No entry when the previous HA candle is a doji: its body
    less than DOJI_BODY of its range.
  * SL ATR_MULT x ATR(ATR_PERIOD) of the 15-minute candles from the entry. After each
    15-minute candle closes, it moves to that close -/+ ATR_MULT x ATR when that is closer,
    never back. Hit when a 5-minute bar touches it (at the bar's open if it gapped past).
  * Squared off at 3:15 PM: intraday only.
"""

from __future__ import annotations

from datetime import datetime, time, timedelta

SYMBOLS = ("NIFTY", "BANKNIFTY")
TIMEFRAME = "15 MIN"
MINUTES = 15
ATR_PERIOD = 14
ATR_MULT = 1.1
DOJI_BODY = 0.10  # a previous HA candle whose body is less than this share of its range is a doji
SESSION_OPEN, SESSION_END, SQUARE_OFF = time(9, 15), time(15, 30), time(15, 15)
BAR = 300  # seconds in a 5-minute bar


def candle_start(ts: int, tz) -> int:
    """Start (seconds) of the 15-minute candle a time is in, from 9:15 AM."""
    moment = datetime.fromtimestamp(ts, tz)
    session = datetime.combine(moment.date(), SESSION_OPEN, tz)
    return int((session + (moment - session) // timedelta(minutes=MINUTES) * timedelta(minutes=MINUTES)).timestamp())


def candles(bars: list[tuple], tz) -> list[list]:
    """15-minute candles [start, open, high, low, close] from 5-minute bars (start, open, high,
    low, close), session hours only; the last one may still be running."""
    out: list[list] = []
    for start, open_, high, low, close in bars:
        moment = datetime.fromtimestamp(start, tz).time()
        if not SESSION_OPEN <= moment < SESSION_END:
            continue
        key = candle_start(start, tz)
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


def doji(candle: tuple) -> bool:
    """Whether an HA candle (open, high, low, close) is a doji: its body (close - open) less than
    DOJI_BODY of its range (high - low)."""
    open_, high, low, close = candle
    span = high - low
    return span <= 0 or abs(close - open_) < DOJI_BODY * span


def signal(rows: list[list]) -> str | None:
    """"BUY", "SELL" or None for the last (running) candle of `rows`: the close above the
    previous HA high with the previous HA candle red (HA close < HA open). Short: the close
    below the previous HA low with the previous HA candle green (HA close > HA open). None
    when the previous HA candle is a doji (doji)."""
    if len(rows) < 2:
        return None
    ha = heikin_ashi(rows)
    if doji(ha[-2]):
        return None
    close = rows[-1][4]
    p_open, p_high, p_low, p_close = ha[-2]
    if close > p_high and p_close < p_open:
        return "BUY"
    if close < p_low and p_close > p_open:
        return "SELL"
    return None


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
