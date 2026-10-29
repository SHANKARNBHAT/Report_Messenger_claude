"""
Futures Trade Simulator
=======================

Paper trading for NSE futures. Shown as the "Futures simulator" page of report_app.py.

  * Signals     the widgets of the Chartink dashboard (chartink_dashboard.py) of a
                timeframe (2H first), checked against the Active Watchlist of the
                Market reports; executed at the latest price or at the open of the
                candle the signal is on
  * Order form  buy or sell, lots from futures stocks.csv, SL, target, trailing SL
  * Positions   entry price, latest price and unrealized P&L
  * EOD         intraday positions are squared off at 3:15 PM IST
  * Ledger      statutory charges, net P&L, win rate and the trade log

Prices are not streamed. An order is entered at the latest price, or at a price
typed in. Latest prices come from the price-list widgets of the Chartink dashboard
("Futures stocks below 325"...), else from Yahoo Finance; the 5-minute bars that
settlement replays come from Yahoo Finance. Open positions are checked whenever the page opens, on
"Refresh prices" and on "Run EOD settlement": the 5-minute bars since each entry
are replayed, so a trade that touched its SL, trailing SL or target closes at that
level, and one still open at 3:15 PM closes at the 3:15 PM price. "Run EOD
settlement" also closes, at the latest price, positions whose session is not over.

Positions, capital and trade history are kept in the browser (localStorage): they
survive a refresh, but each browser has its own account.
"""

from __future__ import annotations

import math
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, time, timedelta
from pathlib import Path
from urllib.parse import quote

import pandas as pd
import requests
import streamlit as st

from chartink_dashboard import (CHARTINK_HEADERS, DEFAULT_DASHBOARD, IST, TIMEFRAMES, ChartinkError,
                                fetch_dashboard, fetch_watchlists)

# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------
FUTURES_CSV = Path(__file__).with_name("futures stocks.csv")
# Futures symbol of the index rows of the CSV, which hold Yahoo symbols.
INDEX_SYMBOLS = {
    "^NSEI": "NIFTY", "^NSEBANK": "BANKNIFTY",
    "NIFTY_FIN_SERVICE.NS": "FINNIFTY", "NIFTY_MID_SELECT.NS": "MIDCPNIFTY",
}
START_CAPITAL = 1_000_000
MARGIN_RATE = 0.20          # required margin, as a share of turnover
AUTO_SL_PCT = 1.5           # stop loss and target of auto-executed signals
AUTO_TP_PCT = 3.0
DEFAULT_TIMEFRAME = "2 HOUR"  # tab name in chartink_dashboard.TIMEFRAMES
# Candle length in minutes of the intraday timeframes; candles start at 9:15 AM.
CANDLE_MINUTES = {"2 HOUR": 120, "4 HOUR": 240}
SESSION_OPEN = time(9, 15)
SQUARE_OFF = time(15, 15)
# Estimated statutory charges on NSE futures, as a share of turnover.
CHARGES = {
    "brokerage_per_order": 20.0,  # ₹20 or 0.03% per executed order, whichever is lower
    "brokerage_rate": 0.0003,
    "stt_sell": 0.0005,           # STT on the sell side, 0.05% from 1 April 2026
    "exchange": 0.0000173,        # NSE transaction charges, both sides
    "sebi": 0.000001,             # ₹10 per crore, both sides
    "stamp_buy": 0.00002,         # stamp duty on the buy side
    "gst": 0.18,                  # on brokerage, exchange and SEBI charges
}

YAHOO_CHART_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{}"
BAR_SECONDS = 300  # 5-minute bars
KEY = "futures_simulator"
RESULT = "_futures_simulator_result"


# ---------------------------------------------------------------------------
# Instruments and prices
# ---------------------------------------------------------------------------
@st.cache_data(show_spinner=False)
def load_instruments() -> list[dict]:
    """NSE futures from futures stocks.csv: symbol, name, lot size and Yahoo symbol."""
    table = pd.read_csv(FUTURES_CSV).drop_duplicates("Symbol")
    rows = []
    for _, row in table.iterrows():
        yahoo = str(row["Symbol"]).strip()
        if "=" in yahoo:  # MCX commodities, priced in US dollars on Yahoo
            continue
        symbol = INDEX_SYMBOLS.get(yahoo, yahoo)
        if yahoo not in INDEX_SYMBOLS and not yahoo.startswith("^"):
            yahoo += ".NS"
        rows.append({"symbol": symbol, "name": str(row["Stock Name"]).strip(),
                     "lot": int(row["Lot size"]), "yahoo": yahoo})
    return sorted(rows, key=lambda r: r["symbol"])


