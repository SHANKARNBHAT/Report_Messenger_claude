"""
Alerts log page: every alert the Auto trader sent on Telegram (auto_trader.log_alerts, the Auto trader's
branch, alerts/YYYY-MM.json): the HA CE / PE turn-green alerts on the ATM options (opt_ha_alert.py) and
the RSI 2H / 4H / Daily entries. Newest first, with filters.
"""

from __future__ import annotations

from datetime import date, datetime
from urllib.parse import quote

import pandas as pd
import requests
import streamlit as st

import futures_simulator as fs
import telegram_alerts

TABS = {"15m": "15 min", "30m": "30 min", "1H": "1 hour", "2H": "2 hour", "3H": "3 hour", "4H": "4 hour"}  # the timeframe tabs
MONTHS_BACK = 24  # months of logs looked for (the bot keeps the log under 1 MB: auto_trader.ALERTS_MAX_BYTES)


def months(today: date) -> list[str]:
    out, year, month = [], today.year, today.month
    for _ in range(MONTHS_BACK):
        out.append(f"{year}-{month:02d}")
        year, month = (year, month - 1) if month > 1 else (year - 1, 12)
    return out


@st.cache_data(ttl=120, show_spinner=False)
def load(today: str) -> tuple[list[dict], str | None]:
    """The logged alerts of the last MONTHS_BACK months, and an error message if a month could not be read."""
    rows, error = [], None
    for month in months(date.fromisoformat(today)):
        try:
            response = fs.data_file(f"alerts/{month}.json")
            if response.status_code == 404:
                if rows:  # the oldest months go first, so none before a missing one
                    break
                continue
            response.raise_for_status()
            rows += response.json()
        except (requests.RequestException, ValueError) as exc:
            error = f"{month}: {exc}"
    return rows, error


def table(rows: list[dict]) -> pd.DataFrame:
    df = pd.DataFrame(rows)
    for column in ("option", "rule", "price", "ltp", "spot", "lot", "atr", "ratio", "replay"):
        if column not in df:
            df[column] = None
    df["when"] = pd.to_datetime(df["time"], unit="ms", utc=True).dt.tz_convert(fs.IST).dt.tz_localize(None)
    df["day"] = df["when"].dt.date
    # The stock's Chartink chart with the dashboard's indicators; the name after "#" is the link's text.
    df["chart"] = df["symbol"].map(lambda s: telegram_alerts.CHARTINK.format(quote(s, safe="")) + f"#{s}")
    df["direction"] = df["side"].map({"CE": "🟢 Up", "BUY": "🟢 Up", "PE": "🔴 Down", "SELL": "🔴 Down"})
    df["replay"] = df["replay"].map(lambda v: v is True)
    df["rule"] = df["rule"].replace({"HA doji": "HA doji, candle solid green",
                                     "After green doji": "HA solid green after a green HA doji"})
    return df.sort_values("when", ascending=False).reset_index(drop=True)


def main() -> None:
    st.title("🔔 Alerts log")
    st.caption("Every alert the Auto trader sent on Telegram: the HA turn-green alerts on the ATM CE / PE "
               "(15 min to 4 hours), the RSI entries and the OPT RSI option buys: the log is kept under 1 MB, the oldest alerts deleted past it; a replayed day (the ha-today "
               "action) is marked, its strike being the ATM at the close.")
    if st.button("↻ Refresh", key="alerts-refresh"):
        load.clear()
    rows, error = load(date.today().isoformat())
    if error:
        st.warning(f"Could not read part of the log ({error}).")
    if not rows:
        st.info("No alerts logged yet. They appear here as the Auto trader sends them.")
        return
    df = table(rows)

    cols = st.columns([2, 2, 3])
    days = sorted(df["day"].unique(), reverse=True)
    day_pick = cols[0].selectbox("Day", ["All days", *days],
                                 format_func=lambda d: d if isinstance(d, str) else d.strftime("%a %d %b"))
    kinds = cols[1].multiselect("Alert", sorted(df["type"].unique()), placeholder="All")
    search = cols[2].text_input("Stock", placeholder="e.g. KAYNES").strip().upper()
    shown = df
    if not isinstance(day_pick, str):
        shown = shown[shown["day"] == day_pick]
    if kinds:
        shown = shown[shown["type"].isin(kinds)]
    if search:
        shown = shown[shown["symbol"].str.contains(search, regex=False)]

    # A tab per timeframe (RSI Daily only if it has alerts), then all of them together.
    frames = [*TABS, *(["Daily"] if (shown["timeframe"] == "Daily").any() else [])]
    tabs = st.tabs([f"{TABS.get(f, f)} ({int((shown['timeframe'] == f).sum())})" for f in frames] + [f"All ({len(shown)})"])
    for tab, frame in zip(tabs, [*frames, None]):
        with tab:
            alerts_view(shown if frame is None else shown[shown["timeframe"] == frame], frame or "all")


def alerts_view(shown: pd.DataFrame, key: str) -> None:
    """One tab: its counts and alerts, newest first."""
    if shown.empty:
        st.caption("No alerts on this timeframe yet.")
        return
    m = st.columns(4)
    m[0].metric("Alerts", len(shown))
    m[1].metric("🟢 Up (CE / buy)", int((shown["direction"] == "🟢 Up").sum()))
    m[2].metric("🔴 Down (PE / sell)", int((shown["direction"] == "🔴 Down").sum()))
    m[3].metric("Stocks", shown["symbol"].nunique())
    st.dataframe(
        shown[["when", "type", "timeframe", "direction", "chart", "option", "rule", "price", "ltp", "spot", "lot",
               "atr", "ratio", "replay"]],
        hide_index=True, width="stretch", height=min(700, 38 + 35 * len(shown)), key=f"alerts-table-{key}",
        column_config={
            "when": st.column_config.DatetimeColumn("Candle / signal", format="DD MMM HH:mm"),
            "type": "Alert", "timeframe": "TF", "direction": "Direction",
            "chart": st.column_config.LinkColumn("Stock", display_text=r"#(.+)$",
                                                 help="Opens the stock's chart on Chartink, with your indicators"),
            "option": "Option", "rule": "Rule",
            "price": st.column_config.NumberColumn("Close", help="HA alerts: the option's candle close; RSI: the stock's entry price"),
            "ltp": st.column_config.NumberColumn("LTP", help="The option's last price when the alert was sent"),
            "spot": st.column_config.NumberColumn("Spot", format="%.2f"),
            "lot": st.column_config.NumberColumn("Lot"),
            "atr": st.column_config.NumberColumn("ATR ₹", format="%.0f", help="Daily ATR (14) in rupees: stocks above ₹100 are scanned"),
            "ratio": st.column_config.NumberColumn("ATR ×", format="%.1f", help="Daily ATR in strike intervals"),
            "replay": st.column_config.CheckboxColumn("Replay"),
        })
    st.download_button("⬇ Download CSV", shown.drop(columns=["day", "chart"]).to_csv(index=False).encode(),
                       file_name=f"alerts_{key}_{date.today():%Y%m%d}.csv", mime="text/csv", key=f"alerts-csv-{key}")


if __name__ == "__main__":
    main()
