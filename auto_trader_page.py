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

import importlib
import time
from datetime import datetime
from pathlib import Path
from datetime import time as clock
from html import escape
from urllib.parse import quote

import pandas as pd
import plotly.graph_objects as go
import requests
import streamlit as st

import futures_simulator as fs
import ha_index
import ha_vp
import trade_report

# Streamlit Cloud keeps imported modules in memory when it pulls new code, so a
# futures_simulator.py changed since the server started is reloaded here.
if Path(fs.__file__).stat().st_mtime > getattr(fs, "_loaded_at", 0):
    fs = importlib.reload(fs)
    fs._loaded_at = Path(fs.__file__).stat().st_mtime
# ha_index uses ha_vp's rules: a changed ha_vp.py is reloaded first, and ha_index with it.
if Path(ha_vp.__file__).stat().st_mtime > getattr(ha_vp, "_loaded_at", 0):
    ha_vp = importlib.reload(ha_vp)
    ha_vp._loaded_at = Path(ha_vp.__file__).stat().st_mtime
    ha_index._loaded_at = 0
if Path(ha_index.__file__).stat().st_mtime > getattr(ha_index, "_loaded_at", 0):
    ha_index = importlib.reload(ha_index)
    ha_index._loaded_at = Path(ha_index.__file__).stat().st_mtime

from chartink_dashboard import CHART_SITES, MY_LAYOUT
from page_settings import remember

GITHUB_OWNER, GITHUB_REPO = "SHANKARNBHAT", "Report_Messenger_Claude"
DISPATCH_URL = (f"https://api.github.com/repos/{GITHUB_OWNER}/{GITHUB_REPO}/actions/workflows/"
                "auto_trader.yml/dispatches")
RUNS_URL = f"https://github.com/{GITHUB_OWNER}/{GITHUB_REPO}/actions/workflows/auto_trader.yml"
LIVE_EVERY = 5  # minutes between the runs the page starts while open
LIVE_HOURS = (clock(9, 15), clock(15, 35))  # IST, weekdays: through the 3:15 PM square-off
# Series colours, in this fixed order (the default categorical palette of the dataviz
# skill): the three bot styles, then the simulator's trades.
SERIES_COLORS = ["#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#9b59d0", "#7f8c8d"]
YOU = "You (simulator)"
# The stock strategies; HA IDX (NIFTY and BANKNIFTY) has a section of its own (show_index).
# HA and HA MTF take no new trades (HA VP replaced them), nor RSI's EOD and RSI exit styles
# (futures_simulator.RETIRED_STYLES): hidden from the results for now, their trades kept.
SHOW_RETIRED = False
# Retired on 18 Nov 2026 for their losses (auto_trader.RETIRED): HA pure 2H and Daily, HA breakout, HA VP.
RETIRED_SYSTEMS = {"HA": "Heikin Ashi (retired)", "HA MTF": "HA multi-timeframe (retired)",
                   "HA PURE 2H": "HA pure 2H (retired)", "HA PURE Daily": "HA pure Daily (retired)",
                   "HA BRK 2H": "HA breakout 2H (retired)", "HA BRK 4H": "HA breakout 4H (retired)",
                   "HA BRK Daily": "HA breakout Daily (retired)", "HA VP": "HA + volume profile (retired)",
                   "HA VP TRAIL": "HA VP · trail past target (retired)",
                   "HA PURE 4H": "HA pure 4H (retired)", "HA WK BRK": "HA weekly breakout (retired)"}
# RSI 2H and 4H take no new trades since 27 Nov 2026 (auto_trader.RETIRED), and RSI Weekly: hidden from the page
# (HIDDEN_SYSTEMS), as the others retired.
HIDDEN_SYSTEMS = {"RSI", "RSI 4H", "RSI Weekly"}
SYSTEM_NAMES = {"RSI Daily": "RSI Daily",
                **(RETIRED_SYSTEMS if SHOW_RETIRED else {})}
INDEX_SYSTEM = "HA IDX"
# The 15-minute index strategies (auto_trader.INDEX_SYSTEMS): name, and its rule (ha_index.rule_signal):
# "trend" with the SMA trend filter, "pure" without, "break" the HA breakout with the HA candle trail.
INDEX_SYSTEMS = {INDEX_SYSTEM: ("HA IDX · SMA trend", "trend"), "HA IDX PURE": ("HA IDX · no SMA", "pure"),
                 "HA IDX BRK": ("HA IDX · breakout", "break")}
SMA_FILTERS = ["Against: long below 50, short above 50", "With: long above 50, short below 50", "Not recorded"]
SHORT_TF = {"15 MIN": "15m", "2 HOUR": "2H", "4 HOUR": "4H", "DAILY": "Daily", "WEEKLY": "Weekly"}
HOLDS = [(60, "< 1 h"), (180, "1–3 h"), (375, "3 h – same day"), (2 * 1440, "1–2 days"), (float("inf"), "3+ days")]
MIN_TRADES = 3  # trades on a timeframe before it can be a stock's best one
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


def load_skips() -> pd.DataFrame:
    """The signals the simulator did not take for lack of margin (manual-skips/)."""
    try:
        rows = [skip for path, sha in listing("manual-skips") for skip in branch_file(path, sha)]
    except (requests.RequestException, ValueError) as exc:
        st.warning(f"Could not load manual-skips/: {exc}")
        rows = []
    return pd.DataFrame(rows, columns=["id", "time", "symbol", "side", "entry", "reason", "source"])


def load_signals() -> pd.DataFrame:
    try:
        rows = [{"system": "RSI", **entry, "date": Path(path).stem} for path, sha in listing("signals")
                for entry in branch_file(path, sha).values()]
    except (requests.RequestException, ValueError) as exc:
        st.warning(f"Could not load the signal log: {exc}")
        rows = []
    return pd.DataFrame(rows)


COLUMNS = ["id", "symbol", "side", "qty", "entry", "exit", "entry_time", "exit_time", "reason", "source", "system",
           "style", "timeframes", "strategies", "first_seen", "carried", "passed", "sl", "tp", "high", "low", "nifty", "agrees",
           "sma_rsi", "gross", "charges", "net"]


DERIVED = ["Date", "Week", "Exited at", "System", "Style", "Timeframes", "Best %", "Worst %", "Made %", "Held", "Mood"]


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
    trades["System"] = trades["system"].fillna("RSI")
    trades["Style"] = trades["style"].fillna("eod").map(fs.BOT_STYLES)
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


def chart_site_picker() -> None:
    """Where the Symbol links open (My layout, Chartink or TradingView): the same choice as on the
    Chartink dashboard page, kept across the pages."""
    st.radio("Open charts on", list(CHART_SITES), key="charts", horizontal=True,
             on_change=remember, args=("charts",))


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


