"""
Auto trader: paper-trades every Chartink signal on the 2H, 4H and Daily
timeframes, intraday, without the simulator page open.

.github/workflows/auto_trader.yml runs it every 15 minutes in market hours. Each run:
  * replays the open positions on the 5-minute bars since entry, closing those that
    touched their SL, trailing SL or target, at the 3:15 PM square-off or expiry, or
    on an RSI exit (the same rules as the simulator: futures_simulator.check_positions)
  * buys bullish and sells bearish signals at the latest price, with SL AUTO_SL_PCT
    and target AUTO_TP_PCT away, once in each exit style (futures_simulator.STYLES) to
    compare them on the same signals: squared off at 3:15 PM, held with a trailing SL
    after the target, held until the RSI exit. A stock is traded once per side and
    style per session. Every signal is taken, to test the strategy, so the margin used
    can exceed the capital: the most it reached is kept as "peak_margin"
  * applies a request from the Auto trader page (auto_trader_page.py): keep a
    position going past its target with a trailing SL, exit at the target, or exit now

While the Auto trader page is open it starts a run every few minutes itself, on
time, and the scheduled runs (--scheduled) stay quiet: they skip while the page
started one in the last PAGE_QUIET minutes. When it is closed, the scheduled runs
trade as above, or with "unattended" off (a switch on the page) only close
positions and take no new signals.

Each run in market hours also notes when a signal (stock, timeframe, bias) was first
seen and its price then, in signals_seen.json beside the account: trades keep it as
"first_seen", to measure what entering later than that cost (the simulator reads it too).

The account is kept in auto_trades.json on the auto-trader-data branch, so its
updates stay out of main.

    python auto_trader.py ACCOUNT.json [--action ACTION] [--position ID] [--scheduled]
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime, time
from pathlib import Path

import futures_simulator as fs

TIMEFRAMES = ("2 HOUR", "4 HOUR", "DAILY")
SHORT = {"2 HOUR": "2H", "4 HOUR": "4H", "DAILY": "Daily"}
LOTS = 1
LAST_ENTRY = time(15, 0)  # no new trades after this, ahead of the 3:15 PM square-off
SESSION_CLOSE = time(15, 30)
SEEN_DAYS = 10  # sessions kept in signals_seen.json
PAGE_QUIET = 12  # minutes: scheduled runs skip while the page started a run this recently
ACTIONS = ["run", "trail", "untrail", "exit", "unattended-on", "unattended-off"]


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


def load(path: Path) -> dict:
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    return {"capital": fs.START_CAPITAL, "positions": [], "history": [], "seq": 0, "log": []}


def signal_key(signal: dict) -> str:
    return f"{signal['symbol']}|{signal['timeframe']}|{signal['bias']}"


def record_seen(seen: dict, signals: list[dict], dashboard: dict, now: datetime) -> dict:
    """Note the signals seen for the first time today, with the time and price; returns today's."""
    today = seen.setdefault(now.date().isoformat(), {})
    new = [s for s in signals if signal_key(s) not in today]
    if new:
        instruments = {i["symbol"]: i for i in fs.load_instruments()}
        quotes = fs.latest_prices({s["symbol"] for s in new}, instruments, dashboard)
        for signal in new:
            today[signal_key(signal)] = {"time": int(now.timestamp() * 1000), "strategy": signal["strategy"],
                                         "price": (quotes.get(signal["symbol"]) or {}).get("price")}
    for day in sorted(seen)[:-SEEN_DAYS]:
        del seen[day]
    return today


def close(account: dict, position: dict, price: float, when_ms: int, reason: str) -> None:
    qty = position["qty"]
    buy, sell = (position["entry"], price) if position["side"] == "BUY" else (price, position["entry"])
    gross = (sell - buy) * qty
    cost = charges(buy * qty, sell * qty)
    account["history"].append({
        "id": position["id"], "symbol": position["symbol"], "side": position["side"], "lots": position["lots"],
        "qty": qty, "entry": position["entry"], "exit": round(price, 2), "entry_time": position["time"],
        "exit_time": when_ms, "reason": reason, "source": position["source"], "timeframes": position["timeframes"],
        "style": position.get("style", "eod"), "passed": position.get("passed", False), "carried": bool(position.get("run")), "tp": position["tp"],
        "strategies": position.get("strategies", []), "first_seen": position.get("first_seen"),
        "agrees": position.get("agrees"), "gross": round(gross, 2), "charges": round(cost, 2),
        "net": round(gross - cost, 2),
    })
    account["positions"].remove(position)


def auto_run(entry: float) -> dict:
    """Trailing amounts past the target (as autoRun() in futures_simulator.js)."""
    return {"trail": tick(entry * fs.AUTO_TRAIL_PCT / 100), "step": tick(entry * fs.AUTO_STEP_PCT / 100)}


def apply_request(account: dict, action: str, position_id: str, now: datetime, note) -> None:
    position = next((p for p in account["positions"] if p["id"] == position_id), None)
    if position is None:
        note(f"{action}: no open position {position_id}")
        return
    if action in ("trail", "untrail") and position.get("style", "eod") != "eod":
        note(f"{position['symbol']}: a {fs.STYLES[position['style']]} position, no target exit to change")
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
        close(account, position, tick(quote["price"]), int(now.timestamp() * 1000), "Manual exit")
        note(f"{position['symbol']}: exited at {tick(quote['price'])}")


