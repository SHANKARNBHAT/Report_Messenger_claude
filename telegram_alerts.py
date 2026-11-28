"""
Telegram alerts from the Auto trader runs (auto_trader.py): trades of the alerted strategies (ALERT_SYSTEMS),
problems, and a summary of the day after the close.

Needs TELEGRAM_BOT_TOKEN (from @BotFather) and TELEGRAM_CHAT_ID (the chat the bot writes to; send the bot a
message first) in the environment, the repository's Actions secrets. Without them nothing is sent. An alert
that fails to send is printed and never stops a run.
"""

from __future__ import annotations

import os

from html import escape
from urllib.parse import quote

import requests
from datetime import datetime
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")

API = "https://api.telegram.org/bot{token}/sendMessage"
APP = "https://reportmessengerclaude.streamlit.app/auto-index"
# A stock's Chartink chart with the dashboard's indicators (chartink_dashboard.DEFAULT_SNAPSHOT).
CHARTINK = ("https://chartink.com/stocks-new?load-snapshot=simple-moving-average-simple-moving-average-simple-moving-"
            "average-pivot-s2-pivot-copy-pivot-simple-moving-average-volume-weighted-moving-averag-chart-snapshot&symbol={}")
ALERT_SYSTEMS = {"OPT ATM HA", "OPT RSI"}  # strategies whose entries and exits are alerted (opt_atm, rsi_opt)


def ready() -> bool:
    return bool(os.environ.get("TELEGRAM_BOT_TOKEN") and os.environ.get("TELEGRAM_CHAT_ID"))


def send(text: str) -> bool:
    """Send one message (HTML formatting); False, printed, if it could not be sent."""
    if not ready():
        return False
    try:
        reply = requests.post(API.format(token=os.environ["TELEGRAM_BOT_TOKEN"].strip()), timeout=15, json={
            "chat_id": os.environ["TELEGRAM_CHAT_ID"].strip(), "text": text[:4000], "parse_mode": "HTML",
            "disable_web_page_preview": True})
        if reply.ok:
            return True
        print(f"Telegram: HTTP {reply.status_code} {reply.text[:200]}")
    except requests.RequestException as exc:
        print(f"Telegram: {exc}")
    return False


def chart(symbol: str, text: str | None = None) -> str:
    """`text` (the symbol) as a link to the stock's Chartink chart."""
    return f'<a href="{CHARTINK.format(quote(symbol, safe=""))}">{escape(text or symbol)}</a>'


def send_lines(head: str, lines: list[str]) -> None:
    """`head` and the lines in as few messages as fit Telegram's 4096 characters, each under the head."""
    parts: list[list[str]] = [[]]
    for line in lines:
        if parts[-1] and len(head) + sum(len(x) + 1 for x in parts[-1]) + len(line) > 3800:
            parts.append([])
        parts[-1].append(line)
    for k, part in enumerate(parts):
        send("\n".join([head + (f" ({k + 1}/{len(parts)})" if len(parts) > 1 else ""), *part]))


def entry(position: dict) -> None:
    if position.get("system") in ALERT_SYSTEMS:
        name = position.get("underlying")
        why = f"\n{chart(name)} · {escape(position['why'])}" if position.get("why") and name else ""
        send(f"🟢 <b>BUY {escape(position['symbol'])}</b> × {position['qty']} @ {position['entry']}\n"
             f"SL {position['sl']} · {position.get('system')}{why}\n<a href=\"{APP}\">Open the app</a>")


def exit(trade: dict) -> None:
    if trade.get("system") in ALERT_SYSTEMS:
        mark = "✅" if trade["net"] > 0 else "🔻"
        send(f"{mark} <b>EXIT {trade['symbol']}</b> @ {trade['exit']} ({trade['reason']})\n"
             f"Entry {trade['entry']} · net ₹{trade['net']:,.0f} · {trade.get('system')}")


