"""
Fundamentals: the long-term Chartink dashboard, where institutions (FIIs, mutual funds,
insurance companies) are raising their holdings in companies with good fundamentals.

One tab per widget, with the widget's columns as on Chartink, after a Breakouts tab: the
stocks on those lists that the dashboard's weekly breakout scan also finds. Holdings change once a
quarter, so the page does not refresh by itself. Shown as the "Fundamentals" page of
report_app.py.
"""

import pandas as pd
import streamlit as st

from chartink_dashboard import (BREAKOUT_MARK, BREAKOUT_WIDGET, CHART_SITES, DEFAULT_SNAPSHOT, FUNDAMENTALS_DASHBOARD,
                                FUNDAMENTALS_ROWS, ChartinkError, fetch_dashboard, show_table, show_widget)
from page_settings import remember

# What each widget finds, by its name on Chartink. "DII" is mutual funds plus insurance
# companies; "good fundamentals" is the filter written out in FUNDAMENTALS_NOTE.
WIDGET_NOTES = {
    "FII DII growing interest": (
        "FIIs or DIIs raised their holding every quarter for 3 quarters, by at least 1 point "
        "in all, in companies with good fundamentals."
    ),
    "Mutual Funds growing Interest": (
        "Mutual funds raised their holding every quarter for 3 quarters, by at least 1 point "
        "in all, and now hold at least 2%. Market cap over ₹1000 Cr and volume over 1 lakh; "
        "no other fundamentals."
    ),
    "FII DII new interest": (
        "FIIs or DIIs started buying last quarter: from under 0.5% to at least 1%, in "
        "companies with good fundamentals."
    ),
    "FII DII Mutual ALL Interest": (
        "FIIs and DIIs both raised their holding for 2 quarters in a row, and all "
        "institutions together hold at least 2 points more than a year ago, in companies "
        "with good fundamentals."
    ),
    "All cash RSI scan": (
        "Weekly breakout scan, on all cash stocks and indices: weekly SMA of RSI(14) between "
        "30 and 60, and this week either RSI(14) crossed above 60 or the close crossed above "
        "its 14-week average."
    ),
}
FUNDAMENTALS_NOTE = (
    "Good fundamentals: market cap over ₹1000 Cr; yearly sales and profit both up 18% or more "
    "on last year, with a profit last year; this quarter's sales above the same quarter last "
    "year; ROE over 12%; debt to equity under 1; pays a dividend."
)

st.set_page_config(page_title="Fundamentals", page_icon="🏛️", layout="wide",
                   initial_sidebar_state="collapsed")
st.title("🏛️ Fundamentals")
st.caption(
    "Stocks institutions are steadily buying, from the Chartink dashboard "
    f"[{FUNDAMENTALS_DASHBOARD.rsplit('/', 1)[1]}]({FUNDAMENTALS_DASHBOARD}). Shareholding is "
    "published once a quarter, about three weeks after the quarter ends, so these lists "
    "change slowly."
)

st.session_state.setdefault("snapshot", DEFAULT_SNAPSHOT)  # for "My indicators"
charts_col, button_col = st.columns([3, 1], vertical_alignment="bottom")
charts_col.radio("Open charts on", list(CHART_SITES), key="charts", horizontal=True,
                 on_change=remember, args=("charts",))
if button_col.button("↻ Refresh now", width="stretch"):
    fetch_dashboard.clear()

try:
    with st.spinner("Loading the dashboard from Chartink..."):
        data = fetch_dashboard(FUNDAMENTALS_DASHBOARD, FUNDAMENTALS_ROWS)
except ChartinkError as exc:
    st.error(f"Chartink: {exc}")
    st.stop()

st.caption(f"Updated {data['fetched_at']:%d %b %Y, %H:%M:%S} IST · {FUNDAMENTALS_NOTE}")
widgets = data["widgets"]
if not widgets:
    st.info("The dashboard has no widgets.")
    st.stop()

breakout = next((w for w in widgets if w["name"] == BREAKOUT_WIDGET and w["table"] is not None), None)
holdings = [w for w in widgets if w["name"] != BREAKOUT_WIDGET]
# The breakout widget goes last: the institutional lists are what this page is about.
widgets = holdings + ([w for w in widgets if w["name"] == BREAKOUT_WIDGET])
breaking = set(breakout["table"]["Symbol"]) if breakout else set()


def breakouts_table() -> pd.DataFrame:
    """Stocks breaking out that institutions are buying, with the lists each one is in."""
    found = [w for w in holdings if w["table"] is not None]
    listed = set().union(*(set(w["table"]["Symbol"]) for w in found)) if found else set()
    table = breakout["table"][breakout["table"]["Symbol"].isin(listed)].reset_index(drop=True)
    for w in found:
        table[w["name"]] = table["Symbol"].map(lambda s, names=set(w["table"]["Symbol"]): "✅" if s in names else "")
    return table


tabs = st.tabs([f"{BREAKOUT_MARK} Breakouts"] * bool(breakout) + [w["name"] for w in widgets])
if breakout:
    with tabs[0]:
        st.markdown(
            f"Stocks on the **{BREAKOUT_WIDGET}** that are also on at least one institutional "
            "list, with a ✅ under each list it is on."
        )
        table = breakouts_table()
        st.caption(f"{len(table)} stock(s) · breakout data as of {breakout['as_of']:%d %b, %H:%M} IST"
                   if breakout["as_of"] else f"{len(table)} stock(s)")
        if table.empty:
            st.info("None of the stocks breaking out this week is on an institutional list.")
        else:
            show_table(table)
for tab, widget in zip(tabs[bool(breakout):], widgets):
    with tab:
        if widget["name"] in WIDGET_NOTES:
            st.markdown(WIDGET_NOTES[widget["name"]])
        if breakout and widget is not breakout and widget["table"] is not None:
            # Mark this list's stocks that are breaking out, right after the symbol.
            marked = widget["table"].copy()
            marked.insert(1, "Breakout", marked["Symbol"].map(lambda s: BREAKOUT_MARK if s in breaking else ""))
            widget = {**widget, "table": marked}
        show_widget(None, widget)
