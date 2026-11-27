"""
Telegram alerts from the Auto trader runs (auto_trader.py): trades of the alerted strategies (ALERT_SYSTEMS),
problems, and a summary of the day after the close.

Needs TELEGRAM_BOT_TOKEN (from @BotFather) and TELEGRAM_CHAT_ID (the chat the bot writes to; send the bot a
message first) in the environment, the repository's Actions secrets. Without them nothing is sent. An alert
that fails to send is printed and never stops a run.
"""

from __future__ import annotations

import os

import requests

API = "https://api.telegram.org/bot{token}/sendMessage"
APP = "https://reportmessengerclaude.streamlit.app/auto-index"
ALERT_SYSTEMS = {"OPT ATM HA"}  # strategies whose entries and exits are alerted (opt_atm.SYSTEM)


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


def entry(position: dict) -> None:
    if position.get("system") in ALERT_SYSTEMS:
        send(f"🟢 <b>BUY {position['symbol']}</b> × {position['qty']} @ {position['entry']}\n"
             f"SL {position['sl']} · {position.get('system')}\n<a href=\"{APP}\">Open the app</a>")


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
        lines.append(f"{at}{arrow} <b>{s['symbol']}</b> @ {s['price']:g} → <b>{strike}</b> · lot {s['lot']}")
    send("\n".join(lines))


def option_ha(when: str, hits: list[dict]) -> None:
    """The ATM calls whose 1H / 2H / 4H candle just turned after a red HA one (opt_ha_alert.scan): HA solid
    green, or an HA doji with a solid green normal candle."""
    lines = [f"🟢 <b>HA turn after red · ATM CE</b> · candles ending {when}"]
    for h in hits:
        extra = " · ".join(x for x in (
            f"LTP {h['ltp']:g}" if h.get("ltp") is not None else "",
            f"spot {h['spot']:,.2f}",
            f"lot {h['lot']}" if h.get("lot") else "",
            f"ATR {h['ratio']:.1f}× strike" if h.get("ratio") else "") if x)
        kind = "HA doji, candle solid green" if h["candle"].get("kind") == "HA doji" else "HA solid green"
        lines.append(f"• <b>{h['timeframe']}</b> · <b>{h['option']}</b> · {kind}\n   close {h['candle']['close']:g} · {extra}")
    send("\n".join(lines))


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