def rsi_signals(timeframe: str, when: str, signals: list[dict]) -> None:
    """The stocks an RSI strategy just entered, with the ATM option of the trade's side (auto_trader.atm_of)."""
    lines = [f"📊 <b>RSI {timeframe} signals</b> · {when}"]
    for s in signals:
        arrow = "🟢 BUY" if s["side"] == "BUY" else "🔴 SELL"
        strike = f"ATM {s['strike']:g} {s['leg']}" if s.get("strike") else "ATM strike unknown"
        at = f"{s['time']} " if s.get("time") else ""
        lines.append(f"{at}{arrow} <b>{chart(s['symbol'])}</b> @ {s['price']:g} → <b>{strike}</b> · lot {s['lot']}")
    send_lines(lines[0], lines[1:])


def option_ha(when: str, hits: list[dict]) -> None:
    """The ATM calls and puts whose 15m to 4H candle just turned after a red HA one (opt_ha_alert.scan): HA solid
    green, or an HA doji with a solid green normal candle."""
    head = f"🔔 <b>HA turn green · ATM CE / PE</b> · {when}"
    timed = len({h.get("end") for h in hits}) > 1  # a replay of the day: each its candle's end
    lines = []
    for h in hits:
        extra = " · ".join(x for x in (
            f"LTP {h['ltp']:g}" if h.get("ltp") is not None else "",
            f"spot {h['spot']:,.2f}",
            f"lot {h['lot']}" if h.get("lot") else "",
            f"ATR {h['ratio']:.1f}× strike" if h.get("ratio") else "") if x)
        kind = {"HA doji": "HA doji, candle solid green",
                "After green doji": "HA solid green after a green HA doji"}.get(h["candle"].get("kind"), "HA solid green")
        mark = "🔴" if h.get("side") == "PE" else "🟢"  # a put premium turning up: the stock turning down
        at = f"{datetime.fromtimestamp(h['end'], IST):%H:%M} " if timed and h.get("end") else ""
        name = h.get("name") or h["option"].split()[0]
        option = chart(name) + escape(h["option"][len(name):]) if h["option"].startswith(name) else escape(h["option"])
        lines.append(f"{mark} {at}<b>{h['timeframe']}</b> · <b>{option}</b> · {kind}\n   close {h['candle']['close']:g} · {extra}")
    send_lines(head, lines)


def sample_option_ha() -> None:
    """A made-up HA CE / PE alert, one line of each kind, to see how it looks (test-telegram)."""
    def hit(timeframe, side, option, kind, close, ltp, spot, lot, ratio):
        return {"timeframe": timeframe, "side": side, "option": option, "ltp": ltp, "spot": spot, "lot": lot,
                "ratio": ratio, "candle": {"kind": kind, "close": close}}
    option_ha("candles ending 13:15 · <i>SAMPLE, not a real alert</i>", [
        hit("4H", "CE", "KAYNES 29DEC 6000 CE", "HA solid green", 212, 210.5, 5990, 100, 4.2),
        hit("2H", "PE", "NIFTY 01DEC 26200 PE", "HA doji", 95.4, 96, 26190.5, 65, None),
        hit("1H", "CE", "MANAPPURAM 29DEC 300 CE", "After green doji", 8.45, 8.5, 298.6, 3000, 3.8),
    ])


def problem(text: str) -> None:
    send(f"⚠️ Auto trader: {text}")


def summary(day: str, trades: list[dict], open_count: int) -> None:
    """The day's closed trades by strategy, with the net P&L."""
    if not trades and not open_count:
        return
    by: dict[str, list[float]] = {}
    for t in trades:
        by.setdefault(t.get("system", "RSI"), []).append(t["net"])
    lines = [f"📊 <b>Day summary {day}</b>"]
    for system, nets in sorted(by.items(), key=lambda kv: -abs(sum(kv[1]))):
        wins = sum(n > 0 for n in nets)
        lines.append(f"{system}: {len(nets)} trades, {wins} won, net ₹{sum(nets):,.0f}")
    lines.append(f"<b>Total: {len(trades)} trades, net ₹{sum(t['net'] for t in trades):,.0f}</b>")
    if open_count:
        lines.append(f"Still open: {open_count}")
    send("\n".join(lines))