def compare_table(both: pd.DataFrame, positions: list[dict], order: list[str]) -> pd.DataFrame:
    """Results of each series in `order`, with the money it needed and its drawdown."""
    table = summary(both, "Series").reindex(order)
    table["Max drawdown ₹"] = [round(drawdown(both[both["Series"] == name].sort_values("exit_time")["net"]), 2)
                               for name in order]
    table["Margin needed ₹"] = [
        margin_needed(both[both["Series"] == name],
                      [] if name == YOU else [p for p in positions if series_of(p) == name])
        for name in order]
    table["Return on margin %"] = (table["Net P&L ₹"] / table["Margin needed ₹"].where(table["Margin needed ₹"] > 0)
                                   * 100).round(2)
    table["Turnover ₹"] = [round((both[both["Series"] == name]["entry"] * both[both["Series"] == name]["qty"]).sum())
                           for name in order]
    return table


def series_of(position: dict) -> str:
    return fs.BOT_STYLES[position.get("style", "eod")]


def style_names(system: str) -> list[str]:
    return [fs.BOT_STYLES[style] for style in
            (*fs.SYSTEMS[system], *(fs.RETIRED_STYLES.get(system, ()) if SHOW_RETIRED else ()))]


def show_versus(history: pd.DataFrame, positions: list[dict]) -> None:
    """The strategies side by side, in each exit style."""
    rows = []
    for system, name in SYSTEM_NAMES.items():
        trades = history[history["System"] == system]
        held = [p for p in positions if p.get("system", "RSI") == system]
        order = [style for style in style_names(system) if style in set(trades["Style"])]
        if order:
            table = compare_table(trades.assign(Series=trades["Style"]), held, order).reset_index(names="Style")
            rows.append(table.assign(Strategy=name))
    if rows:
        table = pd.concat(rows, ignore_index=True).set_index(["Strategy", "Style"])
        st.dataframe(table, width="stretch")
    else:
        st.caption("No closed trades in either strategy yet.")


# The strategies started (or changed: RSI's 30-minute RSI exit) on 16 Nov 2026, tracked apart from
# that day on, by the trades entered since.
NEW_SINCE = datetime(2026, 11, 16, tzinfo=fs.IST)
NEW_STRATEGIES = {"HA IDX PURE": "HA IDX · no SMA (15m index)",
                  "RSI Daily": "RSI Daily",
                  "HA IDX BRK": "HA IDX · breakout (15m index)"}


def show_new(history: pd.DataFrame, positions: list[dict]) -> None:
    """The NEW_STRATEGIES, in each exit style, by the trades entered from NEW_SINCE: closed and open."""
    since = NEW_SINCE.timestamp() * 1000
    trades = history[pd.to_numeric(history["entry_time"], errors="coerce") >= since]
    held = [p for p in positions if p.get("system", "RSI") in NEW_STRATEGIES and p["time"] >= since]
    instruments = {i["symbol"]: i for i in fs.load_instruments()}
    quotes = fs.latest_prices({p["symbol"] for p in held}, instruments, None) if held else {}

    def open_pnl(p):
        q = quotes.get(p["symbol"])
        return (q["price"] - p["entry"]) * p["qty"] * (1 if p["side"] == "BUY" else -1) if q else 0.0

    rows = []
    for system, name in NEW_STRATEGIES.items():
        for style in fs.SYSTEMS[system]:
            label = fs.BOT_STYLES[style]
            done = trades[(trades["System"] == system) & (trades["style"].fillna("eod") == style)]
            live = [p for p in held if p.get("system", "RSI") == system and p.get("style", "eod") == style]
            if done.empty and not live:
                continue
            net = float(done["net"].sum())
            unrealized = sum(open_pnl(p) for p in live)
            rows.append({"Strategy": name, "Style": label, "Closed": len(done),
                         "Win rate %": round(100 * (done["net"] > 0).mean(), 1) if len(done) else None,
                         "Net P&L closed ₹": round(net), "Open": len(live), "Open P&L ₹": round(unrealized),
                         "Total ₹": round(net + unrealized)})
    if not rows:
        st.caption(f"No trades in the new strategies since {NEW_SINCE:%d %b} yet.")
        return
    table = pd.DataFrame(rows).set_index(["Strategy", "Style"])
    st.dataframe(table, width="stretch",
                 column_config={c: st.column_config.NumberColumn(format="%d") for c in
                                ("Net P&L closed ₹", "Open P&L ₹", "Total ₹")})
    st.caption(f"Trades entered from {NEW_SINCE:%d %b %Y} only, 1 lot each, after charges (closed) and at the LTP "
               "(open). HA pure: the HA entry pattern without the SMA trend on 4H (2H and Daily retired), SL trailed on "
               "30-minute candles, out at the first HA doji, held overnight. HA IDX · no SMA: the same on 15-minute "
               "NIFTY / BANKNIFTY, intraday, only with the previous 4H HA candle solid green / red (also on the "
               "Autotrader INDEX\\FUTURES tab). HA breakout: a wickless HA "
               "candle (green no lower wick / red no upper wick) closing past the previous HA high / low, no SMA "
               "filter, SL trailed to the previous HA candle's low / high, no other exit, on 15-minute NIFTY / "
               "BANKNIFTY intraday with the same 4H filter (the 2H / 4H / Daily stock version retired). RSI: with the 30-minute RSI "
               "SMA exit (below 40 long, above 60 short) added this day. HA weekly breakout (from 18 Nov): last week's Weekly HA "
               "candle red (green), armed when a 4H HA close goes above its HA high (below its HA low), entered on a "
               "pullback to the 30-minute SMA 8; SL the 30-minute candle's low (high), at least 1% away, moved to "
               "breakeven at +1R and then trailed on 30-minute lows (highs); held to expiry.")


def show_sma_filter(trades: pd.DataFrame) -> None:
    """Heikin Ashi results by how the trade stood against the SMA of its timeframe's RSI at entry."""
    gap = (pd.to_numeric(trades["sma_rsi"], errors="coerce") - 50) * direction(trades)
    rows = trades.assign(Filter=pd.Categorical(
        gap.map(lambda g: SMA_FILTERS[0] if g < 0 else SMA_FILTERS[1] if g > 0 else SMA_FILTERS[2]), SMA_FILTERS))
    table = summary(rows, ["Filter", "Style"], sort=True)
    table.insert(2, "Avg return %", rows.groupby(["Filter", "Style"], observed=True)["Made %"].mean().round(2))
    st.dataframe(table, width="stretch")
    st.caption("The SMA(14) of the RSI(14) of the signal's timeframe at entry. Against: the idea of trading against "
               "it. With: the usual trend filter. Same signals, split by this one reading.")


