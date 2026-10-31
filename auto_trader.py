"""
Auto trader: paper-trades every Chartink signal on the 2H, 4H and Daily
timeframes without the simulator page open, for two strategies apart (fs.SYSTEMS):
RSI (chartink_dashboard.DEFAULT_DASHBOARD) and Heikin Ashi (HA_DASHBOARD). A stock
on both gets a trade in each, so that each strategy's results stay its own.

.github/workflows/auto_trader.yml runs it every 15 minutes in market hours. Each run:
  * replays the open positions on the 5-minute bars since entry, closing those that
    touched their SL, trailing SL or target, at the 3:15 PM square-off or expiry, or
    on an RSI exit (the same rules as the simulator: futures_simulator.check_positions)
  * buys bullish and sells bearish signals at the latest price, with SL AUTO_SL_PCT
    and target AUTO_TP_PCT away, once in each exit style (futures_simulator.STYLES) to
    compare them on the same signals: squared off at 3:15 PM, held with a trailing SL
    after the target, held until the RSI exit (for Heikin Ashi: the HA exit, see
    ha_exit). A stock is traded once per side and style per session, in each strategy.
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
from datetime import datetime, time, timedelta
from pathlib import Path

import futures_simulator as fs

TIMEFRAMES = ("2 HOUR", "4 HOUR", "DAILY")
SHORT = {"2 HOUR": "2H", "4 HOUR": "4H", "DAILY": "Daily"}
LOTS = 1
# The column of the HA widgets with the SMA of the RSI of the signal's own timeframe.
HA_SMA_RSI = {"2 HOUR": "Sma 2hr rsi", "4 HOUR": "Sma 4hr rsi", "DAILY": "Dly SMA RSI"}
LAST_ENTRY = time(15, 0)  # no new trades after this, ahead of the 3:15 PM square-off
SESSION_CLOSE = time(15, 30)
PAGE_QUIET = 12  # minutes: scheduled runs skip while the page started a run this recently
ACTIONS = ["run", "trail", "untrail", "exit", "unattended-on", "unattended-off", "test-chartink"]


def tick(price: float) -> float:
    return round(round(price * 20) / 20, 2)  # NSE futures tick size ₹0.05


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
            path.write_text(json.dumps(self.data[name], indent=None if name.startswith("signals/") else 1),
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
    cost = charges(buy * qty, sell * qty)
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


def ha_exit(position: dict, candles: dict, now: datetime) -> dict | None:
    """The HA exit of a position, if due: the first candle of its timeframe (the shortest if
    several) since the entry whose close, live while it runs, went past its level (ha_level).
    A finished candle, in case the runs missed it, exits at its end. Exit price, time (ms)
    and reason, else None."""
    timeframe = position["timeframes"][0]
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
    for result in fs.check_positions(account["positions"], now, settle=False):
        position = next(p for p in account["positions"] if p["id"] == result["id"])
        # A candle that went past since the last run may have come before what the bars show.
        due = ha_exit(position, candles, now) if position.get("style") in fs.HA_EXITS else None
        if due and (result["status"] != "closed" or due["exit_time"] < result["exit_time"]):
            result = {**result, **due, "status": "closed"}
        if result["status"] == "closed":
            close(files, account, position, result["exit"], result["exit_time"], result["reason"],
                  result.get("high"), result.get("low"))
            note(f"{position['symbol']}: {result['reason']} at {result['exit']} ({fs.BOT_STYLES[position['style']]})")
        elif result["status"] == "error":
            note(f"{position['symbol']}: no bars ({result['error']})")
        else:
            position.update(stop=result["stop"], status=result["status"], passed=result.get("passed", False),
                            high=result.get("high"), low=result.get("low"))


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
    ha_dashboard, ha_error = fs.load_dashboard(fs.HA_DASHBOARD)
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


def enter_signals(files: Files, account: dict, signals: list[dict], dashboard: dict, seen: dict, now: datetime,
                  trading: bool, note, system: str) -> None:
    """Trade one strategy's signals, and note in `seen` what was done with each."""
    instruments = {i["symbol"]: i for i in fs.load_instruments()}
    mark(seen, [s for s in signals if not s["fno"]], "Not F&O")
    mark(seen, [s for s in signals if s["fno"] and s["timeframe"] not in TIMEFRAMES], "Timeframe not traded")
    by_symbol: dict[str, list[dict]] = {}
    for signal in signals:
        if signal["timeframe"] in TIMEFRAMES and signal["fno"]:
            by_symbol.setdefault(signal["symbol"], []).append(signal)
    if not trading:
        mark(seen, [s for found in by_symbol.values() for s in found], "Not traded: switch off, page closed")
        return
    if now.time() >= LAST_ENTRY:
        mark(seen, [s for found in by_symbol.values() for s in found], "After 3:00 PM")
        return

    today = now.date()
    entered_today = [t for t in trades_of(files, now.strftime("%Y-%m"))
                     if datetime.fromtimestamp(t["entry_time"] / 1000, fs.IST).date() == today]
    def taken(symbol, side, style):
        """Open in this style, or entered today in it, in this strategy."""
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
    for symbol, (side, found, styles) in sorted(wanted.items()):
        quote = quotes.get(symbol)
        if not quote:
            mark(seen, found, "No price")
            note(f"{symbol}: no price, skipped")
            continue
        entry, qty = tick(quote["price"]), LOTS * instruments[symbol]["lot"]
        long = side == "BUY"
        sl = tick(entry * (1 - fs.AUTO_SL_PCT / 100 * (1 if long else -1)))
        tp = tick(entry * (1 + fs.AUTO_TP_PCT / 100 * (1 if long else -1)))
        timeframes = sorted({s["timeframe"] for s in found}, key=TIMEFRAMES.index)
        first = min((seen[signal_key(s)] for s in found if signal_key(s) in seen), key=lambda f: f["time"], default=None)
        # The SMA RSI of the shortest timeframe, as for the exit.
        sma_rsi = next((s["sma_rsi"] for s in sorted(found, key=lambda s: TIMEFRAMES.index(s["timeframe"]))
                        if s.get("sma_rsi") is not None), None)
        for style in styles:
            account["seq"] += 1
            account["positions"].append({
                "id": f"{int(now.timestamp() * 1000)}-{account['seq']}", "symbol": symbol, "side": side, "lots": LOTS,
                "qty": qty, "entry": entry, "sl": sl, "tp": None if style == "rsi" or style in fs.HA_EXITS else tp, "tsl": {"on": False},
                "run": auto_run(entry) if style == "trail" else None, "style": style, "system": system,
                "stop": sl, "time": int(now.timestamp() * 1000), "status": "open", "passed": False,
                "high": entry, "low": entry, "nifty": nifty,
                "source": ("Auto " if system == "RSI" else f"Auto {system} ") + "+".join(SHORT[tf] for tf in timeframes),
                "timeframes": timeframes,
                "strategies": sorted({s["strategy"] for s in found}), "agrees": found[0]["agrees"], "sma_rsi": sma_rsi,
                "first_seen": first and {"time": first["time"], "price": first["price"]},
            })
        mark(seen, found, "Traded")
        note(f"{symbol}: {system} {side} {qty} at {entry} ({', '.join(timeframes)}; {', '.join(styles)})")


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
    elif args.action != "run":
        apply_request(files, account, args.action, args.position, now, note)
    if not args.scheduled:
        account["page_run"] = now_ms
    settle(files, account, now, note)
    if args.action == "run" and market_open(now):
        dashboard, error = fs.load_dashboard()
        if dashboard:
            instruments = {i["symbol"]: i for i in fs.load_instruments()}
            signals = [{**s, "system": "RSI"} for s in fs.signal_rows(dashboard, instruments)[0]]
            ha_dashboard, ha_error = fs.load_dashboard(fs.HA_DASHBOARD)
            if ha_dashboard:
                sma_rsi = ha_sma_rsi(ha_dashboard)
                signals += [{**s, "system": "HA", "sma_rsi": sma_rsi.get((s["strategy"], s["symbol"]))}
                            for s in fs.signal_rows(ha_dashboard, instruments)[0]]
            else:
                note(f"No HA signals: {ha_error}")
            day_file = f"signals/{now.date().isoformat()}.json"
            seen = files.get(day_file, {})
            record_seen(seen, signals, dashboard, now)
            trading = not args.scheduled or account.get("unattended", True)
            for system in fs.SYSTEMS:
                enter_signals(files, account, [s for s in signals if s["system"] == system], dashboard, seen, now,
                              trading, note, system)
            files.changed.add(day_file)
        else:
            note(f"No signals: {error}")

    margin = sum(p["entry"] * p["qty"] * fs.MARGIN_RATE for p in account["positions"])
    account["peak_margin"] = round(max(account.get("peak_margin", 0), margin), 2)
    account["updated"] = now_ms
    # The last runs' notes, newest first, shown on the Auto trader page.
    stamp = now.strftime("%d %b %H:%M")
    account["log"] = ([f"{stamp}  {line}" for line in reversed(lines)] + account.get("log", []))[:300]
    files.changed.add(name)
    files.save()


if __name__ == "__main__":
    main()