def settle(account: dict, now: datetime, note) -> None:
    """Close the positions that hit their SL, trailing SL or target, or the 3:15 PM square-off."""
    if not account["positions"]:
        return
    for result in fs.check_positions(account["positions"], now, settle=False):
        position = next(p for p in account["positions"] if p["id"] == result["id"])
        if result["status"] == "closed":
            close(account, position, result["exit"], result["exit_time"], result["reason"])
            note(f"{position['symbol']}: {result['reason']} at {result['exit']}")
        elif result["status"] == "error":
            note(f"{position['symbol']}: no bars ({result['error']})")
        else:
            position.update(stop=result["stop"], status=result["status"], passed=result.get("passed", False))


def market_open(now: datetime) -> bool:
    """Whether NSE is trading now: in session hours, with NIFTY bars from today (not a holiday)."""
    if now.weekday() >= 5 or not fs.SESSION_OPEN <= now.time() < SESSION_CLOSE:
        return False
    bars = fs.fetch_bars((("^NSEI", "1d"),)).get(("^NSEI", "1d"))
    return isinstance(bars, list) and bool(bars) and datetime.fromtimestamp(bars[-1][0], fs.IST).date() == now.date()


def enter_signals(account: dict, signals: list[dict], dashboard: dict, seen: dict, now: datetime, note) -> None:
    instruments = {i["symbol"]: i for i in fs.load_instruments()}
    by_symbol: dict[str, list[dict]] = {}
    for signal in signals:
        if signal["timeframe"] in TIMEFRAMES and signal["fno"]:
            by_symbol.setdefault(signal["symbol"], []).append(signal)

    today = now.date()
    def taken(symbol, side, style):
        """Open in this style, or entered today in it."""
        return any(p["symbol"] == symbol and p.get("style", "eod") == style for p in account["positions"]) or any(
            t["symbol"] == symbol and t["side"] == side and t.get("style", "eod") == style
            and datetime.fromtimestamp(t["entry_time"] / 1000, fs.IST).date() == today for t in account["history"])

    wanted = {}
    for symbol, found in by_symbol.items():
        biases = {s["bias"] for s in found}
        if len(biases) > 1:
            continue  # bullish on one timeframe, bearish on another
        side = "BUY" if biases == {"Bullish"} else "SELL"
        styles = [style for style in fs.STYLES if not taken(symbol, side, style)]
        if styles:
            wanted[symbol] = (side, found, styles)
    if not wanted:
        return

    quotes = fs.latest_prices(set(wanted), instruments, dashboard)
    for symbol, (side, found, styles) in sorted(wanted.items()):
        quote = quotes.get(symbol)
        if not quote:
            note(f"{symbol}: no price, skipped")
            continue
        entry, qty = tick(quote["price"]), LOTS * instruments[symbol]["lot"]
        long = side == "BUY"
        sl = tick(entry * (1 - fs.AUTO_SL_PCT / 100 * (1 if long else -1)))
        tp = tick(entry * (1 + fs.AUTO_TP_PCT / 100 * (1 if long else -1)))
        timeframes = sorted({s["timeframe"] for s in found}, key=TIMEFRAMES.index)
        first = min((seen[signal_key(s)] for s in found if signal_key(s) in seen), key=lambda f: f["time"], default=None)
        for style in styles:
            account["seq"] += 1
            account["positions"].append({
                "id": f"{int(now.timestamp() * 1000)}-{account['seq']}", "symbol": symbol, "side": side, "lots": LOTS,
                "qty": qty, "entry": entry, "sl": sl, "tp": None if style == "rsi" else tp, "tsl": {"on": False},
                "run": auto_run(entry) if style == "trail" else None, "style": style,
                "stop": sl, "time": int(now.timestamp() * 1000), "status": "open", "passed": False,
                "source": "Auto " + "+".join(SHORT[tf] for tf in timeframes), "timeframes": timeframes,
                "strategies": sorted({s["strategy"] for s in found}), "agrees": found[0]["agrees"],
                "first_seen": first and {"time": first["time"], "price": first["price"]},
            })
        note(f"{symbol}: {side} {qty} at {entry} ({', '.join(timeframes)}; {', '.join(styles)})")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("account", type=Path)
    parser.add_argument("--action", default="run", choices=ACTIONS)
    parser.add_argument("--position", default="")
    parser.add_argument("--scheduled", action="store_true", help="a run of the schedule, not one the page started")
    args = parser.parse_args()

    account = load(args.account)
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
    elif args.action != "run":
        apply_request(account, args.action, args.position, now, note)
    if not args.scheduled:
        account["page_run"] = now_ms
    settle(account, now, note)
    seen_path = args.account.with_name("signals_seen.json")
    if args.action == "run" and market_open(now):
        dashboard, error = fs.load_dashboard()
        if dashboard:
            signals, _ = fs.signal_rows(dashboard, {i["symbol"]: i for i in fs.load_instruments()})
            seen = json.loads(seen_path.read_text(encoding="utf-8")) if seen_path.exists() else {}
            today = record_seen(seen, signals, dashboard, now)
            seen_path.parent.mkdir(parents=True, exist_ok=True)
            seen_path.write_text(json.dumps(seen), encoding="utf-8")
            trading = not args.scheduled or account.get("unattended", True)
            if trading and now.time() < LAST_ENTRY:
                enter_signals(account, signals, dashboard, today, now, note)
        else:
            note(f"No signals: {error}")

    margin = sum(p["entry"] * p["qty"] * fs.MARGIN_RATE for p in account["positions"])
    account["peak_margin"] = round(max(account.get("peak_margin", 0), margin), 2)
    account["updated"] = now_ms
    # The last runs' notes, newest first, shown on the Auto trader page.
    stamp = now.strftime("%d %b %H:%M")
    account["log"] = ([f"{stamp}  {line}" for line in reversed(lines)] + account.get("log", []))[:200]
    args.account.parent.mkdir(parents=True, exist_ok=True)
    args.account.write_text(json.dumps(account, indent=1), encoding="utf-8")


if __name__ == "__main__":
    main()
