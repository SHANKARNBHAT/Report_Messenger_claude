"""
Auto trader page: the account auto_trader.py trades on a schedule (see
.github/workflows/auto_trader.yml), read from the auto-trader-data branch.

While open in market hours, the page starts a run every LIVE_EVERY minutes, on
time (GitHub's schedule is often late), and the scheduled runs skip. A request on
a position (keep going past the target with a trailing SL, exit at the target,
exit now) or a setting also starts a run, with that request, so the account is
only ever changed by the workflow, one run at a time.
"""

from __future__ import annotations

import time
from datetime import datetime
from datetime import time as clock
from urllib.parse import quote

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
LIVE_EVERY = 5  # minutes between the runs the page starts while open
LIVE_HOURS = (clock(9, 15), clock(15, 35))  # IST, weekdays: through the 3:15 PM square-off
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


def chart_link(symbol: str) -> str:
    """Link to the stock's chart on the site picked on the Chartink dashboard page."""
    links = fs.chart_links()
    link = links.get(st.session_state.get("charts") or MY_LAYOUT) or next(iter(links.values()))
    return link.replace("SYMBOL", quote(symbol, safe=""))


SYMBOL_LINK = st.column_config.LinkColumn("Symbol", display_text=r"symbol=(?:NSE%3A)?([^&]+)$",
                                          help="Opens the chart")


def when(ms) -> str:
    return datetime.fromtimestamp(ms / 1000, fs.IST).strftime("%d %b %H:%M") if ms else "—"


@st.fragment(run_every=60)
def live(shown: int | None) -> None:
    """Start a run every LIVE_EVERY minutes in market hours, and show the account a run saved."""
    account, _ = load_account()
    updated = (account or {}).get("updated")
    if updated != shown:
        st.rerun(scope="app")
    now = datetime.now(fs.IST)
    if now.weekday() >= 5 or not LIVE_HOURS[0] <= now.time() < LIVE_HOURS[1]:
        st.caption("🌙 Market closed: the page starts runs from 9:15 AM on weekdays while it is open.")
        return
    # The last run started from this page or another one open on another device.
    last = max(st.session_state.get("live_sent", 0), (account or {}).get("page_run", 0))
    if time.time() * 1000 - last >= LIVE_EVERY * 60_000 and dispatch("run"):
        st.session_state["live_sent"] = last = int(time.time() * 1000)
    st.caption(f"🟢 Live: this page starts a run every {LIVE_EVERY} minutes while it is open (last started "
               f"{when(last)}); GitHub's schedule waits. Keep the screen on: a phone pauses the page when it sleeps.")


def switch(unattended: bool) -> None:
    if dispatch("unattended-off" if unattended else "unattended-on"):
        st.toast("Switch sent: it applies in about a minute.")


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
    live((account or {}).get("updated"))
    if account is None:
        st.info(error)
        return

    unattended = account.get("unattended", True)
    # Keyed on the saved setting: once a run applies the switch, it shows the new one.
    st.toggle("Auto-trade when this page is closed", value=unattended, key=f"unattended-{unattended}",
              on_change=switch, args=(unattended,),
              help="On: GitHub's schedule takes the signals every 15 minutes while no page is open. "
                   "Off: it only closes positions (SL, target, 3:15 PM) and takes no new signals.")

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
        table = pd.DataFrame([{
            "Symbol": chart_link(p["symbol"]),
            "Side": "LONG" if p["side"] == "BUY" else "SHORT", "Signal": p["source"], "Qty": p["qty"],
            "Entry": p["entry"], "LTP": (quotes.get(p["symbol"]) or {}).get("price"),
            "SL / TSL": p["stop"], "Target": p["tp"],
            "At target": "Passed · trailing" if p.get("passed") else "Trail" if p.get("run") else "Exit",
            "Unrealized ₹": unrealized(p), "Entered": when(p["time"]), "Agrees": p.get("agrees"),
        } for p in positions])
        picked = st.dataframe(
            table, hide_index=True, width="stretch", on_select="rerun", selection_mode="single-row",
            column_config={"Symbol": SYMBOL_LINK, "Unrealized ₹": st.column_config.NumberColumn(format="%.2f")},
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
            trades = history.assign(Entered=history["entry_time"].map(when), Exited=history["exit_time"].map(when),
                                    Symbol=history["symbol"].map(chart_link))
            st.dataframe(trades[["Exited", "Symbol", "side", "Timeframes", "qty", "entry", "exit", "reason",
                                 "gross", "charges", "net", "Entered"]], hide_index=True, width="stretch",
                         column_config={"Symbol": SYMBOL_LINK})
    else:
        st.caption("No closed trades yet.")

    with st.expander("Run log"):
        st.code("\n".join(account.get("log", [])) or "Nothing yet.", language=None)


main()
