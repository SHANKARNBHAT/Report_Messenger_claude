"""
Auto trader page: the account auto_trader.py trades on a schedule (see
.github/workflows/auto_trader.yml), read from the auto-trader-data branch.

While open in market hours, the page starts a run every LIVE_EVERY minutes, on
time (GitHub's schedule is often late), and the scheduled runs skip. A request on
a position (keep going past the target with a trailing SL, exit at the target,
exit now) or a setting also starts a run, with that request, so the account is
only ever changed by the workflow, one run at a time.

The results come from the branch's trade files (see auto_trader.py): the Auto
trader's (trades/), the simulator's (manual/) and the signal log (signals/).
"""

from __future__ import annotations

import time
from datetime import datetime
from pathlib import Path
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
# Series colours, in this fixed order (the default categorical palette of the dataviz
# skill): the three bot styles, then the simulator's trades.
SERIES_COLORS = ["#2a78d6", "#eb6834", "#1baf7a", "#eda100"]
YOU = "You (simulator)"
SHORT_TF = {"2 HOUR": "2H", "4 HOUR": "4H", "DAILY": "Daily", "WEEKLY": "Weekly"}
HOLDS = [(60, "< 1 h"), (180, "1–3 h"), (375, "3 h – same day"), (2 * 1440, "1–2 days"), (float("inf"), "3+ days")]
MOODS = [(-0.5, "NIFTY down > 0.5%"), (0.5, "NIFTY flat (±0.5%)"), (float("inf"), "NIFTY up > 0.5%")]
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


@st.cache_data(ttl=30, show_spinner=False)
def listing(folder: str) -> list[tuple[str, str]]:
    """(path, sha) of the JSON files in a folder of the branch."""
    response = requests.get(fs.DATA_URL.format(folder), headers=fs.github_headers(raw=False), timeout=15)
    if response.status_code == 404:
        return []
    response.raise_for_status()
    return [(item["path"], item["sha"]) for item in response.json() if item["name"].endswith(".json")]


@st.cache_data(show_spinner=False, max_entries=500)
def branch_file(path: str, sha: str):
    """A file of the branch; cached by its sha, so only changed files are fetched again."""
    response = fs.data_file(path)
    response.raise_for_status()
    return response.json()


def load_trades(folder: str) -> pd.DataFrame:
    try:
        rows = [trade for path, sha in listing(folder) for trade in branch_file(path, sha)]
    except (requests.RequestException, ValueError) as exc:
        st.warning(f"Could not load {folder}/: {exc}")
        rows = []
    return prepare(pd.DataFrame(rows))


def load_signals() -> pd.DataFrame:
    try:
        rows = [{**entry, "date": Path(path).stem} for path, sha in listing("signals")
                for entry in branch_file(path, sha).values()]
    except (requests.RequestException, ValueError) as exc:
        st.warning(f"Could not load the signal log: {exc}")
        rows = []
    return pd.DataFrame(rows)


COLUMNS = ["id", "symbol", "side", "qty", "entry", "exit", "entry_time", "exit_time", "reason", "source", "style",
           "timeframes", "strategies", "first_seen", "carried", "passed", "sl", "tp", "high", "low", "nifty", "agrees",
           "gross", "charges", "net"]


DERIVED = ["Date", "Week", "Exited at", "Style", "Timeframes", "Best %", "Worst %", "Made %", "Held", "Mood"]


def prepare(trades: pd.DataFrame) -> pd.DataFrame:
    """Closed trades with the columns the results use."""
    trades = trades.reindex(columns=list(dict.fromkeys(COLUMNS + list(trades.columns))))
    if trades.empty:
        return trades.reindex(columns=[*trades.columns, *DERIVED])
    trades = trades.sort_values("exit_time", ascending=False)
    exited = pd.to_datetime(trades["exit_time"], unit="ms", utc=True).dt.tz_convert("Asia/Kolkata")
    trades["Date"] = exited.dt.date
    trades["Week"] = (exited - pd.to_timedelta(exited.dt.weekday, unit="D")).dt.date
    trades["Exited at"] = exited.dt.tz_localize(None)
    trades["Style"] = trades["style"].fillna("eod").map(fs.STYLES)
    trades["Timeframes"] = [
        "+".join(SHORT_TF.get(tf, tf) for tf in tfs) if isinstance(tfs, list) and tfs else str(src).removeprefix("Auto ")
        for tfs, src in zip(trades["timeframes"], trades["source"])]
    sign = direction(trades)
    trades["Best %"] = ((trades["high"].where(sign > 0, trades["low"]) - trades["entry"]) / trades["entry"] * 100 * sign)
    trades["Worst %"] = ((trades["low"].where(sign > 0, trades["high"]) - trades["entry"]) / trades["entry"] * 100 * sign)
    trades["Made %"] = (trades["exit"] - trades["entry"]) / trades["entry"] * 100 * sign
    minutes = (trades["exit_time"] - trades["entry_time"]) / 60000
    trades["Held"] = pd.cut(minutes, [-1] + [h for h, _ in HOLDS], labels=[label for _, label in HOLDS])
    trades["Mood"] = pd.cut(pd.to_numeric(trades["nifty"], errors="coerce"), [float("-inf")] + [m for m, _ in MOODS],
                            labels=[label for _, label in MOODS])
    return trades


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


