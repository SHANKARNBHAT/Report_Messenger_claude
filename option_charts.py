"""
The ATM CE / PE charts on 15-minute candles (Heikin Ashi or normal, from Dhan), at the day's opening ATM
strike and at the ATM now, with the strategy's entry stars (the first solid green HA candle after a red one,
opt_atm.py trades it): shared by the Option chain page (option_chain_page.py) and the Autotrader INDEX\\FUTURES
page (index_trader_page.py).
"""

from __future__ import annotations

from datetime import datetime, time, timedelta

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

import dhan_data as dd

CANDLE_UP, CANDLE_DOWN, SIGNAL_COLOR = "#1a9e6f", "#d64545", "#eda100"  # as the Auto trader chart
CHART_DAYS = 2  # trading days of 15-minute candles on the small charts
# The CE / PE chart timeframes: minutes -> (calendar days of Dhan's 15-minute bars to ask, trading days shown).
LEG_TIMEFRAMES = {15: (5, 2), 30: (8, 3), 60: (12, 5), 120: (25, 10), 240: (45, 20)}
SESSION_OPEN, SESSION_CLOSE = time(9, 15), time(15, 30)


def tf_label(minutes: int) -> str:
    return f"{minutes} min" if minutes < 60 else f"{minutes // 60} hour"


def resample(bars: list[tuple], minutes: int) -> list[tuple]:
    """15-minute bars (start, open, high, low, close, volume) in `minutes` candles from 9:15 AM (as
    Chartink: 2 hours = 9:15-11:15, 11:15-1:15, 1:15-3:15, 3:15-3:30), oldest first."""
    if minutes <= 15:
        return bars
    out: list[list] = []
    step = timedelta(minutes=minutes)
    for start, open_, high, low, close, *rest in bars:
        moment = datetime.fromtimestamp(start, dd.IST)
        if not SESSION_OPEN <= moment.time() < SESSION_CLOSE:
            continue
        session = datetime.combine(moment.date(), SESSION_OPEN, dd.IST)
        begins = int((session + (moment - session) // step * step).timestamp())
        volume = (rest[0] if rest else 0) or 0
        if not out or out[-1][0] != begins:
            out.append([begins, open_, high, low, close, volume])
        else:
            c = out[-1]
            c[2], c[3], c[4], c[5] = max(c[2], high), min(c[3], low), close, c[5] + volume
    return [tuple(c) for c in out]


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
def load_candles(security_id: int, instrument: str, days: int = 5) -> list[tuple]:
    return dd.candles(security_id, minutes=15, days=days, instrument=instrument)


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


def ha_chart(ha: list[dict], key: str, normal: list[dict] | None = None, days_shown: int = CHART_DAYS) -> None:
    """A small candle chart (HA, or the `normal` candles) of the last `days_shown` trading days, the
    column's width, with an amber star under each HA entry candle. Times are categories, so nights leave no gap."""
    shown = normal or ha
    days = sorted({datetime.fromtimestamp(c["start"], dd.IST).date() for c in ha})[-days_shown:]
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
    every = max(1, len(keep) // 6)
    figure.update_layout(
        height=230, margin=dict(l=4, r=4, t=4, b=4), showlegend=False, hovermode="x",
        xaxis=dict(type="category", rangeslider_visible=False, tickangle=0, nticks=6,
                   tickvals=[label[n] for n in range(0, len(keep), every)],
                   ticktext=[label[n][7:] if n not in day_starts else label[n][:6] for n in range(0, len(keep), every)],
                   gridcolor="rgba(128,128,128,0.10)"),
        yaxis=dict(tickformat=",.0f", gridcolor="rgba(128,128,128,0.15)", side="right"))
    for n in (day_starts[1:] if len(days) <= 5 else []):  # day lines only while they stay readable
        figure.add_vline(x=n - 0.5, line=dict(color="rgba(128,128,128,0.45)", width=1, dash="dot"))
    st.plotly_chart(figure, width="stretch", key=key, config={"displayModeBar": False})


def leg_charts(index: str, underlying: dict, row: pd.Series, key: str, normal: bool, minutes: int = 15) -> None:
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
                ask, days_shown = LEG_TIMEFRAMES[minutes]
                raw = resample(load_candles(int(sid), underlying["instrument"], ask), minutes)
                ha = dd.heikin_ashi(raw)
            except dd.DhanError as e:
                st.error(str(e))
                continue
            if not ha:
                st.caption("No candles yet.")
                continue
            ha_chart(ha, key=f"{key}-{index}-{side}-{minutes}",
                     normal=[{"start": b[0], "open": b[1], "high": b[2], "low": b[3], "close": b[4]} for b in raw]
                     if normal else None, days_shown=days_shown)
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
    head = st.columns([2, 3, 2])
    minutes = head[1].segmented_control("Timeframe", list(LEG_TIMEFRAMES), default=15, format_func=tf_label,
                                        key=f"{prefix}-leg-tf") or 15
    head[0].subheader(f"ATM CE / PE, {tf_label(minutes)} candles")
    style = head[2].segmented_control("Chart candles", ["Heikin Ashi", "Normal"], default="Heikin Ashi",
                                      key=f"{prefix}-leg-candles",
                                      help="The table and the stars stay Heikin Ashi (the strategy's)")
    normal = style == "Normal"
    st.caption("Strategy entry (amber star): the first solid green HA candle (no lower wick) after a red one "
               "(the auto trader trades it on 15 minutes). SL: that candle's previous HA candle's low. Charts: the "
               f"last {LEG_TIMEFRAMES[minutes][1]} trading days; 30 minutes and up are made from Dhan's 15-minute "
               "candles from 9:15 AM, as Chartink's.")
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
            leg_charts(index, underlying, df.loc[j], key=f"{prefix}-open", normal=normal, minutes=minutes)

    st.markdown(f'<div id="{prefix}-now"></div>', unsafe_allow_html=True)  # the jump link's target
    st.markdown(f"##### Now: ATM {atm:,.0f}")
    leg_charts(index, underlying, df.loc[i], key=f"{prefix}-now", normal=normal, minutes=minutes)
