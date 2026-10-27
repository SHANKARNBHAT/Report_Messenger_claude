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


# Shorter than the quickest refresh, so every refresh gets new results.
@st.cache_data(ttl=50, show_spinner=False)
def fetch_dashboard(link: str) -> dict:
    """Name of a public dashboard and the current results of each of its widgets."""
    match = re.search(r"(\d+)\s*/?\s*$", link.strip())
    if not match:
        raise ChartinkError("Enter a dashboard link like https://chartink.com/dashboard/501779.")
    url = CHARTINK_DASHBOARD_URL.format(match.group(1))

    with requests.Session() as session:
        session.headers.update(CHARTINK_HEADERS)
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
            raw_widgets = json.loads(tag[":widgets"])
        except ValueError as exc:
            raise ChartinkError("The dashboard's widget data could not be read.") from exc

        widgets = []
        for widget in raw_widgets:
            groups = (widget.get("jsondetails") or {}).get("groups") or {}
            name = (widget.get("name") or f"Widget {widget.get('id')}").strip()
            result = {"name": name, "table": None, "as_of": None, "error": None,
                      "timeframe": timeframe_of(name, widget.get("query") or ""),
                      "bearish": bool(re.search(BEARISH_WORDS, name, re.IGNORECASE))}
            try:
                # The same request the dashboard page makes for each widget.
                response = session.post(
                    CHARTINK_WIDGET_URL,
                    data={
                        "query": widget.get("query") or "",
                        "use_live": "1",
                        "limit": groups.get("limit") or 50,
                        "size": groups.get("size") or 10,
                        "widget_id": widget.get("id"),
                    },
                    headers={"x-csrf-token": token["content"], "X-Requested-With": "XMLHttpRequest",
                             "Referer": url},
                    timeout=30,
                )
                response.raise_for_status()
                result["table"], result["as_of"] = widget_table(widget.get("query") or "", response.json())
            except (requests.RequestException, ValueError, ChartinkError) as exc:
                result["error"] = str(exc) or type(exc).__name__
            widgets.append(result)

    return {
        "name": details.get("name") or f"Dashboard {match.group(1)}",
        "link": url,
        "widgets": widgets,
        "fetched_at": datetime.now(IST),
    }


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


def common_stocks(widgets: list[dict]) -> pd.DataFrame:
    """Stocks found in two or more timeframes, with bullish or bearish for each."""
    found: dict[str, dict[str, set[str]]] = {}  # symbol -> timeframe -> {"Bullish", "Bearish"}
    for widget in widgets:
        if widget["timeframe"] is None or widget["table"] is None:
            continue
        side = "Bearish" if widget["bearish"] else "Bullish"
        for symbol in widget["table"]["Symbol"]:
            found.setdefault(symbol, {}).setdefault(widget["timeframe"], set()).add(side)

    marks = {"Bullish": "🟢 Bullish", "Bearish": "🔴 Bearish"}
    rows = []
    for symbol, timeframes in found.items():
        if len(timeframes) < 2:
            continue
        sides = set().union(*timeframes.values())
        row = {"Symbol": symbol, "Timeframes": len(timeframes),
               "Signal": next(iter(sides)) if len(sides) == 1 else "Mixed"}
        for timeframe in TIMEFRAMES:
            row[timeframe] = " / ".join(marks[s] for s in sorted(timeframes.get(timeframe, ()), reverse=True))
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
        st.rerun(scope="fragment")
    st.caption(
        f"Updated {data['fetched_at']:%d %b %Y, %H:%M:%S} IST · refreshes every {refresh}"
    )

    names = ["COMMON", *TIMEFRAMES]
    others = [w for w in data["widgets"] if w["timeframe"] is None]
    if others:
        names.append("OTHER")
    for tab, timeframe in zip(st.tabs(names), names):
        with tab:
            if timeframe == "COMMON":
                common = common_stocks(data["widgets"])
                st.caption(f"{len(common)} stock(s) found in two or more timeframes")
                if common.empty:
                    st.info("No stock is in more than one timeframe right now.")
                else:
                    show_table(common)
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
