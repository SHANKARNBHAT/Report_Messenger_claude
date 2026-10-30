"""
Auto trader page: the account auto_trader.py trades on a schedule (see
.github/workflows/auto_trader.yml), read from the auto-trader-data branch.

A request on a position (keep going past the target with a trailing SL, exit at
the target, exit now) starts a run of the workflow with that request, so the
account is only ever changed by the workflow, one run at a time.
"""

from __future__ import annotations

import time
from datetime import datetime

import pandas as pd
import requests
import streamlit as st

import futures_simulator as fs
from chartink_dashboard import MY_LAYOUT

GITHUB_OWNER, GITHUB_REPO = "SHANKARNBHAT", "Report_Messenger_Claude"
ACCOUNT_URL = f"https://raw.githubusercontent.com/{GITHUB_OWNER}/{GITHUB_REPO}/auto-trader-data/auto_trades.json"
DISPATCH_URL = (f"https://api.github.com/repos/{GITHUB_OWNER}/{GITHUB_REPO}/actions/workflows/"
                "auto_trader.yml/dispatches")
RUNS_URL = f"https://github.com/{GITHUB_OWNER}/{GITHUB_REPO}/actions/workflows/auto_trader.yml"
REQUESTS = {
    "trail": "📈 Keep going at target (trailing SL)",
    "untrail": "🎯 Exit at target",
    "exit": "🚪 Exit now at LTP",
}


@st.cache_data(ttl=30, show_spinner=False)
def load_account() -> tuple[dict | None, str | None]:
    try:
        # A changing query string skips GitHub's ~5 minute raw-file cache.
        response = requests.get(ACCOUNT_URL, params={"t": int(time.time())}, timeout=15)
        if response.status_code == 404:
            return None, "No trades yet: the first scheduled run in market hours starts the account."
        response.raise_for_status()
        return response.json(), None
    except (requests.RequestException, ValueError) as exc:
        return None, f"Could not load the account: {exc}"


def dispatch(action: str, position: str = "") -> bool:
    token = st.secrets.get("GITHUB_TOKEN", "").strip()
    if not token:
        st.error("GITHUB_TOKEN is not configured in Streamlit secrets.")
        return False
    try:
        response = requests.post(
            DISPATCH_URL, json={"ref": "main", "inputs": {"action": action, "position": position}},
            headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json",
                     "X-GitHub-Api-Version": "2022-11-28"}, timeout=10)
    except requests.RequestException as exc:
        st.error(f"Could not start the auto trader: {exc}")
        return False
    if response.status_code != 204:
        st.error(f"Could not start the auto trader: {response.text}")
        return False
    return True


def when(ms) -> str:
    return datetime.fromtimestamp(ms / 1000, fs.IST).strftime("%d %b %H:%M") if ms else "—"


def summary(trades: pd.DataFrame, by: str) -> pd.DataFrame:
    grouped = trades.groupby(by, sort=False)
    table = pd.DataFrame({
        "Trades": grouped.size(),
        "Win rate %": grouped["net"].apply(lambda n: round(100 * (n > 0).mean(), 1)),
        "Net P&L ₹": grouped["net"].sum().round(2),
        "Avg per trade ₹": grouped["net"].mean().round(2),
    })
    return table


