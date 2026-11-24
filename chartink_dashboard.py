"""
Chartink Dashboard Viewer
=========================

Shows the live results of every widget on a public Chartink dashboard, one tab
per widget, with the same columns the widget has on Chartink. The page
refreshes itself every 5 minutes, or as often as chosen on the page. All data
comes from Chartink, but for the VOLUME PROFILE tab, which shows a file the report
pipeline writes. The COMMON tab also compares the futures stocks with the lists of the
Fundamentals dashboard (FUNDAMENTALS_DASHBOARD).

Shown as the "Chartink dashboard" page of report_app.py.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, time, timedelta
from pathlib import Path
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
HA_DASHBOARD = "https://chartink.com/dashboard/502621"  # the Heikin Ashi strategy, on the COMMON tab
# The long-term dashboard of the Fundamentals page (fundamentals_page.py), also compared with
# the futures stocks on the COMMON tab. Its widgets list stocks institutions are buying, but
# BREAKOUT_WIDGET, a weekly breakout scan. Each widget is read in full, up to
# FUNDAMENTALS_ROWS stocks, not only the first 50 its Chartink limit allows.
FUNDAMENTALS_DASHBOARD = "https://chartink.com/dashboard/503131"
FUNDAMENTALS_ROWS = 300
BREAKOUT_WIDGET = "All cash RSI scan"
BREAKOUT_MARK = "🚀"
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
# Candles read for the Heikin Ashi exit of the Auto trader: the column of each timeframe
# and how many of its candles (about a week of 2H candles, more of the others).
HA_COLUMNS = {"2 HOUR": "[0] 2 hour", "4 HOUR": "[0] 4 hour", "DAILY": "Daily"}
HA_CANDLES = 20
HA_QUERY = ("select {0} Close as 'Close', {0} ha-low as 'HA low', {0} ha-high as 'HA high', "
            "{0} ha-open as 'HA open', {0} ha-close as 'HA close' WHERE( {{33489}} ( daily close > 0 ) ) GROUP BY symbol ORDER BY 1 desc")
# The Heikin Ashi entry scans the Auto trader runs itself (fetch_ha_signals), instead of the
# HA dashboard's widgets: per timeframe, the running candle, the one before and the one before
# that, and the column of the SMA of the RSI (recorded with the trade, not a condition).
HA_SCAN_TIMEFRAMES = {
    "2 HOUR": ("2H", "[0] 2 hour", "[-1] 2 hour", "[-2] 2 hour", "Sma 2hr rsi"),
    "4 HOUR": ("4H", "[0] 4 hour", "[-1] 4 hour", "[-2] 4 hour", "Sma 4hr rsi"),
    "DAILY": ("Daily", "daily", "1 day ago", "2 day ago", "Dly SMA RSI"),
}
# The 2H candles of the HA VP strategy (ha_vp.py): close, high, low and HA values of each
# F&O stock, about two weeks of them, to work out the signal, ATR(14) and EMA 8 from.
VP_QUERY = ("select [0] 2 hour Close as 'Close', [0] 2 hour High as 'High', [0] 2 hour Low as 'Low', "
            "[0] 2 hour ha-open as 'HA open', [0] 2 hour ha-high as 'HA high', [0] 2 hour ha-low as 'HA low', "
            "[0] 2 hour ha-close as 'HA close', [0] 2 hour Volume as 'Volume' "
            "WHERE( {33489} ( daily close > 0 ) ) GROUP BY symbol ORDER BY 1 desc")
# Slots: about 165 candles (older days have an empty 3:15 PM slot). Chartink answers an empty list past
# about 220 (since Nov 2026; it took 300 before), so fetch_vp_candles steps down from here.
VP_CANDLES = 220
VP_LIVE = 5  # the latest candles, the running one with them: a long series leaves it out
# The 4H and Daily candles of the HA PURE strategies (ha_pure.py), as VP_QUERY's rows (volume 0).
HA_TF_COLUMNS = {"4 HOUR": "[0] 4 hour", "DAILY": "daily", "WEEKLY": "weekly"}  # WEEKLY: HA WK BRK (ha_weekly.py)
HA_TF_QUERY = ("select {0} Close as 'Close', {0} High as 'High', {0} Low as 'Low', {0} ha-open as 'HA open', "
               "{0} ha-high as 'HA high', {0} ha-low as 'HA low', {0} ha-close as 'HA close' "
               "WHERE( {{33489}} ( daily close > 0 ) ) GROUP BY symbol ORDER BY 1 desc")
HA_TF_CANDLES = 60
# The RSI strategies' 30-minute exit (auto_trader.rsi30_exit): the close and the SMA(14) of the RSI(14)
# of each 30-minute candle (from 9:15 AM), about three sessions of them, the running one last.
RSI30_QUERY = ("select [0] 30 minute Close as 'Close', [0] 30 minute Sma( [0] 30 minute Rsi( 14 ) , 14 ) as 'Rsi sma' "
               "WHERE( {33489} ( daily close > 0 ) ) GROUP BY symbol ORDER BY 1 desc")
RSI30_CANDLES = 40
HA_DOJI_BODY = 0.1  # the previous HA candle's body must be more than this share of its range
HA_SCAN_ROWS = 200


def ha_scan(timeframe: str, bearish: bool) -> str:
    """The HA entry scan of a timeframe, for F&O stocks. Bullish: the running candle is green on
    above-average volume (5 candles) and its HA close breaks above the previous HA high, after a
    previous HA candle that was red, not a doji, and had not closed above the HA high before it.
    Bearish: the mirror image."""
    _, now, prev, prev2, sma = HA_SCAN_TIMEFRAMES[timeframe]
    if bearish:
        trigger = (f"{now} close < {now} open and {now} ha-close < {prev} ha-low and "
                   f"{prev} ha-close >= {prev2} ha-low and {prev} ha-close > {prev} ha-open and "
                   f"( {prev} ha-close - {prev} ha-open ) > ( {prev} ha-high - {prev} ha-low ) * {HA_DOJI_BODY}")
    else:
        trigger = (f"{now} close > {now} open and {now} ha-close > {prev} ha-high and "
                   f"{prev} ha-close <= {prev2} ha-high and {prev} ha-close < {prev} ha-open and "
                   f"( {prev} ha-open - {prev} ha-close ) > ( {prev} ha-high - {prev} ha-low ) * {HA_DOJI_BODY}")
    return (f"select Daily Close as 'Price', {now} Rsi( 14 ) as 'RSI', {now} Sma( {now} Rsi( 14 ) , 14 ) as '{sma}' "
            f"WHERE( {{33489}} ( {now} volume > {now} sma( {now} volume , 5 ) and {trigger} ) ) "
            f"GROUP BY symbol ORDER BY 1 desc")


NO_VALUE = 1e300  # Chartink fills the candles a stock has no data for with 1.7e308
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
# Active Watchlist of the Market reports: segment -> the report file in that segment's GitHub
# repository. The Futures one sets whether a signal agrees with the reports (in the simulator
# and Auto trader); the Cash one is compared with the dashboard on the COMMON tab.
WATCHLISTS = {
    "Futures": "https://raw.githubusercontent.com/SHANKARNBHAT/Report_Messenger_Claude/main/"
               "rvol_active_signals_report.txt",
}
CASH_WATCHLISTS = {
    "Cash": "https://raw.githubusercontent.com/SHANKARNBHAT/Report_Messenger_cash/main/"
            "rvol_active_signals_report.txt",
}
# Volume profile levels of the futures stocks, written after each close by volume_profile.py.
VOLUME_PROFILE_FILE = "volume_profile_levels.json"
VOLUME_PROFILE_URL = ("https://raw.githubusercontent.com/SHANKARNBHAT/Report_Messenger_Claude/main/"
                      + VOLUME_PROFILE_FILE)
VOLUME_PROFILE_PERIODS = ("Weekly", "Monthly", "3M", "6M", "1Y")
VOLUME_PROFILE_TAB = "VOLUME PROFILE"
# HA TREND tab: each F&O stock's Heikin Ashi trend on four timeframes. Bullish: a green HA candle with
# no lower wick; bearish: a red one with no upper wick. The running candle on 2H and 4H, the last
# finished one on Daily and Weekly (ha_trend_at). Daily and Weekly also read HA_TREND_HISTORY candles,
# to count how long each colour run has lasted.
HA_TREND_TAB = "HA TREND"
HA_TREND_COLUMNS = {"2 HOUR": "[0] 2 hour", "4 HOUR": "[0] 4 hour", "DAILY": "daily", "WEEKLY": "weekly"}
HA_TREND_NAMES = {"2 HOUR": "2H", "4 HOUR": "4H", "DAILY": "Daily", "WEEKLY": "Weekly"}
HA_TREND_RUNNING = ("2 HOUR", "4 HOUR")
HA_TREND_CANDLES = 3
HA_TREND_HISTORY = 40
HA_TREND_UNITS = {"DAILY": ("day", "days"), "WEEKLY": ("week", "weeks")}
DAY_CLOSE = time(15, 30)
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
    if not isinstance(payload, dict):  # Chartink at times answers a bare (empty) list
        raise ChartinkError(f"Chartink answered {type(payload).__name__} {str(payload)[:40]}, not a table")
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
def fetch_dashboard(link: str, min_rows: int = 0) -> dict:
    """Name of a public dashboard and the current results of each of its widgets.

    Each widget lists as many stocks as its limit on Chartink, or `min_rows` if more.
    """
    with requests.Session() as session:
        session.headers.update(CHARTINK_HEADERS)
        url, token, details, raw_widgets = open_dashboard(session, link)

        widgets = []
        for widget in raw_widgets:
            groups = (widget.get("jsondetails") or {}).get("groups") or {}
            name = (widget.get("name") or f"Widget {widget.get('id')}").strip()
            prices = bool(re.search(PRICE_WIDGETS, name, re.IGNORECASE))
            result = {"name": name, "query": widget.get("query") or "", "table": None, "as_of": None,
                      "error": None, "prices": prices,
                      "timeframe": None if prices else timeframe_of(name, widget.get("query") or ""),
                      "bearish": bool(re.search(BEARISH_WORDS, name, re.IGNORECASE))}
            limit = max(int(groups.get("limit") or 50), min_rows)
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
                bars[group.get("name")] = [row for row in zip(starts, *ohlc)
                                           if all(v is not None and v < NO_VALUE for v in row)]
    return bars


@st.cache_data(ttl=120, show_spinner=False)
def fetch_ha_candles(link: str = HA_DASHBOARD) -> dict[str, dict[str, list[tuple]]]:
    """timeframe -> symbol -> its last HA_CANDLES candles (start in seconds, close, HA low,
    HA high, HA open, HA close), oldest first, for the F&O stocks. The dashboard only lends a
    widget to ask with."""
    candles: dict[str, dict[str, list[tuple]]] = {}
    with requests.Session() as session:
        session.headers.update(CHARTINK_HEADERS)
        url, token, _, raw_widgets = open_dashboard(session, link)
        for timeframe, column in HA_COLUMNS.items():
            widget = {"query": HA_QUERY.format(column), "id": raw_widgets[0].get("id")}
            try:
                payload = widget_payload(session, url, token, widget, PRICE_WIDGET_ROWS, HA_CANDLES)
            except (requests.RequestException, ValueError) as exc:
                raise ChartinkError(f"Could not read the {timeframe} candles: {exc}") from exc
            if payload.get("error"):
                raise ChartinkError(str(payload["error"]))
            starts = [t // 1000 for t in (payload.get("metaData") or [{}])[0].get("tradeTimes") or []]
            candles[timeframe] = {}
            for group in payload.get("groupData") or []:
                columns = {alias: values for result in group.get("results") or [] for alias, values in result.items()}
                rows = zip(starts, *(columns.get(c) or [] for c in ("close", "ha low", "ha high", "ha open", "ha close")))
                candles[timeframe][group.get("name")] = [
                    row for row in rows if all(v is not None and v < NO_VALUE for v in row)]
    return candles


def vp_rows(payload: dict) -> dict[str, list[tuple]]:
    """symbol -> its 2H candle rows (fetch_vp_candles) in a VP_QUERY answer."""
    starts = [t // 1000 for t in (payload.get("metaData") or [{}])[0].get("tradeTimes") or []]
    candles = {}
    for group in payload["groupData"]:
        columns = {alias: values for result in group.get("results") or [] for alias, values in result.items()}
        volume = columns.get("volume") or []
        rows = zip(starts, *(columns.get(c) or [] for c in
                             ("close", "high", "low", "ha open", "ha high", "ha low", "ha close")))
        candles[group.get("name")] = [
            (*row, volume[i] if i < len(volume) and volume[i] is not None and volume[i] < NO_VALUE else 0)
            for i, row in enumerate(rows) if all(v is not None and v < NO_VALUE for v in row)]
    return candles


@st.cache_data(ttl=50, show_spinner=False)
def fetch_vp_candles(link: str = HA_DASHBOARD) -> dict[str, list[tuple]]:
    """symbol -> its last 2H candles (start in seconds, close, high, low, HA open, HA high, HA low,
    HA close, volume: 0 when not known, as for an index), oldest first, the running one last, for
    the F&O stocks, NIFTY and BANKNIFTY (ha_vp.py).

    Two requests: Chartink answers a long series (VP_CANDLES) without the running
    candle, and sometimes with an empty list, so the last VP_LIVE candles come from a short one."""
    with requests.Session() as session:
        session.headers.update(CHARTINK_HEADERS)
        url, token, _, raw_widgets = open_dashboard(session, link)
        widget = {"query": VP_QUERY, "id": raw_widgets[0].get("id")}

        def ask(sizes):
            for wanted in sizes:
                try:
                    payload = widget_payload(session, url, token, widget, PRICE_WIDGET_ROWS, wanted)
                except (requests.RequestException, ValueError):
                    continue
                if isinstance(payload, dict) and payload.get("groupData"):
                    return vp_rows(payload)
            return None

        live = ask((VP_LIVE, VP_LIVE, VP_LIVE + 1))
        history = ask((VP_CANDLES, VP_CANDLES - 20, 150, 100, 60))
    if live is None and history is None:
        raise ChartinkError("No 2H candles")
    candles = {}
    for symbol in set(live or {}) | set(history or {}):
        rows = {row[0]: row for row in (history or {}).get(symbol, [])}
        rows.update({row[0]: row for row in (live or {}).get(symbol, [])})  # the live answer wins
        candles[symbol] = [rows[start] for start in sorted(rows)]
    return candles


@st.cache_data(ttl=50, show_spinner=False)
def fetch_ha_tf_candles(timeframe: str, link: str = HA_DASHBOARD) -> dict[str, list[tuple]]:
    """symbol -> its last HA_TF_CANDLES candles of `timeframe` (HA_TF_COLUMNS), oldest first, the
    running one last, as fetch_vp_candles' rows (volume 0), for the F&O stocks, NIFTY and BANKNIFTY."""
    with requests.Session() as session:
        session.headers.update(CHARTINK_HEADERS)
        url, token, _, raw_widgets = open_dashboard(session, link)
        widget = {"query": HA_TF_QUERY.format(HA_TF_COLUMNS[timeframe]), "id": raw_widgets[0].get("id")}
        for wanted in (HA_TF_CANDLES, HA_TF_CANDLES - 5, HA_TF_CANDLES - 15, 40):
            try:
                payload = widget_payload(session, url, token, widget, PRICE_WIDGET_ROWS, wanted)
            except (requests.RequestException, ValueError):
                continue
            if isinstance(payload, dict) and payload.get("groupData"):
                return vp_rows(payload)
    raise ChartinkError(f"No {timeframe} HA candles")