def yahoo_chart(yahoo: str, interval: str, period: str) -> dict:
    response = requests.get(YAHOO_CHART_URL.format(quote(yahoo, safe="")),
                            params={"interval": interval, "range": period},
                            headers=CHARTINK_HEADERS, timeout=15)
    response.raise_for_status()
    result = (response.json().get("chart") or {}).get("result")
    if not result:
        raise ValueError(f"No Yahoo Finance data for {yahoo}")
    return result[0]


@st.cache_data(ttl=60, show_spinner=False)
def fetch_quotes(symbols: tuple[tuple[str, str], ...]) -> dict[str, dict]:
    """symbol -> latest price and its time (ms), for (symbol, Yahoo symbol) pairs."""
    def one(pair):
        symbol, yahoo = pair
        try:
            meta = yahoo_chart(yahoo, "1d", "1d")["meta"]
        except (requests.RequestException, ValueError, KeyError):
            return symbol, None
        price, when = meta.get("regularMarketPrice"), meta.get("regularMarketTime")
        return symbol, {"price": price, "time": when * 1000 if when else None} if price else None

    with ThreadPoolExecutor(8) as pool:
        return {symbol: q for symbol, q in pool.map(one, symbols) if q}


def chartink_prices(dashboard: dict | None) -> dict[str, dict]:
    """symbol -> latest price and its time (ms), from the price-list widgets of the dashboard."""
    prices = {}
    for widget in (dashboard or {}).get("widgets", []):
        table = widget["table"]
        if not widget["prices"] or table is None or "Price" not in table:
            continue
        when = int(widget["as_of"].timestamp() * 1000) if widget["as_of"] else None
        for symbol, price in zip(table["Symbol"], table["Price"]):
            if pd.notna(price):
                prices[symbol] = {"price": round(float(price), 2), "time": when, "source": "Chartink"}
    return prices


def latest_prices(symbols: set[str], instruments: dict[str, dict], dashboard: dict | None) -> dict[str, dict]:
    """symbol -> latest price, time (ms) and source: Chartink first, else Yahoo Finance."""
    found = {s: q for s, q in chartink_prices(dashboard).items() if s in symbols}
    rest = tuple(sorted((s, instruments[s]["yahoo"]) for s in symbols if s in instruments and s not in found))
    found.update({s: {**q, "source": "Yahoo"} for s, q in fetch_quotes(rest).items()})
    return found


def load_dashboard() -> tuple[dict | None, str | None]:
    """The Chartink dashboard, or None and the error."""
    try:
        return fetch_dashboard(DEFAULT_DASHBOARD), None
    except ChartinkError as exc:
        return None, f"Chartink: {exc}"


@st.cache_data(ttl=120, show_spinner=False)
def fetch_bars(requests_: tuple[tuple[str, str], ...]) -> dict[tuple[str, str], list | str]:
    """(Yahoo symbol, range) -> 5-minute bars (start, open, high, low, close), or an error."""
    def one(request):
        yahoo, period = request
        try:
            chart = yahoo_chart(yahoo, "5m", period)
            bars = chart["indicators"]["quote"][0]
            rows = zip(chart.get("timestamp") or [], bars["open"], bars["high"], bars["low"], bars["close"])
            return request, [row for row in rows if None not in row]
        except (requests.RequestException, ValueError, KeyError) as exc:
            return request, str(exc) or type(exc).__name__

    with ThreadPoolExecutor(8) as pool:
        return dict(pool.map(one, requests_))


def bars_range(entered: datetime, now: datetime) -> str:
    """Yahoo range that covers the bars since the entry (5-minute bars go back 60 days)."""
    days = (now - entered).days
    return "5d" if days <= 3 else "1mo" if days <= 20 else "60d"


