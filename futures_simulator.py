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
("Futures stocks below 325"...), else from Yahoo Finance. The 5-minute bars that
settlement replays come from the 5-minute Open, High, Low and Close columns of the
same widgets (about the last 4 sessions), else from Yahoo Finance. Open positions are checked whenever the page opens, on
"Refresh prices" and on "Run EOD settlement": the 5-minute bars since each entry
are replayed, so a trade that touched its SL, trailing SL or target closes at that
level, and one still open at 3:15 PM closes at the 3:15 PM price. "Run EOD
settlement" also closes, at the latest price, positions whose session is not over.

Positions, capital and trade history are kept in the browser (localStorage): they
survive a refresh, but each browser has its own account.
"""

from __future__ import annotations

import calendar
import importlib
import math
import re
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, time, timedelta
from pathlib import Path
from urllib.parse import quote

import pandas as pd
import requests
import streamlit as st

import chartink_dashboard

# Streamlit Cloud keeps imported modules in memory when it pulls new code, so a
# chartink_dashboard.py changed since the server started is reloaded here.
if Path(chartink_dashboard.__file__).stat().st_mtime > getattr(chartink_dashboard, "_loaded_at", 0):
    chartink_dashboard = importlib.reload(chartink_dashboard)
    chartink_dashboard._loaded_at = Path(chartink_dashboard.__file__).stat().st_mtime

from chartink_dashboard import (CHART_SITES, CHARTINK_HEADERS, DEFAULT_DASHBOARD, DEFAULT_SNAPSHOT, IST,
                                MY_LAYOUT, TIMEFRAMES, ChartinkError, fetch_dashboard, fetch_price_bars,
                                fetch_watchlists)

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
# A position kept going past its target with no trailing SL amounts of its own trails
# this far behind the price, in jump steps of AUTO_STEP_PCT (of the entry price).
AUTO_TRAIL_PCT = 1.5
AUTO_STEP_PCT = 0.5
DEFAULT_TIMEFRAME = "2 HOUR"  # tab name in chartink_dashboard.TIMEFRAMES
# Candle length in minutes of the intraday timeframes; candles start at 9:15 AM.
CANDLE_MINUTES = {"2 HOUR": 120, "4 HOUR": 240}
# How a position exits (its "style"):
#   eod    SL and target (or kept going past it with a trailing SL), squared off at 3:15 PM
#   trail  held overnight: SL until the target, then a trailing SL from there
#   rsi    held overnight: exits when the RSI of its timeframe crosses back (below 60 for
#          a long, above 40 for a short: the "RSI crossed" widgets), or at its SL
# Held positions close at 3:15 PM on the futures expiry day.
STYLES = {"eod": "EOD 3:15 PM", "trail": "Hold · trail", "rsi": "Hold · RSI exit"}
HELD = ("trail", "rsi")
# The "RSI crossed" widgets list a stock when its RSI on any timeframe crossed the level;
# their column of each timeframe's RSI tells if the trade's own one is past it.
RSI_EXITS = {"BUY": (r"rsi crossed below", 60), "SELL": (r"rsi crossed above", 40)}
RSI_COLUMNS = {"2 HOUR": r"^2 ?h", "4 HOUR": r"^4 ?h", "DAILY": r"^da", "WEEKLY": r"^we"}
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

# When the Auto trader runs (auto_trader.py) first saw each signal, and its price then.
SEEN_URL = ("https://raw.githubusercontent.com/SHANKARNBHAT/Report_Messenger_Claude/auto-trader-data/"
            "signals_seen.json")
YAHOO_CHART_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{}"
BAR_SECONDS = 300  # 5-minute bars
KEY = "futures_simulator"
RESULT = "_futures_simulator_result"


# ---------------------------------------------------------------------------
# Instruments and prices
# ---------------------------------------------------------------------------
@st.cache_data(show_spinner=False)
def _load_instruments(modified: float) -> list[dict]:
    """NSE futures from futures stocks.csv: symbol, name, lot size and Yahoo symbol.

    `modified` is the file's timestamp, so an updated CSV is read again."""
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


def load_instruments() -> list[dict]:
    return _load_instruments(FUTURES_CSV.stat().st_mtime)


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


