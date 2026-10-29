"""
Chartink Dashboard Viewer
=========================

Shows the live results of every widget on a public Chartink dashboard, one tab
per widget, with the same columns the widget has on Chartink. The page
refreshes itself every 5 minutes, or as often as chosen on the page. All data
comes from Chartink.

Shown as the "Chartink dashboard" page of report_app.py.
"""

from __future__ import annotations

import json
import re
from datetime import datetime
from urllib.parse import quote
from zoneinfo import ZoneInfo

import pandas as pd
import requests
import streamlit as st
from bs4 import BeautifulSoup

from page_settings import remember

try:
    # Trust the certificates installed in Windows/macOS, not only Python's own
    # list; needed behind antivirus or office proxies that inspect HTTPS.
    import truststore

    truststore.inject_into_ssl()
except ImportError:
    pass

# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------
DEFAULT_DASHBOARD = "https://chartink.com/dashboard/501779"
REFRESH_CHOICES = {
    "1 min": 60, "3 min": 180, "5 min": 300, "10 min": 600,
    "15 min": 900, "30 min": 1800, "1 hr": 3600, "2 hr": 7200,
}
DEFAULT_REFRESH = "5 min"
# Tab name -> words that mark a widget as that timeframe (in its name, else its scan).
TIMEFRAMES = {
    "2 HOUR": (r"\b2\s*h", r"2 hour"),
    "4 HOUR": (r"\b4\s*h", r"4 hour"),
    "DAILY": (r"\bdaily\b|\bdly\b|\bday\b", r"\bdaily\b|\bday ago\b"),
    "WEEKLY": (r"\bweekly\b|\bwkly\b|\bweek\b", r"\bweekly\b|\bweek ago\b"),
}
BEARISH_WORDS = r"short|bear|sell"
# Widgets that list prices, not signals ("Futures stocks below 325"...): kept out of
# the tabs, read in full, and used by the Futures simulator for the latest prices.
PRICE_WIDGETS = r"^\s*futures stocks\b"
PRICE_WIDGET_ROWS = 500
# 5-minute bars read from the price-list widgets for the simulator: about 4 sessions
# (Chartink returns at most 300).
PRICE_WIDGET_BARS = 300
IST = ZoneInfo("Asia/Kolkata")

CHARTINK_DASHBOARD_URL = "https://chartink.com/dashboard/{}"
CHARTINK_WIDGET_URL = "https://chartink.com/widget/process"
CHARTINK_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
}
# A Chartink chart snapshot (Share button on a chart). It opens any stock with the
# snapshot's indicators, for everyone, logged in or not.
DEFAULT_SNAPSHOT = (
    "https://chartink.com/stocks-new?load-snapshot=simple-moving-average-simple-moving-average-"
    "simple-moving-average-pivot-s2-pivot-copy-pivot-simple-moving-average-volume-weighted-"
    "moving-averag-chart-snapshot"
)
MY_LAYOUT = "My indicators"
# Active Watchlist of the Market reports, compared with the dashboard on the COMMON tab.
# Segment -> the report file in that segment's GitHub repository.
WATCHLISTS = {
    "Futures": "https://raw.githubusercontent.com/SHANKARNBHAT/Report_Messenger_Claude/main/"
               "rvol_active_signals_report.txt",
    "Cash": "https://raw.githubusercontent.com/SHANKARNBHAT/Report_Messenger_cash/main/"
            "rvol_active_signals_report.txt",
}
# Chart site -> link to a stock's chart on it, given the snapshot's name.
CHART_SITES = {
    MY_LAYOUT: lambda symbol, snapshot: (
        f"https://chartink.com/stocks-new?load-snapshot={snapshot}&symbol=" + quote(symbol, safe="")
    ),
    "Chartink": lambda symbol, _: "https://chartink.com/stocks-new?symbol=" + quote(symbol, safe=""),
    "TradingView": lambda symbol, _: (
        "https://in.tradingview.com/chart/?symbol=" + quote(f"NSE:{symbol}", safe="")
    ),
}


# ---------------------------------------------------------------------------
# Chartink
# ---------------------------------------------------------------------------
class ChartinkError(Exception):
    """Raised when the dashboard cannot be read."""


def column_names(query: str, aliases: list[str]) -> list[str]:
    """Column titles as written in the widget ("Dly RSI"); Chartink returns them lower-cased."""
    select = re.split(r"\bWHERE\b", query, maxsplit=1, flags=re.IGNORECASE)[0]
    names = [" ".join(name.split()) for name in re.findall(r"\bas\s+'([^']*)'", select, re.IGNORECASE)]
    return names if len(names) == len(aliases) else [alias.title() for alias in aliases]


