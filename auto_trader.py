"""
Auto trader: paper-trades every Chartink signal without the simulator page open, for several strategies apart (fs.SYSTEMS):
RSI (chartink_dashboard.DEFAULT_DASHBOARD: its 2H signals; its 4H, Daily and Weekly ones are
RSI 4H, RSI Daily and RSI Weekly, see RSI_SYSTEMS: entered on their own timeframe's signal, then
traded exactly as RSI: SL from the previous 2H candle, the same styles and 2H SMA exits), Heikin Ashi (its own scans, chartink_dashboard.ha_scan) and HA MTF (the
HA signals of 2H, 4H and Daily: a timeframe counts unless its previous HA candle is a
doji, and the position is held until the HA exit of the highest one that counted, which
trails it, with a fixed SL MTF_SL_PCT from the entry until then). A stock on several gets a trade in
each, so that each strategy's results stay its own. A fourth, HA IDX (ha_index.py), trades
NIFTY and BANKNIFTY on 15-minute Heikin Ashi candles with an ATR trailing SL, intraday; HA IDX PURE
the same without the SMA trend filter (INDEX_SYSTEMS); all of them only when the previous 4H HA
candle is solid green (long) or red (short) (ha_index.htf_side), and the plain 15-minute candle is
green (long) or red (short) too (ha_index.candle_agrees). HA PURE 2H, 4H and Daily (ha_pure.py) trade
that rule on the F&O stocks, NIFTY and BANKNIFTY on those timeframes, held overnight, SL trailed
on 30-minute candles. HA BRK 2H, 4H and Daily (ha_break.py) trade the HA breakout (a wickless HA
candle closing past the previous HA high / low) the same way, SL trailed to the previous HA
candle's low / high; HA IDX BRK the same on the 15-minute index candles, intraday.
A fifth, HA VP (ha_vp.py), and its twin HA VP TRAIL (trails past the target instead of taking it), trade the F&O stocks, NIFTY and BANKNIFTY on their running 2H Heikin Ashi candle, with an
ATR trailing SL, a volume profile target, HA doji and EMA 8 exits, intraday. HA and HA MTF take no new
trades any more: their open positions still close by their rules.

.github/workflows/auto_trader.yml runs it every 15 minutes in market hours. Each run:
  * replays the open positions on the 5-minute bars since entry, closing those that
    touched their SL, trailing SL or target, at the 3:15 PM square-off or expiry, or
    on an RSI exit (the same rules as the simulator: futures_simulator.check_positions)
  * buys bullish and sells bearish signals at the latest price, with the SL at the
    previous candle's low (a long) or high (a short), see entry_sl, and target AUTO_TP_PCT away, once in each exit style (futures_simulator.STYLES) to
    compare them on the same signals: squared off at 3:15 PM, held with a trailing SL
    after the target, held until the RSI exit (for Heikin Ashi: the HA exit, see
    ha_exit). RSI now trades Hold · trail and two SMA exits instead (sma_exit): held until
    a finished 2H candle closes below (a long) / above (a short) the SMA 8, or the SMA 20,
    of the 2H closes (RSI 4H, Daily and Weekly too); and in every RSI strategy and style, out when a
    finished 30-minute candle's SMA 14 of RSI 14 is below 40 (a long) / above 60 (a short) (rsi30_exit); its EOD and RSI exit styles are retired (futures_simulator.RETIRED_STYLES). A stock is traded once per side and style per session, in each strategy.
    Every signal is taken, with no capital limit, to test the strategy: the Auto trader page works out the margin that needed from the trades
  * applies a request from the Auto trader page (auto_trader_page.py): keep a
    position going past its target with a trailing SL, exit at the target, or exit now

While the Auto trader page is open it starts a run every few minutes itself, on
time, and the scheduled runs (--scheduled) stay quiet: they skip while the page
started one in the last PAGE_QUIET minutes. When it is closed, the scheduled runs
trade as above, or with "unattended" off (a switch on the page) only close
positions and take no new signals.

Its files, in the folder of ACCOUNT.json (the auto-trader-data branch, out of main),
split so that each run changes little:
  auto_trades.json         the account: open positions, settings, the run log
  trades/YYYY-MM.json      closed trades, by the month they closed in
  signals/YYYY-MM-DD.json  each signal (stock, timeframe, bias) seen that day: when it
                           was first and last seen, its price when first seen, and what
                           was done with it (traded, or why not). Trades keep the first
                           sighting as "first_seen", to measure the cost of entering later.
  manual/YYYY-MM.json      the simulator's closed trades (written by the simulator page)

    python auto_trader.py ACCOUNT.json [--action ACTION] [--position ID] [--scheduled]
"""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
from datetime import datetime, time, timedelta
from pathlib import Path

import futures_simulator as fs
import ha_break
import ha_index
import ha_pure
import ha_vp
import ha_weekly
import opt_atm
import opt_ha_alert
import telegram_alerts
from chartink_dashboard import NO_VALUE

TIMEFRAMES = ("2 HOUR", "4 HOUR", "DAILY", "WEEKLY")
# The timeframes whose signals are traded; the others are only recorded, as "Timeframe not traded".
# HA and HA MTF ("HA": ("2 HOUR",), "HA MTF": ("2 HOUR", "4 HOUR", "DAILY")) take no new trades: HA VP replaced them.
TRADED_TIMEFRAMES = {"RSI": ("2 HOUR",), "RSI 4H": ("4 HOUR",), "RSI Daily": ("DAILY",), "RSI Weekly": ("WEEKLY",)}
# The RSI dashboard's signals go to the strategy of their timeframe.
RSI_SYSTEMS = {tf: system for system, (tf,) in TRADED_TIMEFRAMES.items()}
MTF_SL_PCT = 3.0  # HA MTF: the initial SL; the HA exit of the exit timeframe trails from there
DOJI_BODY = 0.10  # HA MTF: a previous HA candle with a body this share of its range or less is a doji
SHORT = {"2 HOUR": "2H", "4 HOUR": "4H", "DAILY": "Daily", "WEEKLY": "Weekly"}
LOTS = 1
# The column of the HA widgets with the SMA of the RSI of the signal's own timeframe.
HA_SMA_RSI = {"2 HOUR": "Sma 2hr rsi", "4 HOUR": "Sma 4hr rsi", "DAILY": "Dly SMA RSI"}
# The RSI strategies' 30-minute exit: SMA 14 of the 30-minute RSI 14 below (long) / above (short) this.
RSI30_EXIT = {"BUY": 40, "SELL": 60}
RSI30_SINCE = datetime(2026, 11, 16, 11, 30, tzinfo=fs.IST).timestamp()  # rule added: candles ending earlier do not count
LAST_ENTRY = time(15, 0)  # no new trades after this, ahead of the 3:15 PM square-off
SESSION_CLOSE = time(15, 30)
PAGE_QUIET = 12  # minutes: scheduled runs skip while the page started a run this recently
ACTIONS = ["run", "trail", "untrail", "exit", "unattended-on", "unattended-off", "test-chartink", "test-telegram",
           "rsi-today", "ha-today"]


def tick(price: float) -> float:
    return round(round(price * 20) / 20, 2)  # NSE futures tick size ₹0.05


def protection(position: dict, atrs: dict[str, float], since_ms: int) -> dict | None:
    """The profit lock of an RSI position (futures_simulator.replay, PROTECT_*): its daily ATR, the
    round-trip charges per unit plus a tick, and from when; None without an ATR."""
    atr = atrs.get(position["symbol"])
    if not atr:
        return None
    value = position["entry"] * position["qty"]
    cost = charges(value, value) / position["qty"] + 0.05
    return {"atr": round(atr, 2), "cost": round(cost, 2), "since": since_ms}


def index_pairs(symbols: set[str]) -> tuple[tuple[str, str], ...]:
    """(symbol, Yahoo symbol) of the indices among `symbols`, for fs.daily_atrs."""
    instruments = {i["symbol"]: i for i in fs.load_instruments()}
    return tuple(sorted((s, instruments[s]["yahoo"]) for s in symbols
                        if s in instruments and fs.is_index(instruments[s])))


def charges(buy_value: float, sell_value: float) -> float:
    """Brokerage and taxes of a round trip (as charges() in futures_simulator.js)."""
    rates = fs.CHARGES
    brokerage = sum(min(rates["brokerage_per_order"], v * rates["brokerage_rate"]) for v in (buy_value, sell_value))
    exchange = (buy_value + sell_value) * rates["exchange"]
    sebi = (buy_value + sell_value) * rates["sebi"]
    stt = sell_value * rates["stt_sell"]
    stamp = buy_value * rates["stamp_buy"]
    return brokerage + exchange + sebi + stt + stamp + (brokerage + exchange + sebi) * rates["gst"]


class Files:
    """The JSON files beside the account, read once and written back if changed."""

    def __init__(self, folder: Path):
        self.folder, self.data, self.changed = folder, {}, set()

    def get(self, name: str, default):
        if name not in self.data:
            path = self.folder / name
            self.data[name] = json.loads(path.read_text(encoding="utf-8")) if path.exists() else default
        return self.data[name]

    def save(self) -> None:
        for name in self.changed:
            path = self.folder / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(self.data[name], indent=None if name.startswith(("signals/", "alerts/")) else 1),
                            encoding="utf-8")


def month_of(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, fs.IST).strftime("%Y-%m")


def trades_of(files: Files, month: str) -> list[dict]:
    return files.get(f"trades/{month}.json", [])


def load(files: Files, name: str) -> dict:
    account = files.data[name] = files.get(name, None) or {
        "capital": fs.START_CAPITAL, "positions": [], "seq": 0, "log": []}
    # Accounts from before the monthly trade files kept their trades in "history".
    for trade in account.pop("history", []):
        trades_of(files, month_of(trade["exit_time"])).append(trade)
        files.changed.add(f"trades/{month_of(trade['exit_time'])}.json")
    return account


def signal_key(signal: dict) -> str:
    """RSI signals keep the plain key: the simulator looks them up by it (load_seen)."""
    key = f"{signal['symbol']}|{signal['timeframe']}|{signal['bias']}"
    return key if signal["system"] == "RSI" else f"{signal['system']}|{key}"


