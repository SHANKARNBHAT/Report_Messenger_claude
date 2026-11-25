"""
Autotrader NIFTY\\BNF page: the Auto trader's OPT ATM HA strategy (opt_atm.py), the NIFTY and
BANKNIFTY ATM CE / PE on 15-minute candles from Dhan, apart from the stock strategies of the Auto
trader page (auto_trader_page.py, whose account, runs and helpers it shares). The retired HA IDX
strategies' trades are below it, folded.

While open in market hours it starts a run every few minutes, as the Auto trader page does,
so the 15-minute signals are checked on time.
"""

from __future__ import annotations

import importlib
from pathlib import Path

import pandas as pd
import streamlit as st

import auto_trader_page as atp
import dhan_data as dd
import opt_atm
import option_charts as oc

# Streamlit Cloud keeps imported modules in memory when it pulls new code, so an
# auto_trader_page.py changed since the server started is reloaded here.
if Path(atp.__file__).stat().st_mtime > getattr(atp, "_loaded_at", 0):
    atp = importlib.reload(atp)
    atp._loaded_at = Path(atp.__file__).stat().st_mtime

fs = atp.fs



def option_charts(name: str) -> None:
    """The index's ATM CE / PE charts the strategy trades (nearest expiry), at the day's opening ATM
    and the ATM now, as on the Option chain page (option_charts.py)."""
    st.divider()
    if not oc.connected():
        st.caption("ATM CE / PE charts: Dhan is not set up in the app's secrets (see the Option chain page).")
        return
    underlying = dd.INDICES[name]
    try:
        expiry = oc.load_expiries(underlying)[0]
        price, rows, at = oc.load_chain(underlying, expiry)
    except (dd.DhanError, IndexError) as e:
        st.caption(f"No {name} option chain from Dhan: {e}")
        return
    df = oc.regular_strikes(rows, underlying)
    if df.empty:
        st.caption(f"Empty {name} option chain from Dhan.")
        return
    st.caption(f"{name} {price:,.2f} · nearest expiry {expiry} (the one the strategy trades) · Dhan, {at} IST")
    oc.atm_section(name, underlying, df, price, prefix="ix")


def main() -> None:
    st.set_page_config(page_title="Autotrader NIFTY\\BNF", page_icon="📈", layout="wide")
    st.title("📈 Autotrader NIFTY\\BNF")
    atp.chart_site_picker()
    account, error = atp.load_account()
    top = st.columns([1, 1, 4])
    if top[0].button("↻ Reload", width="stretch", key="index-reload"):
        atp.load_account.clear()
        atp.listing.clear()
        st.rerun()
    if top[1].button("▶ Run now", width="stretch", key="index-run", help="Check the positions and signals now"):
        if atp.dispatch("run"):
            st.toast("Started a run: it takes about a minute. Reload then.")
    top[2].caption(f"[Runs on GitHub]({atp.RUNS_URL}) · the same account and runs as the Auto trader tab. "
                   f"Last run {atp.when((account or {}).get('updated'))}.")
    atp.live((account or {}).get("updated"))
    if account is None:
        st.info(error)
        return

    st.caption(f"**{opt_atm.NAME}** (NIFTY and BANKNIFTY, nearest expiry, 1 lot): buys the ATM CE or PE at the close "
               "of the Option chain page's star: a 15-minute Heikin Ashi candle that completes solid green (no "
               "lower wick) right after a red one. SL at the previous HA candle's low, moved up to each completed "
               "HA candle's low; squared off at 3:15 PM. Prices: Dhan's 1-minute bars, LTP as of the last run.")
    # One index for the whole page: the totals, positions, trades, the index chart and the CE / PE charts.
    name = st.segmented_control("Index", list(dd.INDICES), default="NIFTY", key="index-page-symbol") or "NIFTY"
    history = atp.load_trades("trades")
    mine = history[history["System"] == opt_atm.SYSTEM]
    trades = mine[mine["symbol"].astype(str).str.startswith(f"{name} ")]  # e.g. "NIFTY 01DEC 25350 PE"
    positions = [p for p in account["positions"] if p.get("system") == opt_atm.SYSTEM and p.get("underlying") == name]
    # The option's last price is saved by each run (Dhan), not looked up here.
    quotes = {p["symbol"]: {"price": p.get("ltp"), "prev": None} for p in positions if p.get("ltp")}

    def unrealized(p):
        q = quotes.get(p["symbol"])
        return None if not q else round((q["price"] - p["entry"]) * p["qty"], 2)

    atp.show_index(trades, positions, unrealized, quotes, symbol=name)
    option_charts(name)

    # The retired HA IDX strategies (24 Nov 2026): their trades, and any position until its exit.
    retired = history[history["System"].isin(list(atp.INDEX_SYSTEMS))]
    still_open = [p for p in account["positions"] if p.get("system") in atp.INDEX_SYSTEMS]
    if not retired.empty or still_open:
        with st.expander(f"Retired HA IDX strategies: {len(retired)} trades, net ₹{retired['net'].sum():,.0f}"
                         + (f", {len(still_open)} still open" if still_open else "")):
            if still_open:
                st.dataframe(pd.DataFrame([{"Symbol": p["symbol"], "Strategy": p["system"], "Side": p["side"],
                                            "Entry": p["entry"], "SL": p.get("stop")} for p in still_open]),
                             hide_index=True, width="stretch")
            st.dataframe(retired.assign(Entered=retired["entry_time"].map(atp.when),
                                        Exited=retired["exit_time"].map(atp.when))
                         [["Exited", "symbol", "System", "side", "qty", "entry", "exit", "reason", "net", "Entered"]],
                         hide_index=True, width="stretch")


if __name__ == "__main__":
    main()