def timeframe_of(name: str, query: str) -> str | None:
    """Tab for a widget: from its name ("2H RSI SCAN"), else the timeframe its scan uses most."""
    for tab, (in_name, _) in TIMEFRAMES.items():
        if re.search(in_name, name, re.IGNORECASE):
            return tab
    where = re.split(r"\bWHERE\b", query, maxsplit=1, flags=re.IGNORECASE)[-1]
    counts = {tab: len(re.findall(in_scan, where, re.IGNORECASE)) for tab, (_, in_scan) in TIMEFRAMES.items()}
    best = max(counts, key=counts.get)
    return best if counts[best] else None


def widget_table(query: str, payload: dict) -> tuple[pd.DataFrame, datetime | None]:
    """One row per stock with the latest value of each widget column, and the data time."""
    if payload.get("error"):
        raise ChartinkError(str(payload["error"]))
    meta = (payload.get("metaData") or [{}])[0]
    names = column_names(query, meta.get("columnAliases") or [])
    columns = list(dict.fromkeys(names))  # a column listed twice (e.g. 'Price') is shown once

    rows = []
    for group in payload.get("groupData") or []:
        row = {"Symbol": group.get("name")}
        # Each column holds a series over the widget's recent candles; the last one is now.
        series = [next(iter(result.values()), []) for result in group.get("results") or []]
        for name, values in zip(names, series):
            row.setdefault(name, values[-1] if values else None)
        rows.append(row)

    updated = meta.get("lastUpdateTime")
    as_of = datetime.fromtimestamp(updated / 1000, IST) if updated else None
    return pd.DataFrame(rows, columns=["Symbol"] + columns), as_of


def open_dashboard(session: requests.Session, link: str) -> tuple[str, str, dict, list[dict]]:
    """Link, CSRF token, details and raw widgets of a public dashboard."""
    match = re.search(r"(\d+)\s*/?\s*$", link.strip())
    if not match:
        raise ChartinkError("Enter a dashboard link like https://chartink.com/dashboard/501779.")
    url = CHARTINK_DASHBOARD_URL.format(match.group(1))
    try:
        page = session.get(url, timeout=20)
        page.raise_for_status()
    except requests.RequestException as exc:
        raise ChartinkError(f"Could not open the dashboard: {exc}") from exc

    soup = BeautifulSoup(page.text, "html.parser")
    token = soup.find("meta", attrs={"name": "csrf-token"})
    tag = soup.find("dashboard")
    if token is None or tag is None or not tag.get(":widgets"):
        raise ChartinkError("No widgets found. The dashboard may be private, or Chartink's page changed.")
    try:
        details = json.loads(tag.get(":dashboard") or "{}")
        details.setdefault("name", f"Dashboard {match.group(1)}")
        return url, token["content"], details, json.loads(tag[":widgets"])
    except ValueError as exc:
        raise ChartinkError("The dashboard's widget data could not be read.") from exc


def widget_payload(session: requests.Session, url: str, token: str, widget: dict, limit: int, size: int) -> dict:
    """A widget's results: `limit` stocks, each column a series over the last `size` candles."""
    # The same request the dashboard page makes for each widget.
    response = session.post(
        CHARTINK_WIDGET_URL,
        data={"query": widget.get("query") or "", "use_live": "1", "limit": limit, "size": size,
              "widget_id": widget.get("id")},
        headers={"x-csrf-token": token, "X-Requested-With": "XMLHttpRequest", "Referer": url},
        timeout=30,
    )
    response.raise_for_status()
    return response.json()


