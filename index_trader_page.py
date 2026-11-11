"""
Autotrader NIFTY\\BNF page: the Auto trader's HA IDX strategy (ha_index.py), NIFTY and
BANKNIFTY on 15-minute Heikin Ashi candles, apart from the stock strategies of the Auto
trader page (auto_trader_page.py, whose account, runs and helpers it shares).

While open in market hours it starts a run every few minutes, as the Auto trader page does,
so the 15-minute signals are checked on time.
"""

from __future__ import annotations

import streamlit as st

import auto_trader_page as atp

fs = atp.fs


def main() -> None:
    st.set_page_config(page_title="Autotrader NIFTY\\BNF", page_icon="📈", layout="wide")
    st.title("📈 Autotrader NIFTY\\BNF")
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

    history = atp.load_trades("trades")
    trades = history[history["System"] == atp.INDEX_SYSTEM]
    positions = [p for p in account["positions"] if p.get("system") == atp.INDEX_SYSTEM]
    instruments = {i["symbol"]: i for i in fs.load_instruments()}
    quotes = fs.latest_prices({p["symbol"] for p in positions}, instruments, None) if positions else {}

    def unrealized(p):
        q = quotes.get(p["symbol"])
        return None if not q else round((q["price"] - p["entry"]) * p["qty"] * (1 if p["side"] == "BUY" else -1), 2)

    atp.show_index(trades, positions, unrealized)


if __name__ == "__main__":
    main()
