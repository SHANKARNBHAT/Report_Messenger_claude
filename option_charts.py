"""
The ATM CE / PE charts on 15-minute candles (Heikin Ashi or normal, from Dhan), at the day's opening ATM
strike and at the ATM now, with the strategy's entry stars (the first solid green HA candle after a red one,
opt_atm.py trades it): shared by the Option chain page (option_chain_page.py) and the Autotrader NIFTY\\BNF
page (index_trader_page.py).
"""

from __future__ import annotations

from datetime import datetime

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

import dhan_data as dd

CANDLE_UP, CANDLE_DOWN, SIGNAL_COLOR = "#1a9e6f", "#d64545", "#eda100"  # as the Auto trader chart
CHART_DAYS = 2  # trading days of 15-minute candles on the small charts


def connected() -> bool:
    return bool(dd.secret("DHAN_CLIENT_ID") and (dd.can_make_token() or dd.secret("DHAN_ACCESS_TOKEN")))


@st.cache_data(ttl=600, show_spinner=False)
def load_expiries(underlying: dict) -> list[str]:
    return dd.expiries(underlying)


@st.cache_data(ttl=5, show_spinner=False)  # Dhan allows one chain request every 3 seconds
def load_chain(underlying: dict, expiry: str) -> tuple[float, list[dict], str]:
    price, rows = dd.option_chain(expiry, underlying)
    return price, rows, datetime.now(dd.IST).strftime("%H:%M:%S")


@st.cache_data(ttl=60, show_spinner=False)
def load_candles(security_id: int, instrument: str) -> list[tuple]:
    return dd.candles(security_id, minutes=15, days=5, instrument=instrument)


@st.cache_data(ttl=300, show_spinner=False)
def load_day_open(underlying: dict) -> tuple[float, str] | None:
    """The underlying's open on its latest trading day in Dhan's 15-minute candles (today once
    the market opens), with that day's date; None if Dhan gives no candles."""
    bars = dd.underlying_candles(underlying)
    if not bars:
        return None
    day = datetime.fromtimestamp(bars[-1][0], dd.IST).date()
    first = next(b for b in bars if datetime.fromtimestamp(b[0], dd.IST).date() == day)
    return float(first[1]), day.strftime("%d %b")


def regular_strikes(rows: list[dict], underlying: dict) -> pd.DataFrame:
    """The chain's rows on the regular strike interval: after a dividend NSE also lists adjusted
    strikes (e.g. 364.75 by 365)."""
    df = pd.DataFrame(rows)
    if df.empty:
        return df
    step = underlying["step"]
    return df[((df["strike"] / step) - (df["strike"] / step).round()).abs() < 1e-6].reset_index(drop=True)


def is_entry(ha: list[dict], k: int) -> bool:
    """The strategy's entry candle: solid green (no lower wick) right after a red one."""
    return k > 0 and ha[k]["solid"] and not ha[k - 1]["green"]


def ha_chart(ha: list[dict], key: str, normal: list[dict] | None = None) -> None:
    """A small 15-minute candle chart (HA, or the `normal` candles) of the last CHART_DAYS trading days,
    the column's width, with an amber star under each HA entry candle. Times are categories, so nights leave no gap."""
    shown = normal or ha
    days = sorted({datetime.fromtimestamp(c["start"], dd.IST).date() for c in ha})[-CHART_DAYS:]
    keep = [k for k, c in enumerate(ha) if datetime.fromtimestamp(c["start"], dd.IST).date() in days]
    if not keep:
        return
    label = [datetime.fromtimestamp(ha[k]["start"], dd.IST).strftime("%d %b %H:%M") for k in keep]
    figure = go.Figure(go.Candlestick(
        x=label, open=[shown[k]["open"] for k in keep], high=[shown[k]["high"] for k in keep],
        low=[shown[k]["low"] for k in keep], close=[shown[k]["close"] for k in keep], name="Normal" if normal else "HA",
        increasing=dict(line=dict(color=CANDLE_UP, width=1), fillcolor=CANDLE_UP),
        decreasing=dict(line=dict(color=CANDLE_DOWN, width=1), fillcolor=CANDLE_DOWN)))
    entries = [n for n, k in enumerate(keep) if is_entry(ha, k)]
    if entries:
        gap = 0.04 * (max(shown[k]["high"] for k in keep) - min(shown[k]["low"] for k in keep))
        figure.add_trace(go.Scatter(
            x=[label[n] for n in entries], y=[shown[keep[n]]["low"] - gap for n in entries], mode="markers",
            name="Entry", marker=dict(symbol="star-triangle-up", size=10, color=SIGNAL_COLOR,
                                      line=dict(width=1, color="white")),
            hovertemplate="Entry candle<br>%{x}<extra></extra>"))
    day_starts = [n for n in range(len(keep)) if n == 0 or label[n][:6] != label[n - 1][:6]]
    figure.update_layout(
        height=230, margin=dict(l=4, r=4, t=4, b=4), showlegend=False, hovermode="x",
        xaxis=dict(type="category", rangeslider_visible=False, tickangle=0, nticks=6,
                   tickvals=[label[n] for n in range(0, len(keep), 8)],
                   ticktext=[label[n][7:] if n not in day_starts else label[n][:6] for n in range(0, len(keep), 8)],
                   gridcolor="rgba(128,128,128,0.10)"),
        yaxis=dict(tickformat=",.0f", gridcolor="rgba(128,128,128,0.15)", side="right"))
    for n in day_starts[1:]:
        figure.add_vline(x=n - 0.5, line=dict(color="rgba(128,128,128,0.45)", width=1, dash="dot"))
    st.plotly_chart(figure, width="stretch", key=key, config={"displayModeBar": False})