def show_compare(both: pd.DataFrame, positions: list[dict], styles: list[str]) -> None:
    """Each bot style and the simulator side by side: results, the money they needed,
    drawdown and the running P&L."""
    order = [name for name in [*styles, YOU] if name in set(both["Series"])]
    st.dataframe(compare_table(both, positions, order), width="stretch")
    st.caption(f"Margin needed: the most margin ({fs.MARGIN_RATE:.0%} of the futures' value) the style's trades held "
               "at the same time, open positions included: the money to take every one of its signals, with no "
               "capital limit. Return on margin: net P&L on that. Turnover: the value of all the entries.")
    curve = (both.sort_values("exit_time").assign(Running=lambda t: t.groupby("Series")["net"].cumsum())
             .pivot_table(index="Exited at", columns="Series", values="Running", aggfunc="last")
             .reindex(columns=order).ffill())
    st.markdown("**Running net P&L ₹**, by exit time")
    st.line_chart(curve, color=[SERIES_COLORS[[*styles, YOU].index(name)] for name in order],
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


def show_stocks(trades: pd.DataFrame) -> None:
    """Each stock's results on each timeframe it was signalled on, and the timeframe that did best."""
    rows = trades.assign(Timeframe=trades["timeframes"].map(
        lambda tfs: [SHORT_TF.get(tf, tf) for tf in tfs] if isinstance(tfs, list) and tfs else ["No signal"]))
    rows = rows.explode("Timeframe")
    grouped = rows.groupby(["symbol", "Timeframe"])
    stats = pd.DataFrame({
        "trades": grouped.size(),
        "win %": grouped["net"].apply(lambda n: round(100 * (n > 0).mean())),
        "avg %": grouped["Made %"].mean().round(2),
    }).reset_index()
    order = [tf for tf in [*SHORT_TF.values(), "No signal"] if tf in set(stats["Timeframe"])]
    table = stats.pivot(index="symbol", columns="Timeframe")
    table.columns = [f"{tf} {measure}" for measure, tf in table.columns]
    table = table[[f"{tf} {m}" for tf in order for m in ("avg %", "win %", "trades")]]
    # The timeframe(s) with the highest avg %: ties (often the same trades, on several timeframes) are joined.
    ranked = stats[stats["trades"] >= MIN_TRADES]
    top = ranked.groupby("symbol")["avg %"].transform("max")
    leaders = ranked[ranked["avg %"] >= top - 0.005]
    table.insert(0, "Best timeframe", leaders.groupby("symbol")["Timeframe"].agg(
        lambda tfs: " = ".join(sorted(tfs, key=order.index))))
    table.insert(1, "Best avg %", leaders.groupby("symbol")["avg %"].max())
    table.insert(2, "Trades", rows.groupby("symbol")["id"].nunique())
    table = table.sort_values(["Best avg %", "Trades"], ascending=False)
    table.index = table.index.map(chart_link)
    st.dataframe(table.rename_axis("Symbol").reset_index(), hide_index=True, width="stretch",
                 column_config={"Symbol": SYMBOL_LINK, **{c: st.column_config.NumberColumn(c, format="%.2f%%")
                                for c in table.columns if c.endswith("avg %")},
                                **{c: st.column_config.NumberColumn(c, format="%d%%")
                                   for c in table.columns if c.endswith("win %")},
                                **{c: st.column_config.NumberColumn(c, format="%d")
                                   for c in table.columns if c.endswith("trades")}})
    st.caption(f"avg %: the average return of the trades, as % of the entry price (lot sizes differ, so ₹ would "
               f"favour the big ones); win %: trades in profit after charges. Best timeframe: the one with the "
               f"highest avg %, among those with at least {MIN_TRADES} trades ('2H = 4H': the same; often the same "
               "trades, as a trade on stocks found on several timeframes counts under each). With All bot styles, each signal counts once per exit style: pick "
               "one style for one trade per signal.")
    by_tf = summary(rows, "Timeframe").reindex([tf for tf in order])
    by_tf.insert(2, "Avg return %", rows.groupby("Timeframe")["Made %"].mean().round(2))
    st.markdown("**All stocks, by timeframe**")
    st.dataframe(by_tf, width="stretch")


REPORTS = {"Yes": "Market reports agree", "No": "Market reports disagree", "Not on watchlist": "Not on the watchlist"}


def show_confirmation(trades: pd.DataFrame) -> None:
    """Results by the two confirmations of a signal: the Market reports' Active Watchlist with the
    same bias, and more Chartink timeframes with the same signal."""
    count = trades["timeframes"].map(lambda tfs: len(tfs) if isinstance(tfs, list) else 0)
    rows = trades.assign(
        Reports=pd.Categorical(trades["agrees"].map(REPORTS).fillna("Not recorded"),
                               [*REPORTS.values(), "Not recorded"]),
        Timeframes=pd.Categorical(count.map(lambda n: f"On {n} timeframe{'s' if n > 1 else ''}" if n else "No signal"),
                                  [f"On {n} timeframe{'s' if n > 1 else ''}" for n in (1, 2, 3, 4)] + ["No signal"]))
    both = rows[(rows["agrees"] == "Yes") & (count >= 2)]
    neither = rows[(rows["agrees"] != "Yes") & (count == 1)]
    cols = st.columns(2)
    for col, label, group in ((cols[0], "Both: Market reports agree, 2+ timeframes", both),
                              (cols[1], "Neither: not confirmed, 1 timeframe", neither)):
        col.metric(label, f"{100 * (group['net'] > 0).mean():.0f}% win · {group['Made %'].mean():+.2f}% avg"
                   if len(group) else "—", help=f"{len(group)} trades")

    def table(by):
        t = summary(rows, by, sort=True)
        t.insert(2, "Avg return %", rows.groupby(by, observed=True)["Made %"].mean().round(2))
        return t

    st.dataframe(table(["Reports", "Timeframes"]), width="stretch")
    left, right = st.columns(2)
    left.dataframe(table("Reports"), width="stretch")
    right.dataframe(table("Timeframes"), width="stretch")
    st.caption("Market reports agree: the stock was on the Active Watchlist with the same bias when it was traded "
               "(the COMMON tab of the Chartink dashboard). Timeframes: on how many of 2H, 4H and Daily it was "
               "signalled the same way. Avg return: % of the entry price.")


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
        ["First seen", "Last seen", "Symbol", "Timeframe", "bias", "system", "strategy", "price", "status"]],
        hide_index=True, width="stretch", column_config={"Symbol": SYMBOL_LINK})


def ist_date(ms: pd.Series) -> pd.Series:
    return pd.to_datetime(ms, unit="ms", utc=True).dt.tz_convert("Asia/Kolkata").dt.date