# ---------------------------------------------------------------------------
# Settlement
# ---------------------------------------------------------------------------
def replay(position: dict, bars: list, now: datetime, latest: float | None, settle: bool) -> dict:
    """Where a position stands after the bars since its entry.

    Closed at its SL, trailing SL or target if a bar touched it (at the bar's open if
    the price gapped past it; when SL and target fall in the same bar the SL is taken),
    closed at 3:15 PM once its session is over, else still open with its current stop.
    With `settle`, a position whose session is not over closes at the latest price.
    """
    long = position["side"] == "BUY"
    entry = float(position["entry"])
    stop, target = position.get("sl"), position.get("tp")
    tsl = position.get("tsl") or {}
    trail, step = float(tsl.get("trail") or 0), float(tsl.get("step") or 0)
    trailing = bool(tsl.get("on")) and trail > 0 and step > 0
    entered = datetime.fromtimestamp(position["time"] / 1000, IST)

    def closed(price, when, reason):
        return {"id": position["id"], "status": "closed", "exit": round(float(price), 2),
                "exit_time": int(when * 1000), "reason": reason, "stop": stop}

    def day_of(bar):
        return datetime.fromtimestamp(bar[0], IST).date()

    # The session of the trade: the entry day if entered during market hours on a
    # trading day, else the next session. Bars count from the first one starting at
    # or after the entry (an entry at a bar's open, such as a candle open, includes it).
    first = -(-int(entered.timestamp()) // BAR_SECONDS) * BAR_SECONDS
    later = [bar for bar in bars if bar[0] >= first]
    day = None
    if SESSION_OPEN <= entered.time() < SQUARE_OFF and any(day_of(bar) == entered.date() for bar in bars):
        day = entered.date()
    elif later:
        day = day_of(later[0])
    if day is None:  # no trading since the entry
        if settle and latest:
            return closed(latest, now.timestamp(), "Settled at LTP")
        return {"id": position["id"], "status": "waiting", "stop": stop}

    cutoff = datetime.combine(day, SQUARE_OFF, IST).timestamp()
    anchor, trailed = entry, False
    for start, open_, high, low, close in (bar for bar in later if bar[0] < cutoff):
        if stop is not None:
            gap = open_ <= stop if long else open_ >= stop
            if gap or (low <= stop if long else high >= stop):
                return closed(open_ if gap else stop, start, "Trailing SL" if trailed else "Stop loss")
        if target is not None:
            gap = open_ >= target if long else open_ <= target
            if gap or (high >= target if long else low <= target):
                return closed(open_ if gap else target, start, "Target")
        if trailing:
            # Every full jump step the price moves in favour, the stop follows at `trail` behind.
            move = high - anchor if long else anchor - low
            if move >= step:
                anchor += math.floor(move / step) * step * (1 if long else -1)
                level = round(anchor - trail if long else anchor + trail, 2)
                if stop is None or (level > stop if long else level < stop):
                    stop, trailed = level, True

    if now.timestamp() >= cutoff:
        session = [bar for bar in bars if bar[0] < cutoff and day_of(bar) == day]
        price = session[-1][4] if session else latest or entry
        return closed(price, cutoff, "EOD 3:15 PM")
    if settle:
        recent = [bar for bar in later if bar[0] < cutoff]
        return closed(latest or (recent[-1][4] if recent else entry), now.timestamp(), "Settled at LTP")
    return {"id": position["id"], "status": "open", "stop": stop}


def run_check() -> None:
    """Answer a check sent by the simulator: replay each open position and keep the result."""
    request = component_value("check")
    if not request:
        return
    if request.get("refresh"):
        fetch_quotes.clear()
        fetch_bars.clear()
        fetch_dashboard.clear()
    now = datetime.now(IST)
    instruments = {i["symbol"]: i for i in load_instruments()}
    positions = [p for p in request.get("positions") or [] if p.get("symbol") in instruments]
    wanted = {p["id"]: (instruments[p["symbol"]]["yahoo"],
                        bars_range(datetime.fromtimestamp(p["time"] / 1000, IST), now)) for p in positions}
    bars = fetch_bars(tuple(sorted(set(wanted.values()))))
    quotes = latest_prices({p["symbol"] for p in positions}, instruments, load_dashboard()[0])

    results = []
    for position in positions:
        found = bars[wanted[position["id"]]]
        if isinstance(found, str):
            results.append({"id": position["id"], "status": "error", "error": found})
            continue
        latest = (quotes.get(position["symbol"]) or {}).get("price")
        results.append(replay(position, found, now, latest, request.get("mode") == "eod"))
    st.session_state[RESULT] = {"id": request.get("id"), "mode": request.get("mode"), "results": results}


# ---------------------------------------------------------------------------
# Signals
# ---------------------------------------------------------------------------
def candle_open(timeframe: str, bars: list, now: datetime) -> dict | None:
    """Open price and time (ms) of the candle a signal on `timeframe` is on: the
    latest candle of the latest session (Chartink does not say when a stock matched)."""
    if not bars:
        return None
    last = datetime.fromtimestamp(bars[-1][0], IST)
    if last.date() != now.date():  # no trading yet today: the last session's last candle
        now = last
    session = datetime.combine(now.date(), SESSION_OPEN, IST)
    minutes = CANDLE_MINUTES.get(timeframe)
    if timeframe == "WEEKLY":
        start = session - timedelta(days=now.weekday())
    elif minutes:
        length = timedelta(minutes=minutes)
        start = session + max(now - session, timedelta(0)) // length * length
    else:
        start = session
    bar = next((bar for bar in bars if bar[0] >= start.timestamp()), None)
    return {"price": round(bar[1], 2), "time": bar[0] * 1000} if bar else None


def signal_rows(dashboard: dict, instruments: dict[str, dict]) -> tuple[list[dict], str]:
    """Stocks on the Chartink widgets of each timeframe, with bias and agreement with the Market reports."""
    watchlists, _ = fetch_watchlists()
    report_bias: dict[str, set[str]] = {}
    for table in watchlists.values():
        for _, row in table.iterrows():
            report_bias.setdefault(row["symbol"], set()).add(str(row["bias"]).title())

    rows = []
    for widget in dashboard["widgets"]:
        if widget["timeframe"] is None or widget["table"] is None:
            continue
        bias = "Bearish" if widget["bearish"] else "Bullish"
        for symbol in widget["table"]["Symbol"]:
            report = report_bias.get(symbol)
            rows.append({
                "symbol": symbol, "timeframe": widget["timeframe"], "strategy": widget["name"], "bias": bias,
                "agrees": "Not on watchlist" if not report else "Yes" if report == {bias} else "No",
                "fno": symbol in instruments,
            })
    return rows, f"Updated {dashboard['fetched_at']:%d %b, %H:%M} IST"


# ---------------------------------------------------------------------------
# Page
# ---------------------------------------------------------------------------
def component_value(name: str):
    """A state or trigger value the simulator sent, or None."""
    try:
        return st.session_state[KEY][name]
    except (KeyError, TypeError):
        return None


_SIMULATOR = st.components.v2.component(
    "futures_simulator",
    html=Path(__file__).with_name("futures_simulator.html").read_text(encoding="utf-8"),
    css=Path(__file__).with_name("futures_simulator.css").read_text(encoding="utf-8"),
    js=Path(__file__).with_name("futures_simulator.js").read_text(encoding="utf-8"),
)


def main() -> None:
    st.set_page_config(page_title="Futures Simulator", page_icon="🧪", layout="wide",
                       initial_sidebar_state="collapsed")
    instruments = load_instruments()
    by_symbol = {i["symbol"]: i for i in instruments}
    with st.spinner("Loading signals from Chartink..."):
        dashboard, error = load_dashboard()
        signals, signals_note = signal_rows(dashboard, by_symbol) if dashboard else ([], error)

    # Prices of the tradable signals, and of the stocks the simulator asked for
    # (open positions and the instrument picked in the order form).
    tradable = {s["symbol"] for s in signals if s["fno"]}
    quotes = latest_prices(tradable | set(component_value("symbols") or []), by_symbol, dashboard)
    # Open of the candle each tradable signal is on, to enter at the signal's candle.
    bars = fetch_bars(tuple(sorted((by_symbol[s]["yahoo"], "5d") for s in tradable)))
    now = datetime.now(IST)
    for signal in signals:
        found = bars.get((by_symbol[signal["symbol"]]["yahoo"], "5d")) if signal["fno"] else None
        signal["open"] = candle_open(signal["timeframe"], found, now) if isinstance(found, list) else None

    _SIMULATOR(
        key=KEY,
        data={
            "instruments": [{k: i[k] for k in ("symbol", "name", "lot")} for i in instruments],
            "signals": signals,
            "signals_note": signals_note,
            "quotes": quotes,
            "result": st.session_state.get(RESULT),
            "timeframes": list(TIMEFRAMES),
            "settings": {"start_capital": START_CAPITAL, "margin_rate": MARGIN_RATE,
                         "default_timeframe": DEFAULT_TIMEFRAME,
                         "auto_sl_pct": AUTO_SL_PCT, "auto_tp_pct": AUTO_TP_PCT, "charges": CHARGES},
        },
        on_check_change=run_check,
        on_symbols_change=lambda: None,
        width="stretch",
    )


if __name__ == "__main__":
    main()