def leg_charts(index: str, underlying: dict, row: pd.Series, key: str, normal: bool) -> None:
    """The CE and PE of one strike side by side: the chart and the last 12 HA candles."""
    cols = st.columns(2)
    for col, side in zip(cols, ("ce", "pe")):
        sid = row.get(f"{side}_id")
        with col:
            st.markdown(f"**{index} {int(row['strike'])} {side.upper()}** · LTP {row.get(f'{side}_ltp')}")
            if not sid:
                st.caption("No security id from Dhan.")
                continue
            try:
                raw = load_candles(int(sid), underlying["instrument"])
                ha = dd.heikin_ashi(raw)
            except dd.DhanError as e:
                st.error(str(e))
                continue
            if not ha:
                st.caption("No candles yet.")
                continue
            ha_chart(ha, key=f"{key}-{index}-{side}",
                     normal=[{"start": b[0], "open": b[1], "high": b[2], "low": b[3], "close": b[4]} for b in raw]
                     if normal else None)
            last = ha[-12:]
            frame = pd.DataFrame({
                "Time": [datetime.fromtimestamp(c["start"], dd.IST).strftime("%d %b %H:%M") for c in last],
                "HA Open": [c["open"] for c in last], "HA High": [c["high"] for c in last],
                "HA Low": [c["low"] for c in last], "HA Close": [c["close"] for c in last],
                "Candle": ["🟢 solid" if c["solid"] else "🟢" if c["green"] else "🔴" for c in last],
            })
            start = len(ha) - len(last)
            frame["Entry?"] = ["✅" if is_entry(ha, start + k) else "" for k in range(len(last))]
            st.dataframe(frame.iloc[::-1].style.format(precision=2), hide_index=True, width="stretch")
            st.caption("The newest candle (top) may still be forming.")


def atm_section(index: str, underlying: dict, df: pd.DataFrame, price: float, prefix: str) -> None:
    """The heading with the Heikin Ashi / normal switch, then the CE and PE charts at the day's opening
    ATM strike (when it differs from the ATM now) and at the ATM now. `df`: regular_strikes of the chain;
    `prefix` keeps the widget keys of each page apart."""
    atm = dd.atm_strike(price, underlying)
    i = (df["strike"] - atm).abs().idxmin()
    head = st.columns([3, 2])
    head[0].subheader("ATM CE / PE, 15-minute candles")
    style = head[1].segmented_control("Chart candles", ["Heikin Ashi", "Normal"], default="Heikin Ashi",
                                      key=f"{prefix}-leg-candles",
                                      help="The table and the stars stay Heikin Ashi (the strategy's)")
    normal = style == "Normal"
    st.caption("Strategy entry (amber star): the first solid green HA candle (no lower wick) after a red one. "
               "SL: that candle's previous HA candle's low. Charts: the last two trading days.")
    # First the day's opening (BOD) ATM strike: where the ATM was at the first candle, e.g. 25350
    # when the day ended at 25800; shown only when it differs from the ATM now. Then the ATM now.
    try:
        opened = load_day_open(underlying)
    except dd.DhanError as e:
        opened = None
        st.caption(f"Opening ATM not available: {e}")
    if opened:
        open_price, open_day = opened
        open_atm = dd.atm_strike(open_price, underlying)
        j = (df["strike"] - open_atm).abs().idxmin()
        if open_atm == atm:
            st.caption(f"Opening ATM ({open_day}, open {open_price:,.2f}) is the same strike, {atm:,.0f}.")
        elif abs(df.loc[j, "strike"] - open_atm) > 1e-6:
            st.caption(f"Opening ATM {open_atm:,.0f} is not in Dhan's chain for this expiry.")
        else:
            st.markdown(f"[⬇ Jump to Now: ATM {atm:,.0f}](#{prefix}-now)")
            st.markdown(f"##### At the open ({open_day}, {index} {open_price:,.2f}): ATM {open_atm:,.0f}")
            leg_charts(index, underlying, df.loc[j], key=f"{prefix}-open", normal=normal)

    st.markdown(f'<div id="{prefix}-now"></div>', unsafe_allow_html=True)  # the jump link's target
    st.markdown(f"##### Now: ATM {atm:,.0f}")
    leg_charts(index, underlying, df.loc[i], key=f"{prefix}-now", normal=normal)
