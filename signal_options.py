"""
Signals · ATM CE & PE (a section of the Auto trader page): the day's RSI 2H and 4H signals (the signal log,
whatever was done with them), and for the one picked, the ATM call and put of its nearest expiry on 15-minute,
2-hour or 4-hour candles (Heikin Ashi or normal) from Dhan, with the signal's candle marked. For deciding on an
option trade by hand: no trades are made here.

Needs the Dhan secrets (as the Option chain page). Dhan allows one option chain request every 3 seconds, so the
expiry list and chain are cached.
"""

from __future__ import annotations

import json
import time as _time
from datetime import date, datetime, time, timedelta
from pathlib import Path

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

import dhan_data as dd

SYSTEMS = {"RSI": "2H", "RSI 4H": "4H"}  # the signal log's strategies shown, and their timeframe
SESSION_OPEN, SESSION_CLOSE = time(9, 15), time(15, 30)
CANDLES = {"15 min": (15, 6, 60), "2 hour": (120, 20, 40), "4 hour": (240, 40, 40)}  # minutes, days of bars, shown
UP, DOWN, MARK = "#1a9e6f", "#d64545", "#eda100"
CHAIN_PAUSE = 3.1  # seconds: Dhan's one option chain request every 3 seconds


def underlying(symbol: str) -> dict | None:
    """Dhan's underlying of an index or F&O stock (strike_steps.json), else None."""
    if symbol in dd.INDICES:
        return dd.INDICES[symbol]
    saved = json.loads(Path(__file__).with_name("strike_steps.json").read_text(encoding="utf-8"))
    if symbol in saved["ids"] and symbol in saved["steps"]:
        return dd.stock(symbol, saved["ids"][symbol], saved["steps"][symbol])
    return None


@st.cache_data(ttl=900, show_spinner=False)
def nearest_expiry(symbol: str, today: str) -> str:
    """The nearest expiry; the next one on its expiry day (that contract ends in the afternoon)."""
    _time.sleep(CHAIN_PAUSE)
    expiries = dd.expiries(underlying(symbol))
    later = [e for e in expiries if e > today]
    return later[0] if later else expiries[-1]


@st.cache_data(ttl=60, show_spinner=False)
def atm(symbol: str, expiry: str) -> dict:
    """{"spot", "strike", "CE": (id, ltp), "PE": (id, ltp)} of the strike nearest the price."""
    _time.sleep(CHAIN_PAUSE)
    spot, rows = dd.option_chain(expiry, underlying(symbol))
    rows = [r for r in rows if r.get("ce_id") or r.get("pe_id")]
    if not rows:
        raise IndexError("no strikes in Dhan's chain")
    row = min(rows, key=lambda r: abs(r["strike"] - spot))
    return {"spot": spot, "strike": row["strike"], "CE": (row.get("ce_id"), row.get("ce_ltp")),
            "PE": (row.get("pe_id"), row.get("pe_ltp"))}


@st.cache_data(ttl=120, show_spinner=False)
def bars(security_id: int, instrument: str, days: int) -> list[tuple]:
    return dd.candles(security_id, minutes=15, days=days, instrument=instrument)


