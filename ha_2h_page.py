"""
2h HA CE PE page: the ATM call and put of NIFTY, BANKNIFTY and the F&O stocks whose daily ATR is above
the saved F&O ATR ratio, on 2-hour Heikin Ashi candles (the auto trader's Telegram alert, opt_2h_alert.py:
a solid green HA candle, no lower wick, right after a red one); and for a picked stock its own 15-minute,
30-minute, 2-hour, 4-hour and daily candles with its current ATM CE and PE.

Intraday candles from Dhan (15-minute ones, made into 30 minutes, 2 hours and 4 hours from 9:15 AM, as
Chartink), daily ones from Yahoo. Needs the Dhan secrets, as the Option chain page.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

import dhan_data as dd
import futures_simulator as fs
import opt_2h_alert as oa

st.set_page_config(page_title="2h HA CE PE", page_icon="🕯️", layout="wide")
st.title("🕯️ 2h HA CE PE")

UP, DOWN, STAR = "#1a9e6f", "#d64545", "#eda100"
SESSION_OPEN = oa.SESSION_OPEN
SESSION_CLOSE = oa.SESSION_CLOSE
# Timeframe -> (minutes, calendar days of Dhan 15-minute bars to fetch); None = daily (Yahoo).
TIMEFRAMES = {"15 min": (15, 5), "30 min": (30, 8), "2 hour": (120, 25), "4 hour": (240, 45), "Daily": (None, 0)}
SHOWN = {"15 min": 50, "30 min": 50, "2 hour": 40, "4 hour": 40, "Daily": 80}  # candles on a chart
YAHOO_INDEX = {"NIFTY": "^NSEI", "BANKNIFTY": "^NSEBANK"}

if not dd.secret("DHAN_CLIENT_ID") or not (dd.can_make_token() or dd.secret("DHAN_ACCESS_TOKEN")):
    st.info("Dhan is not connected: see the Option chain page for the setup.")
    st.stop()


# ---------------------------------------------------------------------------
# Candles
# ---------------------------------------------------------------------------
def resample(bars: list[tuple], minutes: int) -> list[dict]:
    """15-minute bars (start, open, high, low, close, volume) in `minutes` candles from 9:15 AM, oldest first."""
    out: list[dict] = []
    step = timedelta(minutes=minutes)
    for bar in bars:
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
    """HA open, high, low, close of each candle, with green and solid (green, no lower wick)."""
    out: list[dict] = []
    for c in candles:
        ha_close = (c["open"] + c["high"] + c["low"] + c["close"]) / 4
        ha_open = (c["open"] + c["close"]) / 2 if not out else (out[-1]["open"] + out[-1]["close"]) / 2
        low, high = min(c["low"], ha_open, ha_close), max(c["high"], ha_open, ha_close)
        green = ha_close > ha_open
        out.append({"start": c["start"], "open": ha_open, "high": high, "low": low, "close": ha_close,
                    "green": green, "solid": green and low >= ha_open})
    return out


def turned(ha: list[dict], k: int) -> bool:
    """The alert candle: solid green right after a red one."""
    return k > 0 and ha[k]["solid"] and not ha[k - 1]["green"]


@st.cache_data(ttl=120, show_spinner=False)
def dhan_bars(security_id: int, instrument: str, segment: str, days: int) -> list[tuple]:
    return dd.candles(security_id, minutes=15, days=days, instrument=instrument, segment=segment)


def underlying_bars(u: dict, days: int) -> list[tuple]:
    instrument = "INDEX" if u["segment"] == "IDX_I" else "EQUITY"
    return dhan_bars(u["id"], instrument, u["segment"], days)


def option_bars(security_id: int, u: dict, days: int) -> list[tuple]:
    return dhan_bars(int(security_id), u["instrument"], "NSE_FNO", days)


@st.cache_data(ttl=900, show_spinner=False)
def daily_candles(name: str) -> list[dict]:
    chart = fs.yahoo_chart(YAHOO_INDEX.get(name, f"{name}.NS"), "1d", "1y")
    q = chart["indicators"]["quote"][0]
    return [{"start": t, "open": o, "high": h, "low": low, "close": c}
            for t, o, h, low, c in zip(chart["timestamp"], q["open"], q["high"], q["low"], q["close"])
            if None not in (o, h, low, c)]


@st.cache_data(ttl=3600, show_spinner=False)
def load_underlyings(ratio: float) -> list[dict]:
    return oa.underlyings(ratio)


@st.cache_data(ttl=600, show_spinner=False)
def nearest_expiry(name: str, today: str) -> str:
    return oa.nearest_expiry(oa.expiries(UNDERLYINGS[name]), datetime.fromisoformat(today).date())


def expiry_of(name: str, today: str) -> str:
    """The nearest expiry; every stock option shares the monthly one, so the stocks ask Dhan once."""
    if UNDERLYINGS[name]["instrument"] == "OPTSTK":
        try:
            return nearest_expiry(next(n for n, u in UNDERLYINGS.items() if u["instrument"] == "OPTSTK"), today)
        except dd.DhanError:
            pass  # that stock's list failed: this one's own
    return nearest_expiry(name, today)


@st.cache_data(ttl=60, show_spinner=False)
def atm_legs(name: str, expiry: str) -> dict:
    """{"spot", "strike", "CE": (id, ltp), "PE": (id, ltp)} of the strike nearest the price."""
    spot, rows = oa.chain(UNDERLYINGS[name], expiry)
    rows = [r for r in rows if r.get("ce_id") or r.get("pe_id")]
    if not rows:
        raise IndexError("no strikes in Dhan's chain")
    row = min(rows, key=lambda r: abs(r["strike"] - spot))
    return {"spot": spot, "strike": row["strike"], "CE": (row.get("ce_id"), row.get("ce_ltp")),
            "PE": (row.get("pe_id"), row.get("pe_ltp"))}


# ---------------------------------------------------------------------------
# Charts
# ---------------------------------------------------------------------------
def candle_chart(candles: list[dict], ha: list[dict], shown: int, key: str, height: int = 300,
                 daily: bool = False) -> None:
    """The last `shown` candles (HA or normal, the page's choice), a star under each solid green HA candle after red."""
    if not candles:
        st.caption("No candles.")
        return
    start = max(0, len(candles) - shown)
    plot = (ha if st.session_state.get("h2-style", "Heikin Ashi") == "Heikin Ashi" else candles)[start:]
    fmt = "%d %b" if daily else "%d %b %H:%M"
    x = [datetime.fromtimestamp(c["start"], dd.IST).strftime(fmt) for c in plot]
    figure = go.Figure(go.Candlestick(
        x=x, open=[c["open"] for c in plot], high=[c["high"] for c in plot], low=[c["low"] for c in plot],
        close=[c["close"] for c in plot],
        increasing=dict(line=dict(color=UP, width=1), fillcolor=UP),
        decreasing=dict(line=dict(color=DOWN, width=1), fillcolor=DOWN)))
    stars = [k - start for k in range(start, len(ha)) if turned(ha, k)]
    if stars:
        gap = 0.04 * (max(c["high"] for c in plot) - min(c["low"] for c in plot))
        figure.add_trace(go.Scatter(
            x=[x[n] for n in stars], y=[plot[n]["low"] - gap for n in stars], mode="markers",
            marker=dict(symbol="star", size=11, color=STAR, line=dict(width=1, color="white")),
            hovertemplate="Solid green HA after red<br>%{x}<extra></extra>"))
    step = max(1, len(x) // 6)
    figure.update_layout(
        height=height, margin=dict(l=4, r=4, t=4, b=4), showlegend=False, hovermode="x",
        xaxis=dict(type="category", rangeslider_visible=False, tickvals=x[::step], gridcolor="rgba(128,128,128,0.10)"),
        yaxis=dict(tickformat=",.2f", gridcolor="rgba(128,128,128,0.15)", side="right"))
    st.plotly_chart(figure, width="stretch", key=key, config={"displayModeBar": False})


def state(ha: list[dict], k: int) -> str:
    if k < 0 or k >= len(ha):
        return "—"
    return "★ solid green after red" if turned(ha, k) else "🟢 solid" if ha[k]["solid"] else \
        "🟢 green" if ha[k]["green"] else "🔴 red"


# ---------------------------------------------------------------------------
# The scan of every ATM CE and PE
# ---------------------------------------------------------------------------
def completed(ha: list[dict], now: datetime) -> int:
    """Index of the last 2-hour candle that has completed by `now`."""
    for k in range(len(ha) - 1, -1, -1):
        if oa.window_end(ha[k]["start"]) <= now.timestamp():
            return k
    return -1


@st.cache_data(ttl=900, show_spinner=False)
def scan(names: tuple[str, ...], window: str) -> pd.DataFrame:
    """Each underlying's ATM CE and PE: the last completed 2-hour HA candle (`window` keys the cache)."""
    now = datetime.now(dd.IST)
    rows, bar = [], st.progress(0.0, text="Asking Dhan (one option chain every 3 seconds)...")
    for n, name in enumerate(names):
        bar.progress(n / len(names), text=f"{name} ({n + 1} of {len(names)})")
        u = UNDERLYINGS[name]
        row = {"Stock": name, "ATR ÷ strike": round(u["ratio"], 2) if u["ratio"] else None}
        try:
            expiry = expiry_of(name, now.date().isoformat())
            legs = atm_legs(name, expiry)
        except (dd.DhanError, IndexError) as exc:
            rows.append({**row, "Note": str(exc)[:120]})
            continue
        row |= {"Expiry": expiry, "Spot": legs["spot"], "ATM strike": legs["strike"]}
        for side in ("CE", "PE"):
            sid, ltp = legs[side]
            row[f"{side} LTP"] = ltp
            if not sid:
                continue
            try:
                ha = heikin_ashi(resample(option_bars(sid, u, 7), 120))
            except dd.DhanError as exc:
                row["Note"] = str(exc)[:120]
                continue
            k = completed(ha, now)
            row[f"{side} 2H"] = state(ha, k)
            row[f"{side} last 4"] = "".join("★" if turned(ha, j) else "🟢" if ha[j]["green"] else "🔴"
                                             for j in range(max(0, k - 3), k + 1))
        rows.append(row)
    bar.empty()
    return pd.DataFrame(rows)


RATIO = fs.atr_strikes_above()
try:
    UNDERLYINGS = {u["name"]: u for u in load_underlyings(RATIO)}
except Exception as exc:  # no database or strike intervals
    st.error(f"Could not read the F&O ATR list: {exc}")
    st.stop()
NAMES = list(UNDERLYINGS)
now = datetime.now(dd.IST)
last = oa.last_end(now)
window = (f"{datetime.fromtimestamp(last, dd.IST):%d %b %H:%M}" if last else f"{now:%d %b} before 11:15")

st.caption(f"NIFTY, BANKNIFTY and the {len(NAMES) - 2} F&O stocks whose daily ATR is above {RATIO:g} strike "
           "intervals (the ratio saved on the F&O ATR page). ★ = a 2-hour Heikin Ashi candle that completed solid "
           "green (no lower wick) right after a red one: the Telegram alert, sent for the ATM CE at about 11:20 AM, "
           "1:20 PM and 3:20 PM.")
st.segmented_control("Candles", ["Heikin Ashi", "Normal"], default="Heikin Ashi", key="h2-style")

# ---- One stock
pick = st.selectbox("Stock / index", NAMES, key="h2-pick",
                    format_func=lambda n: n + (f" · ATR {UNDERLYINGS[n]['ratio']:.1f}× strike"
                                               if UNDERLYINGS[n]["ratio"] else " · index"))
u = UNDERLYINGS[pick]
st.subheader(f"{pick}")
cells = st.columns(2) + st.columns(2) + st.columns(2)
for cell, (label, (minutes, days)) in zip(cells, TIMEFRAMES.items()):
    with cell:
        st.markdown(f"**{label}**")
        try:
            candles = daily_candles(pick) if minutes is None else resample(underlying_bars(u, days), minutes)
        except dd.DhanError as exc:
            st.caption(f"No candles from Dhan: {exc}")
            continue
        except Exception as exc:  # Yahoo
            st.caption(f"No daily candles: {exc}")
            continue
        ha = heikin_ashi(candles)
        candle_chart(candles, ha, SHOWN[label], key=f"h2-{pick}-{label}", daily=minutes is None)
        st.caption(f"Last candle: {state(ha, len(ha) - 1)} (may still be forming)")

st.subheader(f"{pick} · current ATM CE and PE")
try:
    expiry = expiry_of(pick, now.date().isoformat())
    legs = atm_legs(pick, expiry)
except (dd.DhanError, IndexError) as exc:
    st.error(str(exc))
    legs = None
if legs:
    leg_tf = st.segmented_control("Option candles", ["15 min", "2 hour"], default="2 hour", key="h2-leg-tf") or "2 hour"
    st.caption(f"Expiry {expiry} · spot {legs['spot']:,.2f} · ATM strike {legs['strike']:g}")
    for col, side in zip(st.columns(2), ("CE", "PE")):
        sid, ltp = legs[side]
        with col:
            st.markdown(f"**{oa.label(pick, expiry, legs['strike']).removesuffix(' CE')} {side}** · LTP {ltp}")
            if not sid:
                st.caption("No security id from Dhan.")
                continue
            try:
                bars = option_bars(sid, u, 7)
            except dd.DhanError as exc:
                st.error(str(exc))
                continue
            candles = resample(bars, 15 if leg_tf == "15 min" else 120)
            ha = heikin_ashi(candles)
            candle_chart(candles, ha, 40, key=f"h2-leg-{pick}-{side}-{leg_tf}", height=320)
            k = completed(ha, now) if leg_tf == "2 hour" else len(ha) - 1
            st.caption(f"Last completed 2H candle: {state(ha, k)}" if leg_tf == "2 hour"
                       else f"Last candle: {state(ha, k)} (may still be forming)")

# ---- Every ATM CE and PE
st.subheader("All ATM CE and PE · last completed 2-hour HA candle")
st.caption(f"About 3 seconds a stock (Dhan allows one option chain request every 3 seconds), so the scan runs on "
           f"request; kept for 15 minutes. Last 2H candle: {window}.")
if st.button("🔎 Scan all", type="primary") or st.session_state.get("h2-scanned"):
    st.session_state["h2-scanned"] = True
    table = scan(tuple(NAMES), window)
    order = [c for c in ("Stock", "ATR ÷ strike", "Expiry", "Spot", "ATM strike", "CE LTP", "CE 2H", "CE last 4",
                         "PE LTP", "PE 2H", "PE last 4", "Note") if c in table]
    table = table[order]
    hits = table[table.filter(like=" 2H").apply(lambda r: r.astype(str).str.startswith("★").any(), axis=1)]
    if len(hits):
        st.success("★ now: " + ", ".join(
            f"{r['Stock']} " + " & ".join(s for s in ("CE", "PE") if str(r.get(f"{s} 2H", "")).startswith("★"))
            for _, r in hits.iterrows()))
    st.dataframe(table, hide_index=True, width="stretch",
                 column_config={"Spot": st.column_config.NumberColumn(format="%.2f")})
