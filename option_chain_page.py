"""
Option chain page: the NIFTY, BANKNIFTY or an F&O stock (daily ATR above ₹100, opt_ha_alert.MIN_ATR)'s option chain from Dhan (dhan_data.py), the ATM strike and
5 strikes each side of it, with OI, volume, IV and premium changes; and below it the ATM call and put
on 15-minute to 4-hour candles, flagging the planned strategy's entry candle (the first
solid green HA candle, no lower wick, after a red one). At the top, a picker of the recent RSI 2H / 4H
signals (signal_options.py) switches the index / stock to the signal's.

Needs DHAN_CLIENT_ID and DHAN_PIN + DHAN_TOTP_SECRET (or DHAN_ACCESS_TOKEN) in the Streamlit secrets.
"""

from __future__ import annotations

from datetime import datetime
from urllib.parse import quote

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

import dhan_data as dd
import opt_ha_alert
import option_charts as oc
import signal_options
import strike_steps

st.set_page_config(page_title="Option chain", page_icon="🧮", layout="wide")
st.title("🧮 Option chain")

SETUP = """
**Dhan is not connected yet.** To connect it (the Dhan Data APIs plan must be active):

1. On [web.dhan.co](https://web.dhan.co) → **Profile** → **Trading & Data APIs**, note your
   **Client ID** and set up **TOTP**; keep the secret key shown under the QR code.
2. In Streamlit Cloud: app → **Settings** → **Secrets** (on this PC: `.streamlit/secrets.toml`), add
   ```toml
   DHAN_CLIENT_ID = "your client id"
   DHAN_PIN = "your Dhan PIN"
   DHAN_TOTP_SECRET = "the TOTP secret key"
   ```
   The access token is then made automatically every day. (Or, instead of the PIN and
   TOTP key, a token made by hand there: `DHAN_ACCESS_TOKEN = "..."`, valid 24 hours.)
3. Reload this page.
"""


# The Chartink chart snapshot the dashboard's "My indicators" links use (chartink_dashboard.DEFAULT_SNAPSHOT).
CHARTINK_SNAPSHOT = ("simple-moving-average-simple-moving-average-simple-moving-average-pivot-s2-pivot-copy-pivot-"
                     "simple-moving-average-volume-weighted-moving-averag-chart-snapshot")

@st.cache_data(ttl=3600, show_spinner=False)
def load_underlyings(min_atr: float) -> tuple[dict[str, dict], str | None]:
    """NIFTY, BANKNIFTY and the F&O stocks whose daily ATR (14) is above `min_atr` rupees (as the HA CE / PE
    alerts and OPT RSI), biggest ATR first, each with a label for the picker; and why the stocks are
    missing, if they are."""
    out = {name: {**u, "label": name} for name, u in dd.INDICES.items()}
    try:
        frame, _, _ = strike_steps.fno_atr()
    except Exception as e:  # no database or strike intervals: the indices only
        return out, f"Could not read the F&O ATR data ({e}): indices only."
    picked = frame[frame["atr_14"] > min_atr].dropna(subset=["dhan_id"]).sort_values("atr_14", ascending=False)
    for row in picked.itertuples():
        out[row.symbol] = {**dd.stock(row.symbol, row.dhan_id, row.step),
                           "label": f"{row.symbol} · ATR ₹{row.atr_14:,.0f}"}
    return out, None


@st.cache_data(ttl=600, show_spinner=False)
def load_expiries(name: str) -> list[str]:
    return dd.expiries(UNDERLYINGS[name])


@st.cache_data(ttl=5, show_spinner=False)  # Dhan allows one chain request every 3 seconds
def load_chain(name: str, expiry: str) -> tuple[float, list[dict], str]:
    price, rows = dd.option_chain(expiry, UNDERLYINGS[name])
    return price, rows, datetime.now(dd.IST).strftime("%H:%M:%S")


if not dd.secret("DHAN_CLIENT_ID") or not (dd.can_make_token() or dd.secret("DHAN_ACCESS_TOKEN")):
    st.info(SETUP)
    st.stop()

MIN_ATR = opt_ha_alert.MIN_ATR
UNDERLYINGS, missing = load_underlyings(MIN_ATR)
if missing:
    st.warning(missing)
# A recent RSI 2H / 4H signal picked here switches the index / stock below to its stock.
signal = signal_options.picker(UNDERLYINGS, "oc-underlying")
index = st.selectbox("Index / stock", list(UNDERLYINGS), index=0, key="oc-underlying",
                     format_func=lambda n: UNDERLYINGS[n]["label"],
                     help=f"NIFTY, BANKNIFTY and the {len(UNDERLYINGS) - len(dd.INDICES)} F&O stocks whose daily "
                          f"ATR (14) is above ₹{MIN_ATR:g}, biggest first (as the HA CE / PE alerts and OPT RSI).")
