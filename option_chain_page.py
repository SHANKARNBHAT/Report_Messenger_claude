"""
Option chain page: the NIFTY or BANKNIFTY option chain from Dhan (dhan_data.py), the ATM strike and
5 strikes each side of it, with OI, volume, IV and premium changes; and below it the ATM call and put
on 15-minute Heikin Ashi candles, flagging the planned strategy's entry candle (the first
solid green HA candle, no lower wick, after a red one).

Needs DHAN_CLIENT_ID and DHAN_PIN + DHAN_TOTP_SECRET (or DHAN_ACCESS_TOKEN) in the Streamlit secrets.
"""

from __future__ import annotations

from datetime import datetime

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

import dhan_data as dd

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


@st.cache_data(ttl=600, show_spinner=False)
def load_expiries(index: str) -> list[str]:
    return dd.expiries(dd.INDICES[index])


@st.cache_data(ttl=5, show_spinner=False)  # Dhan allows one chain request every 3 seconds
def load_chain(index: str, expiry: str) -> tuple[float, list[dict], str]:
    price, rows = dd.option_chain(expiry, dd.INDICES[index])
    return price, rows, datetime.now(dd.IST).strftime("%H:%M:%S")


@st.cache_data(ttl=60, show_spinner=False)
def load_candles(security_id: int) -> list[tuple]:
    return dd.candles(security_id, minutes=15, days=5)


if not dd.secret("DHAN_CLIENT_ID") or not (dd.can_make_token() or dd.secret("DHAN_ACCESS_TOKEN")):
    st.info(SETUP)
    st.stop()

index = st.segmented_control("Index", list(dd.INDICES), default="NIFTY", key="oc-index") or "NIFTY"
underlying = dd.INDICES[index]

try:
    expiries = load_expiries(index)
except dd.DhanError as e:
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
    load_candles.clear()


def fmt_lakh(v):
    return None if v is None or pd.isna(v) else round(v / 1e5, 2)


CANDLE_UP, CANDLE_DOWN, SIGNAL_COLOR = "#1a9e6f", "#d64545", "#eda100"  # as the Auto trader chart
CHART_DAYS = 2  # trading days of 15-minute HA candles on the small charts


def is_entry(ha: list[dict], k: int) -> bool:
    """The strategy's entry candle: solid green (no lower wick) right after a red one."""
    return k > 0 and ha[k]["solid"] and not ha[k - 1]["green"]


def ha_chart(ha: list[dict], key: str) -> None:
    """A small 15-minute HA candle chart of the last CHART_DAYS trading days, the column's width,
    with an amber star under each entry candle. Times are categories, so nights leave no gap."""
    days = sorted({datetime.fromtimestamp(c["start"], dd.IST).date() for c in ha})[-CHART_DAYS:]
    keep = [k for k, c in enumerate(ha) if datetime.fromtimestamp(c["start"], dd.IST).date() in days]
    if not keep:
        return
    label = [datetime.fromtimestamp(ha[k]["start"], dd.IST).strftime("%d %b %H:%M") for k in keep]
    figure = go.Figure(go.Candlestick(
        x=label, open=[ha[k]["open"] for k in keep], high=[ha[k]["high"] for k in keep],
        low=[ha[k]["low"] for k in keep], close=[ha[k]["close"] for k in keep], name="HA",
        increasing=dict(line=dict(color=CANDLE_UP, width=1), fillcolor=CANDLE_UP),
        decreasing=dict(line=dict(color=CANDLE_DOWN, width=1), fillcolor=CANDLE_DOWN)))
    entries = [n for n, k in enumerate(keep) if is_entry(ha, k)]
    if entries:
        gap = 0.04 * (max(ha[k]["high"] for k in keep) - min(ha[k]["low"] for k in keep))
        figure.add_trace(go.Scatter(
            x=[label[n] for n in entries], y=[ha[keep[n]]["low"] - gap for n in entries], mode="markers",
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

    st.subheader("ATM CE / PE on 15-minute Heikin Ashi")
    st.caption("Strategy entry (amber star): the first solid green HA candle (no lower wick) after a red one. "
               "SL: that candle's previous HA candle's low. Charts: the last two trading days.")
    atm_row = df.loc[i]
    cols = st.columns(2)
    for col, side in zip(cols, ("ce", "pe")):
        sid = atm_row.get(f"{side}_id")
        with col:
            st.markdown(f"**{index} {int(atm_row['strike'])} {side.upper()}** · LTP {atm_row.get(f'{side}_ltp')}")
            if not sid:
                st.caption("No security id from Dhan.")
                continue
            try:
                ha = dd.heikin_ashi(load_candles(int(sid)))
            except dd.DhanError as e:
                st.error(str(e))
                continue
            if not ha:
                st.caption("No candles yet.")
                continue
            ha_chart(ha, key=f"oc-ha-{index}-{side}")
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


chain_view()
