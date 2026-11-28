"""
Autotrader INDEX\\FUTURES page: the Auto trader's OPT ATM HA strategy (opt_atm.py), the NIFTY and
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
import rsi_opt
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


def show_rsi_opt(history: pd.DataFrame, account: dict) -> None:
    """OPT RSI (rsi_opt.py): the RSI 2H / 4H / Daily triggers of the stocks and indices, bought through the
    ATM CE / PE once its 15m / 30m candle agrees. Totals, open positions, today's watched triggers, trades."""
    st.divider()
    st.subheader("OPT RSI · RSI trigger, confirmed on the ATM option")
    st.caption(f"An F&O stock with a daily ATR above ₹{rsi_opt.MIN_ATR}, or NIFTY / BANKNIFTY, on the RSI 2H, 4H or Daily scans today: bullish → its ATM CE, bearish → its ATM PE. "
               "Bought (1 lot) at the close of the option's first 15m or 30m candle after the trigger that is a strong HA "
               "candle (as the HA CE / PE alerts) or a green candle with a higher high. SL the previous HA candle's low, "
               "trailed to each 15-minute HA low; squared off at 3:15 PM. Same-day triggers only, one entry per side a day.")
    trades = history[history["System"] == rsi_opt.SYSTEM]
    positions = [p for p in account["positions"] if p.get("system") == rsi_opt.SYSTEM]
    open_pnl = sum((p["ltp"] - p["entry"]) * p["qty"] for p in positions if p.get("ltp"))
    m = st.columns(4)
    m[0].metric("Net P&L (closed)", f"₹{trades['net'].sum():,.0f}")
    m[1].metric("Open P&L", f"₹{open_pnl:,.0f}")
    m[2].metric("Trades closed", len(trades))
    m[3].metric("Win rate", f"{100 * (trades['net'] > 0).mean():.1f}%" if len(trades) else "—")
    if positions:
        st.markdown(f"**Open ({len(positions)})**")
        st.dataframe(pd.DataFrame([{"Entered": atp.when(p["time"]), "Option": p["symbol"], "Why": p.get("why"),
                                    "Qty": p["qty"], "Entry": p["entry"], "SL now": p.get("stop"), "LTP": p.get("ltp"),
                                    "P&L ₹": round((p["ltp"] - p["entry"]) * p["qty"]) if p.get("ltp") else None}
                                   for p in positions]), hide_index=True, width="stretch")
    watch = (account.get("rsi_opt") or {}).get("watch") or {}
    if watch:
        with st.expander(f"Triggers of {account['rsi_opt']['day']}: {len(watch)}"):
            st.dataframe(pd.DataFrame([{"Underlying": w["symbol"], "Option": w["leg"], "RSI": "/".join(w["timeframes"]),
                                        "Seen": atp.when(w["since"] * 1000), "Status": w["status"]}
                                       for w in watch.values()]), hide_index=True, width="stretch")
    if not trades.empty:
        st.markdown(f"**Closed ({len(trades)})**")
        st.dataframe(trades.assign(Entered=trades["entry_time"].map(atp.when), Exited=trades["exit_time"].map(atp.when),
                                   Why=trades["strategies"].map(lambda v: v[0] if isinstance(v, list) and v else None))
                     [["Exited", "symbol", "Why", "qty", "entry", "exit", "reason", "net", "Entered"]],
                     hide_index=True, width="stretch")


def main() -> None:
    st.set_page_config(page_title="Autotrader INDEX\\FUTURES", page_icon="📈", layout="wide")
    st.title("📈 Autotrader INDEX\\FUTURES")
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
    show_rsi_opt(history, account)

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