# Shorter than the quickest refresh, so every refresh gets new results.
@st.cache_data(ttl=50, show_spinner=False)
def fetch_dashboard(link: str) -> dict:
    """Name of a public dashboard and the current results of each of its widgets."""
    with requests.Session() as session:
        session.headers.update(CHARTINK_HEADERS)
        url, token, details, raw_widgets = open_dashboard(session, link)

        widgets = []
        for widget in raw_widgets:
            groups = (widget.get("jsondetails") or {}).get("groups") or {}
            name = (widget.get("name") or f"Widget {widget.get('id')}").strip()
            prices = bool(re.search(PRICE_WIDGETS, name, re.IGNORECASE))
            result = {"name": name, "table": None, "as_of": None, "error": None, "prices": prices,
                      "timeframe": None if prices else timeframe_of(name, widget.get("query") or ""),
                      "bearish": bool(re.search(BEARISH_WORDS, name, re.IGNORECASE))}
            limit = groups.get("limit") or 50
            try:
                payload = widget_payload(session, url, token, widget,
                                         max(limit, PRICE_WIDGET_ROWS) if prices else limit,
                                         groups.get("size") or 10)
                result["table"], result["as_of"] = widget_table(widget.get("query") or "", payload)
            except (requests.RequestException, ValueError, ChartinkError) as exc:
                result["error"] = str(exc) or type(exc).__name__
            widgets.append(result)

    return {
        "name": details["name"],
        "link": url,
        "widgets": widgets,
        "fetched_at": datetime.now(IST),
    }


