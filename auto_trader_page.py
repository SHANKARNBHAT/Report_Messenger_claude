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
        response = fs.data_file("auto_trades.json")
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
    if not response.ok:  # 204 No Content when started
        try:
            message = response.json().get("message") or response.text
        except ValueError:
            message = response.text
        st.error(f"Could not start the auto trader: GitHub answered {response.status_code} "
                 f"{response.reason}. {message}".strip())
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


def direction(trades: pd.DataFrame) -> pd.Series:
    return trades["side"].map({"BUY": 1, "SELL": -1})


def show_carry(history: pd.DataFrame) -> None:
    """The positions kept going past the target with a trailing SL: a success is a trade in profit."""
    carried = history[history.get("carried", pd.Series(False, index=history.index)).eq(True)]
    if carried.empty:
        st.caption("No carry-forward decisions yet: select an open position and choose Keep going at target.")
        return
    passed = carried[carried["passed"].eq(True)]
    extra = ((passed["exit"] - passed["tp"]) * passed["qty"] * direction(passed)).sum()
    cols = st.columns(4)
    cols[0].metric("Decisions", len(carried))
    cols[1].metric("Successful (in profit)", f"{(carried['net'] > 0).sum()} · {100 * (carried['net'] > 0).mean():.0f}%")
    cols[2].metric("Reached the target", len(passed), help="Trailing only started for these; the rest closed "
                   "before the target, as they would have anyway")
    cols[3].metric("vs exiting at target", f"₹{extra:,.0f}", help="Gained (or lost) past the target by "
                   "trailing instead of exiting there, before charges")
    table = carried.assign(Symbol=carried["symbol"].map(chart_link), Exited=carried["exit_time"].map(when),
                           **{"vs target ₹": ((carried["exit"] - carried["tp"]) * carried["qty"] * direction(carried))
                              .where(carried["passed"].eq(True)).round(2)})
    st.dataframe(table[["Exited", "Symbol", "side", "entry", "tp", "exit", "reason", "net", "vs target ₹"]],
                 hide_index=True, width="stretch", column_config={"Symbol": SYMBOL_LINK})


DELAYS = [(5, "≤ 5 min"), (15, "5–15 min"), (30, "15–30 min"), (60, "30–60 min"), (120, "1–2 h"), (float("inf"), "> 2 h")]


def delay_table(trades: pd.DataFrame) -> pd.DataFrame | None:
    """Trades with a first-seen signal: minutes from first seen to entry, and the price move in between."""
    seen = trades.get("first_seen", pd.Series(index=trades.index, dtype=object))
    has = seen.map(lambda f: isinstance(f, dict) and f.get("time") is not None)
    trades, seen = trades[has], seen[has]
    if trades.empty:
        return None
    price = seen.map(lambda f: f.get("price"))
    minutes = (trades["entry_time"] - seen.map(lambda f: f["time"])) / 60000
    return trades.assign(
        Delay=pd.cut(minutes.clip(lower=0), [-1] + [d for d, _ in DELAYS], labels=[label for _, label in DELAYS]),
        **{"Seen at": price,
           "Moved before entry %": ((trades["entry"] - price) / price * 100 * direction(trades)).round(2),
           "Delay cost ₹": ((trades["entry"] - price) * trades["qty"] * direction(trades)).round(2)})


def show_delay(history: pd.DataFrame) -> None:
    table = delay_table(history)
    if table is None:
        st.caption("No trades with a first-seen time yet.")
        return
    grouped = table.groupby("Delay", observed=True)
    st.dataframe(pd.DataFrame({
        "Trades": grouped.size(),
        "Win rate %": grouped["net"].apply(lambda n: round(100 * (n > 0).mean(), 1)),
        "Avg net ₹": grouped["net"].mean().round(2),
        "Avg moved before entry %": grouped["Moved before entry %"].mean().round(2),
        "Delay cost ₹": grouped["Delay cost ₹"].sum().round(2),
    }), width="stretch")
    st.caption("Delay: from when the signal was first seen (by this page or the scheduled runs) to the entry. "
               "Moved before entry: how far the price went the signal's way in that time; positive means a "
               "worse entry than at first sight, and Delay cost is what it cost in ₹.")


def show_results(history: pd.DataFrame) -> None:
    if history.empty:
        st.caption("No closed trades in this style yet.")
        return
    tabs = st.tabs(["By day", "By signal", "By strategy", "By exit", "Carry forward", "Entry delay", "Trades"])
    with tabs[0]:
        st.dataframe(summary(history, "Day"), width="stretch")
    with tabs[1]:
        counts = history.assign(**{"On timeframes": history["Timeframes"].str.count(r"\+") + 1})
        st.dataframe(summary(counts.sort_values("On timeframes"), "On timeframes"), width="stretch")
        st.caption("How many timeframes (2H, 4H, Daily) the stock was on, the same way, when it was traded.")
        st.dataframe(summary(history, "Timeframes"), width="stretch")
    with tabs[2]:
        strategies = history.assign(Strategy=history.get("strategies", pd.Series(index=history.index, dtype=object))
                                    .map(lambda s: s if isinstance(s, list) and s else ["—"])).explode("Strategy")
        st.dataframe(summary(strategies, "Strategy"), width="stretch")
        st.caption("A trade on stocks found by several strategies counts under each.")
    with tabs[3]:
        st.dataframe(summary(history, "reason"), width="stretch")
    with tabs[4]:
        show_carry(history)
    with tabs[5]:
        show_delay(history)
    with tabs[6]:
        trades = history.assign(Entered=history["entry_time"].map(when), Exited=history["exit_time"].map(when),
                                Symbol=history["symbol"].map(chart_link))
        st.dataframe(trades[["Exited", "Symbol", "side", "Style", "Timeframes", "qty", "entry", "exit", "reason",
                             "gross", "charges", "net", "Entered"]], hide_index=True, width="stretch",
                     column_config={"Symbol": SYMBOL_LINK})