def main() -> None:
    st.set_page_config(page_title="Auto Trader", page_icon="🤖", layout="wide")
    st.title("🤖 Auto trader")
    st.caption(
        f"Paper-trades every 2H, 4H and Daily Chartink signal, 1 lot, intraday: SL {fs.AUTO_SL_PCT}%, "
        f"target {fs.AUTO_TP_PCT}%, squared off at 3:15 PM. Runs every 15 minutes in market hours on "
        f"GitHub, no need to keep this page open. A stock is traded once per side per day.")

    account, error = load_account()
    top = st.columns([1, 1, 4])
    if top[0].button("↻ Reload", width="stretch"):
        load_account.clear()
        st.rerun()
    if top[1].button("▶ Run now", width="stretch", help="Check the positions and take new signals now"):
        if dispatch("run"):
            st.toast("Started a run: it takes about a minute. Reload then.")
    top[2].caption(f"[Runs on GitHub]({RUNS_URL}) · requests from here apply in about a minute.")
    if account is None:
        st.info(error)
        return

    history = pd.DataFrame(account["history"])
    positions = account["positions"]
    instruments = {i["symbol"]: i for i in fs.load_instruments()}
    quotes = fs.latest_prices({p["symbol"] for p in positions}, instruments, None) if positions else {}

    def unrealized(p):
        q = quotes.get(p["symbol"])
        return None if not q else round((q["price"] - p["entry"]) * p["qty"] * (1 if p["side"] == "BUY" else -1), 2)

    net = history["net"].sum() if len(history) else 0
    open_pnl = sum(v for v in map(unrealized, positions) if v is not None)
    stats = st.columns(6)
    stats[0].metric("Net P&L (closed)", f"₹{net:,.0f}")
    stats[1].metric("Open P&L", f"₹{open_pnl:,.0f}")
    stats[2].metric("Trades closed", len(history))
    stats[3].metric("Win rate", f"{100 * (history['net'] > 0).mean():.1f}%" if len(history) else "—")
    stats[4].metric("Peak margin used", f"₹{account.get('peak_margin', 0):,.0f}",
                    help=f"Every signal is taken; capital ₹{account['capital']:,.0f}")
    stats[5].metric("Last run", when(account.get("updated")))

    # Open positions, with requests on the one selected.
    st.subheader(f"Open positions ({len(positions)})")
    if positions:
        links = fs.chart_links()
        link = links.get(st.session_state.get("charts") or MY_LAYOUT) or next(iter(links.values()))
        table = pd.DataFrame([{
            "Symbol": link.replace("SYMBOL", p["symbol"]),
            "Side": "LONG" if p["side"] == "BUY" else "SHORT", "Signal": p["source"], "Qty": p["qty"],
            "Entry": p["entry"], "LTP": (quotes.get(p["symbol"]) or {}).get("price"),
            "SL / TSL": p["stop"], "Target": p["tp"],
            "At target": "Passed · trailing" if p.get("passed") else "Trail" if p.get("run") else "Exit",
            "Unrealized ₹": unrealized(p), "Entered": when(p["time"]), "Agrees": p.get("agrees"),
        } for p in positions])
        picked = st.dataframe(
            table, hide_index=True, width="stretch", on_select="rerun", selection_mode="single-row",
            column_config={"Symbol": st.column_config.LinkColumn("Symbol", display_text=r"symbol=(?:NSE%3A)?([^&]+)$"),
                           "Unrealized ₹": st.column_config.NumberColumn(format="%.2f")},
        )
        rows = picked.selection.rows
        if rows:
            position = positions[rows[0]]
            cols = st.columns(len(REQUESTS))
            for col, (action, label) in zip(cols, REQUESTS.items()):
                if col.button(f"{label}: {position['symbol']}", key=f"{action}-{position['id']}", width="stretch"):
                    if dispatch(action, position["id"]):
                        st.toast(f"Sent: {label} for {position['symbol']}. Applies in about a minute; Reload then.")
        else:
            st.caption("Select a row to keep it going past its target, set it back to exit there, or exit it now.")
    else:
        st.caption("None open.")

    st.subheader("Results")
    if len(history):
        history = history.sort_values("exit_time", ascending=False)
        history["Day"] = history["exit_time"].map(lambda ms: when(ms)[:6])
        history["Timeframes"] = history["source"].str.removeprefix("Auto ")
        tabs = st.tabs(["By day", "By signal", "By exit", "Trades"])
        with tabs[0]:
            st.dataframe(summary(history, "Day"), width="stretch")
        with tabs[1]:
            st.dataframe(summary(history, "Timeframes"), width="stretch")
        with tabs[2]:
            st.dataframe(summary(history, "reason"), width="stretch")
        with tabs[3]:
            trades = history.assign(Entered=history["entry_time"].map(when), Exited=history["exit_time"].map(when))
            st.dataframe(trades[["Exited", "symbol", "side", "Timeframes", "qty", "entry", "exit", "reason",
                                 "gross", "charges", "net", "Entered"]], hide_index=True, width="stretch")
    else:
        st.caption("No closed trades yet.")

    with st.expander("Run log"):
        st.code("\n".join(account.get("log", [])) or "Nothing yet.", language=None)


main()
