"""
The RSI 2H and 4H signal picker of the Option chain page: the Auto trader's signal log (the
auto-trader-data branch, signals/YYYY-MM-DD.json: every signal it saw, whatever it did with it) for
a recent day, and a pick switches the page's index / stock to the signal's, so its option chain and
ATM CE / PE charts show. Only stocks the page lists (ATR above the F&O ATR page's ratio).
"""

from __future__ import annotations

from datetime import date, datetime, timedelta

import requests
import streamlit as st

import dhan_data as dd
import futures_simulator as fs

SYSTEMS = {"RSI": "2H", "RSI 4H": "4H"}  # the signal log's strategies, and their timeframe
DAYS_BACK = 10  # calendar days searched for logged signals


@st.cache_data(ttl=300, show_spinner=False)
def load_days(today: str) -> dict[str, list[dict]]:
    """Up to three recent days with logged RSI 2H / 4H signals on F&O stocks, newest first:
    day -> its signals, by first seen time."""
    found: dict[str, list[dict]] = {}
    day = date.fromisoformat(today)
    for back in range(DAYS_BACK):
        if len(found) == 3:
            break
        name = (day - timedelta(days=back)).isoformat()
        try:
            response = fs.data_file(f"signals/{name}.json")
            if response.status_code == 404:
                continue
            response.raise_for_status()
            rows = response.json()
        except (requests.RequestException, ValueError):
            continue
        rows = sorted((r for r in rows.values() if r.get("system") in SYSTEMS and r.get("fno")),
                      key=lambda r: r["time"])
        if rows:
            found[name] = rows
    return found


def label(signal: dict) -> str:
    return (f"{datetime.fromtimestamp(signal['time'] / 1000, dd.IST):%H:%M} · {signal['symbol']} · "
            f"{SYSTEMS[signal['system']]} · {signal['bias']} @ {signal['price']:g}")


def picker(listed: dict[str, dict], target_key: str) -> dict | None:
    """A day and signal picker; picking a signal puts its stock in the selectbox of `target_key`. The
    signal picked (or None). `listed`: the page's stocks, so only theirs are offered."""
    try:
        days = load_days(date.today().isoformat())
    except Exception:  # no GitHub access: the page works without the picker
        days = {}
    if not days:
        return None
    cols = st.columns([1, 4])
    day = cols[0].selectbox("Signals of", list(days), key="oc-signal-day",
                            format_func=lambda d: datetime.fromisoformat(d).strftime("%a %d %b"))
    signals = [s for s in days[day] if s["symbol"] in listed]
    options = [None, *range(len(signals))]

    def apply():
        k = st.session_state.get(f"oc-signal-{day}")
        if k is not None:
            st.session_state[target_key] = signals[k]["symbol"]

    k = cols[1].selectbox(
        f"RSI 2H / 4H signal ({len(signals)} on the high-ATR stocks)", options, key=f"oc-signal-{day}",
        format_func=lambda k: "— pick one to show its stock —" if k is None else label(signals[k]), on_change=apply,
        help="The Auto trader's signal log: picking one switches the index / stock below to it. CE for a bullish "
             "signal, PE for a bearish one.")
    return None if k is None else signals[k]
