"""
Volume profile levels of the futures stocks
===========================================

Writes volume_profile_levels.json: the Point of Control (POC) and the 70% value
area high and low (VAH, VAL) of each stock, over five periods. Run by the report
pipeline after the close; the Chartink page shows the file. A run reads about
600 Yahoo addresses, too many for a page to do while it opens, and the levels
only change with a new close.

Method
  Bars      15-minute bars for the last 60 days. Older days use 60-minute bars,
            as Yahoo keeps 15-minute bars only that long. Yahoo gives most
            60-minute 9:15 bars zero volume, so such a bar gets the day's volume
            less the other hours, and leaves volume before a split unadjusted,
            which is put right from the daily bars (fill_open_volume). Prices
            and volume are the cash stock on NSE, not the futures contract.
            Dividends are not adjusted. Bars before a demerger are dropped.
  Periods   Weekly and Monthly are the last finished calendar week and month, so
            they stay fixed until the next one ends. 3M, 6M and 1Y are rolling,
            ending at the last close.
  Bins      BIN_PCT of the stock's last close in the period, at least MIN_BIN.
  Spreading A bar's volume is spread evenly from its low to its high.
  POC       The middle of the bin with the most volume.
  VAH, VAL  From the POC, the fuller of the next bin above and below is added
            until the bins hold 70% of the volume; these are the outer edges.

Run: python volume_profile.py
"""

from __future__ import annotations

import json
import sys
import time as clock
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, time
from pathlib import Path
from urllib.parse import quote
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
import requests

try:
    # Trust the certificates installed in Windows/macOS, not only Python's own list.
    import truststore

    truststore.inject_into_ssl()
except ImportError:
    pass

# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------
FUTURES_CSV = Path(__file__).with_name("futures stocks.csv")
OUTPUT = Path(__file__).with_name("volume_profile_levels.json")
YAHOO_CHART_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{}"
HEADERS = {"User-Agent": "Mozilla/5.0"}
IST = ZoneInfo("Asia/Kolkata")
PERIODS = ("Weekly", "Monthly", "3M", "6M", "1Y")
ROLLING_MONTHS = {"3M": 3, "6M": 6, "1Y": 12}
VALUE_AREA = 0.70
BIN_PCT = 0.25          # bin width, in % of the last close
MIN_BIN = 0.01          # rupees: the smallest tick
HISTORY_DAYS = 400      # of 60-minute and daily bars: over a year, past the weekend and holidays
SETTLED = time(15, 45)  # a session's bars count from this time on
OPEN_BAR = time(9, 15)
LATE_START_DAYS = 7     # a stock with no bars this long into a period is left blank for it
SPLIT_DAYS = 11         # sessions looked at to tell a split from a day's odd volume
SPLIT_STEP = 1.4        # below the smallest usual split or bonus, 1:2 (1.5 times)
GAP = 0.25              # overnight move taken as a demerger
WORKERS = 5
RETRIES = 4
MIN_STOCKS = 0.9        # share of the stocks needed to replace the last file


# ---------------------------------------------------------------------------
# Yahoo
# ---------------------------------------------------------------------------
def yahoo_bars(symbol: str, interval: str, **query) -> pd.DataFrame:
    """A stock's bars, with times in IST."""
    url = YAHOO_CHART_URL.format(quote(f"{symbol}.NS", safe=""))
    for attempt in range(RETRIES):
        try:
            response = requests.get(url, params={"interval": interval, **query}, headers=HEADERS, timeout=30)
            response.raise_for_status()
            result = response.json()["chart"]["result"][0]
            break
        except (requests.RequestException, ValueError, KeyError, TypeError):
            if attempt == RETRIES - 1:
                raise
            clock.sleep(5 * (attempt + 1))
    quotes = result["indicators"]["quote"][0]
    bars = pd.DataFrame({name: quotes[name] for name in ("high", "low", "close", "volume")},
                        index=pd.to_datetime(result["timestamp"], unit="s", utc=True).tz_convert(IST))
    return bars.dropna(subset=["high", "low", "close"]).fillna({"volume": 0})