@st.cache_data(ttl=60, show_spinner=False)
def load_seen() -> dict:
    """Session date -> "SYMBOL|TIMEFRAME|Bias" -> first seen time (ms) and price, or {} if not there."""
    try:
        # A changing query string skips GitHub's ~5 minute raw-file cache.
        response = requests.get(SEEN_URL, params={"t": int(datetime.now().timestamp()) // 60}, timeout=10)
        return response.json() if response.ok else {}
    except (requests.RequestException, ValueError):
        return {}


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
def expiry_after(day: date) -> date:
    """The monthly futures expiry after `day`: the last Tuesday of its month, else of the
    next month (NSE holidays, which move it a day earlier, are not counted)."""
    for ahead in (0, 1):
        year, month = divmod(day.year * 12 + day.month - 1 + ahead, 12)
        last = date(year, month + 1, calendar.monthrange(year, month + 1)[1])
        tuesday = last - timedelta(days=(last.weekday() - calendar.TUESDAY) % 7)
        if tuesday > day:
            return tuesday


def replay(position: dict, bars: list, now: datetime, latest: float | None, settle: bool) -> dict:
    """Where a position stands after the bars since its entry.

    Closed at its SL, trailing SL or target if a bar touched it (at the bar's open if
    the price gapped past it; when SL and target fall in the same bar the SL is taken),
    closed at 3:15 PM once its session is over, else still open with its current stop.
    A held position (style in HELD) runs on over the next sessions until 3:15 PM on the
    futures expiry day instead.
    A position with `run` is not closed at its target: from there its stop trails the
    price by run["trail"] in jumps of run["step"], and "passed" is set.
    With `settle`, a position whose session is not over closes at the latest price
    (not a held one).
    """
    long = position["side"] == "BUY"
    entry = float(position["entry"])
    stop, target = position.get("sl"), position.get("tp")
    tsl = position.get("tsl") or {}
    trail, step = float(tsl.get("trail") or 0), float(tsl.get("step") or 0)
    trailing = bool(tsl.get("on")) and trail > 0 and step > 0
    run = position.get("run") or {}
    run_trail, run_step = float(run.get("trail") or 0), float(run.get("step") or 0)
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

    held = position.get("style") in HELD
    if held:
        day = expiry_after(day)  # entered on an expiry day: next month's contract
    cutoff = datetime.combine(day, SQUARE_OFF, IST).timestamp()
    anchor, trailed, passed = entry, False, False
    for start, open_, high, low, close in (bar for bar in later if bar[0] < cutoff):
        if stop is not None:
            gap = open_ <= stop if long else open_ >= stop
            if gap or (low <= stop if long else high >= stop):
                reason = "TSL after target" if passed else "Trailing SL" if trailed else "Stop loss"
                return closed(open_ if gap else stop, start, reason)
        if target is not None:
            gap = open_ >= target if long else open_ <= target
            if gap or (high >= target if long else low <= target):
                hit = open_ if gap else target
                if run_trail <= 0 or run_step <= 0:
                    return closed(hit, start, "Target")
                # Kept going: from the target on, the stop trails the price.
                anchor = max(anchor, hit) if long else min(anchor, hit)
                trail, step, trailing, target, passed = run_trail, run_step, True, None, True
                level = round(anchor - trail if long else anchor + trail, 2)
                if stop is None or (level > stop if long else level < stop):
                    stop, trailed = level, True
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
        return closed(price, cutoff, "Expiry 3:15 PM" if held else "EOD 3:15 PM")
    if settle and not held:
        recent = [bar for bar in later if bar[0] < cutoff]
        return closed(latest or (recent[-1][4] if recent else entry), now.timestamp(), "Settled at LTP")
    return {"id": position["id"], "status": "open", "stop": stop, "passed": passed}


def run_check() -> None:
    """Answer a check sent by the simulator: replay each open position and keep the result."""
    request = component_value("check")
    if not request:
        return
    if request.get("refresh"):
        fetch_quotes.clear()
        fetch_bars.clear()
        fetch_dashboard.clear()
        fetch_price_bars.clear()
    results = check_positions(request.get("positions") or [], datetime.now(IST), request.get("mode") == "eod")
    st.session_state[RESULT] = {"id": request.get("id"), "mode": request.get("mode"), "results": results}


def check_positions(positions: list[dict], now: datetime, settle: bool) -> list[dict]:
    """Replay each position on the 5-minute bars since its entry (see replay); also used by auto_trader.py."""
    instruments = {i["symbol"]: i for i in load_instruments()}
    positions = [p for p in positions if p.get("symbol") in instruments]
    # Chartink's 5-minute bars when they reach back to the entry, else Yahoo Finance's.
    try:
        chartink = fetch_price_bars(DEFAULT_DASHBOARD) if positions else {}
    except ChartinkError:
        chartink = {}
    bars = {p["id"]: chartink[p["symbol"]] for p in positions
            if chartink.get(p["symbol"]) and chartink[p["symbol"]][0][0] <= p["time"] / 1000}
    wanted = {p["id"]: (instruments[p["symbol"]]["yahoo"],
                        bars_range(datetime.fromtimestamp(p["time"] / 1000, IST), now))
              for p in positions if p["id"] not in bars}
    yahoo = fetch_bars(tuple(sorted(set(wanted.values()))))
    bars.update({id_: yahoo[request_] for id_, request_ in wanted.items()})
    quotes = latest_prices({p["symbol"] for p in positions}, instruments, load_dashboard()[0])

    dashboard = load_dashboard()[0]
    results = []
    for position in positions:
        found = bars[position["id"]]
        if isinstance(found, str):
            results.append({"id": position["id"], "status": "error", "error": found})
            continue
        latest = (quotes.get(position["symbol"]) or {}).get("price")
        result = replay(position, found, now, latest, settle)
        crossed = rsi_exit(position, dashboard) if result["status"] == "open" else None
        if crossed and latest:
            result = {"id": position["id"], "status": "closed", "exit": round(float(latest), 2),
                      "exit_time": int(now.timestamp() * 1000), "reason": crossed, "stop": result["stop"]}
        results.append(result)
    return results


def rsi_exit(position: dict, dashboard: dict | None) -> str | None:
    """For a position of the rsi style on an "RSI crossed" widget with the RSI of its
    timeframe (the shortest if several) past the level: the exit reason, else None."""
    if position.get("style") != "rsi" or not dashboard:
        return None
    timeframes = [tf for tf in TIMEFRAMES if tf in (position.get("timeframes") or [])]
    name, level = RSI_EXITS[position["side"]]
    if not timeframes:
        return None
    for widget in dashboard["widgets"]:
        table = widget["table"]
        if not re.search(name, widget["name"], re.IGNORECASE) or table is None or table.empty:
            continue
        column = next((c for c in table.columns if re.search(RSI_COLUMNS[timeframes[0]], c.strip().lower())), None)
        row = table[table["Symbol"] == position["symbol"]]
        if column is None or row.empty or pd.isna(row[column].iloc[0]):
            continue
        rsi = float(row[column].iloc[0])
        if rsi < level if position["side"] == "BUY" else rsi > level:
            return f"RSI exit ({timeframes[0].replace(' HOUR', 'H').title()} {rsi:.1f})"
    return None


# ---------------------------------------------------------------------------
# Signals
# ---------------------------------------------------------------------------
def candle_open(timeframe: str, bars: list, now: datetime) -> dict | None:
    """Open price and time (ms) of the candle a signal on `timeframe` is on: the
    latest candle of the latest session (Chartink does not say when a stock matched)."""
    if not bars:
        return None
    # After the close, or before today's open, the candle of the session's last bar.
    now = min(now, datetime.fromtimestamp(bars[-1][0], IST))
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
def chart_links() -> dict[str, str]:
    """Chart site -> link to a stock's chart on it, with SYMBOL where the stock goes.

    Uses the snapshot link set on the Chartink dashboard page, as its symbol links do.
    """
    snapshot = re.search(r"load-snapshot=([\w-]+)", st.session_state.get("snapshot") or DEFAULT_SNAPSHOT)
    return {site: link("SYMBOL", snapshot.group(1) if snapshot else None)
            for site, link in CHART_SITES.items() if snapshot or site != MY_LAYOUT}


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
    # NIFTY's bars give the date of the latest session, for carrying signals over.
    bars = fetch_bars(tuple(sorted({(by_symbol[s]["yahoo"], "5d") for s in tradable} | {("^NSEI", "5d")})))
    now = datetime.now(IST)
    nifty = bars.get(("^NSEI", "5d"))
    session = (datetime.fromtimestamp(nifty[-1][0], IST) if isinstance(nifty, list) and nifty else now).date()
    for signal in signals:
        found = bars.get((by_symbol[signal["symbol"]]["yahoo"], "5d")) if signal["fno"] else None
        signal["open"] = candle_open(signal["timeframe"], found, now) if isinstance(found, list) else None
    # When the Auto trader first saw each signal; the page adds when it first saw them itself.
    seen = load_seen().get(now.date().isoformat(), {})
    for signal in signals:
        first = seen.get(f"{signal['symbol']}|{signal['timeframe']}|{signal['bias']}")
        signal["seen"] = first and {"time": first["time"], "price": first.get("price")}

    _SIMULATOR(
        key=KEY,
        data={
            "instruments": [{k: i[k] for k in ("symbol", "name", "lot")} for i in instruments],
            "signals": signals,
            "signals_note": signals_note,
            "session_date": session.isoformat(),
            "quotes": quotes,
            "result": st.session_state.get(RESULT),
            "timeframes": list(TIMEFRAMES),
            "charts": chart_links(),
            "default_chart": st.session_state.get("charts") or MY_LAYOUT,
            "settings": {"start_capital": START_CAPITAL, "margin_rate": MARGIN_RATE,
                         "default_timeframe": DEFAULT_TIMEFRAME,
                         "auto_sl_pct": AUTO_SL_PCT, "auto_tp_pct": AUTO_TP_PCT,
                         "auto_trail_pct": AUTO_TRAIL_PCT, "auto_step_pct": AUTO_STEP_PCT, "charges": CHARGES,
                         "styles": STYLES},
        },
        on_check_change=run_check,
        on_symbols_change=lambda: None,
        width="stretch",
    )


if __name__ == "__main__":
    main()
