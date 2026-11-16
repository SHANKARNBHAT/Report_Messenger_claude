"""
Autotrader NIFTY\\BNF page: the Auto trader's HA IDX strategies (ha_index.py; HA IDX PURE
without the SMA trend filter), NIFTY and
BANKNIFTY on 15-minute Heikin Ashi candles, apart from the stock strategies of the Auto
trader page (auto_trader_page.py, whose account, runs and helpers it shares).

While open in market hours it starts a run every few minutes, as the Auto trader page does,
so the 15-minute signals are checked on time.
"""

from __future__ import annotations

import importlib
from pathlib import Path

import streamlit as st

import auto_trader_page as atp

# Streamlit Cloud keeps imported modules in memory when it pulls new code, so an
# auto_trader_page.py changed since the server started is reloaded here.
if Path(atp.__file__).stat().st_mtime > getattr(atp, "_loaded_at", 0):
    atp = importlib.reload(atp)
    atp._loaded_at = Path(atp.__file__).stat().st_mtime

fs = atp.fs


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

    system = st.segmented_control("Strategy", list(atp.INDEX_SYSTEMS), format_func=lambda s: atp.INDEX_SYSTEMS[s][0],
                                  default=atp.INDEX_SYSTEM, key="index-system") or atp.INDEX_SYSTEM
    history = atp.load_trades("trades")
    trades = history[history["System"] == system]
    positions = [p for p in account["positions"] if p.get("system") == system]
    instruments = {i["symbol"]: i for i in fs.load_instruments()}
    quotes = fs.latest_prices({p["symbol"] for p in positions}, instruments, None) if positions else {}

    def unrealized(p):
        q = quotes.get(p["symbol"])
        return None if not q else round((q["price"] - p["entry"]) * p["qty"] * (1 if p["side"] == "BUY" else -1), 2)

    atp.show_index(trades, positions, unrealized, atp.INDEX_SYSTEMS[system][1], quotes)


if __name__ == "__main__":
    main()