def record_seen(seen: dict, signals: list[dict], dashboard: dict, now: datetime) -> None:
    """Note each signal of today: first seen (time, price) and last seen."""
    now_ms = int(now.timestamp() * 1000)
    new = [s for s in signals if signal_key(s) not in seen]
    quotes = {}
    if new:
        instruments = {i["symbol"]: i for i in fs.load_instruments()}
        quotes = fs.latest_prices({s["symbol"] for s in new}, instruments, dashboard)
    for signal in signals:
        entry = seen.setdefault(signal_key(signal), {
            "symbol": signal["symbol"], "timeframe": signal["timeframe"], "bias": signal["bias"],
            "strategy": signal["strategy"], "system": signal["system"], "fno": signal["fno"], "time": now_ms,
            "price": (quotes.get(signal["symbol"]) or {}).get("price"), "status": None})
        entry["last"] = now_ms


def mark(seen: dict, signals: list[dict], status: str) -> None:
    """What was done with signals: kept once traded."""
    for signal in signals:
        entry = seen.get(signal_key(signal))
        if entry and not (entry.get("status") or "").startswith("Traded"):
            entry["status"] = status


def close(files: Files, account: dict, position: dict, price: float, when_ms: int, reason: str,
          high: float | None = None, low: float | None = None) -> None:
    qty = position["qty"]
    buy, sell = (position["entry"], price) if position["side"] == "BUY" else (price, position["entry"])
    gross = (sell - buy) * qty
    cost = (opt_atm.charges if position.get("system") == opt_atm.SYSTEM else charges)(buy * qty, sell * qty)
    high = max(high or position.get("high") or position["entry"], price)
    low = min(low or position.get("low") or position["entry"], price)
    trades_of(files, month_of(when_ms)).append({
        "id": position["id"], "symbol": position["symbol"], "side": position["side"], "lots": position["lots"],
        "qty": qty, "entry": position["entry"], "exit": round(price, 2), "entry_time": position["time"],
        "exit_time": when_ms, "reason": reason, "source": position["source"], "timeframes": position["timeframes"],
        "system": position.get("system", "RSI"), "style": position.get("style", "eod"),
        "passed": position.get("passed", False),
        "carried": bool(position.get("run")), "sl": position["sl"], "tp": position["tp"],
        "high": round(high, 2), "low": round(low, 2), "nifty": position.get("nifty"),
        "strategies": position.get("strategies", []), "first_seen": position.get("first_seen"),
        "agrees": position.get("agrees"), "sma_rsi": position.get("sma_rsi"),
        "gross": round(gross, 2), "charges": round(cost, 2),
        "net": round(gross - cost, 2),
    })
    files.changed.add(f"trades/{month_of(when_ms)}.json")
    account["positions"].remove(position)
    telegram_alerts.exit(trades_of(files, month_of(when_ms))[-1])


def alert_problem(account: dict, key: str, text: str, now: datetime) -> None:
    """A Telegram alert for a problem, at most once a day for each kind (key)."""
    sent = account.setdefault("alerted", {})
    if sent.get(key) != now.date().isoformat():
        sent[key] = now.date().isoformat()
        telegram_alerts.problem(text)


def auto_run(entry: float) -> dict:
    """Trailing amounts past the target (as autoRun() in futures_simulator.js)."""
    return {"trail": tick(entry * fs.AUTO_TRAIL_PCT / 100), "step": tick(entry * fs.AUTO_STEP_PCT / 100)}


def apply_request(files: Files, account: dict, action: str, position_id: str, now: datetime, note) -> None:
    position = next((p for p in account["positions"] if p["id"] == position_id), None)
    if position is None:
        note(f"{action}: no open position {position_id}")
        return
    if action in ("trail", "untrail") and position.get("style", "eod") != "eod":
        note(f"{position['symbol']}: a {fs.BOT_STYLES[position['style']]} position, no target exit to change")
        return
    if action == "trail":
        position["run"] = auto_run(position["entry"])
        note(f"{position['symbol']}: keeps going at the target with a trailing SL")
    elif action == "untrail":
        if position.get("passed"):
            note(f"{position['symbol']}: already past its target, still trailing")
            return
        position["run"] = None
        note(f"{position['symbol']}: exits at the target")
    elif action == "exit" and position.get("system") == opt_atm.SYSTEM:
        try:
            bars = opt_atm.bars_of(position["security_id"])
        except opt_atm.dd.DhanError as exc:
            note(f"{position['symbol']}: no price to exit at ({exc})")
            return
        if not bars:
            note(f"{position['symbol']}: no price to exit at")
            return
        close(files, account, position, tick(bars[-1][4]), int(now.timestamp() * 1000), "Manual exit")
        note(f"{position['symbol']}: exited at {tick(bars[-1][4])}")
    elif action == "exit":
        instruments = {i["symbol"]: i for i in fs.load_instruments()}
        quote = fs.latest_prices({position["symbol"]}, instruments, fs.load_dashboard()[0]).get(position["symbol"])
        if not quote:
            note(f"{position['symbol']}: no price to exit at")
            return
        close(files, account, position, tick(quote["price"]), int(now.timestamp() * 1000), "Manual exit")
        note(f"{position['symbol']}: exited at {tick(quote['price'])}")


def candle_end(timeframe: str, start: int) -> float:
    """When a candle of `timeframe` that started at `start` (seconds) ends: its length on
    from 9:15 AM, with the day's last one running to the close (Chartink's 2H and 4H do)."""
    opened = datetime.fromtimestamp(start, fs.IST)
    if timeframe == "WEEKLY":  # Monday to Friday's close
        friday = opened.date() + timedelta(days=4 - opened.weekday())
        return datetime.combine(friday, SESSION_CLOSE, fs.IST).timestamp()
    session_close = datetime.combine(opened.date(), SESSION_CLOSE, fs.IST)
    minutes = fs.CANDLE_MINUTES.get(timeframe)
    if not minutes:
        return session_close.timestamp()
    end = opened + timedelta(minutes=minutes)
    return (session_close if end.time() >= fs.SQUARE_OFF else end).timestamp()


def ha_level(rows: list[tuple], index: int, long: bool, style: str) -> float | None:
    """The price the candle at `index` must close past: the low (a long) or high (a short) of
    the HA candle before it or, in the ha_green style, of the last HA candle before it that
    was green (a long) or red (a short). None if there is none."""
    earlier = rows[:index]
    if style == "ha_green":
        earlier = [row for row in earlier if row[5] != row[4] and (row[5] > row[4]) == long]
    return earlier[-1][2 if long else 3] if earlier else None