@st.cache_data(ttl=120, show_spinner=False)
def fetch_price_bars(link: str, size: int = PRICE_WIDGET_BARS) -> dict[str, list[tuple]]:
    """symbol -> its last `size` 5-minute bars (start in seconds, open, high, low, close),
    from the price-list widgets that have 5-minute Open, High, Low and Close columns."""
    bars: dict[str, list[tuple]] = {}
    with requests.Session() as session:
        session.headers.update(CHARTINK_HEADERS)
        url, token, _, raw_widgets = open_dashboard(session, link)
        for widget in raw_widgets:
            if not re.search(PRICE_WIDGETS, widget.get("name") or "", re.IGNORECASE):
                continue
            # Chartink sometimes answers a long series with an empty list: then ask for fewer bars.
            payload = None
            for bars_wanted in dict.fromkeys((size, min(size, 150), min(size, 75))):
                try:
                    payload = widget_payload(session, url, token, widget, PRICE_WIDGET_ROWS, bars_wanted)
                except (requests.RequestException, ValueError):
                    payload = None
                if isinstance(payload, dict) and payload.get("groupData"):
                    break
            if not isinstance(payload, dict):
                continue
            meta = (payload.get("metaData") or [{}])[0]
            starts = [t // 1000 for t in meta.get("tradeTimes") or []]
            for group in payload.get("groupData") or []:
                columns = {alias: values for result in group.get("results") or [] for alias, values in result.items()}
                ohlc = [next((v for a, v in columns.items() if re.fullmatch(rf"5\s*min\w*\s+{part}", a, re.I)), None)
                        for part in ("open", "high", "low", "close")]
                if any(series is None or len(series) != len(starts) for series in ohlc):
                    continue
                bars[group.get("name")] = [row for row in zip(starts, *ohlc) if None not in row]
    return bars


# ---------------------------------------------------------------------------
# Market reports
# ---------------------------------------------------------------------------
def parse_watchlist(text: str) -> pd.DataFrame:
    """The table of an Active Watchlist report: symbol, strategy, bias, last_trigger, ..."""
    lines = text.splitlines()
    start = next((i for i, line in enumerate(lines) if line.strip().lower().startswith("symbol")), None)
    if start is None:
        return pd.DataFrame(columns=["symbol", "strategy", "bias", "last_trigger"])
    columns = lines[start].split()
    rows = [line.split() for line in lines[start + 1:] if line.strip()]
    return pd.DataFrame([row for row in rows if len(row) == len(columns)], columns=columns)


@st.cache_data(ttl=50, show_spinner=False)
def fetch_watchlists() -> tuple[dict[str, pd.DataFrame], list[str]]:
    """Each segment's Active Watchlist from GitHub, and messages for any that failed."""
    watchlists, errors = {}, []
    for segment, url in WATCHLISTS.items():
        try:
            # A changing query string skips GitHub's ~5 minute raw-file cache.
            response = requests.get(url, params={"t": int(datetime.now().timestamp())}, timeout=15)
            response.raise_for_status()
            watchlists[segment] = parse_watchlist(response.text)
        except requests.RequestException as exc:
            errors.append(f"Could not load the {segment} watchlist: {exc}")
    return watchlists, errors


# ---------------------------------------------------------------------------
# Page
# ---------------------------------------------------------------------------
def show_table(table: pd.DataFrame) -> None:
    site = st.session_state.get("charts") or MY_LAYOUT
    snapshot = re.search(r"load-snapshot=([\w-]+)", st.session_state.get("snapshot") or "")
    if site == MY_LAYOUT and not snapshot:
        site = "Chartink"  # no usable snapshot link in the sidebar
    config = {
        "Symbol": st.column_config.LinkColumn(
            "Symbol", display_text=r"[?&]symbol=(?:NSE%3A)?([^&]+)$",
            help="Opens the chart" + (" with your indicators" if site == MY_LAYOUT else f" on {site}"),
        ),
    }
    for column in table.columns[1:]:
        if "volume" in column.lower() and "perc" not in column.lower():
            config[column] = st.column_config.NumberColumn(column, format="localized")
        elif pd.api.types.is_float_dtype(table[column]):
            config[column] = st.column_config.NumberColumn(column, format="%.2f")
    link = CHART_SITES[site]
    shown = table.assign(Symbol=table["Symbol"].map(lambda s: link(s, snapshot and snapshot.group(1))))
    st.dataframe(shown, column_config=config, hide_index=True, width="stretch",
                 height=min(600, 38 + 35 * len(shown)))


MARKS = {"Bullish": "🟢 Bullish", "Bearish": "🔴 Bearish"}


def chartink_sides(widgets: list[dict]) -> dict[str, dict[str, set[str]]]:
    """symbol -> timeframe -> {"Bullish", "Bearish"}, for every stock on the dashboard."""
    found: dict[str, dict[str, set[str]]] = {}
    for widget in widgets:
        if widget["timeframe"] is None or widget["table"] is None:
            continue
        side = "Bearish" if widget["bearish"] else "Bullish"
        for symbol in widget["table"]["Symbol"]:
            found.setdefault(symbol, {}).setdefault(widget["timeframe"], set()).add(side)
    return found


def timeframe_columns(timeframes: dict[str, set[str]]) -> dict[str, str]:
    """One column per timeframe: "🟢 Bullish", "🔴 Bearish", both, or blank."""
    return {timeframe: " / ".join(MARKS[s] for s in sorted(timeframes.get(timeframe, ()), reverse=True))
            for timeframe in TIMEFRAMES}


def watchlist_matches(widgets: list[dict], watchlists: dict[str, pd.DataFrame]) -> pd.DataFrame:
    """Stocks on both the dashboard and a Market reports Active Watchlist."""
    found = chartink_sides(widgets)
    reports: dict[str, list[tuple[str, pd.Series]]] = {}  # symbol -> [(segment, report row)]
    for segment, table in watchlists.items():
        for _, row in table.iterrows():
            if row["symbol"] in found:
                reports.setdefault(row["symbol"], []).append((segment, row))

    rows = []
    for symbol, entries in reports.items():
        biases = {str(row["bias"]).title() for _, row in entries}
        sides = set().union(*found[symbol].values())
        rows.append({
            "Symbol": symbol,
            "Watchlist": ", ".join(segment for segment, _ in entries),
            "Report bias": " / ".join(MARKS.get(b, b) for b in sorted(biases, reverse=True)),
            "Strategy": ", ".join(dict.fromkeys(str(row["strategy"]) for _, row in entries)),
            "Last trigger": max(str(row["last_trigger"]) for _, row in entries),
            "Agrees": "✅ Yes" if biases == sides and len(sides) == 1 else "⚠️ No",
            **timeframe_columns(found[symbol]),
        })
    columns = ["Symbol", "Watchlist", "Report bias", "Strategy", "Last trigger", "Agrees", *TIMEFRAMES]
    table = pd.DataFrame(rows, columns=columns)
    # Stocks where the report and Chartink agree first.
    return table.sort_values(["Agrees", "Symbol"], key=lambda c: c.ne("✅ Yes") if c.name == "Agrees" else c)


def common_stocks(widgets: list[dict]) -> pd.DataFrame:
    """Stocks found in two or more timeframes, with bullish or bearish for each."""
    rows = []
    for symbol, timeframes in chartink_sides(widgets).items():
        if len(timeframes) < 2:
            continue
        sides = set().union(*timeframes.values())
        row = {"Symbol": symbol, "Timeframes": len(timeframes),
               "Signal": next(iter(sides)) if len(sides) == 1 else "Mixed"}
        row.update(timeframe_columns(timeframes))
        rows.append(row)
    table = pd.DataFrame(rows, columns=["Symbol", "Timeframes", "Signal", *TIMEFRAMES])
    order = {"Bullish": 0, "Bearish": 1, "Mixed": 2}
    return table.sort_values(["Timeframes", "Signal", "Symbol"], ascending=[False, True, True],
                             key=lambda c: c.map(order) if c.name == "Signal" else c)


def live_dashboard(link: str, refresh: str) -> None:
    """Runs again by itself every `refresh` (see main)."""
    try:
        with st.spinner("Loading the dashboard from Chartink..."):
            data = fetch_dashboard(link)
    except ChartinkError as exc:
        st.error(f"Chartink: {exc}")
        return

    charts_col, refresh_col, button_col = st.columns([2.2, 1, 1], vertical_alignment="bottom")
    charts_col.radio("Open charts on", list(CHART_SITES), key="charts", horizontal=True,
                     on_change=remember, args=("charts",))
    refresh_col.selectbox("Refresh every", list(REFRESH_CHOICES), key="refresh",
                          on_change=remember, args=("refresh",))
    if st.session_state.refresh != refresh:
        st.rerun()  # the whole page, so the new refresh interval takes effect
    if button_col.button("↻ Refresh now", width="stretch"):
        fetch_dashboard.clear()
        fetch_watchlists.clear()
        st.rerun(scope="fragment")
    st.caption(
        f"Updated {data['fetched_at']:%d %b %Y, %H:%M:%S} IST · refreshes every {refresh}"
    )

    names = ["COMMON", *TIMEFRAMES]
    others = [w for w in data["widgets"] if w["timeframe"] is None and not w["prices"]]
    if others:
        names.append("OTHER")
    for tab, timeframe in zip(st.tabs(names), names):
        with tab:
            if timeframe == "COMMON":
                show_common(data["widgets"])
                continue
            if timeframe == "OTHER":
                for widget in others:
                    show_widget("⚪", widget)
                continue
            for bearish, label in ((False, "🟢 Bullish"), (True, "🔴 Bearish")):
                widgets = [w for w in data["widgets"]
                           if w["timeframe"] == timeframe and w["bearish"] == bearish]
                st.subheader(label)
                if not widgets:
                    hint = ' with "SHORT" in its name' if bearish else ""
                    st.info(f"No {label.split()[1].lower()} {timeframe.lower()} widget on the dashboard. "
                            f"Add one{hint} on Chartink and it will show here.")
                for widget in widgets:
                    show_widget(None, widget)


def show_common(widgets: list[dict]) -> None:
    """COMMON tab: stocks shared with the Market reports, then across timeframes."""
    st.subheader("🔗 Common between Chartink and Market reports")
    watchlists, errors = fetch_watchlists()
    for error in errors:
        st.warning(error)
    matches = watchlist_matches(widgets, watchlists)
    st.caption(f"{len(matches)} stock(s) on this Chartink dashboard that are also on the Active "
               "Watchlist of the Market reports. Agrees: the report's bias matches every Chartink signal.")
    if matches.empty:
        st.info("No stock is on both the Chartink dashboard and the Active Watchlist right now.")
    else:
        show_table(matches)

    st.subheader("🔁 Common across Chartink timeframes")
    common = common_stocks(widgets)
    st.caption(f"{len(common)} stock(s) found in two or more timeframes")
    if common.empty:
        st.info("No stock is in more than one timeframe right now.")
    else:
        show_table(common)


def show_widget(icon: str | None, widget: dict) -> None:
    """One widget's results: its name, stock count and data time, then the table."""
    if icon:
        st.subheader(f"{icon} {widget['name']}")
    if widget["error"]:
        st.error(f"Could not load {widget['name']}: {widget['error']}")
        return
    table = widget["table"]
    as_of = f" · data as of {widget['as_of']:%d %b, %H:%M} IST" if widget["as_of"] else ""
    st.caption(f"{widget['name']} · {len(table)} stock(s){as_of}")
    if table.empty:
        st.info("No stocks match this scan right now.")
    else:
        show_table(table)


def main() -> None:
    st.set_page_config(page_title="Chartink Dashboard", page_icon="📊", layout="wide",
                       initial_sidebar_state="collapsed")
    st.session_state.setdefault("snapshot", DEFAULT_SNAPSHOT)
    st.sidebar.text_input(f'Chart snapshot link (for "{MY_LAYOUT}")', key="snapshot",
                          on_change=remember, args=("snapshot",))
    st.sidebar.caption("On a Chartink chart with your indicators, click Share and paste the link here.")
    st.session_state.setdefault("refresh", DEFAULT_REFRESH)
    refresh = st.session_state.refresh
    st.fragment(live_dashboard, run_every=REFRESH_CHOICES[refresh])(DEFAULT_DASHBOARD, refresh)


if __name__ == "__main__":
    main()