def summary(trades: pd.DataFrame, by: str | list[str], sort: bool = False) -> pd.DataFrame:
    grouped = trades.groupby(by, sort=sort, observed=True)
    table = pd.DataFrame({
        "Trades": grouped.size(),
        "Win rate %": grouped["net"].apply(lambda n: round(100 * (n > 0).mean(), 1)),
        "Net P&L ₹": grouped["net"].sum().round(2),
        "Avg per trade ₹": grouped["net"].mean().round(2),
    })
    return table


def drawdown(net: pd.Series) -> float:
    """Largest fall of the running P&L from a high, in time order."""
    running = net.cumsum()
    return float((running - running.cummax().clip(lower=0)).min())


def series(bot: pd.DataFrame, you: pd.DataFrame) -> pd.DataFrame:
    """The bot's trades by exit style and the simulator's, as one table with a "Series" column."""
    parts = [frame for frame in (bot.assign(Series=bot["Style"]), you.assign(Series=YOU)) if not frame.empty]
    return pd.concat(parts, ignore_index=True)


def margin_needed(trades: pd.DataFrame, positions: list[dict]) -> float:
    """The most margin the trades (and the open positions) held at the same time: what trading
    them all would have needed, with no capital limit."""
    trades = trades.dropna(subset=["entry", "qty", "entry_time", "exit_time"])
    events = [(t.entry_time, t.entry * t.qty * fs.MARGIN_RATE) for t in trades.itertuples()]
    events += [(t.exit_time, -t.entry * t.qty * fs.MARGIN_RATE) for t in trades.itertuples()]
    events += [(p["time"], p["entry"] * p["qty"] * fs.MARGIN_RATE) for p in positions]
    held = peak = 0.0
    for _, change in sorted(events):  # at the same time, exits (negative) first
        held += change
        peak = max(peak, held)
    return round(peak, 2)


def show_compare(both: pd.DataFrame, positions: list[dict]) -> None:
    """Each bot style and the simulator side by side: results, the money they needed,
    drawdown and the running P&L."""
    order = [name for name in [*fs.STYLES.values(), YOU] if name in set(both["Series"])]
    table = summary(both, "Series").reindex(order)
    table["Max drawdown ₹"] = [round(drawdown(both[both["Series"] == name].sort_values("exit_time")["net"]), 2)
                               for name in order]
    table["Margin needed ₹"] = [
        margin_needed(both[both["Series"] == name],
                      [] if name == YOU else [p for p in positions if fs.STYLES[p.get("style", "eod")] == name])
        for name in order]
    table["Return on margin %"] = (table["Net P&L ₹"] / table["Margin needed ₹"].where(table["Margin needed ₹"] > 0)
                                   * 100).round(2)
    table["Turnover ₹"] = [round((both[both["Series"] == name]["entry"] * both[both["Series"] == name]["qty"]).sum())
                           for name in order]
    st.dataframe(table, width="stretch")
    st.caption(f"Margin needed: the most margin ({fs.MARGIN_RATE:.0%} of the futures' value) the style's trades held "
               "at the same time, open positions included: the money to take every one of its signals, with no "
               "capital limit. Return on margin: net P&L on that. Turnover: the value of all the entries.")
    curve = (both.sort_values("exit_time").assign(Running=lambda t: t.groupby("Series")["net"].cumsum())
             .pivot_table(index="Exited at", columns="Series", values="Running", aggfunc="last")
             .reindex(columns=order).ffill())
    st.markdown("**Running net P&L ₹**, by exit time")
    st.line_chart(curve, color=[SERIES_COLORS[[*fs.STYLES.values(), YOU].index(name)] for name in order],
                  x_label="Exit time", y_label="Net P&L ₹")
    st.caption("Max drawdown: the largest fall of the running P&L from its high. Each signal is traded by the "
               "Auto trader in every style, so the styles compare on the same signals.")