def show_decisions(bot: pd.DataFrame, you: pd.DataFrame, skips: pd.DataFrame) -> None:
    """Your simulator trades against the Auto trader's trades on the same signals (stock, side,
    day): the signals you took against those you passed on, and your timing."""
    if you.empty and skips.empty:
        st.caption("Your simulator trades show here once they are backed up: which signals you took and passed "
                   "on, compared with the Auto trader's trades on the same signals.")
        return
    style = st.selectbox("Judge the signals by the Auto trader's trades in", list(fs.STYLES.values()), index=1,
                         help="The Auto trader trades every signal in each style; its trades in this one tell "
                              "how good each signal was.")
    ref = bot[bot["Style"] == style].assign(day=lambda t: ist_date(t["entry_time"]))
    ref = ref.sort_values("entry_time").drop_duplicates(["day", "symbol", "side"])
    yours = you.assign(day=ist_date(you["entry_time"])) if len(you) else you.assign(day=None)
    skipped = skips.assign(day=ist_date(skips["time"])) if len(skips) else skips.assign(day=None)
    # Only the days you traded (or tried to): the signals of other days were not your decision.
    days = set(yours["day"].dropna()) | set(skipped["day"].dropna())
    signals = ref[ref["day"].isin(days)]
    if signals.empty:
        st.caption(f"No Auto trader trades in {style} on the days you traded yet.")
        return
    key = lambda t: list(zip(t["day"], t["symbol"], t["side"]))
    taken, no_funds = set(key(yours)), set(key(skipped))
    signals = signals.assign(Decision=[
        "Taken" if k in taken else "Skipped: not enough margin" if k in no_funds else "Skipped"
        for k in key(signals)])
    grouped = signals.groupby("Decision")
    table = pd.DataFrame({
        "Signals": grouped.size(),
        "Bot win rate %": grouped["net"].apply(lambda n: round(100 * (n > 0).mean(), 1)),
        "Bot avg return %": grouped["Made %"].mean().round(2),
        "Bot net P&L ₹": grouped["net"].sum().round(2),
    }).reindex(["Taken", "Skipped: not enough margin", "Skipped"]).dropna(how="all")
    st.dataframe(table, width="stretch")
    st.caption(f"The Auto trader's results ({style}) on the signals of the days you traded: did you take the "
               "better ones? Skipped: not enough margin: signals the simulator could not afford.")

    matched = yours.merge(ref[["day", "symbol", "side", "entry_time", "entry", "Made %"]],
                          on=["day", "symbol", "side"], suffixes=("", " bot"))
    if len(matched):
        sign = direction(matched)
        matched = matched.assign(**{
            "Later by min": ((matched["entry_time"] - matched["entry_time bot"]) / 60000).round(),
            "Entry vs bot %": ((matched["entry"] - matched["entry bot"]) / matched["entry bot"] * 100 * sign).round(2),
            "Your return %": matched["Made %"].round(2), "Bot return %": matched["Made % bot"].round(2)})
        cols = st.columns(4)
        cols[0].metric("Your trades on bot signals", len(matched))
        cols[1].metric("Entered later than the bot by", f"{matched['Later by min'].median():.0f} min",
                       help="Median; negative: earlier")
        cols[2].metric("Entry price vs the bot", f"{matched['Entry vs bot %'].mean():+.2f}%",
                       help="Average, the signal's way: positive is a worse entry than the bot's")
        cols[3].metric("Your return vs the bot's", f"{matched['Your return %'].mean():+.2f}%",
                       delta=f"{matched['Your return %'].mean() - matched['Bot return %'].mean():+.2f}% vs bot",
                       help=f"Average return per trade, % of entry: yours against the bot's in {style}")
        st.dataframe(matched.assign(Symbol=matched["symbol"].map(chart_link), Entered=matched["entry_time"].map(when),
                                    Style=matched["Style"])[
            ["Entered", "Symbol", "side", "Style", "Later by min", "Entry vs bot %", "Your return %", "Bot return %",
             "net"]].sort_values("Entered", ascending=False),
            hide_index=True, width="stretch", column_config={"Symbol": SYMBOL_LINK})
    unmatched = len(yours) - len(matched)
    if unmatched > 0:
        st.caption(f"{unmatched} of your trades had no Auto trader trade in {style} on the same stock, side and day.")


def closed_report(history: pd.DataFrame) -> bytes:
    """The closed trades report (trade_report.workbook), made again only when the trades change."""
    key = f"{len(history)}-{history['exit_time'].max()}-{history['net'].sum():.2f}"
    return _closed_report(key, history)


@st.cache_data(show_spinner=False, max_entries=2)
def _closed_report(key: str, _history: pd.DataFrame) -> bytes:
    return trade_report.workbook(_history, fs.BOT_STYLES)


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
    tabs = st.tabs(["Daily report", "Weekly report", "By stock", "Confirmation", "By signal", "By strategy", "By exit",
                    "Price reached", "Holding time", "Market mood", "Carry forward", "Entry delay", "Trades"])
    with tabs[0]:
        st.dataframe(report(history, "Date", signals), width="stretch")
    with tabs[1]:
        st.dataframe(report(history, "Week", signals).rename_axis("Week of"), width="stretch")
    with tabs[2]:
        show_stocks(history)
    with tabs[3]:
        show_confirmation(history)
    with tabs[4]:
        counts = history.assign(**{"On timeframes": history["Timeframes"].str.count(r"\+") + 1})
        st.dataframe(summary(counts.sort_values("On timeframes"), "On timeframes"), width="stretch")
        st.caption("How many timeframes (2H, 4H, Daily) the stock was on, the same way, when it was traded.")
        st.dataframe(summary(history, "Timeframes"), width="stretch")
    with tabs[5]:
        strategies = history.assign(Strategy=history.get("strategies", pd.Series(index=history.index, dtype=object))
                                    .map(lambda s: s if isinstance(s, list) and s else ["—"])).explode("Strategy")
        st.dataframe(summary(strategies, "Strategy"), width="stretch")
        st.caption("A trade on stocks found by several strategies counts under each.")
    with tabs[6]:
        st.dataframe(summary(history, "reason"), width="stretch")
    with tabs[7]:
        show_reach(history)
    with tabs[8]:
        st.dataframe(summary(history, "Held", sort=True), width="stretch")
        st.caption("From entry to exit.")
    with tabs[9]:
        show_mood(history)
    with tabs[10]:
        show_carry(history)
    with tabs[11]:
        show_delay(history)
    with tabs[12]:
        trades = history.assign(Entered=history["entry_time"].map(when), Exited=history["exit_time"].map(when),
                                Symbol=history["symbol"].map(chart_link))
        st.dataframe(trades[["Exited", "Symbol", "side", "Style", "Timeframes", "qty", "entry", "exit", "high", "low",
                             "reason", "gross", "charges", "net", "Entered"]], hide_index=True, width="stretch",
                     column_config={"Symbol": SYMBOL_LINK})


