"""
F&O ATR page: the F&O stocks whose daily ATR (14) is greater than their option strike interval,
i.e. that usually move more than one strike a day.

ATR, close and lot size: stock_market_data.db (stock_ohlc_indicators, refreshed every weekday
by the pipeline). Strike intervals: strike_steps.json (strike_steps.py, refreshed monthly by
the strike_steps GitHub Actions job).
"""

from __future__ import annotations

import os
import sqlite3
from datetime import datetime
from urllib.parse import quote

import pandas as pd
import streamlit as st

import futures_simulator as fs
import strike_steps
from page_settings import remember

HERE = os.path.dirname(os.path.abspath(__file__))
DB = os.path.join(HERE, "stock_market_data.db")
STEPS = os.path.join(HERE, "strike_steps.json")

st.title("📏 F&O stocks: ATR vs strike interval")


@st.cache_data(ttl=3600, show_spinner=False)
def load(db_mtime: float, steps_mtime: float) -> tuple[pd.DataFrame, str, str]:
    """The latest day's ATR rows joined with the strike intervals; (table, ATR date, steps date)."""
    return strike_steps.fno_atr()


try:
    data, atr_date, steps_date = load(os.path.getmtime(DB), os.path.getmtime(STEPS))
except (OSError, sqlite3.Error, ValueError, KeyError) as e:
    st.error(f"Could not read the ATR data or strike intervals: {e}")
    st.stop()


def nice(day: str) -> str:
    try:
        return datetime.strptime(day, "%Y-%m-%d").strftime("%d %b %Y")
    except ValueError:
        return day


saved_ratio = fs.atr_strikes_above()
# Starts at the saved ratio; kept while another page is open (report_app.py restores both keys).
st.session_state.setdefault("atr-min-ratio", saved_ratio)

top = st.columns([2, 2, 1.2, 1.8], vertical_alignment="bottom")
least = top[0].number_input("ATR above × strike interval", min_value=0.5, max_value=10.0,
                            step=0.5, key="atr-min-ratio", on_change=remember, args=("atr-min-ratio",),
                            help="1 = ATR greater than one strike interval; 3.5 = ATR covers more than "
                                 "three and a half strikes.")
search = top[1].text_input("Find a stock", key="atr-search", placeholder="e.g. RELIANCE",
                           on_change=remember, args=("atr-search",)).strip().upper()
if top[2].button("💾 Save ratio", disabled=least == saved_ratio, width="stretch",
                 help="Use this ratio in the Futures simulator and Auto trader too: they only trade "
                      "stocks whose ATR is above it."):
    with st.spinner("Saving..."):
        error = fs.save_atr_strikes_above(least)
    if error:
        st.error(f"Could not save the ratio: {error}")
    else:
        st.toast(f"Saved: stocks with ATR above {least:g} strikes are traded")
        st.rerun()
top[3].caption(f"Saved ratio: **{saved_ratio:g}** (Futures simulator and Auto trader)"
               + ("" if least == saved_ratio else " · not saved yet"))

# "above", as the simulator and auto trader filter, so this table lists exactly the stocks they trade
shown = data[data["ratio"] > least]
if search:
    shown = shown[shown["symbol"].str.contains(search, regex=False)]
shown = shown.sort_values("ratio", ascending=False)

m = st.columns(3)
m[0].metric("Stocks shown", f"{len(shown)} of {len(data)}")
m[1].metric("ATR as of", nice(atr_date))
m[2].metric("Strike intervals as of", nice(steps_date))

table = pd.DataFrame({
    # Chartink chart; the name after "#" is what the link shows (so M&M reads M&M, not M%26M)
    "Stock": [f"https://chartink.com/stocks-new?symbol={quote(s, safe='')}#{s}" for s in shown["symbol"]],
    "Close": shown["close"].round(2),
    "ATR (14)": shown["atr_14"].round(2),
    "ATR %": (100 * shown["atr_14"] / shown["close"]).round(2),
    "Strike interval": shown["step"],
    "ATR ÷ interval": shown["ratio"].round(2),
    "Lot": shown["lot_size"].astype("Int64"),
    "ATR × lot (₹)": (shown["atr_14"] * shown["lot_size"]).round(0).astype("Int64"),
})
st.dataframe(
    table, hide_index=True, width="stretch", height=min(35 * (len(table) + 1) + 3, 760),
    column_config={
        "Stock": st.column_config.LinkColumn("Stock", display_text=r"#(.+)$", help="Opens the chart on Chartink"),
        "ATR ÷ interval": st.column_config.ProgressColumn(
            "ATR ÷ interval", format="%.2f", min_value=0.0, max_value=max(5.0, float(data["ratio"].max() or 5)),
            help="How many strikes the stock moves on an average day."),
        "ATR × lot (₹)": st.column_config.NumberColumn(format="%d", help="A day's average move on one lot."),
        "ATR %": st.column_config.NumberColumn(format="%.2f%%"),
    })
st.caption("ATR: 14-day average true range of daily candles, from the market data pipeline (every weekday). "
           "Strike interval: the stock's regular option strike gap around its price, nearest expiry, from "
           "Dhan's instrument list (refreshed monthly; adjusted strikes after dividends are ignored). "
           "ATR ÷ interval: how many strikes an average day's range covers.")