def candle_key(timeframe: str, start: int) -> tuple:
    """The candle of `timeframe` a 5-minute bar starting at `start` (seconds) is in: its day and
    number in the day, from 9:15 AM, with the day's last one running to the close (as candle_end)."""
    opened = datetime.fromtimestamp(start, fs.IST)
    minutes = fs.CANDLE_MINUTES.get(timeframe)
    if not minutes:
        return (opened.date(), 0)
    since = opened - datetime.combine(opened.date(), fs.SESSION_OPEN, fs.IST)
    last = (fs.SQUARE_OFF.hour * 60 + fs.SQUARE_OFF.minute - 1
            - fs.SESSION_OPEN.hour * 60 - fs.SESSION_OPEN.minute) // minutes
    return (opened.date(), min(max(int(since.total_seconds() // 60), 0) // minutes, last))


def previous_candle(bars: list[tuple], timeframe: str, now: datetime) -> tuple[float, float] | None:
    """Low and high of the candle of `timeframe` before the running one, from 5-minute bars
    (start in seconds, open, high, low, close). None if the bars do not reach back to it."""
    current = candle_key(timeframe, int(now.timestamp()))
    candles: dict[tuple, list[tuple]] = {}
    for bar in bars:
        if bar[0] <= now.timestamp():
            candles.setdefault(candle_key(timeframe, bar[0]), []).append(bar)
    earlier = [key for key in candles if key < current]
    if not earlier:
        return None
    found = candles[max(earlier)]
    return min(bar[3] for bar in found), max(bar[2] for bar in found)


def entry_sl(entry: float, long: bool, system: str, timeframe: str, symbol: str, sources: dict,
             now: datetime) -> float:
    """The SL of a new position. RSI: the low (a long) or high (a short) of the previous candle
    of its timeframe, or AUTO_SL_PCT away, whichever is lower (a long) or higher (a short).
    HA: the low (a long) or high (a short) of the previous HA candle. AUTO_SL_PCT away when
    the candle is not known, or is on the wrong side of the entry."""
    percent = tick(entry * (1 - fs.AUTO_SL_PCT / 100 * (1 if long else -1)))
    level = None
    if system.startswith("HA"):
        rows = (sources.get(timeframe) or {}).get(symbol) or []
        done = [row for row in rows if candle_end(timeframe, row[0]) <= now.timestamp()]
        level = done[-1][2 if long else 3] if done else None
    else:
        found = previous_candle(sources.get(symbol) or [], timeframe, now)
        level = found and found[0 if long else 1]
    if level is None or (level >= entry if long else level <= entry):
        return percent
    level = tick(level)
    if system.startswith("HA"):
        return level
    return min(level, percent) if long else max(level, percent)


def previous_doji(candles: dict, timeframe: str, symbol: str, now: datetime) -> bool:
    """HA MTF: whether the last finished HA candle of `timeframe` (the one before the running
    candle) is a doji: its body (HA close - HA open) DOJI_BODY of its range (HA high - HA low)
    or less. False when the candles are not known."""
    rows = (candles.get(timeframe) or {}).get(symbol) or []
    done = [row for row in rows if candle_end(timeframe, row[0]) <= now.timestamp()]
    if not done:
        return False
    _, _, low, high, open_, close = done[-1]
    return abs(close - open_) <= DOJI_BODY * (high - low)


def ha_exit(position: dict, candles: dict, now: datetime) -> dict | None:
    """The HA exit of a position, if due: the first candle of its timeframe (the shortest if
    several) since the entry whose close, live while it runs, went past its level (ha_level).
    A finished candle, in case the runs missed it, exits at its end. Exit price, time (ms)
    and reason, else None."""
    timeframe = position.get("exit_tf") or position["timeframes"][0]
    rows = (candles.get(timeframe) or {}).get(position["symbol"]) or []
    long = position["side"] == "BUY"
    style = position.get("style")
    name = "HA exit" if style != "ha_green" else "HA green exit" if long else "HA red exit"
    for index, candle in enumerate(rows):
        end = candle_end(timeframe, candle[0])
        level = ha_level(rows, index, long, style)
        if end * 1000 <= position["time"] or level is None:
            continue
        if candle[1] < level if long else candle[1] > level:
            return {"exit": tick(candle[1]), "exit_time": int(min(end, now.timestamp()) * 1000),
                    "reason": f"{name} ({SHORT[timeframe]})"}
    return None


def sma_exit(position: dict, candles: dict, now: datetime) -> dict | None:
    """The SMA exit of a position of an SMA style (fs.SMA_EXITS), if due: the first finished 2H
    candle ending after the entry that closed below (a long) or above (a short) the SMA of the
    last N 2H closes, at that close (whatever the signal's timeframe: RSI 4H, Daily and Weekly
    exit as RSI). Exit price, time (ms) and reason, else None."""
    length = fs.SMA_EXITS[position["style"]]
    timeframe = "2 HOUR"
    rows = candles.get(position["symbol"]) or []
    long = position["side"] == "BUY"
    for index in range(length - 1, len(rows)):
        end = candle_end(timeframe, rows[index][0])
        if end * 1000 <= position["time"] or end > now.timestamp():
            continue
        close = rows[index][1]
        sma = sum(row[1] for row in rows[index - length + 1:index + 1]) / length
        if close < sma if long else close > sma:
            return {"exit": tick(close), "exit_time": int(end * 1000),
                    "reason": f"{SHORT[timeframe]} close past SMA {length}"}
    return None


def rsi30_exit(position: dict, series: dict, now: datetime) -> dict | None:
    """The 30-minute RSI exit of an RSI position, if due: the first finished 30-minute candle ending
    after the entry whose SMA 14 of RSI 14 is below RSI30_EXIT["BUY"] (a long) or above
    RSI30_EXIT["SELL"] (a short), at its close. Exit price, time (ms) and reason, else None."""
    long = position["side"] == "BUY"
    level = RSI30_EXIT[position["side"]]
    for start, close, value in series.get(position["symbol"]) or []:
        opened = datetime.fromtimestamp(start, fs.IST)
        end = min(start + 1800, datetime.combine(opened.date(), SESSION_CLOSE, fs.IST).timestamp())
        if end * 1000 <= position["time"] or end <= RSI30_SINCE or end > now.timestamp():
            continue
        if value < level if long else value > level:
            return {"exit": tick(close), "exit_time": int(end * 1000),
                    "reason": f"30m RSI SMA {value:.1f} {'<' if long else '>'} {level}"}
    return None


def settle(files: Files, account: dict, now: datetime, note) -> None:
    """Close the positions that hit their SL, trailing SL or target, the 3:15 PM square-off,
    expiry or their RSI or HA exit; note the highest and lowest price of the others."""
    if not account["positions"]:
        return
    candles = {}
    if any(p.get("style") in fs.HA_EXITS for p in account["positions"]):
        try:
            candles = fs.fetch_ha_candles()
        except fs.ChartinkError as exc:
            note(f"No candles for the HA exits: {exc}")
    two_hour = {}
    if any(p.get("style") in fs.SMA_EXITS for p in account["positions"]):
        try:
            two_hour = fs.fetch_vp_candles()
        except fs.ChartinkError as exc:
            note(f"No 2H candles for the SMA exits: {exc}")
        longest = max((len(rows) for rows in two_hour.values()), default=0)
        if two_hour and longest < max(fs.SMA_EXITS.values()):
            note(f"Only {longest} 2H candles from Chartink: too few for the SMA exits")
    rsi30 = {}
    if any(p.get("system", "RSI").startswith("RSI") for p in account["positions"]):
        try:
            rsi30 = fs.fetch_rsi30()
        except fs.ChartinkError as exc:
            note(f"No 30-minute RSI for the RSI exits: {exc}")
    settle_index(files, account, now, note)
    settle_options(files, account, now, note)
    settle_vp(files, account, now, note)
    for module in (*HELD_MODULES, ha_weekly):
        settle_pure(files, account, now, note, module)
    # RSI positions from before the profit lock get it now, counting from this run on.
    bare = [p for p in account["positions"] if p.get("system", "RSI").startswith("RSI") and not p.get("protect")]
    if bare:
        atrs = fs.daily_atrs(index_pairs({p["symbol"] for p in bare}))
        for position in bare:
            found = protection(position, atrs, int(now.timestamp() * 1000))
            if found:
                position["protect"] = found
    for result in fs.check_positions([p for p in account["positions"]
                                      if p.get("system") not in (*INDEX_SYSTEMS, *ha_vp.SYSTEMS, *ha_pure.SYSTEMS, *ha_break.SYSTEMS,
                                                                 *ha_weekly.SYSTEMS, opt_atm.SYSTEM)], now, settle=False):
        position = next(p for p in account["positions"] if p["id"] == result["id"])
        # A candle that went past since the last run may have come before what the bars show.
        due = (ha_exit(position, candles, now) if position.get("style") in fs.HA_EXITS
               else sma_exit(position, two_hour, now) if position.get("style") in fs.SMA_EXITS else None)
        crossed = rsi30_exit(position, rsi30, now) if position.get("system", "RSI").startswith("RSI") else None
        if crossed and (due is None or crossed["exit_time"] < due["exit_time"]):
            due = crossed
        if due and (result["status"] != "closed" or due["exit_time"] < result["exit_time"]):
            result = {**result, **due, "status": "closed"}
        if result["status"] == "closed" and not 0 < result["exit"] < NO_VALUE:
            note(f"{position['symbol']}: ignored a {result['reason']} exit at {result['exit']} (bad price)")
        elif result["status"] == "closed":
            close(files, account, position, result["exit"], result["exit_time"], result["reason"],
                  result.get("high"), result.get("low"))
            note(f"{position['symbol']}: {result['reason']} at {result['exit']} ({fs.BOT_STYLES[position['style']]})")
        elif result["status"] == "error":
            note(f"{position['symbol']}: no bars ({result['error']})")
        else:
            position.update(stop=result["stop"], status=result["status"], passed=result.get("passed", False),
                            high=result.get("high"), low=result.get("low"))


def load_ha_signals() -> tuple[dict | None, str | None]:
    """The HA entry scans the bot runs itself (chartink_dashboard.ha_scan: with the doji filter,
    without an SMA RSI condition), as a dashboard; else None and the error."""
    try:
        return fs.fetch_ha_signals(), None
    except fs.ChartinkError as exc:
        return None, f"Chartink (HA scans): {exc}"


INDEX_SYSTEM = "HA IDX"
# The 15-minute index strategies and their rule (ha_index.rule_signal): "trend" the HA entry with the
# SMA trend filter, "pure" without it, "break" the HA breakout with the HA candle trail (ha_break.py).
INDEX_SYSTEMS = {INDEX_SYSTEM: "trend", "HA IDX PURE": "pure", ha_break.INDEX_SYSTEM: "break"}
# The held HA strategies of 2H, 4H and Daily candles: HA PURE (30-minute trail, doji exit), HA BRK.
HELD_MODULES = (ha_pure, ha_break)
# Retired on 18 Nov 2026 for their losses: no new trades. Their open positions still close on their own
# exits, and their trades are kept; the page hides them (auto_trader_page.RETIRED_SYSTEMS).
RETIRED = {"HA PURE 2H", "HA PURE Daily", "HA BRK 2H", "HA BRK 4H", "HA BRK Daily", "HA VP", "HA VP TRAIL",
           "HA PURE 4H", "HA WK BRK",  # these two on 24 Nov 2026
           "HA IDX", "HA IDX PURE", "HA IDX BRK",  # the 15-minute index strategies, also on 24 Nov 2026
           "RSI", "RSI 4H"}  # RSI 2H and 4H on 27 Nov 2026 (RSI Daily kept); the page still shows them


def index_bars(note) -> dict[str, list]:
    """symbol -> the 5-minute bars of the last few sessions of each HA IDX index (Yahoo)."""
    instruments = {i["symbol"]: i for i in fs.load_instruments()}
    # A month: SMA 200 of the 15-minute closes (ha_index.signal) needs 200 candles, about 8 sessions.
    wanted = {s: (instruments[s]["yahoo"], "1mo") for s in ha_index.SYMBOLS if s in instruments}
    found = fs.fetch_bars(tuple(sorted(set(wanted.values()))))
    bars = {}
    for symbol, request in wanted.items():
        if isinstance(found.get(request), list) and found[request]:
            bars[symbol] = found[request]
        else:
            note(f"{symbol}: no 5-minute bars ({found.get(request)})")
    return bars


def settle_index(files: Files, account: dict, now: datetime, note) -> None:
    """Close the HA IDX (INDEX_SYSTEMS) positions that hit their ATR SL, a doji or the 3:15 PM square-off;
    move the others' SL."""
    positions = [p for p in account["positions"] if p.get("system") in INDEX_SYSTEMS]
    if not positions:
        return
    bars = index_bars(note)
    for position in positions:
        if position["symbol"] not in bars:
            continue
        replay = ha_index.break_replay if INDEX_SYSTEMS[position["system"]] == "break" else ha_index.replay
        result = replay(position, bars[position["symbol"]], now, fs.IST)
        if result["status"] == "closed":
            close(files, account, position, tick(result["exit"]), result["exit_time"], result["reason"],
                  result["high"], result["low"])
            note(f"{position['symbol']}: {result['reason']} at {tick(result['exit'])} ({position['system']})")
        else:
            position.update(stop=tick(result["stop"]), high=result["high"], low=result["low"])


def enter_index_signals(files: Files, account: dict, seen: dict, now: datetime, trading: bool, note) -> None:
    """Trade the signals of each INDEX_SYSTEMS strategy: an index whose running 15-minute candle meets
    ha_index.signal, unless it has a position open in the strategy or was entered on this candle already."""
    live = {system: rule for system, rule in INDEX_SYSTEMS.items() if system not in RETIRED}
    if now.time() >= LAST_ENTRY or not live:
        return
    instruments = {i["symbol"]: i for i in fs.load_instruments()}
    current = ha_index.candle_start(int(now.timestamp()), fs.IST)
    found = index_bars(note)
    for (symbol, bars), (system, rule) in ((b, s) for b in found.items() for s in live.items()):
        rows = ha_index.candles([b for b in bars if b[0] <= now.timestamp()], fs.IST)
        if not rows or rows[-1][0] != current:  # no bar of the running candle yet
            continue
        side = ha_index.rule_signal(rows, rule)
        if not side:
            continue
        bias = "Bullish" if side == "BUY" else "Bearish"
        signal = {"symbol": symbol, "timeframe": ha_index.TIMEFRAME, "bias": bias, "system": system,
                  "strategy": f"15m {system} {bias}", "fno": True, "agrees": "—"}
        record_seen(seen, [signal], None, now)
        if ha_index.htf_side(bars, fs.IST, int(now.timestamp())) != side:
            mark(seen, [signal], f"Previous 4H HA candle not solid {'green' if side == 'BUY' else 'red'}")
            continue
        if ha_index.previous_2h_against(bars, fs.IST, int(now.timestamp()), side):
            mark(seen, [signal], f"Previous 2H HA candle {'red' if side == 'BUY' else 'green'}")
            continue
        if not ha_index.candle_agrees(rows[-1], side):
            mark(seen, [signal], f"Normal 15m candle not {'green' if side == 'BUY' else 'red'}")
            continue
        if not trading:
            mark(seen, [signal], "Not traded: switch off, page closed")
            continue
        if any(p.get("system") == system and p["symbol"] == symbol for p in account["positions"]):
            mark(seen, [signal], "Already open")
            continue
        if any(t.get("system") == system and t["symbol"] == symbol
               and ha_index.candle_start(t["entry_time"] // 1000, fs.IST) == current
               for t in trades_of(files, now.strftime("%Y-%m"))):
            mark(seen, [signal], "Already traded this candle")
            continue
        quote = fs.latest_prices({symbol}, instruments, None).get(symbol)
        entry = tick(quote["price"] if quote else rows[-1][4])
        long = side == "BUY"
        breakout = rule == "break"
        sl = (ha_index.break_stop(rows, entry, long) if breakout
              else ha_index.initial_stop(rows, entry, long, int(now.timestamp())))
        if sl is None:
            mark(seen, [signal], "No SL" if breakout else "No ATR")
            continue
        sl = tick(sl)
        first = seen.get(signal_key(signal))
        qty = LOTS * instruments[symbol]["lot"]
        account["seq"] += 1
        account["positions"].append({
            "id": f"{int(now.timestamp() * 1000)}-{account['seq']}", "symbol": symbol, "side": side, "lots": LOTS,
            "qty": qty, "entry": entry, "sl": sl, "tp": None, "tsl": {"on": False},
            "run": None, "style": ha_break.INDEX_STYLE if breakout else "atr", "system": system, "stop": sl, "time": int(now.timestamp() * 1000),
            "status": "open", "passed": False, "high": entry, "low": entry, "nifty": fs.nifty_change(),
            "source": f"Auto {system} 15m", "timeframes": [ha_index.TIMEFRAME], "strategies": [signal["strategy"]],
            "agrees": "—", "sma_rsi": None,
            "first_seen": first and {"time": first["time"], "price": first["price"]},
        })
        mark(seen, [signal], "Traded")
        note(f"{symbol}: {system} {side} {qty} at {entry}, SL {sl} "
             f"({'HA candle' if breakout else f'{ha_index.ATR_MULT} ATR'}, 15m)")


def settle_option(files: Files, account: dict, position: dict, bars: list, now: datetime, note) -> None:
    """Close an OPT ATM HA position at its (trailing) SL or the 3:15 PM square-off, else move its SL."""
    result = opt_atm.replay(position, bars, now)
    if result["status"] == "closed":
        close(files, account, position, tick(result["exit"]), result["exit_time"], result["reason"],
              result["high"], result["low"])
        note(f"{position['symbol']}: {result['reason']} at {tick(result['exit'])} ({opt_atm.SYSTEM})")
    else:
        position.update(stop=tick(result["stop"]), high=result["high"], low=result["low"], ltp=result["ltp"])


def settle_options(files: Files, account: dict, now: datetime, note) -> None:
    """The OPT ATM HA positions (opt_atm.py), on their option's 1-minute bars from Dhan."""
    for position in [p for p in account["positions"] if p.get("system") == opt_atm.SYSTEM]:
        try:
            bars = opt_atm.bars_of(position["security_id"])
        except opt_atm.dd.DhanError as exc:
            note(f"{position['symbol']}: no Dhan bars ({exc})")
            alert_problem(account, "dhan", f"Dhan: {exc} (open {position['symbol']} not checked)", now)
            continue
        settle_option(files, account, position, bars, now, note)


def enter_option_signals(files: Files, account: dict, now: datetime, trading: bool, note) -> None:
    """OPT ATM HA (opt_atm.py): buy the nearest expiry's ATM CE or PE of NIFTY and BANKNIFTY at the close
    of a 15-minute HA candle, completed since the last run, that was solid green right after a red one
    (the Option chain page's star); one position at a time in each leg, one entry per leg and candle."""
    checked = account.setdefault("opt_checked", {})
    until = int(now.timestamp())
    if not trading or now.time() >= opt_atm.LAST_ENTRY or opt_atm.SYSTEM in RETIRED:
        checked.update({f"{n} {leg}": until for n in opt_atm.UNDERLYINGS for leg in opt_atm.LEGS})
        return
    if not opt_atm.dd.secret("DHAN_CLIENT_ID"):
        note(f"{opt_atm.SYSTEM}: Dhan is not set up for the runs (DHAN_* secrets), no option trades")
        return
    month = trades_of(files, now.strftime("%Y-%m"))
    for name in opt_atm.UNDERLYINGS:
        try:
            legs = opt_atm.atm_legs(name)
        except (opt_atm.dd.DhanError, IndexError) as exc:
            note(f"{name}: no ATM option from Dhan ({exc})")
            alert_problem(account, "dhan", f"Dhan: no ATM options, so no {opt_atm.SYSTEM} entries ({exc})", now)
            continue
        for leg in opt_atm.LEGS:
            key = f"{name} {leg}"
            since = max(checked.get(key, 0), until - int(opt_atm.LOOKBACK.total_seconds()))
            checked[key] = until
            if any(p.get("system") == opt_atm.SYSTEM and p.get("leg") == key for p in account["positions"]):
                continue
            if not legs[leg]:
                note(f"{key}: no security id for the ATM {legs['strike']:g} in Dhan's chain")
                continue
            try:
                bars = opt_atm.bars_of(legs[leg])
            except opt_atm.dd.DhanError as exc:
                note(f"{key}: no Dhan bars ({exc})")
                continue
            found = opt_atm.find_entry(bars, since, until)
            if not found:
                continue
            if any(t.get("system") == opt_atm.SYSTEM and t["symbol"].startswith(f"{name} ")
                   and t["symbol"].endswith(f" {leg}") and opt_atm.candle_start(t["entry_time"] // 1000 - 60) == found["candle"]
                   for t in month):
                continue  # already traded on this candle
            entry, sl = tick(found["entry"]), tick(found["sl"])
            qty = LOTS * opt_atm.LOT_SIZE[name]
            symbol = opt_atm.symbol(name, legs["expiry"], legs["strike"], leg)
            account["seq"] += 1
            position = {
                "id": f"{found['time'] * 1000}-{account['seq']}", "symbol": symbol, "side": "BUY", "lots": LOTS,
                "qty": qty, "entry": entry, "sl": sl, "tp": None, "tsl": {"on": False}, "run": None,
                "style": "opt_ha", "system": opt_atm.SYSTEM, "stop": sl, "time": found["time"] * 1000,
                "status": "open", "passed": False, "high": entry, "low": entry, "ltp": entry, "nifty": fs.nifty_change(),
                "source": f"Auto {opt_atm.SYSTEM} 15m", "timeframes": ["15 MIN"], "strategies": [opt_atm.NAME],
                "agrees": "—", "sma_rsi": None, "first_seen": None, "leg": key, "underlying": name,
                "security_id": int(legs[leg]), "expiry": legs["expiry"], "strike": legs["strike"],
            }
            account["positions"].append(position)
            telegram_alerts.entry(position)
            when_entered = datetime.fromtimestamp(found["time"], fs.IST).strftime("%H:%M")
            note(f"{symbol}: {opt_atm.SYSTEM} BUY {qty} at {entry} ({when_entered}, solid green HA after red), "
                 f"SL {sl}")
            settle_option(files, account, position, bars, now, note)  # it may have hit its SL since


def log_alerts(files: Files, rows: list[dict]) -> None:
    """Add alerts sent on Telegram to alerts/YYYY-MM.json (by the alert's time), once each: the
    Alerts log page. Each row: time (ms), type ("HA CE/PE" or "RSI"), timeframe, symbol, side, ...."""
    def key(r):
        return r["type"], r["symbol"], r["side"], r["timeframe"], r["time"]
    for row in rows:
        month = files.get(f"alerts/{month_of(row['time'])}.json", [])
        if key(row) not in {key(r) for r in month}:
            month.append(row)
            files.changed.add(f"alerts/{month_of(row['time'])}.json")


ALERTS_MAX_BYTES = 1_000_000  # the Alerts log's size limit (all its files): past it, the oldest alerts go


def alerts_json(rows: list[dict]) -> str:
    """An alerts file as saved (compact, as Files.save writes alerts/)."""
    return json.dumps(rows)


def prune_alerts(files: Files) -> int:
    """Keep the Alerts log within ALERTS_MAX_BYTES: past it, delete the oldest alerts until it fits;
    a month file left empty is deleted. How many were dropped."""
    folder = files.folder / "alerts"
    if not folder.exists():
        return 0
    names = sorted(f"alerts/{p.name}" for p in folder.glob("*.json"))
    total = sum(len(alerts_json(files.get(n, [])).encode()) for n in names)
    if total <= ALERTS_MAX_BYTES:
        return 0
    # The oldest first: each alert's share of the size is its JSON plus the ", " between alerts.
    oldest = sorted(((r["time"], n, k) for n in names for k, r in enumerate(files.get(n, []))))
    drop: dict[str, set[int]] = {}
    for _, n, k in oldest:
        if total <= ALERTS_MAX_BYTES:
            break
        total -= len(json.dumps(files.data[n][k]).encode()) + 2
        drop.setdefault(n, set()).add(k)
    for n, ks in drop.items():
        keep = [r for k, r in enumerate(files.data[n]) if k not in ks]
        if keep:
            files.data[n] = keep
            files.changed.add(n)
        else:
            files.changed.discard(n)
            files.data.pop(n, None)
            (files.folder / n).unlink(missing_ok=True)
    return sum(len(ks) for ks in drop.values())


def ha_rows(hits: list[dict], now: datetime, replay: bool = False) -> list[dict]:
    """opt_ha_alert hits as Alerts log rows."""
    return [{"time": h["end"] * 1000, "sent": int(now.timestamp() * 1000), "type": "HA CE/PE",
             "timeframe": h["timeframe"], "symbol": h["name"], "side": h["side"], "option": h["option"],
             "strike": h["strike"], "rule": h["candle"].get("kind", "HA solid green"), "price": h["candle"]["close"],
             "ltp": h["ltp"], "spot": h["spot"], "lot": h["lot"], "ratio": h["ratio"],
             **({"replay": True} if replay else {})} for h in hits]


def alert_option_ha(files: Files, account: dict, now: datetime, note) -> None:
    """Once per completed 15m / 30m / 1H / 2H / 3H / 4H candle end: the ATM calls and puts of NIFTY,
    BANKNIFTY and the high-ATR F&O stocks whose Heikin Ashi candle on that timeframe turned green
    (opt_ha_alert.turned), on Telegram (opt_ha_alert.py). Alerts only. A candle end a run did not
    finish (Dhan's token renewed elsewhere) is finished by the next one, with the new end, within the hour."""
    latest = opt_ha_alert.last_end(now)
    done = account.get("opt_ha_alert") or {}
    if latest is None or done.get("end") == latest[0] and done.get("complete") or not opt_ha_alert.dd.secret("DHAN_CLIENT_ID"):
        return
    end, frames = latest
    ends, carried = [], None
    if done.get("end") != end:
        # Up to an hour after the candle: later the alert is stale.
        if done.get("end") and not done.get("complete") and done.get("frames") and now.timestamp() <= done["end"] + 3600:
            carried = done
            ends.append((done["end"], done["frames"]))
        done = {"end": end, "frames": frames, "checked": [], "complete": False}
    if now.timestamp() > end + 3600:
        account["opt_ha_alert"] = {**done, "complete": True}
        return
    ends.append((end, frames))
    lots = {i["symbol"]: i["lot"] for i in fs.load_instruments()} | opt_atm.LOT_SIZE
    try:
        found = opt_ha_alert.scan(now, fs.atr_strikes_above(), lots, note, set(done["checked"]), ends=ends)
    except (opt_ha_alert.dd.DhanError, OSError, sqlite3.Error) as exc:
        note(f"HA CE/PE alert: not checked ({exc}); trying again next run")
        return
    if found is None:
        return
    _, _, hits, checked, total = found
    if carried:  # the earlier end: only the names it had not checked
        hits = [h for h in hits if h["end"] != carried["end"] or h["name"] not in carried["checked"]]
    done["checked"] += checked
    done["complete"] = len(done["checked"]) >= total
    account["opt_ha_alert"] = done
    when = f"{datetime.fromtimestamp(end, fs.IST):%H:%M}"
    extra = f" + the {datetime.fromtimestamp(carried['end'], fs.IST):%H:%M} ones left" if carried else ""
    note(f"HA CE/PE alert ({'/'.join(frames)} candles ending {when}{extra}, {len(done['checked'])} of {total} checked): "
         + (", ".join(f"{h['option']} {h['timeframe']}" for h in hits) if hits else "none"))
    if hits:
        hits.sort(key=lambda h: (h["end"], list(opt_ha_alert.TIMEFRAMES).index(h["timeframe"])))
        telegram_alerts.option_ha(f"candles ending {when}", hits)
        log_alerts(files, ha_rows(hits, now))


def vp_bars(positions: list[dict], now: datetime) -> dict[str, list]:
    """position id -> its 5-minute bars since the entry: Chartink's when they reach back to it,
    else Yahoo Finance's (as futures_simulator.check_positions)."""
    instruments = {i["symbol"]: i for i in fs.load_instruments()}
    try:
        chartink = fs.fetch_price_bars(fs.DEFAULT_DASHBOARD)
    except fs.ChartinkError:
        chartink = {}
    bars = {p["id"]: chartink[p["symbol"]] for p in positions
            if chartink.get(p["symbol"]) and chartink[p["symbol"]][0][0] <= p["time"] / 1000}
    wanted = {p["id"]: (instruments[p["symbol"]]["yahoo"], "5d")
              for p in positions if p["id"] not in bars and p["symbol"] in instruments}
    yahoo = fs.fetch_bars(tuple(sorted(set(wanted.values())))) if wanted else {}
    bars.update({id_: yahoo.get(request) for id_, request in wanted.items()})
    return bars


def settle_vp(files: Files, account: dict, now: datetime, note) -> None:
    """Close the HA VP positions that hit their SL, VP target, an HA doji, an EMA 8 cross or the 3:15 PM
    square-off (ha_vp.replay); move the others' SL."""
    positions = [p for p in account["positions"] if p.get("system") in ha_vp.SYSTEMS]
    if not positions:
        return
    try:
        candles = fs.fetch_vp_candles()
    except fs.ChartinkError as exc:
        note(f"No 2H candles for the HA VP exits: {exc}")
        return
    bars = vp_bars(positions, now)
    for position in positions:
        found = bars.get(position["id"])
        if not isinstance(found, list) or not found:
            note(f"{position['symbol']}: no 5-minute bars for HA VP ({found})")
            continue
        rows = candles.get(position["symbol"], [])
        result = ha_vp.replay(position, found, rows, lambda start: candle_end(ha_vp.TIMEFRAME, start), now, fs.IST)
        if result["status"] == "closed":
            close(files, account, position, tick(result["exit"]), result["exit_time"], result["reason"],
                  result["high"], result["low"])
            note(f"{position['symbol']}: {result['reason']} at {tick(result['exit'])} ({position['system']})")
        else:
            position.update(stop=tick(result["stop"]), high=result["high"], low=result["low"])


def enter_vp_signals(files: Files, account: dict, seen: dict, now: datetime, trading: bool, note) -> None:
    """Trade the HA VP signals, a stock or index whose running 2H HA candle meets ha_vp.signal, in each of
    ha_vp.SYSTEMS. One position per stock in each: an opposite signal closes it and enters the
    other way; no second entry on a candle."""
    if now.time() >= LAST_ENTRY or set(ha_vp.SYSTEMS) <= RETIRED:
        return
    try:
        candles = fs.fetch_vp_candles()
    except fs.ChartinkError as exc:
        note(f"No 2H candles for the HA VP entries: {exc}")
        return
    instruments = {i["symbol"]: i for i in fs.entry_instruments()}  # high-ATR stocks only (fs.atr_strikes_above)
    try:
        found = fs.fetch_volume_profile()
        levels = {row["Symbol"]: row for row in found.get("stocks", []) + found.get("indices", [])}
    except (OSError, ValueError) as exc:
        levels = {}
        note(f"No volume profile levels for the HA VP targets: {exc}")
    current = candle_key(ha_vp.TIMEFRAME, int(now.timestamp()))
    month = trades_of(files, now.strftime("%Y-%m"))
    nifty = None
    for symbol, rows in sorted(candles.items()):
        if symbol not in instruments or not rows:
            continue
        if candle_key(ha_vp.TIMEFRAME, rows[-1][0]) != current:  # no running candle yet
            continue
        side = ha_vp.signal(rows)
        if not side:
            continue
        bias = "Bullish" if side == "BUY" else "Bearish"
        values = ha_vp.atr(rows[:-1])
        entry = tick(rows[-1][ha_vp.CLOSE])
        long = side == "BUY"
        for system, style in ha_vp.SYSTEMS.items():
            if system in RETIRED:
                continue
            signal = {"symbol": symbol, "timeframe": ha_vp.TIMEFRAME, "bias": bias, "system": system,
                      "strategy": f"2H {system} {bias}", "fno": True, "agrees": "—"}
            record_seen(seen, [signal], None, now)
            if not trading:
                mark(seen, [signal], "Not traded: switch off, page closed")
                continue
            held = next((p for p in account["positions"] if p.get("system") == system and p["symbol"] == symbol), None)
            if held and held["side"] == side:
                mark(seen, [signal], "Already open")
                continue
            if any(t.get("system") == system and t["symbol"] == symbol
                   and candle_key(ha_vp.TIMEFRAME, t["entry_time"] // 1000) == current for t in month) or (
                    held and candle_key(ha_vp.TIMEFRAME, held["time"] // 1000) == current):
                mark(seen, [signal], "Already traded this candle")
                continue
            if not values or values[-1] is None:
                mark(seen, [signal], "No ATR")
                continue
            if held:
                close(files, account, held, entry, int(now.timestamp() * 1000), "Reverse signal")
                note(f"{symbol}: {system} {held['side']} closed at {entry} (reverse signal)")
            sl = tick(entry - ha_vp.ATR_MULT * values[-1] if long else entry + ha_vp.ATR_MULT * values[-1])
            goal = ha_vp.target(levels.get(symbol), entry, long, ha_vp.MIN_TARGET_ATR * values[-1])
            goal = tick(goal) if goal is not None else None
            first = seen.get(signal_key(signal))
            qty = LOTS * instruments[symbol]["lot"]
            if nifty is None:
                nifty = fs.nifty_change()
            account["seq"] += 1
            account["positions"].append({
                "id": f"{int(now.timestamp() * 1000)}-{account['seq']}", "symbol": symbol, "side": side, "lots": LOTS,
                "qty": qty, "entry": entry, "sl": sl, "tp": goal, "tsl": {"on": False}, "run": None,
                "style": style, "system": system, "stop": sl, "atr": round(values[-1], 2),
                "time": int(now.timestamp() * 1000), "status": "open", "passed": False, "high": entry, "low": entry,
                "nifty": nifty, "source": f"Auto {system} 2H", "timeframes": [ha_vp.TIMEFRAME],
                "strategies": [signal["strategy"]], "agrees": "—", "sma_rsi": None,
                "first_seen": first and {"time": first["time"], "price": first["price"]},
            })
            mark(seen, [signal], "Traded")
            note(f"{symbol}: {system} {side} {qty} at {entry}, SL {sl} (1 ATR {values[-1]:.2f}), target {goal}")


def pure_candles(timeframe: str) -> dict[str, list[tuple]]:
    """symbol -> the HA candles of an HA PURE timeframe (the 2H ones are HA VP's)."""
    return fs.fetch_vp_candles() if timeframe == "2 HOUR" else fs.fetch_ha_tf_candles(timeframe)


def held_bars(positions: list[dict], now: datetime) -> dict[str, list]:
    """position id -> its 5-minute bars since the entry: Chartink's when they reach back to it,
    else Yahoo Finance's, as many days as needed (as futures_simulator.check_positions)."""
    instruments = {i["symbol"]: i for i in fs.load_instruments()}
    try:
        chartink = fs.fetch_price_bars(fs.DEFAULT_DASHBOARD)
    except fs.ChartinkError:
        chartink = {}
    bars = {p["id"]: chartink[p["symbol"]] for p in positions
            if chartink.get(p["symbol"]) and chartink[p["symbol"]][0][0] <= p["time"] / 1000}
    wanted = {p["id"]: (instruments[p["symbol"]]["yahoo"],
                        fs.bars_range(datetime.fromtimestamp(p["time"] / 1000, fs.IST), now))
              for p in positions if p["id"] not in bars and p["symbol"] in instruments}
    yahoo = fs.fetch_bars(tuple(sorted(set(wanted.values())))) if wanted else {}
    bars.update({id_: yahoo.get(request) for id_, request in wanted.items()})
    return bars


def settle_pure(files: Files, account: dict, now: datetime, note, module=ha_pure) -> None:
    """Close the positions of a held HA strategy (HELD_MODULES: HA PURE, by default, or HA BRK) that
    hit their trailing SL, an exit (HA PURE: a doji close) or the expiry square-off (module.replay);
    move the others' SL."""
    positions = [p for p in account["positions"] if p.get("system") in module.SYSTEMS]
    if not positions:
        return
    candles = {}
    for timeframe in sorted({module.SYSTEMS[p["system"]] for p in positions}):
        try:
            candles[timeframe] = pure_candles(timeframe)
        except fs.ChartinkError as exc:
            note(f"No {SHORT[timeframe]} candles for the {module.LABEL} exits: {exc}")
    bars = held_bars(positions, now)
    for position in positions:
        timeframe = module.SYSTEMS[position["system"]]
        found = bars.get(position["id"])
        if timeframe not in candles:
            continue
        if not isinstance(found, list) or not found:
            note(f"{position['symbol']}: no 5-minute bars for {position['system']} ({found})")
            continue
        entered = datetime.fromtimestamp(position["time"] / 1000, fs.IST)
        cutoff = datetime.combine(fs.expiry_after(entered.date()), module.SQUARE_OFF, fs.IST).timestamp()
        result = module.replay(position, found, candles[timeframe].get(position["symbol"], []),
                               lambda start: candle_end(timeframe, start), cutoff, now, fs.IST)
        if result["status"] == "closed":
            close(files, account, position, tick(result["exit"]), result["exit_time"], result["reason"],
                  result["high"], result["low"])
            note(f"{position['symbol']}: {result['reason']} at {tick(result['exit'])} ({position['system']})")
        else:
            position.update(stop=tick(result["stop"]), high=result["high"], low=result["low"])


def entry_bars(symbols: set[str], now: datetime) -> dict[str, list]:
    """symbol -> its 5-minute bars of the last sessions, for the 30-minute SL of a new HA PURE
    position: Chartink's, else Yahoo Finance's."""
    instruments = {i["symbol"]: i for i in fs.load_instruments()}
    try:
        bars = {s: b for s, b in fs.fetch_price_bars(fs.DEFAULT_DASHBOARD).items() if s in symbols and b}
    except fs.ChartinkError:
        bars = {}
    wanted = {s: (instruments[s]["yahoo"], "5d") for s in symbols if s not in bars and s in instruments}
    yahoo = fs.fetch_bars(tuple(sorted(set(wanted.values())))) if wanted else {}
    bars.update({s: yahoo[r] for s, r in wanted.items() if isinstance(yahoo.get(r), list)})
    return {s: [b for b in found if b[0] + 300 <= now.timestamp()] for s, found in bars.items()}


def enter_pure_signals(files: Files, account: dict, seen: dict, now: datetime, trading: bool, note,
                       module=ha_pure) -> None:
    """Trade the signals of a held HA strategy (HELD_MODULES: HA PURE, by default, or HA BRK) on each
    timeframe (module.SYSTEMS): a stock or index whose running HA candle meets module.signal. One position per stock in each: an opposite signal closes it and
    enters the other way; no second entry on a candle."""
    if now.time() >= LAST_ENTRY:
        return
    instruments = {i["symbol"]: i for i in fs.entry_instruments()}  # high-ATR stocks only (fs.atr_strikes_above)
    month = trades_of(files, now.strftime("%Y-%m"))
    found = []
    for system, timeframe in module.SYSTEMS.items():
        if system in RETIRED:
            continue
        try:
            candles = pure_candles(timeframe)
        except fs.ChartinkError as exc:
            note(f"No {SHORT[timeframe]} candles for the {system} entries: {exc}")
            continue
        current = candle_key(timeframe, int(now.timestamp()))
        for symbol, rows in sorted(candles.items()):
            if symbol not in instruments or len(rows) < 2:
                continue
            if candle_key(timeframe, rows[-1][0]) != current:  # no running candle yet
                continue
            side = module.signal(rows)
            if side:
                found.append((system, timeframe, symbol, rows, side, current))
    # HA PURE's SL is from the 30-minute candles of the 5-minute bars; HA BRK's from the HA candle.
    bars = entry_bars({f[2] for f in found}, now) if found and trading and module is ha_pure else {}
    nifty = None
    for system, timeframe, symbol, rows, side, current in found:
        bias = "Bullish" if side == "BUY" else "Bearish"
        signal = {"symbol": symbol, "timeframe": timeframe, "bias": bias, "system": system,
                  "strategy": f"{SHORT[timeframe]} {module.LABEL} {bias}", "fno": True, "agrees": "—"}
        record_seen(seen, [signal], None, now)
        if not trading:
            mark(seen, [signal], "Not traded: switch off, page closed")
            continue
        held = next((p for p in account["positions"] if p.get("system") == system and p["symbol"] == symbol), None)
        if held and held["side"] == side:
            mark(seen, [signal], "Already open")
            continue
        if any(t.get("system") == system and t["symbol"] == symbol
               and candle_key(timeframe, t["entry_time"] // 1000) == current for t in month) or (
                held and candle_key(timeframe, held["time"] // 1000) == current):
            mark(seen, [signal], "Already traded this candle")
            continue
        entry = tick(rows[-1][ha_vp.CLOSE])
        long = side == "BUY"
        sl = module.initial_stop(bars.get(symbol) or [], rows, entry, long, now, fs.IST)
        if sl is None:
            mark(seen, [signal], "No SL")
            continue
        sl = tick(sl)
        if held:
            close(files, account, held, entry, int(now.timestamp() * 1000), "Reverse signal")
            note(f"{symbol}: {system} {held['side']} closed at {entry} (reverse signal)")
        first = seen.get(signal_key(signal))
        qty = LOTS * instruments[symbol]["lot"]
        if nifty is None:
            nifty = fs.nifty_change()
        account["seq"] += 1
        account["positions"].append({
            "id": f"{int(now.timestamp() * 1000)}-{account['seq']}", "symbol": symbol, "side": side, "lots": LOTS,
            "qty": qty, "entry": entry, "sl": sl, "tp": None, "tsl": {"on": False}, "run": None,
            "style": module.STYLE, "system": system, "stop": sl,
            "time": int(now.timestamp() * 1000), "status": "open", "passed": False, "high": entry, "low": entry,
            "nifty": nifty, "source": f"Auto {system}", "timeframes": [timeframe],
            "strategies": [signal["strategy"]], "agrees": "—", "sma_rsi": None,
            "first_seen": first and {"time": first["time"], "price": first["price"]},
        })
        mark(seen, [signal], "Traded")
        note(f"{symbol}: {system} {side} {qty} at {entry}, SL {sl} ({module.SL_NOTE})")


def enter_weekly_signals(files: Files, account: dict, seen: dict, now: datetime, trading: bool, note) -> None:
    """HA WK BRK (ha_weekly.py): arm each stock or index whose 4H HA close this week crossed last week's
    red (green) Weekly HA candle's HA high (low), kept in account["wk_armed"]; then enter an armed one
    on a 5-minute bar that pulled back to the 30-minute SMA 8, once per stock and week."""
    if now.time() >= LAST_ENTRY or ha_weekly.SYSTEM in RETIRED:
        return
    system = ha_weekly.SYSTEM
    week = ha_weekly.monday_of(now.date()).isoformat()
    armed = {s: a for s, a in account.get("wk_armed", {}).items() if a["week"] == week}
    account["wk_armed"] = armed
    try:
        weekly = fs.fetch_ha_tf_candles("WEEKLY")
        four = pure_candles("4 HOUR")
    except fs.ChartinkError as exc:
        note(f"No Weekly / 4H candles for the {system} entries: {exc}")
        return
    instruments = {i["symbol"]: i for i in fs.entry_instruments()}  # high-ATR stocks only (fs.atr_strikes_above)
    current = candle_key("4 HOUR", int(now.timestamp()))
    for symbol, rows in sorted(four.items()):
        if symbol not in instruments or symbol in armed:
            continue
        found = ha_weekly.setup(weekly.get(symbol, []), now, fs.IST)
        if not found:
            continue
        side, level = found
        start = ha_weekly.crossed(rows, side, level, now, fs.IST)
        if start is None:
            continue
        # The running candle arms it now; a finished one (a run missed) when it ended.
        at = int(now.timestamp()) if candle_key("4 HOUR", start) == current else int(candle_end("4 HOUR", start))
        armed[symbol] = {"week": week, "side": side, "level": round(level, 2), "time": at, "entered": False}
        note(f"{symbol}: {system} armed {side} (4H HA close past last week's HA {'high' if side == 'BUY' else 'low'} {level:.2f})")
    waiting = {s: a for s, a in armed.items() if not a["entered"] and s in instruments}
    if not waiting:
        return
    bars = entry_bars(set(waiting), now)
    nifty = None
    for symbol, setup in sorted(waiting.items()):
        side = setup["side"]
        bias = "Bullish" if side == "BUY" else "Bearish"
        signal = {"symbol": symbol, "timeframe": "4 HOUR", "bias": bias, "system": system,
                  "strategy": f"4H {system} {bias}", "fno": True, "agrees": "—"}
        record_seen(seen, [signal], None, now)
        hit = ha_weekly.pullback(bars.get(symbol) or [], side, setup["time"], now, fs.IST)
        if not hit:
            mark(seen, [signal], "Armed: waiting for the 30m SMA 8 pullback")
            continue
        if not trading:
            mark(seen, [signal], "Not traded: switch off, page closed")
            continue
        held = next((p for p in account["positions"] if p.get("system") == system and p["symbol"] == symbol), None)
        if held and held["side"] == side:
            setup["entered"] = True
            mark(seen, [signal], "Already open")
            continue
        bar, fill, sma = hit
        entry, long = tick(fill), side == "BUY"
        at = bar + ha_weekly.BAR  # from the end of the pullback bar: the SL is checked from the next one
        sl = ha_weekly.initial_stop(bars.get(symbol) or [], four.get(symbol, []), entry, long, bar, fs.IST)
        if sl is None:
            mark(seen, [signal], "No SL")
            continue
        sl = tick(sl)
        if held:
            close(files, account, held, entry, at * 1000, "Reverse signal")
            note(f"{symbol}: {system} {held['side']} closed at {entry} (reverse signal)")
        first = seen.get(signal_key(signal))
        qty = LOTS * instruments[symbol]["lot"]
        if nifty is None:
            nifty = fs.nifty_change()
        account["seq"] += 1
        account["positions"].append({
            "id": f"{at * 1000}-{account['seq']}", "symbol": symbol, "side": side, "lots": LOTS,
            "qty": qty, "entry": entry, "sl": sl, "tp": None, "tsl": {"on": False}, "run": None,
            "style": ha_weekly.STYLE, "system": system, "stop": sl,
            "time": at * 1000, "status": "open", "passed": False, "high": entry, "low": entry,
            "nifty": nifty, "source": f"Auto {system}", "timeframes": ["WEEKLY", "4 HOUR"],
            "strategies": [signal["strategy"]], "agrees": "—", "sma_rsi": None, "level": setup["level"],
            "first_seen": first and {"time": first["time"], "price": first["price"]},
        })
        setup["entered"] = True
        mark(seen, [signal], "Traded")
        note(f"{symbol}: {system} {side} {qty} at {entry} (30m SMA 8 {sma:.2f}), SL {sl} ({ha_weekly.SL_NOTE})")


def test_chartink(note) -> None:
    """Fetch the dashboard and the 5-minute bars from here (GitHub), and note what came back."""
    dashboard, error = fs.load_dashboard()
    if not dashboard:
        note(f"Chartink test FAILED: {error}")
        return
    counts = {}
    for widget in dashboard["widgets"]:
        if widget["table"] is not None and widget["timeframe"]:
            counts[widget["timeframe"]] = counts.get(widget["timeframe"], 0) + len(widget["table"])
    exits = {name: next((len(w["table"]) for w in dashboard["widgets"] if w["table"] is not None
                         and re.search(pattern, w["name"], re.IGNORECASE)), None)
             for name, pattern in (("RSI below 60", r"rsi crossed below"), ("RSI above 40", r"rsi crossed above"))}
    failed = [w["name"] for w in dashboard["widgets"] if w["error"]]
    ha_dashboard, ha_error = load_ha_signals()
    if ha_dashboard:
        failed += [w["name"] for w in ha_dashboard["widgets"] if w["error"]]
        ha_signals = sum(len(w["table"]) for w in ha_dashboard["widgets"]
                         if w["table"] is not None and w["timeframe"] in TIMEFRAMES)
        try:
            found = ", ".join(f"{SHORT[tf]} {len(c)}" for tf, c in fs.fetch_ha_candles().items())
            ha = f"HA signals {ha_signals}; HA candles {found}"
        except fs.ChartinkError as exc:
            ha = f"HA signals {ha_signals}; HA candles FAILED ({exc})"
    else:
        ha = f"HA dashboard FAILED ({ha_error})"
    try:
        bars = f"5-minute bars for {len(fs.fetch_price_bars(fs.DEFAULT_DASHBOARD))} stocks"
    except fs.ChartinkError as exc:
        bars = f"5-minute bars FAILED ({exc})"
    note(f"Chartink test OK: {len(dashboard['widgets'])} widgets; signals "
         + ", ".join(f"{tf.replace(' HOUR', 'H').title()} {n}" for tf, n in counts.items())
         + "; exit lists " + ", ".join(f"{name} {n}" for name, n in exits.items()) + f"; {bars}; {ha}"
         + (f"; widgets with errors: {', '.join(failed)}" if failed else ""))


def market_open(now: datetime) -> bool:
    """Whether NSE is trading now: in session hours, with NIFTY bars from today (not a holiday)."""
    if now.weekday() >= 5 or not fs.SESSION_OPEN <= now.time() < SESSION_CLOSE:
        return False
    bars = fs.fetch_bars((("^NSEI", "1d"),)).get(("^NSEI", "1d"))
    return isinstance(bars, list) and bool(bars) and datetime.fromtimestamp(bars[-1][0], fs.IST).date() == now.date()


def ha_sma_rsi(dashboard: dict) -> dict[tuple[str, str], float]:
    """(widget, symbol) -> the SMA RSI of the signal's timeframe, from the HA signal widgets."""
    values = {}
    for widget in dashboard["widgets"]:
        column = HA_SMA_RSI.get(widget["timeframe"])
        table = widget["table"]
        if column is None or table is None or column not in table:
            continue
        for symbol, value in zip(table["Symbol"], table[column]):
            if value is not None and 0 <= value <= 100:
                values[(widget["name"], symbol)] = round(float(value), 2)
    return values


# RSI strategies whose new trades are sent to Telegram with the ATM option (rsi_alert), and their timeframe.
RSI_ALERTS = {"RSI": "2H", "RSI 4H": "4H", "RSI Daily": "Daily"}
INDEX_STEPS = {"NIFTY": 50, "BANKNIFTY": 100}  # strike intervals of the indices (not in strike_steps.json)


def atm_of(symbol: str, price: float) -> float | None:
    """The ATM strike: the price rounded to the symbol's regular strike interval (strike_steps.json)."""
    step = INDEX_STEPS.get(symbol)
    if step is None:
        try:
            step = json.loads(Path(__file__).with_name("strike_steps.json").read_text(encoding="utf-8"))["steps"].get(symbol)
        except (OSError, ValueError, KeyError):
            step = None
    return round(round(price / step) * step, 2) if step else None


def send_rsi_today(files: Files, account: dict, now: datetime, note) -> None:
    """The day's RSI 2H, 4H and Daily entries (open or closed since) on Telegram, as rsi_signals, by strategy."""
    lots = {i["symbol"]: i["lot"] for i in fs.load_instruments()}
    entered = [(p["time"], p) for p in account["positions"]] + [
        (t["entry_time"], t) for t in trades_of(files, now.strftime("%Y-%m"))]
    for system, timeframe in RSI_ALERTS.items():
        found = {}
        for ms, p in sorted(entered, key=lambda e: e[0]):
            if p.get("system", "RSI") != system or datetime.fromtimestamp(ms / 1000, fs.IST).date() != now.date():
                continue
            found.setdefault((p["symbol"], p["side"]), {
                "symbol": p["symbol"], "side": p["side"], "price": p["entry"], "strike": atm_of(p["symbol"], p["entry"]),
                "leg": "CE" if p["side"] == "BUY" else "PE", "lot": lots.get(p["symbol"], p["qty"]),
                "time": datetime.fromtimestamp(ms / 1000, fs.IST).strftime("%H:%M")})
        if found:
            telegram_alerts.rsi_signals(timeframe, f"today, {now:%d %b}", list(found.values()))
        note(f"Telegram: {len(found)} RSI {timeframe} signals of today sent")


def send_ha_today(files: Files, now: datetime, note) -> None:
    """Replay the day's 15m to 4H candles of the ATM CE / PE (opt_ha_alert, the strike as now)
    and send on Telegram every HA turn-green alert they gave, oldest first."""
    lots = {i["symbol"]: i["lot"] for i in fs.load_instruments()} | opt_atm.LOT_SIZE
    upto = min(now, datetime.combine(now.date(), SESSION_CLOSE, fs.IST))
    ends = opt_ha_alert.day_ends(now.date(), upto)
    found = opt_ha_alert.scan(upto, fs.atr_strikes_above(), lots, note, ends=ends) if ends else None
    if found is None:
        note("HA CE/PE today: no completed candles today")
        return
    _, _, hits, checked, total = found
    hits.sort(key=lambda h: (h["end"], list(opt_ha_alert.TIMEFRAMES).index(h["timeframe"])))
    note(f"HA CE/PE today: {len(hits)} alerts on {len(checked)} of {total} underlyings")
    if hits:
        telegram_alerts.option_ha(f"today, {now:%d %b} (replay, ATM strike as at the close)", hits)
        log_alerts(files, ha_rows(hits, now, replay=True))
    else:
        telegram_alerts.send(f"🔔 HA turn green · ATM CE / PE · today, {now:%d %b}: no alerts "
                             f"({len(checked)} of {total} checked)")


def enter_signals(files: Files, account: dict, signals: list[dict], dashboard: dict, seen: dict, now: datetime,
                  trading: bool, note, system: str) -> None:
    """Trade one strategy's signals, and note in `seen` what was done with each."""
    instruments = {i["symbol"]: i for i in fs.entry_instruments()}  # high-ATR stocks only (fs.atr_strikes_above)
    mark(seen, [s for s in signals if not s["fno"]], "Not F&O")
    low_atr = [s for s in signals if s["fno"] and s["symbol"] not in instruments]
    mark(seen, low_atr, f"ATR not above {fs.atr_strikes_above():g} strikes")
    signals = [s for s in signals if s not in low_atr]
    traded = TRADED_TIMEFRAMES[system]
    mark(seen, [s for s in signals if s["fno"] and s["timeframe"] not in traded], "Timeframe not traded")
    by_symbol: dict[str, list[dict]] = {}
    for signal in signals:
        if signal["timeframe"] in traded and signal["fno"]:
            by_symbol.setdefault(signal["symbol"], []).append(signal)
    if system in RETIRED:
        mark(seen, [s for found in by_symbol.values() for s in found], "Strategy retired")
        return
    if not trading:
        mark(seen, [s for found in by_symbol.values() for s in found], "Not traded: switch off, page closed")
        return
    if now.time() >= LAST_ENTRY:
        mark(seen, [s for found in by_symbol.values() for s in found], "After 3:00 PM")
        return

    # Once per session; RSI Weekly once per week, as its signal stays on all week.
    since = now.date() - timedelta(days=now.weekday() if traded == ("WEEKLY",) else 0)
    months = {now.strftime("%Y-%m"), datetime.combine(since, time(), fs.IST).strftime("%Y-%m")}
    entered_today = [t for month in months for t in trades_of(files, month)
                     if datetime.fromtimestamp(t["entry_time"] / 1000, fs.IST).date() >= since]
    def taken(symbol, side, style):
        """Open in this style, or entered this session (week) in it, in this strategy."""
        return any(p["symbol"] == symbol and p.get("style", "eod") == style and p.get("system", "RSI") == system
                   for p in account["positions"]) or any(
            t["symbol"] == symbol and t["side"] == side and t.get("style", "eod") == style
            and t.get("system", "RSI") == system for t in entered_today)

    wanted = {}
    for symbol, found in by_symbol.items():
        biases = {s["bias"] for s in found}
        if len(biases) > 1:
            mark(seen, found, "Mixed: bullish and bearish")
            continue
        side = "BUY" if biases == {"Bullish"} else "SELL"
        styles = [style for style in fs.SYSTEMS[system] if not taken(symbol, side, style)]
        if styles:
            wanted[symbol] = (side, found, styles)
        else:
            mark(seen, found, "Already traded or open")
    if not wanted:
        return

    quotes = fs.latest_prices(set(wanted), instruments, dashboard)
    nifty = fs.nifty_change()
    # For the SL (entry_sl): the HA candles, or the 5-minute bars to make the RSI ones from.
    try:
        sources = fs.fetch_ha_candles() if system.startswith("HA") else fs.fetch_price_bars(fs.DEFAULT_DASHBOARD)
    except fs.ChartinkError as exc:
        sources = {}
        note(f"No candles for the {system} SL, using {fs.AUTO_SL_PCT}%: {exc}")
    alerts = []
    for symbol, (side, found, styles) in sorted(wanted.items()):
        exit_tf = None
        if system == "HA MTF":
            doji = [s for s in found if previous_doji(sources, s["timeframe"], symbol, now)]
            mark(seen, doji, "Doji: previous HA candle")
            found = [s for s in found if s not in doji]
            if not found:
                continue
            exit_tf = max((s["timeframe"] for s in found), key=TIMEFRAMES.index)
        quote = quotes.get(symbol)
        if not quote:
            mark(seen, found, "No price")
            note(f"{symbol}: no price, skipped")
            continue
        entry, qty = tick(quote["price"]), LOTS * instruments[symbol]["lot"]
        long = side == "BUY"
        tp = tick(entry * (1 + fs.AUTO_TP_PCT / 100 * (1 if long else -1)))
        timeframes = sorted({s["timeframe"] for s in found}, key=TIMEFRAMES.index)
        sl = (tick(entry * (1 - MTF_SL_PCT / 100 * (1 if long else -1))) if system == "HA MTF"
              else entry_sl(entry, long, system, "2 HOUR" if system.startswith("RSI") else timeframes[0], symbol,
                            sources, now))
        first = min((seen[signal_key(s)] for s in found if signal_key(s) in seen), key=lambda f: f["time"], default=None)
        # The SMA RSI of the shortest timeframe, as for the exit.
        sma_rsi = next((s["sma_rsi"] for s in sorted(found, key=lambda s: TIMEFRAMES.index(s["timeframe"]))
                        if s.get("sma_rsi") is not None), None)
        for style in styles:
            account["seq"] += 1
            account["positions"].append({
                "id": f"{int(now.timestamp() * 1000)}-{account['seq']}", "symbol": symbol, "side": side, "lots": LOTS,
                "qty": qty, "entry": entry, "sl": sl, "tp": None if style == "rsi" or style in fs.HA_EXITS or style in fs.SMA_EXITS else tp, "tsl": {"on": False},
                "run": auto_run(entry) if style == "trail" else None, "style": style, "system": system,
                "stop": sl, "time": int(now.timestamp() * 1000), "status": "open", "passed": False,
                "high": entry, "low": entry, "nifty": nifty,
                "source": ("Auto " if system.startswith("RSI") else f"Auto {system} ") + "+".join(SHORT[tf] for tf in timeframes),
                "timeframes": timeframes, **({"exit_tf": exit_tf} if exit_tf else {}),
                "strategies": sorted({s["strategy"] for s in found}), "agrees": found[0]["agrees"], "sma_rsi": sma_rsi,
                "first_seen": first and {"time": first["time"], "price": first["price"]},
            })
            if system.startswith("RSI"):
                lock = protection(account["positions"][-1], fs.daily_atrs(index_pairs({symbol})), int(now.timestamp() * 1000))
                if lock:
                    account["positions"][-1]["protect"] = lock
        mark(seen, found, "Traded")
        if system in RSI_ALERTS:
            alerts.append({"symbol": symbol, "side": side, "price": entry, "strike": atm_of(symbol, entry),
                           "leg": "CE" if long else "PE", "lot": instruments[symbol]["lot"]})
        exits = f"; exit on {SHORT[exit_tf]}" if exit_tf else ""
        note(f"{symbol}: {system} {side} {qty} at {entry}, SL {sl} ({', '.join(timeframes)}{exits}; {', '.join(styles)})")
    if alerts:
        telegram_alerts.rsi_signals(RSI_ALERTS[system], now.strftime("%d %b %H:%M"), alerts)
        log_alerts(files, [{"time": int(now.timestamp() * 1000), "sent": int(now.timestamp() * 1000), "type": "RSI",
                            "timeframe": RSI_ALERTS[system], "symbol": a["symbol"], "side": a["side"],
                            "option": a["strike"] and f"{a['symbol']} {a['strike']:g} {a['leg']}", "strike": a["strike"],
                            "rule": f"RSI {RSI_ALERTS[system]} entry", "price": a["price"], "lot": a["lot"]}
                           for a in alerts])


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("account", type=Path)
    parser.add_argument("--action", default="run", choices=ACTIONS)
    parser.add_argument("--position", default="")
    parser.add_argument("--scheduled", action="store_true", help="a run of the schedule, not one the page started")
    args = parser.parse_args()

    files, name = Files(args.account.parent), args.account.name
    account = load(files, name)
    now = datetime.now(fs.IST)
    now_ms = int(now.timestamp() * 1000)
    if args.scheduled and now_ms - account.get("page_run", 0) < PAGE_QUIET * 60_000:
        print("The Auto trader page is open and starting the runs: nothing to do.")
        return
    lines = []
    def note(message):
        lines.append(message)
        print(message)

    if args.action.startswith("unattended-"):
        account["unattended"] = args.action == "unattended-on"
        note("When the page is closed: " + ("auto-trade" if account["unattended"] else "only close positions"))
    elif args.action == "test-chartink":
        test_chartink(note)
    elif args.action == "rsi-today":
        send_rsi_today(files, account, now, note)
    elif args.action == "ha-today":
        send_ha_today(files, now, note)
    elif args.action == "test-telegram":
        if not telegram_alerts.ready():
            note("Telegram: TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID is not set")
        elif telegram_alerts.send("✅ Auto trader alerts are working."):
            note("Telegram: test message sent")
            telegram_alerts.sample_option_ha()
        else:
            note("Telegram: the test message was not sent (see the run log)")
    elif args.action != "run":
        apply_request(files, account, args.action, args.position, now, note)
    if not args.scheduled:
        account["page_run"] = now_ms
    settle(files, account, now, note)
    if args.action == "run" and market_open(now):
        dashboard, error = fs.load_dashboard()
        day_file = f"signals/{now.date().isoformat()}.json"
        seen = files.get(day_file, {})
        trading = not args.scheduled or account.get("unattended", True)
        enter_index_signals(files, account, seen, now, trading, note)
        enter_option_signals(files, account, now, trading, note)
        alert_option_ha(files, account, now, note)
        enter_vp_signals(files, account, seen, now, trading, note)
        for module in HELD_MODULES:
            enter_pure_signals(files, account, seen, now, trading, note, module)
        enter_weekly_signals(files, account, seen, now, trading, note)
        files.changed.add(day_file)
        if dashboard:
            instruments = {i["symbol"]: i for i in fs.load_instruments()}
            # HA and HA MTF no longer trade, so their scans (load_ha_signals) are not read.
            signals = [{**s, "system": RSI_SYSTEMS.get(s["timeframe"], "RSI")}
                       for s in fs.signal_rows(dashboard, instruments)[0]]
            record_seen(seen, signals, dashboard, now)
            for system in TRADED_TIMEFRAMES:
                enter_signals(files, account, [s for s in signals if s["system"] == system], dashboard, seen, now,
                              trading, note, system)
        else:
            note(f"No signals: {error}")

    # After the close, once a day: the day's trades on Telegram (nothing on a holiday).
    today = now.date().isoformat()
    if now.time() >= SESSION_CLOSE and now.weekday() < 5 and account.get("summary_day") != today:
        account["summary_day"] = today
        closed = [t for t in trades_of(files, now.strftime("%Y-%m"))
                  if datetime.fromtimestamp(t["exit_time"] / 1000, fs.IST).date() == now.date()]
        telegram_alerts.summary(now.strftime("%d %b"), closed, len(account["positions"]) if closed else 0)

    margin = sum(p["entry"] * p["qty"] * (1 if p.get("system") == opt_atm.SYSTEM else fs.MARGIN_RATE)
                 for p in account["positions"])  # a bought option needs its whole premium
    account["peak_margin"] = round(max(account.get("peak_margin", 0), margin), 2)
    account["updated"] = now_ms
    # The last runs' notes, newest first, shown on the Auto trader page.
    stamp = now.strftime("%d %b %H:%M")
    account["log"] = ([f"{stamp}  {line}" for line in reversed(lines)] + account.get("log", []))[:300]
    files.changed.add(name)
    prune_alerts(files)
    files.save()


if __name__ == "__main__":
    main()