def fill_open_volume(hourly: pd.DataFrame, daily: pd.DataFrame, fine: pd.DataFrame) -> pd.DataFrame:
    """60-minute bars with the volume they lack, worked out from the daily bars. `fine`, the
    15-minute bars, shows how the two compare where the volume is known to be right."""
    days = hourly.index.normalize()
    is_open = hourly.index.time == OPEN_BAR
    other_hours = hourly["volume"][~is_open].groupby(days[~is_open]).sum()
    day_volume = pd.Series(daily["volume"].to_numpy(), index=daily.index.normalize()).reindex(other_hours.index)
    fine_volume = fine["volume"].groupby(fine.index.normalize()).sum()
    both = other_hours.index.intersection(fine_volume.index)

    # Yahoo adjusts 60-minute prices for a split but not their volume: before one, the daily
    # volume stands a whole split ratio higher against them. The median skips a day's bad value.
    ratio = day_volume / other_hours.where(other_hours > 0)
    step = ratio.rolling(SPLIT_DAYS, center=True, min_periods=3).median() / ratio[both].median()
    step = step.where(step > SPLIT_STEP, 1.0)
    other_hours = other_hours * step

    # The bars miss part of the daily volume, whatever the day: taking that out of the rest
    # keeps it off the 9:15 bar. A daily volume too low to use gets the stock's usual share.
    covered = (fine_volume[both] / day_volume[both]).median()
    missing = day_volume * covered - other_hours
    usual = (missing / other_hours)[missing > 0].median()
    missing = missing.where(missing > 0, other_hours * usual)

    filled = hourly.copy()
    filled["volume"] = hourly["volume"].to_numpy() * step.reindex(days).fillna(1).to_numpy()
    empty = is_open & (hourly["volume"].to_numpy() == 0)
    filled.loc[empty, "volume"] = missing.reindex(days[empty]).fillna(0).to_numpy()
    return filled


def after_last_gap(bars: pd.DataFrame) -> pd.DataFrame:
    """The bars since the last overnight jump too big to be a market move: Yahoo leaves a
    demerger's price drop in the bars, and a profile should not span two different shares."""
    before = bars["close"].shift()
    gaps = np.flatnonzero((bars["low"] > before * (1 + GAP)) | (bars["high"] < before * (1 - GAP)))
    return bars.iloc[gaps[-1]:] if len(gaps) else bars


def stock_bars(symbol: str, now: datetime) -> pd.DataFrame:
    """A stock's bars: 15-minute ones where Yahoo has them, 60-minute ones before that."""
    span = {"period1": int(now.timestamp()) - HISTORY_DAYS * 86400, "period2": int(now.timestamp())}
    fine = yahoo_bars(symbol, "15m", range="60d")
    hourly = fill_open_volume(yahoo_bars(symbol, "60m", **span), yahoo_bars(symbol, "1d", **span), fine)
    return after_last_gap(pd.concat([hourly[hourly.index < fine.index[0].normalize()], fine]))


# ---------------------------------------------------------------------------
# Levels
# ---------------------------------------------------------------------------
def last_settled_day(now: datetime) -> pd.Timestamp:
    """The latest day whose session is over (it may be a day with no session)."""
    day = pd.Timestamp(now.date())
    return day if now.time() >= SETTLED else day - pd.Timedelta(days=1)


def period_spans(end: pd.Timestamp) -> dict[str, tuple[pd.Timestamp, pd.Timestamp]]:
    """Period -> its first and last day, for a settled day `end`."""
    next_weekday = end + pd.offsets.BDay(1)  # the week or month that holds it is the unfinished one
    week = (next_weekday - pd.Timedelta(days=next_weekday.weekday())).normalize()
    month = next_weekday.normalize().replace(day=1)
    spans = {"Weekly": (week - pd.Timedelta(days=7), week - pd.Timedelta(days=3)),  # Monday to Friday
             "Monthly": (month - pd.DateOffset(months=1), month - pd.Timedelta(days=1))}
    for period, months in ROLLING_MONTHS.items():
        spans[period] = (end - pd.DateOffset(months=months) + pd.Timedelta(days=1), end)
    return spans