def show_scoreboard(history: pd.DataFrame, positions: list[dict], unrealized) -> None:
    """Each stock strategy's results in a row, and all together."""
    today = datetime.now(fs.IST).date()
    groups = [(name, history[history["System"] == system], [p for p in positions if p.get("system", "RSI") == system])
              for system, name in SYSTEM_NAMES.items()]
    shown = [s for s in SYSTEM_NAMES]
    groups.append(("All", history[history["System"].isin(shown)], [p for p in positions if p.get("system", "RSI") in shown]))
    rows = []
    for name, closed, held in groups:
        net = float(closed["net"].sum()) if len(closed) else 0.0
        open_pnl = sum(v for v in map(unrealized, held) if v is not None)
        rows.append({
            "Strategy": name,
            "Net P&L closed ₹": net,
            "Open P&L ₹": open_pnl,
            "Total ₹": net + open_pnl,
            "Today closed ₹": float(closed.loc[closed["Date"] == today, "net"].sum()) if len(closed) else 0.0,
            "Trades closed": len(closed),
            "Win rate %": round(100 * (closed["net"] > 0).mean(), 1) if len(closed) else None,
            "Avg per trade ₹": round(net / len(closed), 2) if len(closed) else None,
            "Open positions": len(held),
            "Margin needed ₹": margin_needed(closed, held),
        })
    # An HTML table, for headings in bold and a size up (st.dataframe cannot style them).
    def cell(column, value):
        if value is None or (isinstance(value, float) and pd.isna(value)):
            return "—"
        if column.endswith("₹"):
            return f"₹{value:,.0f}"
        return f"{value:.1f}%" if column.endswith("%") else f"{value:,}"

    columns = list(rows[0])
    head = "".join(f"<th>{escape(c)}</th>" for c in columns)
    body = "".join(
        f"<tr{' class=all' if row['Strategy'] == 'All' else ''}><th>{escape(row['Strategy'])}</th>"
        + "".join(f"<td>{cell(c, row[c])}</td>" for c in columns[1:]) + "</tr>"
        for row in rows)
    st.html(f"""<style>
      .scoreboard {{ overflow-x: auto; margin-bottom: 0.5rem; }}
      .scoreboard table {{ border-collapse: collapse; width: 100%; font-size: 0.95rem; }}
      .scoreboard th, .scoreboard td {{ padding: 0.45rem 0.7rem; text-align: right; white-space: nowrap;
        border-bottom: 1px solid rgba(128, 128, 128, 0.25); }}
      .scoreboard thead th {{ font-weight: 700; font-size: 1rem; border-bottom: 2px solid rgba(128, 128, 128, 0.5); }}
      .scoreboard tbody th {{ text-align: left; font-weight: 700; font-size: 1.05rem; }}
      .scoreboard thead th:first-child {{ text-align: left; }}
      .scoreboard tr.all td, .scoreboard tr.all th {{ font-weight: 700; border-top: 2px solid rgba(128, 128, 128, 0.5); }}
    </style><div class="scoreboard"><table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table></div>""")


INDEX_CHART_RANGE = "1mo"  # days to pick from; SMA 200 of the 15-minute closes needs about 8 sessions
CANDLE_UP, CANDLE_DOWN = "#1a9e6f", "#d64545"
ENTRY_COLOR, STOP_COLOR, SIGNAL_COLOR = "#2a78d6", "#eb6834", "#eda100"


def stop_path(trade: dict, rows: list[list], atrs: list, until: float, ha: list | None = None) -> tuple[list, list]:
    """Times and levels of a trade's SL: from the entry, moved at each 15-minute close after it
    (close -/+ ATR_MULT x ATR, never back), as ha_index.replay, until `until` (seconds). With the HA
    candles `ha` (HA IDX BRK), to each finished HA candle's low (long) / high (short) instead."""
    long = trade["side"] == "BUY"
    start = trade["entry_time"] / 1000
    stop = float(trade["sl"])
    times, levels = [start], [stop]
    for i, (row, value) in enumerate(zip(rows, atrs)):
        end = row[0] + ha_index.MINUTES * 60
        if end <= start or end > until or (value is None and ha is None):
            continue
        if ha is not None:
            level = ha[i][2] if long else ha[i][1]
        else:
            level = row[4] - ha_index.ATR_MULT * value if long else row[4] + ha_index.ATR_MULT * value
        if level > stop if long else level < stop:
            stop = level
        times.append(end)
        levels.append(stop)
    times.append(until)
    levels.append(stop)
    return times, levels