def report(trades: pd.DataFrame, by: str, signals: pd.DataFrame | None = None) -> pd.DataFrame:
    """Daily or weekly report: results with the best and worst trade, and the signals seen and traded."""
    grouped = trades.groupby(by, observed=True)
    table = summary(trades, by, sort=True)
    table["Best trade ₹"] = grouped["net"].max().round(2)
    table["Worst trade ₹"] = grouped["net"].min().round(2)
    table["Longs / shorts"] = grouped["side"].apply(lambda s: f"{(s == 'BUY').sum()} / {(s == 'SELL').sum()}")
    if signals is not None and not signals.empty:
        days = pd.to_datetime(signals["date"]).dt.date
        key = days if by == "Date" else days.map(lambda d: d - pd.Timedelta(days=d.weekday()))
        counted = signals.assign(**{by: key}).groupby(by)
        table = table.join(pd.DataFrame({"Signals seen": counted.size(),
                                         "Signals traded": counted["status"].apply(lambda s: s.eq("Traded").sum())}),
                           how="outer")
    return table.sort_index(ascending=False)


def show_reach(trades: pd.DataFrame) -> None:
    ranged = trades.dropna(subset=["Best %", "Worst %"])
    if ranged.empty:
        st.caption("No trades with their best and worst price yet.")
        return
    stopped = ranged["reason"].str.contains("Stop loss", case=False, na=False) & (ranged["Best %"] >= fs.AUTO_SL_PCT)
    grouped = ranged.assign(Stopped=stopped).groupby("Style", sort=False)
    st.dataframe(pd.DataFrame({
        "Trades": grouped.size(),
        "Avg best %": grouped["Best %"].mean().round(2),
        "Avg worst %": grouped["Worst %"].mean().round(2),
        "Avg made %": grouped["Made %"].mean().round(2),
        "Avg given back %": (grouped["Best %"].mean() - grouped["Made %"].mean()).round(2),
        f"Stopped after +{fs.AUTO_SL_PCT}%": grouped["Stopped"].sum(),
    }), width="stretch")
    st.caption("Best and worst: the furthest the price went the trade's way (+) and against it (−) since entry, "
               "as % of the entry price. Given back: from the best to the exit. Stopped after +"
               f"{fs.AUTO_SL_PCT}%: stop losses hit after being up at least as much as the SL.")


def show_mood(trades: pd.DataFrame) -> None:
    moods = trades.dropna(subset=["Mood"])
    if moods.empty:
        st.caption("No trades with NIFTY's change at entry yet.")
        return
    st.dataframe(summary(moods.assign(Side=moods["side"].map({"BUY": "Longs", "SELL": "Shorts"})), ["Mood", "Side"],
                         sort=True), width="stretch")
    st.caption("NIFTY 50's change on the day when the trade was entered.")


def show_signals(signals: pd.DataFrame) -> None:
    """The Auto trader's signal log: what was done with each signal it saw, by day."""
    if signals.empty:
        st.caption("No signals logged yet: the runs log them in market hours.")
        return
    signals = signals.assign(status=signals["status"].fillna("Seen, not checked"))
    by_day = signals.pivot_table(index="date", columns="status", values="symbol", aggfunc="count", fill_value=0)
    by_day.insert(0, "Seen", by_day.sum(axis=1))
    st.dataframe(by_day.sort_index(ascending=False), width="stretch")
    st.caption("Each signal is a stock on a timeframe's widget, bullish or bearish, counted once a day. Traded: "
               "entered by the Auto trader; the other columns say why a signal was not traded.")
    day = st.selectbox("Signals of", sorted(signals["date"].unique(), reverse=True))
    shown = signals[signals["date"] == day].sort_values("time")
    st.dataframe(shown.assign(
        Symbol=shown["symbol"].map(chart_link), Timeframe=shown["timeframe"].map(lambda t: SHORT_TF.get(t, t)),
        **{"First seen": shown["time"].map(when), "Last seen": shown["last"].map(when)})[
        ["First seen", "Last seen", "Symbol", "Timeframe", "bias", "strategy", "price", "status"]],
        hide_index=True, width="stretch", column_config={"Symbol": SYMBOL_LINK})