def resample(rows: list[tuple], minutes: int) -> list[dict]:
    """15-minute bars in `minutes` candles from 9:15 AM, oldest first."""
    out: list[dict] = []
    step = timedelta(minutes=minutes)
    for bar in rows:
        moment = datetime.fromtimestamp(bar[0], dd.IST)
        if not SESSION_OPEN <= moment.time() < SESSION_CLOSE:
            continue
        session = datetime.combine(moment.date(), SESSION_OPEN, dd.IST)
        start = int((session + (moment - session) // step * step).timestamp())
        if not out or out[-1]["start"] != start:
            out.append({"start": start, "open": bar[1], "high": bar[2], "low": bar[3], "close": bar[4]})
        c = out[-1]
        c["high"], c["low"], c["close"] = max(c["high"], bar[2]), min(c["low"], bar[3]), bar[4]
    return out


def heikin_ashi(candles: list[dict]) -> list[dict]:
    out: list[dict] = []
    for c in candles:
        ha_close = (c["open"] + c["high"] + c["low"] + c["close"]) / 4
        ha_open = (c["open"] + c["close"]) / 2 if not out else (out[-1]["open"] + out[-1]["close"]) / 2
        out.append({"start": c["start"], "open": ha_open, "close": ha_close,
                    "high": max(c["high"], ha_open, ha_close), "low": min(c["low"], ha_open, ha_close)})
    return out


def chart(candles: list[dict], shown: int, signal_ms: int, key: str, heikin: bool) -> None:
    """The last `shown` candles, the one the signal came in dotted amber."""
    if not candles:
        st.caption("No candles from Dhan yet (a contract with no trades).")
        return
    plot = (heikin_ashi(candles) if heikin else candles)[-shown:]
    x = [datetime.fromtimestamp(c["start"], dd.IST).strftime("%d %b %H:%M") for c in plot]
    figure = go.Figure(go.Candlestick(
        x=x, open=[c["open"] for c in plot], high=[c["high"] for c in plot], low=[c["low"] for c in plot],
        close=[c["close"] for c in plot],
        increasing=dict(line=dict(color=UP, width=1), fillcolor=UP),
        decreasing=dict(line=dict(color=DOWN, width=1), fillcolor=DOWN)))
    at = [n for n, c in enumerate(plot) if c["start"] <= signal_ms / 1000][-1:]
    if at and plot[at[0]]["start"] >= plot[0]["start"]:
        figure.add_vline(x=at[0], line=dict(color=MARK, width=1, dash="dot"))
    step = max(1, len(x) // 6)
    figure.update_layout(
        height=320, margin=dict(l=4, r=4, t=4, b=4), showlegend=False, hovermode="x",
        xaxis=dict(type="category", rangeslider_visible=False, tickvals=x[::step], gridcolor="rgba(128,128,128,0.10)"),
        yaxis=dict(tickformat=",.2f", gridcolor="rgba(128,128,128,0.15)", side="right"))
    st.plotly_chart(figure, width="stretch", key=key, config={"displayModeBar": False})


def show(signals: pd.DataFrame, lots: dict[str, int]) -> None:
    """The section: pick a day's 2H / 4H signal, see its ATM CE and PE."""
    if signals.empty or "system" not in signals:
        st.caption("No signals logged yet: the runs log them in market hours.")
        return
    found = signals[signals["system"].isin(SYSTEMS) & signals["fno"].fillna(False).astype(bool)]
    if found.empty:
        st.caption("No RSI 2H or 4H signals on F&O stocks logged yet.")
        return
    top = st.columns([1, 3, 2])
    day = top[0].selectbox("Day", sorted(found["date"].unique(), reverse=True), key="so-day")
    today = found[found["date"] == day].sort_values("time")
    if st.toggle("Only the high-ATR stocks the Auto trader trades (ATR above the F&O ATR page's ratio)", value=True,
                 key="so-atr"):
        today = today[~today["status"].fillna("").str.startswith("ATR not above")]
    if today.empty:
        st.caption("No such signals that day.")
        return
    labels = {
        k: f"{datetime.fromtimestamp(r['time'] / 1000, dd.IST):%H:%M} · {r['symbol']} · "
           f"{SYSTEMS[r['system']]} · {r['bias']} @ {r['price']:g}"
        for k, r in today.iterrows()}
    pick = top[1].selectbox(f"Signal ({len(labels)})", list(labels), format_func=labels.get, key=f"so-sig-{day}")
    tf = top[2].segmented_control("Option candles", list(CANDLES), default="2 hour", key="so-tf") or "2 hour"
    heikin = st.toggle("Heikin Ashi", value=True, key="so-ha")
    signal = today.loc[pick]
    symbol = signal["symbol"]
    u = underlying(symbol)
    if u is None:
        st.warning(f"{symbol}: not in the strike list (strike_steps.json), so no option chain.")
        return
    if not dd.secret("DHAN_CLIENT_ID") or not (dd.can_make_token() or dd.secret("DHAN_ACCESS_TOKEN")):
        st.info("Dhan is not connected: see the Option chain page for the setup.")
        return
    try:
        expiry = nearest_expiry(symbol, date.today().isoformat())
        legs = atm(symbol, expiry)
    except (dd.DhanError, IndexError) as exc:
        st.error(f"{symbol}: {exc}")
        return
    bullish = signal["bias"] == "Bullish"
    st.markdown(f"**{symbol}** · {SYSTEMS[signal['system']]} {signal['bias'].lower()} signal at "
                f"{datetime.fromtimestamp(signal['time'] / 1000, dd.IST):%d %b %H:%M} @ {signal['price']:g} "
                f"({signal.get('status') or 'seen'}) · now {legs['spot']:,.2f} · expiry {expiry} · "
                f"ATM **{legs['strike']:g}** · lot {lots.get(symbol, '—')} · "
                f"the signal's side: **{'CE' if bullish else 'PE'}**")
    minutes, days, shown = CANDLES[tf]
    for col, side in zip(st.columns(2), ("CE", "PE")):
        sid, ltp = legs[side]
        with col:
            mark = " ← the signal's side" if (side == "CE") == bullish else ""
            st.markdown(f"**{symbol} {legs['strike']:g} {side}** · LTP {ltp}{mark}")
            if not sid:
                st.caption("No security id from Dhan.")
                continue
            try:
                rows = bars(int(sid), u["instrument"], days)
            except dd.DhanError as exc:
                st.error(str(exc))
                continue
            chart(resample(rows, minutes), shown, int(signal["time"]), key=f"so-{pick}-{side}-{tf}-{heikin}",
                  heikin=heikin)
    st.caption("The ATM strike is today's (nearest the price now), not the signal time's. Dotted amber line: the "
               "candle the signal came in. The newest candle may still be forming.")