def show_index_chart(trades: pd.DataFrame, positions: list[dict], symbol: str = "NIFTY") -> None:
    """The 15-minute Heikin Ashi (or normal) candles of NIFTY and BANKNIFTY (ha_index, from Yahoo's 5-minute bars),
    with the index trades of the day and their SL."""
    st.markdown("**15-minute chart**")
    instruments = {i["symbol"]: i for i in fs.load_instruments()}
    _, middle, right = st.columns([1, 1, 1])
    request = (instruments[symbol]["yahoo"], INDEX_CHART_RANGE)
    bars = fs.fetch_bars((request,)).get(request)
    if not isinstance(bars, list) or not bars:
        st.caption(f"No 5-minute bars for {symbol} from Yahoo right now ({bars}).")
        return
    rows = ha_index.candles(bars, fs.IST)
    ha, atrs = ha_index.heikin_ashi(rows), ha_index.atr(rows)
    days = sorted({datetime.fromtimestamp(r[0], fs.IST).date() for r in rows}, reverse=True)
    day = right.selectbox("Day", days, format_func=lambda d: f"{d:%a %d %b %Y}", key="index-chart-day")
    style = middle.segmented_control("Candles", ["Heikin Ashi", "Normal"], default="Heikin Ashi",
                                     key="index-chart-candles") or "Heikin Ashi"
    drawn = ha if style == "Heikin Ashi" else [r[1:5] for r in rows]
    shown = [i for i, r in enumerate(rows) if datetime.fromtimestamp(r[0], fs.IST).date() == day]
    if not shown:
        st.caption("No candles that day.")
        return

    def at(seconds):
        return datetime.fromtimestamp(seconds, fs.IST).replace(tzinfo=None)

    times = [at(rows[i][0]) for i in shown]
    figure = go.Figure(go.Candlestick(
        x=times, open=[drawn[i][0] for i in shown], high=[drawn[i][1] for i in shown], low=[drawn[i][2] for i in shown],
        close=[drawn[i][3] for i in shown], name="HA candle" if drawn is ha else "Candle",
        increasing=dict(line=dict(color=CANDLE_UP, width=1), fillcolor=CANDLE_UP),
        decreasing=dict(line=dict(color=CANDLE_DOWN, width=1), fillcolor=CANDLE_DOWN)))

    # The trades of the day, closed and open, with their SL path.
    now = datetime.now(fs.IST).timestamp()
    mine = [t for t in trades[trades["symbol"] == symbol].to_dict("records")
            if datetime.fromtimestamp(t["entry_time"] / 1000, fs.IST).date() == day]
    mine += [{**p, "entry_time": p["time"], "exit_time": None} for p in positions
             if p["symbol"] == symbol and datetime.fromtimestamp(p["time"] / 1000, fs.IST).date() == day]
    for n, t in enumerate(mine):
        long = t["side"] == "BUY"
        until = t["exit_time"] / 1000 if t.get("exit_time") else min(now, rows[shown[-1]][0] + ha_index.MINUTES * 60)
        breakout = INDEX_SYSTEMS.get(t.get("system"), ("", ""))[1] == "break"  # HA candle trail, else ATR
        path_t, path_y = stop_path(t, rows, atrs, until, ha if breakout else None)
        figure.add_trace(go.Scatter(
            x=[at(s) for s in path_t], y=path_y, mode="lines", line=dict(color=STOP_COLOR, width=2, dash="dot",
                                                                         shape="hv"),
            name="Stop", legendgroup="stop", showlegend=n == 0,
            hovertemplate="Stop %{y:,.2f}<br>%{x|%H:%M}<extra></extra>"))
        figure.add_trace(go.Scatter(
            x=[at(t["entry_time"] / 1000)], y=[t["entry"]], mode="markers", name="Entry", legendgroup="entry",
            showlegend=n == 0, marker=dict(symbol="triangle-up" if long else "triangle-down", size=13,
                                          color=ENTRY_COLOR, line=dict(width=1, color="white")),
            hovertemplate=f"{'Buy' if long else 'Short'} at %{{y:,.2f}}<br>%{{x|%H:%M}}<extra></extra>"))
        if t.get("exit_time"):
            figure.add_trace(go.Scatter(
                x=[at(t["exit_time"] / 1000)], y=[t["exit"]], mode="markers", name="Exit", legendgroup="exit",
                showlegend=not any(m.get("exit_time") for m in mine[:n]),
                marker=dict(symbol="x", size=11, color=ENTRY_COLOR, line=dict(width=1, color="white")),
                hovertemplate=f"Exit at %{{y:,.2f}} ({t['reason']}, net ₹{t['net']:,.0f})<br>%{{x|%H:%M}}<extra></extra>"))

    figure.update_layout(height=440, margin=dict(l=8, r=8, t=8, b=8), xaxis_rangeslider_visible=False,
                         legend=dict(orientation="h", yanchor="bottom", y=1.01, x=0), hovermode="closest",
                         yaxis=dict(tickformat=",.0f", gridcolor="rgba(128,128,128,0.15)"),
                         xaxis=dict(tickformat="%H:%M", gridcolor="rgba(128,128,128,0.10)"))
    st.plotly_chart(figure, width="stretch", key="index-chart")
    st.caption(f"{symbol} spot, 15-minute {'Heikin Ashi' if drawn is ha else 'normal'} candles from Yahoo's 5-minute bars, with the index trades "
               "of the day. Dotted line: each trade's stop.")


def show_index(trades: pd.DataFrame, positions: list[dict], unrealized, quotes: dict | None = None,
               symbol: str = "NIFTY") -> None:
    """The NIFTY and BANKNIFTY trades of the index strategies (INDEX_SYSTEMS, all retired on 24 Nov 2026):
    totals, open positions, the 15-minute chart and the closed trades, apart from the stock strategies."""
    def strategy(system):
        return INDEX_SYSTEMS.get(system, (system, ""))[0]

    today = datetime.now(fs.IST).date()
    open_pnl = sum(v for v in map(unrealized, positions) if v is not None)
    stats = st.columns(5)
    stats[0].metric("Net P&L (closed)", f"₹{trades['net'].sum():,.0f}")
    stats[1].metric("Today (closed)", f"₹{trades.loc[trades['Date'] == today, 'net'].sum():,.0f}")
    stats[2].metric("Open P&L", f"₹{open_pnl:,.0f}")
    stats[3].metric("Trades closed", len(trades))
    stats[4].metric("Win rate", f"{100 * (trades['net'] > 0).mean():.1f}%" if len(trades) else "—")

    st.markdown(f"**Open ({len(positions)})**")
    if positions:
        table = pd.DataFrame([{
            "Symbol": chart_link(p["symbol"]), "Strategy": strategy(p.get("system")),
            "Side": "LONG" if p["side"] == "BUY" else "SHORT", "Qty": p["qty"],
            "Entry": p["entry"], "LTP": ((quotes or {}).get(p["symbol"]) or {}).get("price"),
            "Prev close": ((quotes or {}).get(p["symbol"]) or {}).get("prev"),
            "Initial SL": p["sl"], "Trailing SL": p.get("stop"),
            "Unrealized ₹": unrealized(p), "Entered": when(p["time"]),
        } for p in positions])
        picked = st.dataframe(table, hide_index=True, width="stretch", on_select="rerun",
                              selection_mode="single-row", key="index-open",
                              column_config={"Symbol": SYMBOL_LINK,
                                             "Unrealized ₹": st.column_config.NumberColumn(format="%.2f")})
        if picked.selection.rows:
            position = positions[picked.selection.rows[0]]
            label = REQUESTS["exit"]
            if st.button(f"{label}: {position['symbol']}", key=f"index-exit-{position['id']}"):
                if dispatch("exit", position["id"]):
                    st.toast(f"Sent: {label} for {position['symbol']}. Applies in about a minute; Reload then.")
        else:
            st.caption("Select a row to exit it now.")
    else:
        st.caption("None open.")

    show_index_chart(trades, positions, symbol)

    if trades.empty:
        st.caption("No closed index trades yet.")
        return
    tabs = st.tabs(["Trades", "By day", "By index"])
    with tabs[0]:
        shown = trades.assign(Exited=trades["exit_time"].map(when), Entered=trades["entry_time"].map(when),
                              Symbol=trades["symbol"].map(chart_link), Strategy=trades["system"].map(strategy),
                              Side=trades["side"].map({"BUY": "LONG", "SELL": "SHORT"}))
        st.dataframe(shown[["Exited", "Symbol", "Strategy", "Side", "qty", "entry", "exit", "sl", "reason", "Held", "gross",
                            "charges", "net", "Entered"]], hide_index=True, width="stretch",
                     column_config={"Symbol": SYMBOL_LINK})
        st.download_button("⬇ Index trades (CSV)", csv(trades), file_name="index_trades.csv", mime="text/csv",
                           on_click="ignore")
    with tabs[1]:
        st.dataframe(report(trades, "Date"), width="stretch")
    with tabs[2]:
        sides = trades.assign(Index=trades["symbol"], Side=trades["side"].map({"BUY": "Longs", "SELL": "Shorts"}))
        st.dataframe(summary(sides, ["Index", "Side"]), width="stretch")
        st.dataframe(summary(sides, "reason").rename_axis("Exit"), width="stretch")