def csv(frame: pd.DataFrame) -> bytes:
    return frame.drop(columns=["Exited at"], errors="ignore").to_csv(index=False).encode()


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


def show_results(history: pd.DataFrame, signals: pd.DataFrame | None) -> None:
    if history.empty:
        st.caption("No closed trades here yet.")
        return
    tabs = st.tabs(["Daily report", "Weekly report", "By signal", "By strategy", "By exit", "Price reached",
                    "Holding time", "Market mood", "Carry forward", "Entry delay", "Trades"])
    with tabs[0]:
        st.dataframe(report(history, "Date", signals), width="stretch")
    with tabs[1]:
        st.dataframe(report(history, "Week", signals).rename_axis("Week of"), width="stretch")
    with tabs[2]:
        counts = history.assign(**{"On timeframes": history["Timeframes"].str.count(r"\+") + 1})
        st.dataframe(summary(counts.sort_values("On timeframes"), "On timeframes"), width="stretch")
        st.caption("How many timeframes (2H, 4H, Daily) the stock was on, the same way, when it was traded.")
        st.dataframe(summary(history, "Timeframes"), width="stretch")
    with tabs[3]:
        strategies = history.assign(Strategy=history.get("strategies", pd.Series(index=history.index, dtype=object))
                                    .map(lambda s: s if isinstance(s, list) and s else ["—"])).explode("Strategy")
        st.dataframe(summary(strategies, "Strategy"), width="stretch")
        st.caption("A trade on stocks found by several strategies counts under each.")
    with tabs[4]:
        st.dataframe(summary(history, "reason"), width="stretch")
    with tabs[5]:
        show_reach(history)
    with tabs[6]:
        st.dataframe(summary(history, "Held", sort=True), width="stretch")
        st.caption("From entry to exit.")
    with tabs[7]:
        show_mood(history)
    with tabs[8]:
        show_carry(history)
    with tabs[9]:
        show_delay(history)
    with tabs[10]:
        trades = history.assign(Entered=history["entry_time"].map(when), Exited=history["exit_time"].map(when),
                                Symbol=history["symbol"].map(chart_link))
        st.dataframe(trades[["Exited", "Symbol", "side", "Style", "Timeframes", "qty", "entry", "exit", "high", "low",
                             "reason", "gross", "charges", "net", "Entered"]], hide_index=True, width="stretch",
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
        listing.clear()
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

    # Accounts from before the monthly trade files kept the closed trades in "history".
    history = load_trades("trades")
    if account.get("history"):
        history = prepare(pd.concat([history[COLUMNS], pd.DataFrame(account["history"])], ignore_index=True))
    you = load_trades("manual")
    signals = load_signals()
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
    stats[4].metric("Margin needed", f"₹{margin_needed(history, positions):,.0f}",
                    help=f"No capital limit: every signal is taken, in all three styles. This is the most margin "
                         f"({fs.MARGIN_RATE:.0%} of the futures' value) the trades held at the same time, open "
                         "positions included: the money to take them all. Each style alone needs less: see Results.")
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
    if len(history) or len(you):
        show_compare(series(history, you), positions)
        # Each signal is traded in every style, so the other results are for one style at a time.
        picked = st.segmented_control("Results for", ["All bot styles", *fs.STYLES.values(), YOU],
                                      default="All bot styles")
        if picked == YOU:
            show_results(you, None)
        else:
            show_results(history if picked in (None, "All bot styles") else history[history["Style"] == picked], signals)
    else:
        st.caption("No closed trades yet. The simulator's trades show here too once they are backed up.")

    st.subheader("Signals")
    show_signals(signals)

    downloads = st.columns(3)
    downloads[0].download_button("⬇ Auto trader trades (CSV)", csv(history), "auto_trader_trades.csv", "text/csv",
                                 width="stretch", disabled=history.empty)
    downloads[1].download_button("⬇ Simulator trades (CSV)", csv(you), "simulator_trades.csv", "text/csv",
                                 width="stretch", disabled=you.empty)
    downloads[2].download_button("⬇ Signal log (CSV)", signals.to_csv(index=False).encode(), "signals.csv",
                                 "text/csv", width="stretch", disabled=signals.empty)

    with st.expander("Run log"):
        st.code("\n".join(account.get("log", [])) or "Nothing yet.", language=None)


main()
