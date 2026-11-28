"""
OPT RSI: the RSI 2H / 4H / Daily triggers of the futures stocks and indices, entered through their ATM
option once the option agrees. Paper-traded by the Auto trader (auto_trader.enter_rsi_opt), no capital cap.

  * Trigger: a stock (F&O, any ATR) or NIFTY / BANKNIFTY on the RSI dashboard's 2H, 4H or Daily
    widgets today. Bullish -> watch its ATM CE; bearish -> its ATM PE. Watched from the run that first
    saw the trigger until LAST_ENTRY that day (same day only), then dropped.
  * Confirmation, on the option's own 15-minute or 30-minute candle ending after the trigger was seen:
      - a strong HA candle as the HA CE / PE alerts (opt_ha_alert.turned): HA solid green with a green
        normal candle, or an HA doji with a solid green normal candle, right after a red HA candle or a
        green HA doji; or
      - a higher high: a green normal candle whose high is above the previous candle's high.
    A red normal candle never confirms.
  * Entry: bought (1 lot) at that candle's close. SL: the previous HA candle's low on the confirming
    timeframe. Then exactly as OPT ATM HA (opt_atm.replay): the SL trails each completed 15-minute HA
    candle's low, never down; out at the SL or the 3:15 PM square-off.
  * One entry per underlying and side (CE / PE) a day.

The ATM strike is the one nearest the price at the run that checks it (nearest expiry; stock options
the monthly). Dhan: one option chain request every 3 seconds (opt_ha_alert.chain).
"""

from __future__ import annotations

import json
from datetime import date, datetime, time, timedelta
from pathlib import Path

import dhan_data as dd
import opt_ha_alert as oha

SYSTEM = "OPT RSI"  # not "RSI ...": the RSI strategies' exits (auto_trader.settle) must not apply
NAME = "RSI trigger · ATM option HA strong or higher high"
TRIGGER_TIMEFRAMES = {"2 HOUR": "2H", "4 HOUR": "4H", "DAILY": "Daily"}
FRAMES = {"15m": 15, "30m": 30}  # the option's confirming candles
LAST_ENTRY = time(15, 0)
LOOKBACK = timedelta(minutes=30)  # candles completed at most this long ago (a late run makes no stale entry)
DAYS = 5  # days of 15-minute option bars: enough to chain the HA
HERE = Path(__file__).parent


def leg_of(bias: str) -> str:
    return "CE" if bias == "Bullish" else "PE"


def underlying(symbol: str) -> dict | None:
    """The Dhan underlying of an index or F&O stock, or None without its strike data (strike_steps.json)."""
    if symbol in dd.INDICES:
        return dd.INDICES[symbol]
    saved = json.loads((HERE / "strike_steps.json").read_text(encoding="utf-8"))
    if symbol in saved["steps"] and symbol in saved["ids"]:
        return dd.stock(symbol, saved["ids"][symbol], saved["steps"][symbol])
    return None


def confirm(bars: list[tuple], since: float, until: float) -> dict | None:
    """The first 15m or 30m candle of the option ending in (since, until], up to LAST_ENTRY, that
    confirms (see the module): {"entry", "time" (its end, s), "sl", "frame", "rule", "candle"}; else None."""
    best = None
    for frame, minutes in FRAMES.items():
        candles = oha.resample(bars, minutes, until)
        ha = oha.heikin_ashi(candles)
        for k in range(1, len(candles)):
            c, before = candles[k], candles[k - 1]
            end = oha.window_end(c["start"], minutes)
            if not since < end <= until or datetime.fromtimestamp(end, dd.IST).time() > LAST_ENTRY:
                continue
            if best and end >= best["time"]:
                break
            green = c["close"] > c["open"]
            if not green:
                continue
            hit = oha.turned(bars, minutes, end, until)
            rule = (hit["kind"] if hit else None) or ("Higher high" if c["high"] > before["high"] else None)
            if rule:
                best = {"entry": c["close"], "time": end, "sl": ha[k - 1]["low"], "frame": frame, "rule": rule,
                        "candle": c}
                break
    return best


def triggers(signals: list[dict], seen_first: dict[tuple[str, str], float]) -> dict[str, dict]:
    """Today's triggers by "SYMBOL|CE" / "SYMBOL|PE": symbol, leg, bias, timeframes, since (seconds,
    first seen). `signals`: the dashboard rows (futures_simulator.signal_rows, index_signals);
    `seen_first`: (symbol, timeframe) -> when first seen today."""
    out: dict[str, dict] = {}
    for s in signals:
        if s["timeframe"] not in TRIGGER_TIMEFRAMES or not s.get("fno"):
            continue
        leg = leg_of(s["bias"])
        key = f"{s['symbol']}|{leg}"
        t = out.setdefault(key, {"symbol": s["symbol"], "leg": leg, "bias": s["bias"], "timeframes": [],
                                 "since": None})
        short = TRIGGER_TIMEFRAMES[s["timeframe"]]
        if short not in t["timeframes"]:
            t["timeframes"].append(short)
        first = seen_first.get((s["symbol"], s["timeframe"], s["bias"]))
        if first and (t["since"] is None or first < t["since"]):
            t["since"] = first
    return out


def is_today(ts: float, today: date) -> bool:
    return datetime.fromtimestamp(ts, dd.IST).date() == today