TIMEFRAME_COLOURS = ["#2F6FDB", "#E08A00", "#14A38B", "#9B4FC9"]  # fixed order, one per strategy


def show_profit_loss_pies(history: pd.DataFrame) -> None:
    """Two pies over the closed trades: where the profit of the winners came from, and where the
    loss of the losers, one slice per strategy (timeframe)."""
    names = [n for s, n in SYSTEM_NAMES.items() if s.startswith("RSI")]
    systems = [s for s in SYSTEM_NAMES if s.startswith("RSI")]
    closed = history[history["System"].isin(systems)].dropna(subset=["net"])
    if closed.empty:
        st.caption("No closed trades yet.")
        return
    colours = dict(zip(names, TIMEFRAME_COLOURS))
    columns = st.columns(2)
    for column, title, winners in ((columns[0], "Profit", True), (columns[1], "Loss", False)):
        part = closed[closed["net"] > 0] if winners else closed[closed["net"] <= 0]
        rows = [(SYSTEM_NAMES[s], abs(float(part.loc[part["System"] == s, "net"].sum())),
                 int((part["System"] == s).sum())) for s in systems]
        rows = [r for r in rows if r[1] > 0]
        total = sum(r[1] for r in rows)
        count = sum(r[2] for r in rows)
        sign = "" if winners else "−"
        figure = go.Figure(go.Pie(
            labels=[r[0] for r in rows], values=[r[1] for r in rows], hole=0.45, sort=False, direction="clockwise",
            marker={"colors": [colours[r[0]] for r in rows], "line": {"color": "rgba(0,0,0,0)", "width": 2}},
            customdata=[[f"{sign}₹{r[1]:,.0f}", r[2]] for r in rows],
            texttemplate="%{label}<br>%{customdata[0]}<br>%{percent}", textposition="outside",
            hovertemplate="%{label}<br>%{customdata[0]} from %{customdata[1]} trades<br>%{percent} of the "
                          + title.lower() + "<extra></extra>"))
        figure.update_layout(
            title={"text": f"{title}: {sign}₹{total:,.0f} from {count:,} {'winning' if winners else 'losing'} trades",
                   "font": {"size": 15}},
            showlegend=True, legend={"orientation": "h", "y": -0.1}, margin={"t": 110, "b": 40, "l": 20, "r": 20},
            height=470,
            annotations=[{"text": f"{sign}₹{total / 1e5:,.1f}L", "showarrow": False, "font": {"size": 18}}])
        column.plotly_chart(figure, width="stretch", key=f"pie-{title}")
    st.caption("Closed trades only, all exit styles. Profit = the winning trades' net after charges; loss = the "
               "losing trades' (net at or below 0). Each slice is one timeframe's share.")