@st.cache_data(ttl=50, show_spinner=False)
def fetch_ha_trend(link: str = HA_DASHBOARD) -> dict[str, dict]:
    """timeframe -> {"rows": symbol -> its last candles (fetch_vp_candles' rows): HA_TREND_CANDLES of
    2H and 4H, HA_TREND_HISTORY of Daily and Weekly; "error": None or why there are none}."""
    found = {}
    with requests.Session() as session:
        session.headers.update(CHARTINK_HEADERS)
        url, token, _, raw_widgets = open_dashboard(session, link)
        for timeframe, column in HA_TREND_COLUMNS.items():
            widget = {"query": HA_TF_QUERY.format(column), "id": raw_widgets[0].get("id")}
            wanted = HA_TREND_CANDLES if timeframe in HA_TREND_RUNNING else HA_TREND_HISTORY
            found[timeframe] = {"rows": {}, "error": "no candles"}
            for size in dict.fromkeys((wanted, wanted // 2, HA_TREND_CANDLES)):
                try:
                    payload = widget_payload(session, url, token, widget, PRICE_WIDGET_ROWS, size)
                except (requests.RequestException, ValueError) as exc:
                    found[timeframe]["error"] = str(exc) or type(exc).__name__
                    continue
                if isinstance(payload, dict) and payload.get("groupData"):
                    found[timeframe] = {"rows": vp_rows(payload), "error": None}
                    break
    return found


def ha_trend_at(timeframe: str, rows: list[tuple], now: datetime) -> int | None:
    """Index of the candle a timeframe's trend is read from: the running (last) one on 2H and 4H; on
    Daily and Weekly the last finished one, the one before the last while today's (this week's) is
    still open, that is before 3:30 PM (before Friday 3:30 PM)."""
    if not rows:
        return None
    last = len(rows) - 1
    if timeframe in HA_TREND_RUNNING:
        return last
    start = datetime.fromtimestamp(rows[last][0], IST).date()
    today = now.date()
    if timeframe == "DAILY":
        running = start == today and now.time() < DAY_CLOSE
    else:
        monday = today - timedelta(days=today.weekday())
        running = start >= monday and (today.weekday() < 4 or (today.weekday() == 4 and now.time() < DAY_CLOSE))
    if not running:
        return last
    return last - 1 if last > 0 else None


def ha_trend_side(candle: tuple | None) -> str:
    """"Bullish" for a green HA candle with no lower wick, "Bearish" for a red one with no upper wick, else ""."""
    if candle is None:
        return ""
    ha_open, ha_high, ha_low, ha_close = candle[4:8]
    if ha_close > ha_open and ha_low >= ha_open:
        return "Bullish"
    if ha_close < ha_open and ha_high <= ha_open:
        return "Bearish"
    return ""


def ha_colour(candle: tuple) -> int:
    """1 for a green HA candle, -1 for a red one, 0 for neither."""
    return (candle[7] > candle[4]) - (candle[7] < candle[4])


def colour_run(rows: list[tuple], i: int) -> tuple[int, bool]:
    """How many candles in a row, ending at rows[i], have its HA colour; and whether the run reaches
    back past the candles read (so it is at least that long)."""
    colour = ha_colour(rows[i])
    if not colour:
        return 0, False
    n = 0
    while i - n >= 0 and ha_colour(rows[i - n]) == colour:
        n += 1
    return n, i - n < 0


def ha_trend_reads(found: dict[str, dict], now: datetime) -> tuple[dict, dict[str, str]]:
    """symbol -> timeframe -> {"side", "run", "more", "before", "more_before"} for each stock with a
    strong candle (ha_trend_side): run, the candles of its colour so far (more: at least that many);
    before, the run of the other colour before it. And timeframe -> the candle read, as text."""
    reads: dict[str, dict[str, dict]] = {}
    latest: dict[str, int] = {}
    for timeframe, got in found.items():
        for symbol, rows in got["rows"].items():
            i = ha_trend_at(timeframe, rows, now)
            if i is None:
                continue
            latest[timeframe] = max(latest.get(timeframe, 0), rows[i][0])
            side = ha_trend_side(rows[i])
            if not side:
                continue
            run, more = colour_run(rows, i)
            before, more_before = colour_run(rows, i - run) if i - run >= 0 else (0, False)
            reads.setdefault(symbol, {})[timeframe] = {"side": side, "run": run, "more": more,
                                                       "before": before, "more_before": more_before}
    formats = {"DAILY": "%a %d %b", "WEEKLY": "week of %d %b"}
    shown = {tf: datetime.fromtimestamp(t, IST).strftime(formats.get(tf, "%a %d %b %H:%M")) for tf, t in latest.items()}
    return reads, shown


def ha_trend_table(found: dict[str, dict], now: datetime) -> tuple[pd.DataFrame, pd.DataFrame, dict[str, str]]:
    """The HA TREND tables: one row per stock with a strong candle on at least one timeframe (its mark
    on each, how long the Daily and Weekly colour has run, and how many agree), most aligned first;
    the stocks whose last Daily or Weekly candle turned the trend (the first of its colour); and
    timeframe -> the candle read."""
    reads, shown = ha_trend_reads(found, now)
    rows = []
    for symbol, by_tf in reads.items():
        bulls = sum(r["side"] == "Bullish" for r in by_tf.values())
        bears = sum(r["side"] == "Bearish" for r in by_tf.values())
        lead = "Bullish" if bulls >= bears else "Bearish"
        other = "bearish" if lead == "Bullish" else "bullish"
        row = {"Symbol": symbol}
        for tf, name in HA_TREND_NAMES.items():
            row[name] = MARKS.get(by_tf.get(tf, {}).get("side", ""), "")
            if tf in HA_TREND_UNITS:
                row[f"{name} since"] = by_tf[tf]["run"] if tf in by_tf else None
        row["Aligned"] = f"{max(bulls, bears)} {lead.lower()}" + (f" · {min(bulls, bears)} {other}" if bulls and bears else "")
        row["_n"], row["_bull"] = max(bulls, bears) - min(bulls, bears), lead == "Bullish"
        rows.append(row)
    columns = ["Symbol", "2H", "4H", "Daily", "Daily since", "Weekly", "Weekly since", "Aligned", "_n", "_bull"]
    table = pd.DataFrame(rows, columns=columns).astype({"Daily since": "Int64", "Weekly since": "Int64"})
    table = table.sort_values(["_n", "_bull", "Symbol"], ascending=[False, False, True])
    table = table.drop(columns=["_n", "_bull"]).reset_index(drop=True)

    turned = []
    for symbol, by_tf in reads.items():
        for tf in ("WEEKLY", "DAILY"):
            r = by_tf.get(tf)
            if not r or r["run"] != 1:
                continue
            singular, plural = HA_TREND_UNITS[tf]
            was = "red" if r["side"] == "Bullish" else "green"
            after = (f"{r['before']}{'+' if r['more_before'] else ''} {was} {singular if r['before'] == 1 else plural}"
                     if r["before"] else "a flat candle")
            marks = table.loc[table["Symbol"] == symbol, ["2H", "4H", "Daily", "Weekly"]].iloc[0].to_dict()
            turned.append({"Symbol": symbol, "Turned": f"{MARKS[r['side']]} on {HA_TREND_NAMES[tf]}",
                           "After": after, **marks, "_w": tf == "WEEKLY", "_bull": r["side"] == "Bullish"})
    turned = pd.DataFrame(turned, columns=["Symbol", "Turned", "After", "2H", "4H", "Daily", "Weekly", "_w", "_bull"])
    turned = turned.sort_values(["_w", "_bull", "Symbol"], ascending=[False, False, True])
    return table, turned.drop(columns=["_w", "_bull"]).reset_index(drop=True), shown


@st.cache_data(ttl=50, show_spinner=False)
def fetch_rsi30(link: str = HA_DASHBOARD) -> dict[str, list[tuple]]:
    """symbol -> its last RSI30_CANDLES 30-minute candles (start in seconds, close, SMA 14 of RSI 14),
    oldest first, the running one last, for the F&O stocks, NIFTY and BANKNIFTY."""
    with requests.Session() as session:
        session.headers.update(CHARTINK_HEADERS)
        url, token, _, raw_widgets = open_dashboard(session, link)
        widget = {"query": RSI30_QUERY, "id": raw_widgets[0].get("id")}
        for wanted in (RSI30_CANDLES, RSI30_CANDLES - 5, 20):
            try:
                payload = widget_payload(session, url, token, widget, PRICE_WIDGET_ROWS, wanted)
            except (requests.RequestException, ValueError):
                continue
            if isinstance(payload, dict) and payload.get("groupData"):
                break
        else:
            raise ChartinkError("No 30-minute RSI")
    starts = [t // 1000 for t in (payload.get("metaData") or [{}])[0].get("tradeTimes") or []]
    found = {}
    for group in payload["groupData"]:
        columns = {alias: values for result in group.get("results") or [] for alias, values in result.items()}
        rows = zip(starts, columns.get("close") or [], columns.get("rsi sma") or [])
        found[group.get("name")] = [row for row in rows if all(v is not None and v < NO_VALUE for v in row)]
    return found


@st.cache_data(ttl=50, show_spinner=False)
def fetch_ha_signals(link: str = HA_DASHBOARD) -> dict:
    """The HA entry scans (ha_scan) of each timeframe, as fetch_dashboard returns a dashboard's
    widgets. The dashboard only lends a widget to ask with."""
    with requests.Session() as session:
        session.headers.update(CHARTINK_HEADERS)
        url, token, _, raw_widgets = open_dashboard(session, link)
        widgets = []
        for timeframe, (label, *_) in HA_SCAN_TIMEFRAMES.items():
            for bearish in (False, True):
                query = ha_scan(timeframe, bearish)
                result = {"name": f"{label} HA {'Bearish' if bearish else 'Bullish'} scan", "query": query,
                          "table": None, "as_of": None, "error": None, "prices": False,
                          "timeframe": timeframe, "bearish": bearish}
                try:
                    payload = widget_payload(session, url, token, {"query": query, "id": raw_widgets[0].get("id")},
                                             HA_SCAN_ROWS, 1)
                    result["table"], result["as_of"] = widget_table(query, payload)
                except (requests.RequestException, ValueError, ChartinkError) as exc:
                    result["error"] = str(exc) or type(exc).__name__
                widgets.append(result)
    return {"name": "HA entry scans", "link": url, "widgets": widgets, "fetched_at": datetime.now(IST)}


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
def fetch_watchlists(cash: bool = False) -> tuple[dict[str, pd.DataFrame], list[str]]:
    """Each segment's Active Watchlist from GitHub (the Cash one if `cash`, else the Futures one),
    and messages for any that failed."""
    watchlists, errors = {}, []
    for segment, url in (CASH_WATCHLISTS if cash else WATCHLISTS).items():
        try:
            # A changing query string skips GitHub's ~5 minute raw-file cache.
            response = requests.get(url, params={"t": int(datetime.now().timestamp())}, timeout=15)
            response.raise_for_status()
            watchlists[segment] = parse_watchlist(response.text)
        except requests.RequestException as exc:
            errors.append(f"Could not load the {segment} watchlist: {exc}")
    return watchlists, errors


@st.cache_data(ttl=600, show_spinner=False)
def fetch_volume_profile() -> dict:
    """The volume profile file from GitHub, else the copy beside this page."""
    try:
        # A changing query string skips GitHub's ~5 minute raw-file cache.
        response = requests.get(VOLUME_PROFILE_URL, params={"t": int(datetime.now().timestamp())}, timeout=15)
        response.raise_for_status()
        return response.json()
    except (requests.RequestException, ValueError):
        return json.loads(Path(__file__).with_name(VOLUME_PROFILE_FILE).read_text(encoding="utf-8"))


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


def fundamentals_matches(widgets: list[dict], fundamentals: list[dict]) -> pd.DataFrame:
    """Futures stocks (the dashboard's price widgets) on a list of the Fundamentals dashboard:
    a ✅ under each list it is on, and its Chartink signals. Stocks with a signal come first."""
    futures = set().union(*(set(w["table"]["Symbol"]) for w in widgets if w["prices"] and w["table"] is not None))
    # The institutional lists, then the breakout scan.
    lists = sorted((w for w in fundamentals if w["table"] is not None), key=lambda w: w["name"] == BREAKOUT_WIDGET)
    found = chartink_sides(widgets)
    rows = []
    for symbol in sorted(futures):
        on = {(f"{BREAKOUT_MARK} Breakout" if w["name"] == BREAKOUT_WIDGET else w["name"]):
              "✅" if symbol in set(w["table"]["Symbol"]) else "" for w in lists}
        if any(on.values()):
            rows.append({"Symbol": symbol, **on, **timeframe_columns(found.get(symbol, {}))})
    table = pd.DataFrame(rows)
    if table.empty:
        return table
    signalled = table[list(TIMEFRAMES)].ne("").any(axis=1)
    count = table[[c for c in table.columns if c not in ("Symbol", *TIMEFRAMES)]].eq("✅").sum(axis=1)
    order = pd.DataFrame({"signalled": signalled, "count": count, "Symbol": table["Symbol"]})
    return table.loc[order.sort_values(["signalled", "count", "Symbol"], ascending=[False, False, True]).index]


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
    names.insert(1, HA_TREND_TAB)
    names.append(VOLUME_PROFILE_TAB)
    for tab, timeframe in zip(st.tabs(names), names):
        with tab:
            if timeframe == "COMMON":
                show_common(data["widgets"])
                continue
            if timeframe == "OTHER":
                for widget in others:
                    show_widget("⚪", widget)
                continue
            if timeframe == VOLUME_PROFILE_TAB:
                show_volume_profile(data["widgets"])
                continue
            if timeframe == HA_TREND_TAB:
                show_ha_trend()
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
    """COMMON tab: futures stocks on the Fundamentals lists, stocks on the Cash Active Watchlist,
    then stocks across timeframes per strategy."""
    st.subheader("🔗 Common between Chartink and Fundamentals")
    try:
        fundamentals = fetch_dashboard(FUNDAMENTALS_DASHBOARD, FUNDAMENTALS_ROWS)["widgets"]
    except ChartinkError as exc:
        st.error(f"Chartink (Fundamentals dashboard): {exc}")
    else:
        matches = fundamentals_matches(widgets, fundamentals)
        st.caption(f"{len(matches)} futures stock(s) on a list of the Fundamentals page: a ✅ under each list, "
                   "then the stock's signals on this dashboard. Stocks with a signal first.")
        if matches.empty:
            st.info("No futures stock is on a Fundamentals list right now.")
        else:
            show_table(matches)

    st.subheader("🔗 Common between Chartink and CASH")
    watchlists, errors = fetch_watchlists(cash=True)
    for error in errors:
        st.warning(error)
    matches = watchlist_matches(widgets, watchlists)
    st.caption(f"{len(matches)} stock(s) on this Chartink dashboard that are also on the Active Watchlist of the "
               "Cash Market reports. Agrees: the report's bias matches every Chartink signal.")
    if matches.empty:
        st.info("No stock is on both the Chartink dashboard and the Cash Active Watchlist right now.")
    else:
        show_table(matches)

    show_common_triggers("🔁 Common RSI triggers across Chartink timeframes", widgets)
    try:
        ha_widgets = fetch_dashboard(HA_DASHBOARD)["widgets"]
    except ChartinkError as exc:
        st.error(f"Chartink (Heikin Ashi dashboard): {exc}")
        return
    show_common_triggers("🔁 Common Heikin Ashi triggers across Chartink timeframes", ha_widgets)


def show_common_triggers(title: str, widgets: list[dict]) -> None:
    """One strategy's stocks found in two or more timeframes."""
    st.subheader(title)
    common = common_stocks(widgets)
    st.caption(f"{len(common)} stock(s) found in two or more timeframes")
    if common.empty:
        st.info("No stock is in more than one timeframe right now.")
    else:
        show_table(common)


def show_volume_profile(widgets: list[dict]) -> None:
    """VOLUME PROFILE tab: POC, VAH, VAL and the low volume zones of each futures stock, over each period."""
    try:
        levels = fetch_volume_profile()
    except (OSError, ValueError) as exc:
        st.error(f"Could not load the volume profile levels: {exc}")
        return
    periods_col, stocks_col = st.columns([3, 1], vertical_alignment="bottom")
    periods = periods_col.multiselect("Periods", VOLUME_PROFILE_PERIODS, default=VOLUME_PROFILE_PERIODS,
                                      key="volume_profile_periods")
    on_dashboard = stocks_col.toggle("Dashboard stocks only", key="volume_profile_dashboard")
    table = pd.DataFrame(levels["stocks"])
    if on_dashboard:
        table = table[table["Symbol"].isin(chartink_sides(widgets))]
    dates = " · ".join(f"{period}: {pd.Timestamp(first):%d %b %Y} to {pd.Timestamp(last):%d %b %Y}"
                       for period, (first, last) in levels["periods"].items())
    st.caption(f"{len(table)} stock(s) · worked out {levels['generated']} IST · {dates}")
    # A file from before the low volume zones were added has no LV columns.
    columns = ["Symbol", "Close", *(f"{period} {level}" for period in periods
                                    for level in ("POC", "VAH", "VAL", "LV top", "LV bottom")
                                    if level in ("POC", "VAH", "VAL") or f"{period} {level}" in table.columns)]
    if table.empty:
        st.info("None of the dashboard's stocks has levels.")
    else:
        show_table(table[columns])
    st.caption("POC: the price with the most volume. VAH and VAL: the top and bottom of the 70% of volume around it. "
               "Low volume zones, outside the value area: from the VAH up to LV top (the period's high) and from the "
               "VAL down to LV bottom (its low). "
               "Weekly and Monthly are the last finished week and month; 3M, 6M and 1Y are rolling, up to the last "
               "close. From Yahoo's 15-minute bars (60-minute before the last 60 days), in bins of 0.25% of the price, "
               "with each bar's volume spread evenly over its range. Cash stock on NSE, so a futures chart sits apart "
               "by the premium. A blank means too little history for the period, as after a listing or demerger.")


def show_ha_trend() -> None:
    """HA TREND tab: the F&O stocks whose Daily or Weekly Heikin Ashi trend just turned, then every one
    with a strong Heikin Ashi candle on 2H, 4H, Daily or Weekly."""
    try:
        with st.spinner("Loading the Heikin Ashi candles from Chartink..."):
            found = cached_ha_trend()
    except (requests.RequestException, ValueError, ChartinkError) as exc:
        st.error(f"Could not load the Heikin Ashi candles: {exc}")
        return
    for timeframe, got in found.items():
        if got["error"]:
            st.warning(f"No {HA_TREND_NAMES[timeframe]} candles: {got['error']}")
    table, turned, read = ha_trend_table(found, datetime.now(IST))

    st.subheader("🆕 Just turned on Daily / Weekly")
    st.caption(f"The first strong HA candle of a new colour: the Daily candle of {read.get('DAILY', '-')}, the "
               f"Weekly candle of the {read.get('WEEKLY', '-')}. Today's Daily candle counts from 3:30 PM, this "
               "week's Weekly candle from Friday 3:30 PM. After: the run of the other colour it ended.")
    if turned.empty:
        st.info("No stock turned on its last Daily or Weekly candle.")
    else:
        show_table(turned)

    st.subheader("All trends")
    side_col, aligned_col = st.columns([2, 1], vertical_alignment="bottom")
    side = side_col.segmented_control("Show", ["All", "Bullish", "Bearish"], default="All", key="ha_trend_side") or "All"
    least = aligned_col.selectbox("Aligned on at least", [1, 2, 3, 4], index=0, key="ha_trend_least",
                                  format_func=lambda n: f"{n} timeframe{'s' if n > 1 else ''}")
    names = list(HA_TREND_NAMES.values())
    marks = table[names]
    bulls, bears = marks.eq(MARKS["Bullish"]).sum(axis=1), marks.eq(MARKS["Bearish"]).sum(axis=1)
    if side == "Bullish":
        keep = (bulls >= least) & (bears == 0)
    elif side == "Bearish":
        keep = (bears >= least) & (bulls == 0)
    else:
        keep = (bulls >= least) | (bears >= least)
    shown = table[keep]
    candles = " · ".join(f"{HA_TREND_NAMES[tf]}: {read[tf]}" for tf in HA_TREND_COLUMNS if tf in read)
    st.caption(f"{len(shown)} stock(s) · {int((bulls[keep] > 0).sum())} with a bullish, "
               f"{int((bears[keep] > 0).sum())} with a bearish timeframe · candles read: {candles}")
    if shown.empty:
        st.info("No stock matches.")
    else:
        show_table(shown)
    st.caption("Bullish 🟢: a green Heikin Ashi candle with no lower wick (HA low = HA open). Bearish 🔴: a red one "
               "with no upper wick (HA high = HA open). A blank: neither, as a candle with wicks on both sides. 2H and "
               "4H read the running candle, so they can change before it closes; Daily and Weekly the last finished "
               "candle (today's after 3:30 PM, this week's after Friday 3:30 PM). Daily since / Weekly since: the "
               f"candles of that colour in a row so far, 1 the first (a {HA_TREND_HISTORY} may be more). Bullish or "
               "Bearish with a side chosen: only stocks with none the other way. F&O stocks, NIFTY and BANKNIFTY, "
               "from Chartink.")


@st.cache_data(ttl=50, show_spinner=False)
def cached_ha_trend() -> dict[str, dict]:
    return fetch_ha_trend()


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
