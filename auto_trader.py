"""
Auto trader: paper-trades every Chartink signal on the 2H, 4H and Daily
timeframes, intraday, without the simulator page open.

.github/workflows/auto_trader.yml runs it every 15 minutes in market hours. Each run:
  * replays the open positions on the 5-minute bars since entry, closing those that
    touched their SL, trailing SL or target, and all of them at the 3:15 PM square-off
    (the same rules as the simulator: futures_simulator.replay)
  * buys bullish and sells bearish signals at the latest price, with SL AUTO_SL_PCT
    and target AUTO_TP_PCT away; a stock is traded once per side per session. Every
    signal is taken, to test the strategy, so the margin used can exceed the capital:
    the most it reached is kept as "peak_margin"
  * applies a request from the Auto trader page (auto_trader_page.py): keep a
    position going past its target with a trailing SL, exit at the target, or exit now

The account is kept in auto_trades.json on the auto-trader-data branch, so its
updates stay out of main.

    python auto_trader.py ACCOUNT.json [--action run|trail|untrail|exit] [--position ID]
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


def close(account: dict, position: dict, price: float, when_ms: int, reason: str) -> None:
    qty = position["qty"]
    buy, sell = (position["entry"], price) if position["side"] == "BUY" else (price, position["entry"])
    gross = (sell - buy) * qty
    cost = charges(buy * qty, sell * qty)
    account["history"].append({
        "id": position["id"], "symbol": position["symbol"], "side": position["side"], "lots": position["lots"],
        "qty": qty, "entry": position["entry"], "exit": round(price, 2), "entry_time": position["time"],
        "exit_time": when_ms, "reason": reason, "source": position["source"], "timeframes": position["timeframes"],
        "passed": position.get("passed", False), "gross": round(gross, 2), "charges": round(cost, 2),
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
    if not fs.SESSION_OPEN <= now.time() < LAST_ENTRY:
        return False
    bars = fs.fetch_bars((("^NSEI", "1d"),)).get(("^NSEI", "1d"))
    return isinstance(bars, list) and bool(bars) and datetime.fromtimestamp(bars[-1][0], fs.IST).date() == now.date()


def enter_signals(account: dict, now: datetime, note) -> None:
    dashboard, error = fs.load_dashboard()
    if not dashboard:
        note(f"No signals: {error}")
        return
    instruments = {i["symbol"]: i for i in fs.load_instruments()}
    signals, _ = fs.signal_rows(dashboard, instruments)
    by_symbol: dict[str, list[dict]] = {}
    for signal in signals:
        if signal["timeframe"] in TIMEFRAMES and signal["fno"]:
            by_symbol.setdefault(signal["symbol"], []).append(signal)

    today = now.date()
    def traded_today(symbol, side):
        return any(t["symbol"] == symbol and t["side"] == side
                   and datetime.fromtimestamp(t["entry_time"] / 1000, fs.IST).date() == today
                   for t in account["history"])

    wanted = {}
    for symbol, found in by_symbol.items():
        biases = {s["bias"] for s in found}
        if len(biases) > 1:
            continue  # bullish on one timeframe, bearish on another
        side = "BUY" if biases == {"Bullish"} else "SELL"
        if any(p["symbol"] == symbol for p in account["positions"]) or traded_today(symbol, side):
            continue
        wanted[symbol] = (side, found)
    if not wanted:
        return

    quotes = fs.latest_prices(set(wanted), instruments, dashboard)
    for symbol, (side, found) in sorted(wanted.items()):
        quote = quotes.get(symbol)
        if not quote:
            note(f"{symbol}: no price, skipped")
            continue
        entry, qty = tick(quote["price"]), LOTS * instruments[symbol]["lot"]
        long = side == "BUY"
        sl = tick(entry * (1 - fs.AUTO_SL_PCT / 100 * (1 if long else -1)))
        account["seq"] += 1
        timeframes = sorted({s["timeframe"] for s in found}, key=TIMEFRAMES.index)
        account["positions"].append({
            "id": f"{int(now.timestamp() * 1000)}-{account['seq']}", "symbol": symbol, "side": side, "lots": LOTS,
            "qty": qty, "entry": entry, "sl": sl,
            "tp": tick(entry * (1 + fs.AUTO_TP_PCT / 100 * (1 if long else -1))), "tsl": {"on": False}, "run": None,
            "stop": sl, "time": int(now.timestamp() * 1000), "status": "open", "passed": False,
            "source": "Auto " + "+".join(SHORT[tf] for tf in timeframes), "timeframes": timeframes,
            "strategies": sorted({s["strategy"] for s in found}), "agrees": found[0]["agrees"],
        })
        note(f"{symbol}: {side} {qty} at {entry} ({', '.join(timeframes)})")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("account", type=Path)
    parser.add_argument("--action", default="run", choices=["run", "trail", "untrail", "exit"])
    parser.add_argument("--position", default="")
    args = parser.parse_args()

    account = load(args.account)
    now = datetime.now(fs.IST)
    lines = []
    def note(message):
        lines.append(message)
        print(message)

    if args.action != "run":
        apply_request(account, args.action, args.position, now, note)
    settle(account, now, note)
    if args.action == "run" and market_open(now):
        enter_signals(account, now, note)

    margin = sum(p["entry"] * p["qty"] * fs.MARGIN_RATE for p in account["positions"])
    account["peak_margin"] = round(max(account.get("peak_margin", 0), margin), 2)
    account["updated"] = int(now.timestamp() * 1000)
    # The last runs' notes, newest first, shown on the Auto trader page.
    stamp = now.strftime("%d %b %H:%M")
    account["log"] = ([f"{stamp}  {line}" for line in reversed(lines)] + account.get("log", []))[:200]
    args.account.parent.mkdir(parents=True, exist_ok=True)
    args.account.write_text(json.dumps(account, indent=1), encoding="utf-8")


if __name__ == "__main__":
    main()