def main() -> None:
    st.set_page_config(page_title="Auto Trader", page_icon="🤖", layout="wide")
    st.title("🤖 Auto trader")
    st.caption(
        f"Paper-trades every 2H, 4H and Daily Chartink signal, 1 lot, SL {fs.AUTO_SL_PCT}%, in three exit styles "
        f"side by side: **EOD** (target {fs.AUTO_TP_PCT}%, squared off at 3:15 PM), **Hold · trail** (held "
        f"overnight, trailing {fs.AUTO_TRAIL_PCT}% once the target is reached), **Hold · RSI exit** (held until "
        f"the RSI of its timeframe crosses back below 60 / above 40). Held trades close on expiry day at 3:15 PM. "
        f"A stock is traded once per side and style per day. Runs on GitHub: every {LIVE_EVERY} minutes while "
        f"this page is open in market hours, else every 15 minutes if the switch below is on.")

    account, error = load_account()
    top = st.columns([1, 1, 1, 3])
    if top[0].button("↻ Reload", width="stretch"):
        load_account.clear()
        st.rerun()
    if top[1].button("▶ Run now", width="stretch", help="Check the positions and take new signals now"):
        if dispatch("run"):
            st.toast("Started a run: it takes about a minute. Reload then.")
    if top[2].button("🧪 Test Chartink", width="stretch", help="Check that GitHub can read the Chartink "
                     "dashboard: the result shows in the Run log in about a minute"):
        if dispatch("test-chartink"):
            st.toast("Started a Chartink test: Reload in about a minute and open the Run log.")
    top[3].caption(f"[Runs on GitHub]({RUNS_URL}) · requests from here apply in about a minute.")
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
            "Side": "LONG" if p["side"] == "BUY" else "SHORT", "Style": fs.STYLES[p.get("style", "eod")],
            "Signal": p["source"], "Qty": p["qty"],
            "Entry": p["entry"], "LTP": (quotes.get(p["symbol"]) or {}).get("price"),
            "SL / TSL": p["stop"], "Target": p["tp"],
            "At target": "—" if p.get("style") == "rsi" else "Passed · trailing" if p.get("passed")
            else "Trail" if p.get("run") else "Exit",
            "Unrealized ₹": unrealized(p), "Entered": when(p["time"]), "Agrees": p.get("agrees"),
        } for p in positions])
        picked = st.dataframe(
            table, hide_index=True, width="stretch", on_select="rerun", selection_mode="single-row",
            column_config={"Symbol": SYMBOL_LINK, "Unrealized ₹": st.column_config.NumberColumn(format="%.2f")},
        )
        rows = picked.selection.rows
        if rows:
            position = positions[rows[0]]
            # Only an EOD position has a choice at its target.
            actions = {a: l for a, l in REQUESTS.items() if a == "exit" or position.get("style", "eod") == "eod"}
            cols = st.columns(len(REQUESTS))
            for col, (action, label) in zip(cols, actions.items()):
                if col.button(f"{label}: {position['symbol']}", key=f"{action}-{position['id']}", width="stretch"):
                    if dispatch(action, position["id"]):
                        st.toast(f"Sent: {label} for {position['symbol']}. Applies in about a minute; Reload then.")
        else:
            st.caption("Select a row to exit it now, or for an EOD one, to keep it going past its target "
                       "or set it back to exit there.")
    else:
        st.caption("None open.")

    st.subheader("Results")
    if len(history):
        history = history.sort_values("exit_time", ascending=False)
        history["Day"] = history["exit_time"].map(lambda ms: when(ms)[:6])
        history["Timeframes"] = history["source"].str.removeprefix("Auto ")
        history["Style"] = history.get("style", pd.Series("eod", index=history.index)).fillna("eod").map(fs.STYLES)
        st.dataframe(summary(history, "Style"), width="stretch")
        # Each signal is traded in every style, so the other results are for one style at a time.
        style = st.segmented_control("Results for", ["All styles", *fs.STYLES.values()], default="All styles")
        if style and style != "All styles":
            history = history[history["Style"] == style]
        show_results(history)
    else:
        st.caption("No closed trades yet.")

    with st.expander("Run log"):
        st.code("\n".join(account.get("log", [])) or "Nothing yet.", language=None)


main()