def profile(high: np.ndarray, low: np.ndarray, volume: np.ndarray, step: float) -> tuple[np.ndarray, np.ndarray]:
    """Bin edges and the volume in each bin, with each bar's volume spread evenly over its range."""
    first = np.floor(low.min() / step + 1e-9)
    count = max(int(np.ceil(high.max() / step - 1e-9) - first), 1)
    edges = (first + np.arange(count + 1)) * step
    height = high - low
    flat = height <= 0
    overlap = np.clip(np.minimum(high[~flat, None], edges[1:]) - np.maximum(low[~flat, None], edges[:-1]), 0, None)
    bins = (volume[~flat] / height[~flat]) @ overlap
    # A bar with one price holds all its volume at that price.
    np.add.at(bins, np.clip(np.searchsorted(edges, low[flat], side="right") - 1, 0, count - 1), volume[flat])
    return edges, bins


def value_area(bins: np.ndarray) -> tuple[int, int, int]:
    """Indexes of the POC bin and of the lowest and highest bins of the value area."""
    # Bins tied for the most volume: the one nearest the middle of the range.
    tied = np.flatnonzero(bins >= bins.max() * (1 - 1e-9))
    poc = int(tied[np.argmin(np.abs(tied - (len(bins) - 1) / 2))])
    bottom = top = poc
    held, target = bins[poc], bins.sum() * VALUE_AREA
    while held < target:
        above = bins[top + 1] if top + 1 < len(bins) else -1.0
        below = bins[bottom - 1] if bottom > 0 else -1.0
        if above >= below:
            top += 1
            held += above
        if below >= above:
            bottom -= 1
            held += below
    return poc, bottom, top


def levels(bars: pd.DataFrame) -> dict[str, float] | None:
    """POC, VAH and VAL of some bars; None when they hold no volume."""
    bars = bars[bars["volume"] > 0]
    if bars.empty:
        return None
    step = max(float(bars["close"].iloc[-1]) * BIN_PCT / 100, MIN_BIN)
    edges, bins = profile(bars["high"].to_numpy(float), bars["low"].to_numpy(float),
                          bars["volume"].to_numpy(float), step)
    poc, bottom, top = value_area(bins)
    return {"POC": round((edges[poc] + edges[poc + 1]) / 2, 2),
            "VAH": round(edges[top + 1], 2), "VAL": round(edges[bottom], 2)}


def stock_levels(symbol: str, bars: pd.DataFrame, end: pd.Timestamp) -> dict:
    """One row of the file: the last close and each period's levels."""
    bars = bars[bars.index.tz_localize(None).normalize() <= end]
    days = bars.index.tz_localize(None).normalize()
    row = {"Symbol": symbol, "Close": round(float(bars["close"].iloc[-1]), 2)}
    for period, (first, last) in period_spans(end).items():
        found = levels(bars[(days >= first) & (days <= last)])
        if days[0] > first + pd.Timedelta(days=LATE_START_DAYS):
            found = None  # listed or returned part-way through the period
        row.update({f"{period} {level}": (found or {}).get(level) for level in ("POC", "VAH", "VAL")})
    return row


# ---------------------------------------------------------------------------
# Run
# ---------------------------------------------------------------------------
def symbols() -> list[str]:
    """The stocks of the list, without its index and commodity rows (no volume on Yahoo)."""
    names = pd.read_csv(FUTURES_CSV)["Symbol"].dropna().astype(str).str.strip().drop_duplicates()
    return sorted(n for n in names if n and not set(n) & set("^=.") and "NIFTY" not in n)


def main() -> None:
    now = datetime.now(IST)
    end = last_settled_day(now)
    names = symbols()

    def one(symbol: str) -> dict | None:
        try:
            return stock_levels(symbol, stock_bars(symbol, now), end)
        except (requests.RequestException, ValueError, KeyError, TypeError, IndexError) as exc:
            print(f"{symbol}: {type(exc).__name__}: {exc}")
            return None

    with ThreadPoolExecutor(WORKERS) as pool:
        rows = [row for row in pool.map(one, names) if row]
    print(f"Levels for {len(rows)} of {len(names)} stocks")
    if len(rows) < len(names) * MIN_STOCKS:
        sys.exit("Too few stocks: the last file stays")
    OUTPUT.write_text(json.dumps({
        "generated": now.strftime("%Y-%m-%d %H:%M"),
        "periods": {period: [f"{first:%Y-%m-%d}", f"{last:%Y-%m-%d}"]
                    for period, (first, last) in period_spans(end).items()},
        "stocks": rows,
    }, separators=(",", ":")), encoding="utf-8")


if __name__ == "__main__":
    main()