underlying = UNDERLYINGS[index]
if signal and signal["symbol"] == index:
    bullish = signal["bias"] == "Bullish"
    st.info(f"**{index}**: RSI {signal_options.SYSTEMS[signal['system']]} {signal['bias'].lower()} signal at "
            f"{datetime.fromtimestamp(signal['time'] / 1000, dd.IST):%d %b %H:%M} @ {signal['price']:g} "
            f"({signal.get('status') or 'seen'}): its side is the **{'CE' if bullish else 'PE'}**. Set the CE / PE "
            "charts below to 2 hour or 4 hour to see the signal's timeframe.")
# The chosen index / stock's chart on Chartink, with the dashboard's indicator snapshot or plain.
_chartink = "https://chartink.com/stocks-new?symbol=" + quote(index, safe="")
st.markdown(f"📈 **{index}** chart on Chartink: [my indicators]({_chartink}&load-snapshot={CHARTINK_SNAPSHOT})"
            f" · [plain]({_chartink})")

try:
    expiries = load_expiries(index)
except dd.DhanError as e:
    if "813" in str(e) or "Invalid SecurityId" in str(e):  # a stock NSE dropped from F&O since strike_steps.json
        st.warning(f"Dhan lists no {index} options: NSE seems to have dropped it from F&O. It leaves this list "
                   "when strike_steps.json is rebuilt (monthly, or run the Strike steps workflow).")
    else:
        st.error(str(e))
    st.stop()
if not expiries:
    st.warning(f"Dhan listed no {index} expiries.")
    st.stop()

AROUND = 5  # strikes shown each side of the ATM one

top = st.columns([3, 1, 1])
expiry = top[0].selectbox("Expiry", expiries, index=0, key=f"oc-expiry-{index}")
auto = top[1].toggle("Auto refresh", value=False, key="oc-auto", help="Every 15 seconds")
if top[2].button("↻ Refresh", width="stretch"):
    load_chain.clear()
    oc.load_candles.clear()


def fmt_lakh(v):
    return None if v is None or pd.isna(v) else round(v / 1e5, 2)


CANDLE_UP, CANDLE_DOWN = oc.CANDLE_UP, oc.CANDLE_DOWN


# The day chart's candles: minutes -> calendar days of bars; 2 and 4 hours are made from 15-minute ones and
# show several days (the shorter ones only the latest day).
DAY_TIMEFRAMES = {1: 5, 5: 5, 15: 5, 120: 25, 240: 45}


@st.cache_data(ttl=60, show_spinner=False)
def load_underlying_bars(name: str, minutes: int) -> list[tuple]:
    if minutes > 15:
        return oc.resample(dd.underlying_candles(UNDERLYINGS[name], minutes=15, days=DAY_TIMEFRAMES[minutes]), minutes)
    return dd.underlying_candles(UNDERLYINGS[name], minutes=minutes, days=5)


def day_chart(resistance: float | None, support: float | None) -> None:
    """The index or stock's latest trading day in normal candles (Dhan), with the max call OI
    (resistance) and max put OI (support) strikes as lines."""
    head = st.columns([3, 2])
    minutes = head[1].segmented_control("Candles", list(DAY_TIMEFRAMES), default=5, format_func=oc.tf_label,
                                        key="oc-day-minutes") or 5
    try:
        bars = load_underlying_bars(index, minutes)
    except dd.DhanError as e:
        st.caption(f"No {index} candles from Dhan: {e}")
        return
    if not bars:
        st.caption(f"No {index} candles from Dhan.")
        return
    day = datetime.fromtimestamp(bars[-1][0], dd.IST).date()
    many = minutes > 15  # 2 and 4 hours: the last 40 candles, over several days
    bars = bars[-40:] if many else [b for b in bars if datetime.fromtimestamp(b[0], dd.IST).date() == day]
    first = datetime.fromtimestamp(bars[0][0], dd.IST).date()
    span = f"{first:%d %b} to " if many else ""
    head[0].markdown(f"**{index} · {span}{day:%a %d %b}** · open {bars[0][1]:,.2f}"
                     f" · high {max(b[2] for b in bars):,.2f} · low {min(b[3] for b in bars):,.2f}")
    times = [datetime.fromtimestamp(b[0], dd.IST).strftime("%d %b %H:%M") if many
             else datetime.fromtimestamp(b[0], dd.IST).replace(tzinfo=None) for b in bars]
    figure = go.Figure(go.Candlestick(
        x=times, open=[b[1] for b in bars], high=[b[2] for b in bars], low=[b[3] for b in bars],
        close=[b[4] for b in bars], name=index,
        increasing=dict(line=dict(color=CANDLE_UP, width=1), fillcolor=CANDLE_UP),
        decreasing=dict(line=dict(color=CANDLE_DOWN, width=1), fillcolor=CANDLE_DOWN)))
    low, high = min(b[3] for b in bars), max(b[2] for b in bars)
    for level, label, color in ((resistance, "Max call OI", CANDLE_DOWN), (support, "Max put OI", CANDLE_UP)):
        # Only a level near the day's range: a far one would squash the candles.
        if level and low - (high - low) <= level <= high + (high - low):
            figure.add_hline(y=level, line=dict(color=color, width=1, dash="dash"),
                             annotation_text=f"{label} {level:,.0f}", annotation_position="top left")
    figure.update_layout(height=380, margin=dict(l=4, r=4, t=4, b=4), showlegend=False, hovermode="x",
                         xaxis=dict(rangeslider_visible=False, gridcolor="rgba(128,128,128,0.10)",
                                    **({"type": "category", "nticks": 8} if many else {"tickformat": "%H:%M"})),
                         yaxis=dict(tickformat=",.0f", gridcolor="rgba(128,128,128,0.15)", side="right"))
    st.plotly_chart(figure, width="stretch", key=f"oc-day-{index}-{minutes}", config={"displayModeBar": False})


