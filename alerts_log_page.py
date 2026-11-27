"""
Alerts log page: every alert the Auto trader sent on Telegram (auto_trader.log_alerts, the Auto trader's
branch, alerts/YYYY-MM.json): the HA CE / PE turn-green alerts on the ATM options (opt_ha_alert.py) and
the RSI 2H / 4H / Daily entries. Newest first, with filters.
"""

from __future__ import annotations

from datetime import date, datetime

import pandas as pd
import requests
import streamlit as st

import futures_simulator as fs

MONTHS_BACK = 2  # months of logs read (the bot keeps 30 days: auto_trader.ALERTS_KEEP_DAYS)


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
                continue
            response.raise_for_status()
            rows += response.json()
        except (requests.RequestException, ValueError) as exc:
            error = f"{month}: {exc}"
    return rows, error


def table(rows: list[dict]) -> pd.DataFrame:
    df = pd.DataFrame(rows)
    for column in ("option", "rule", "price", "ltp", "spot", "lot", "ratio", "replay"):
        if column not in df:
            df[column] = None
    df["when"] = pd.to_datetime(df["time"], unit="ms", utc=True).dt.tz_convert(fs.IST).dt.tz_localize(None)
    df["day"] = df["when"].dt.date
    df["direction"] = df["side"].map({"CE": "🟢 Up", "BUY": "🟢 Up", "PE": "🔴 Down", "SELL": "🔴 Down"})
    df["replay"] = df["replay"].map(lambda v: v is True)
    df["rule"] = df["rule"].replace({"HA doji": "HA doji, candle solid green",
                                     "After green doji": "HA solid green after a green HA doji"})
    return df.sort_values("when", ascending=False).reset_index(drop=True)


def main() -> None:
    st.title("🔔 Alerts log")
    st.caption("Every alert the Auto trader sent on Telegram: the HA turn-green alerts on the ATM CE / PE "
               "(1H / 2H / 3H / 4H) and the RSI entries: the last 30 days (at most 2,000 alerts), older ones deleted; a replayed day (the ha-today "
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

    cols = st.columns([2, 2, 2, 3])
    days = sorted(df["day"].unique(), reverse=True)
    day_pick = cols[0].selectbox("Day", ["All days", *days],
                                 format_func=lambda d: d if isinstance(d, str) else d.strftime("%a %d %b"))
    kinds = cols[1].multiselect("Alert", sorted(df["type"].unique()), placeholder="All")
    frames = cols[2].multiselect("Timeframe", [t for t in ("1H", "2H", "3H", "4H", "Daily") if t in set(df["timeframe"])],
                                 placeholder="All")
    search = cols[3].text_input("Stock", placeholder="e.g. KAYNES").strip().upper()
    shown = df
    if not isinstance(day_pick, str):
        shown = shown[shown["day"] == day_pick]
    if kinds:
        shown = shown[shown["type"].isin(kinds)]
    if frames:
        shown = shown[shown["timeframe"].isin(frames)]
    if search:
        shown = shown[shown["symbol"].str.contains(search, regex=False)]

    m = st.columns(4)
    m[0].metric("Alerts", len(shown))
    m[1].metric("🟢 Up (CE / buy)", int((shown["direction"] == "🟢 Up").sum()))
    m[2].metric("🔴 Down (PE / sell)", int((shown["direction"] == "🔴 Down").sum()))
    m[3].metric("Stocks", shown["symbol"].nunique())

    st.dataframe(
        shown[["when", "type", "timeframe", "direction", "symbol", "option", "rule", "price", "ltp", "spot", "lot",
               "ratio", "replay"]],
        hide_index=True, width="stretch", height=min(700, 38 + 35 * len(shown)),
        column_config={
            "when": st.column_config.DatetimeColumn("Candle / signal", format="DD MMM HH:mm"),
            "type": "Alert", "timeframe": "TF", "direction": "Direction", "symbol": "Stock",
            "option": "Option", "rule": "Rule",
            "price": st.column_config.NumberColumn("Close", help="HA alerts: the option's candle close; RSI: the stock's entry price"),
            "ltp": st.column_config.NumberColumn("LTP", help="The option's last price when the alert was sent"),
            "spot": st.column_config.NumberColumn("Spot", format="%.2f"),
            "lot": st.column_config.NumberColumn("Lot"),
            "ratio": st.column_config.NumberColumn("ATR ×", format="%.1f", help="Daily ATR in strike intervals"),
            "replay": st.column_config.CheckboxColumn("Replay"),
        })
    st.download_button("⬇ Download CSV", shown.drop(columns=["day"]).to_csv(index=False).encode(),
                       file_name=f"alerts_{date.today():%Y%m%d}.csv", mime="text/csv")


if __name__ == "__main__":
    main()