def main() -> None:
    st.set_page_config(page_title="Auto Trader", page_icon="🤖", layout="wide")
    st.title("🤖 Auto trader")
    chart_site_picker()
    st.caption(
        f"Paper-trades the Chartink RSI signals as **RSI Daily** (RSI 2H and 4H retired on 27 Nov 2026; they and "
        f"RSI Weekly are hidden from this page; only the entry signal's timeframe differs between them, "
        f"the SL and exits are all 2H), 1 lot, SL at the previous 2H candle (Heikin Ashi: the previous HA "
        f"candle's low / high; RSI: the candle's low / high or {fs.AUTO_SL_PCT}%, whichever is further), in its exit styles "
        f"side by side: **Hold · trail** (held "
        f"overnight, trailing {fs.AUTO_TRAIL_PCT}% once the target is reached), **Hold · 2H SMA 8 exit** and "
        f"**Hold · 2H SMA 20 exit** (held until a finished 2H candle closes below / above the SMA 8 or 20 of the 2H "
        f"closes; these replaced EOD and Hold · RSI exit, whose past trades still show; for Heikin Ashi, **Hold · HA exit**: held "
        f"until the live candle of its timeframe goes below the previous HA low / above the previous HA high). "
        f"Every RSI strategy and style also exits when a finished 30-minute candle's SMA 14 of RSI 14 is below 40 "
        f"(long) / above 60 (short), at its close. "
        f"Held trades close on expiry day at 3:15 PM. "
        f"The Heikin Ashi signals come from the bot's own scans: a green (red) candle on above-average volume whose HA "
        f"close breaks above (below) the previous HA high (low), after a red (green) previous HA candle that is not a doji "
        f"(body over 10% of its range); no SMA RSI condition. "
        f"**HA multi-timeframe** takes the Heikin Ashi signals of 2H, 4H and Daily (a timeframe whose previous HA candle "
        f"is a doji, body 10% of its range or less, does not count), held until the HA exit of the highest timeframe "
        f"that matched, which trails it, with an initial SL 3% from the entry. "
        f"**HA 15m index** trades NIFTY and BANKNIFTY futures on 15-minute Heikin Ashi candles (from Yahoo 5-minute "
        f"bars: a green HA candle with no lower wick after a doji or a red HA candle (closing above its body), in an SMA 20/50/200 uptrend, or the mirror for shorts), SL 1.1 x ATR(14) trailed from each 15-minute close, out at the close of the first HA doji, "
        f"squared off at 3:15 PM; a new trade once the last one closed, not twice on one candle. "
        f"A stock is traded once per side, style and strategy per day. Runs on GitHub: every {LIVE_EVERY} minutes while "
        f"this page is open in market hours, else every 15 minutes if the switch below is on.")

    account, error = load_account()
    top = st.columns([1, 1, 1, 1.2, 1.2, 2.2])
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
    top[5].caption(f"[Runs on GitHub]({RUNS_URL}) · requests from here apply in about a minute.")
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
    skips = load_skips()
    signals = load_signals()
    every_trade, every_position = history, account["positions"]
    # The index strategy has its own tab (index_trader_page.py): this page is the stock strategies.
    # Retired strategies are hidden too, with any positions they still hold until their exits.
    hidden = [*INDEX_SYSTEMS, "OPT ATM HA", *HIDDEN_SYSTEMS, *({} if SHOW_RETIRED else RETIRED_SYSTEMS)]  # opt_atm.SYSTEM
    history = history[~history["System"].isin(hidden)]
    positions = [p for p in account["positions"] if p.get("system", "RSI") not in hidden]
    instruments = {i["symbol"]: i for i in fs.load_instruments()}
    quotes = fs.latest_prices({p["symbol"] for p in positions}, instruments, None) if positions else {}

    def unrealized(p):
        q = quotes.get(p["symbol"])
        return None if not q else round((q["price"] - p["entry"]) * p["qty"] * (1 if p["side"] == "BUY" else -1), 2)

    show_scoreboard(history, positions, unrealized)
    st.caption(f"Last run {when(account.get('updated'))}. Margin needed: no capital limit, every signal is taken in "
               f"every style, so this is the most margin ({fs.MARGIN_RATE:.0%} of the futures' value) the strategy's "
               "trades held at the same time, open positions included. Each style alone needs less: see Results.")

    # The Excel reports, in the top row.
    now = datetime.now(fs.IST)
    open_rows = [{
        "Symbol": p["symbol"],
        "Side": "LONG" if p["side"] == "BUY" else "SHORT", "Strategy": p.get("system", "RSI"),
        "Style": series_of(p),
        "Signal": p["source"] + (f" · exit {SHORT_TF[p['exit_tf']]}" if p.get("exit_tf") else ""), "Qty": p["qty"],
        "Entry": p["entry"], "LTP": (quotes.get(p["symbol"]) or {}).get("price"),
        "Prev close": (quotes.get(p["symbol"]) or {}).get("prev"),
        "SL / TSL": p.get("stop"), "Target": p["tp"],
        "At target": "—" if p.get("tp") is None or p.get("style") in fs.HA_EXITS else "Passed · trailing" if p.get("passed")
        else "Trail" if p.get("run") else "Exit",
        "Entered": datetime.fromtimestamp(p["time"] / 1000, fs.IST).replace(tzinfo=None), "Agrees": p.get("agrees"),
    } for p in positions]
    if len(history):
        top[3].download_button(
            "📊 Closed trades report", closed_report(history), width="stretch",
            file_name=f"Auto trader closed trades {now:%d %b %Y}.xlsx", on_click="ignore",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            help="Excel: HA vs RSI by side, exit style and stock, and every closed trade")
    if positions:
        top[4].download_button(
            "📂 Open positions report", trade_report.open_workbook(open_rows, f"{now:%d %b %Y %H:%M} IST"),
            file_name=f"Auto trader open positions {now:%d %b %Y %H%M}.xlsx", width="stretch", on_click="ignore",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            help="Excel: every open position with its unrealized P&L at the LTP, and totals by strategy and side")

    # Open positions, with requests on the one selected.
    st.subheader(f"Open positions ({len(positions)})")
    if positions:
        table = pd.DataFrame(open_rows).assign(
            Symbol=[chart_link(p["symbol"]) for p in positions],
            Entered=[when(p["time"]) for p in positions])
        table.insert(table.columns.get_loc("Entered"), "Unrealized ₹", [unrealized(p) for p in positions])
        instruments = {i["symbol"]: i for i in fs.load_instruments()}
        atrs = fs.daily_atrs(tuple(sorted({(p["symbol"], instruments[p["symbol"]]["yahoo"]) for p in positions
                                        if p["symbol"] in instruments and fs.is_index(instruments[p["symbol"]])})))
        table.insert(table.columns.get_loc("SL / TSL"), "ATR (D)", [atrs.get(p["symbol"]) for p in positions])
        picked = st.dataframe(
            table, hide_index=True, width="stretch", on_select="rerun", selection_mode="single-row",
            column_config={"Symbol": SYMBOL_LINK, "Unrealized ₹": st.column_config.NumberColumn(format="%.2f"),
                           "ATR (D)": st.column_config.NumberColumn(
                               format="%.2f", help="Daily ATR (14) at the last close: the stocks' from the market "
                                                   "data pipeline, the indices' from Yahoo's daily candles")},
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

    st.subheader(f"New strategies (from {NEW_SINCE:%d %b})")
    show_new(every_trade, every_position)

    st.subheader("Results")
    if len(history) or len(you):
        st.markdown("**The strategies side by side**")
        show_versus(history, positions)
        system = st.segmented_control("Strategy", list(SYSTEM_NAMES), format_func=SYSTEM_NAMES.get, default="RSI Daily") or "RSI Daily"
        styles = style_names(system)
        trades = history[(history["System"] == system) & history["Style"].isin(styles)]
        held = [p for p in positions if p.get("system", "RSI") == system and series_of(p) in styles]
        # The simulator trades the RSI signals only, so it shows beside that strategy.
        mine = you if system == "RSI" else you.iloc[0:0]
        logged = signals[signals["system"] == system] if len(signals) else signals
        if len(trades) or len(mine):
            show_compare(series(trades, mine), held, styles)
            if system in ("HA", "HA MTF"):
                st.markdown("**SMA RSI filter**")
                show_sma_filter(trades)
            # Each signal is traded in every style, so the other results are for one style at a time.
            picked = st.segmented_control("Results for", ["All bot styles", *styles, *([YOU] if len(mine) else [])],
                                          default="All bot styles", key=f"results-{system}")
            if picked == YOU:
                show_results(you, None)
            else:
                show_results(trades if picked in (None, "All bot styles") else trades[trades["Style"] == picked], logged)
        else:
            st.caption(f"No closed {SYSTEM_NAMES[system]} trades yet.")
    else:
        st.caption("No closed trades yet. The simulator's trades show here too once they are backed up.")

    st.subheader("Your decisions")
    show_decisions(history[history["System"] == "RSI"], you, skips)

    st.subheader("Signals")
    show_signals(signals)

    downloads = st.columns(3)
    downloads[0].download_button("⬇ Auto trader trades (CSV)", csv(history), "auto_trader_trades.csv", "text/csv",
                                 width="stretch", disabled=history.empty)
    downloads[1].download_button("⬇ Simulator trades (CSV)", csv(you), "simulator_trades.csv", "text/csv",
                                 width="stretch", disabled=you.empty)
    downloads[2].download_button("⬇ Signal log (CSV)", signals.to_csv(index=False).encode(), "signals.csv",
                                 "text/csv", width="stretch", disabled=signals.empty)

    st.subheader("Profit and loss by timeframe")
    show_profit_loss_pies(history)

    with st.expander("Run log"):
        st.code("\n".join(account.get("log", [])) or "Nothing yet.", language=None)


if __name__ == "__main__":  # Streamlit runs a page as __main__; index_trader_page imports it
    main()