@st.fragment(run_every=15 if auto else None)
def chain_view() -> None:
    try:
        price, rows, at = load_chain(index, expiry)
    except dd.DhanError as e:
        st.error(str(e))
        return
    if not rows:
        st.warning("Empty option chain from Dhan.")
        return
    df = pd.DataFrame(rows)
    # Regular strikes only: after a dividend NSE also lists adjusted ones (e.g. 364.75 by 365).
    step = underlying["step"]
    df = df[((df["strike"] / step) - (df["strike"] / step).round()).abs() < 1e-6].reset_index(drop=True)
    if df.empty:
        st.warning("No regular strikes in Dhan's option chain.")
        return
    atm = dd.atm_strike(price, underlying)
    i = (df["strike"] - atm).abs().idxmin()
    view = df.iloc[max(0, i - AROUND): i + AROUND + 1].reset_index(drop=True)

    ce_oi, pe_oi = df["ce_oi"].fillna(0).sum(), df["pe_oi"].fillna(0).sum()
    m = st.columns(5)
    m[0].metric(index, f"{price:,.2f}")
    m[1].metric("ATM strike", f"{atm:,.0f}")
    m[2].metric("PCR (OI)", f"{pe_oi / ce_oi:.2f}" if ce_oi else "–")
    m[3].metric("Max call OI (resistance)", f"{df.loc[df['ce_oi'].idxmax(), 'strike']:,.0f}" if ce_oi else "–")
    m[4].metric("Max put OI (support)", f"{df.loc[df['pe_oi'].idxmax(), 'strike']:,.0f}" if pe_oi else "–")
    day_chart(df.loc[df["ce_oi"].idxmax(), "strike"] if ce_oi else None,
              df.loc[df["pe_oi"].idxmax(), "strike"] if pe_oi else None)

    table = pd.DataFrame({
        "CE OI chg (L)": view["ce_oi_chg"].map(fmt_lakh),
        "CE OI (L)": view["ce_oi"].map(fmt_lakh),
        "CE Vol (L)": view["ce_volume"].map(fmt_lakh),
        "CE IV": pd.to_numeric(view["ce_iv"], errors="coerce").round(1),
        "CE Chg": view["ce_chg"],
        "CE LTP": view["ce_ltp"],
        "Strike": view["strike"].astype(int),
        "PE LTP": view["pe_ltp"],
        "PE Chg": view["pe_chg"],
        "PE IV": pd.to_numeric(view["pe_iv"], errors="coerce").round(1),
        "PE Vol (L)": view["pe_volume"].map(fmt_lakh),
        "PE OI (L)": view["pe_oi"].map(fmt_lakh),
        "PE OI chg (L)": view["pe_oi_chg"].map(fmt_lakh),
    })

    def shade(row):
        strike = row["Strike"]
        styles = []
        for col in row.index:
            if strike == atm:
                styles.append("background-color: #fff3c4; font-weight: 700")
            elif (col.startswith("CE") and strike < price) or (col.startswith("PE") and strike > price):
                styles.append("background-color: #f3f6fb")  # in the money
            elif col == "Strike":
                styles.append("font-weight: 600")
            else:
                styles.append("")
        return styles

    def updown(v):
        if v is None or pd.isna(v) or v == 0:
            return ""
        return "color: #0a7d3b" if v > 0 else "color: #c0392b"

    styled = (table.style.apply(shade, axis=1)
              .map(updown, subset=["CE OI chg (L)", "CE Chg", "PE Chg", "PE OI chg (L)"])
              .format(precision=2, na_rep="–"))
    st.dataframe(styled, hide_index=True, width="stretch", height=min(35 * (len(table) + 1) + 3, 900))
    st.caption(f"Dhan, {at} IST · expiry {expiry} · shaded: in the money · highlighted: ATM · OI and volume in lakh.")

    oc.atm_section(index, underlying, df, price, prefix="oc")


chain_view()
