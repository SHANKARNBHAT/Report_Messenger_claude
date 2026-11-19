"""
NIFTY option data from Dhan (DhanHQ API v2), for the option chain page (option_chain_page.py)
and the planned ATM option HA strategy.

Credentials: DHAN_CLIENT_ID and DHAN_ACCESS_TOKEN, read from the environment (GitHub
Actions secrets) or, in the Streamlit app, from st.secrets. A token made on web.dhan.co
(My Profile -> Access DhanHQ APIs) is valid for 24 hours.

  * expiries()            the NIFTY option expiries Dhan lists (YYYY-MM-DD)
  * option_chain(expiry)  every strike's CE and PE: LTP, OI, volume, IV, greeks, security id
  * candles(security_id)  intraday candles of one option contract (today's included)
  * atm_candles(side)     past candles of the rolling ATM CE / PE (Dhan's expired-options
                          data: the strike follows the index, so no contract list is needed)

Dhan allows one option chain request every 3 seconds; callers cache (the page does).
"""

from __future__ import annotations

import os
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import requests

API = "https://api.dhan.co/v2"
IST = ZoneInfo("Asia/Kolkata")
NIFTY = {"id": 13, "segment": "IDX_I", "step": 50}
TIMEOUT = 15


class DhanError(RuntimeError):
    pass


def credentials() -> tuple[str, str]:
    """(client id, access token) from the environment, else Streamlit secrets."""
    client, token = os.environ.get("DHAN_CLIENT_ID", ""), os.environ.get("DHAN_ACCESS_TOKEN", "")
    if not (client and token):
        try:
            import streamlit as st

            client = client or str(st.secrets.get("DHAN_CLIENT_ID", ""))
            token = token or str(st.secrets.get("DHAN_ACCESS_TOKEN", ""))
        except Exception:  # no Streamlit, or no secrets file
            pass
    client, token = client.strip(), token.strip()
    if not (client and token):
        raise DhanError("DHAN_CLIENT_ID and DHAN_ACCESS_TOKEN are not set.")
    return client, token


def post(path: str, body: dict) -> dict:
    client, token = credentials()
    headers = {"access-token": token, "client-id": client,
               "Content-Type": "application/json", "Accept": "application/json"}
    try:
        reply = requests.post(f"{API}{path}", json=body, headers=headers, timeout=TIMEOUT)
    except requests.RequestException as e:
        raise DhanError(f"Dhan did not answer: {e}") from e
    try:
        data = reply.json()
    except ValueError:
        data = {}
    if reply.status_code != 200 or (isinstance(data, dict) and data.get("status") == "failure"):
        detail = (data.get("errorMessage") or data.get("remarks") or data.get("data")) if isinstance(data, dict) else None
        if reply.status_code == 401 or "token" in str(detail).lower():
            raise DhanError(f"Dhan refused the access token (expired? it lasts 24 hours): {detail or reply.text[:200]}")
        raise DhanError(f"Dhan {path}: HTTP {reply.status_code} {detail or reply.text[:200]}")
    return data


def expiries(underlying: dict = NIFTY) -> list[str]:
    data = post("/optionchain/expirylist", {"UnderlyingScrip": underlying["id"], "UnderlyingSeg": underlying["segment"]})
    return sorted(data.get("data") or [])


def option_chain(expiry: str, underlying: dict = NIFTY) -> tuple[float, list[dict]]:
    """(index price, rows by strike). Each row: strike, then ce_* and pe_* fields (ltp, chg, oi,
    oi_chg, volume, iv, delta, theta, bid, ask, id)."""
    data = post("/optionchain", {"UnderlyingScrip": underlying["id"], "UnderlyingSeg": underlying["segment"],
                                  "Expiry": expiry}).get("data") or {}
    rows = []
    for strike, legs in (data.get("oc") or {}).items():
        row = {"strike": float(strike)}
        for side in ("ce", "pe"):
            leg = (legs or {}).get(side) or {}
            greeks = leg.get("greeks") or {}
            ltp, prev = leg.get("last_price"), leg.get("previous_close_price")
            row |= {
                f"{side}_ltp": ltp,
                f"{side}_chg": round(ltp - prev, 2) if ltp is not None and prev else None,
                f"{side}_oi": leg.get("oi"),
                f"{side}_oi_chg": (leg["oi"] - leg["previous_oi"]) if leg.get("oi") is not None
                                  and leg.get("previous_oi") is not None else None,
                f"{side}_volume": leg.get("volume"),
                f"{side}_iv": leg.get("implied_volatility"),
                f"{side}_delta": greeks.get("delta"),
                f"{side}_theta": greeks.get("theta"),
                f"{side}_bid": leg.get("top_bid_price"),
                f"{side}_ask": leg.get("top_ask_price"),
                f"{side}_id": leg.get("security_id"),
            }
        rows.append(row)
    rows.sort(key=lambda r: r["strike"])
    return float(data.get("last_price") or 0), rows


def atm_strike(price: float, underlying: dict = NIFTY) -> float:
    return round(price / underlying["step"]) * underlying["step"]


def _bars(data: dict) -> list[tuple]:
    """(start epoch, open, high, low, close, volume) from Dhan's parallel arrays."""
    keys = ("timestamp", "open", "high", "low", "close", "volume")
    cols = [data.get(k) or [] for k in keys]
    n = min(len(c) for c in cols[:5])
    vols = cols[5] if len(cols[5]) >= n else [0] * n
    return [(int(cols[0][i]), cols[1][i], cols[2][i], cols[3][i], cols[4][i], vols[i]) for i in range(n)]


def candles(security_id: int | str, minutes: int = 15, days: int = 5) -> list[tuple]:
    """Intraday candles of one NSE option contract, oldest first, from `days` ago up to now."""
    now = datetime.now(IST)
    data = post("/charts/intraday", {
        "securityId": str(security_id), "exchangeSegment": "NSE_FNO", "instrument": "OPTIDX",
        "interval": str(minutes), "oi": False,
        "fromDate": (now - timedelta(days=days)).strftime("%Y-%m-%d 09:15:00"),
        "toDate": now.strftime("%Y-%m-%d %H:%M:%S"),
    })
    return _bars(data)


def atm_candles(side: str, start: date, end: date, minutes: int = 15, strike: str = "ATM",
                underlying: dict = NIFTY, expiry_flag: str = "WEEK", expiry_code: int = 1) -> list[tuple]:
    """Past candles of the rolling ATM (or "ATM+1" ...) call (side "CE") or put ("PE"), from
    `start` to `end` (not included), fetched 30 days at a time (Dhan's limit a call)."""
    out: list[tuple] = []
    day = start
    while day < end:
        upto = min(day + timedelta(days=30), end)
        data = post("/charts/rollingoption", {
            "exchangeSegment": "NSE_FNO", "interval": str(minutes), "securityId": underlying["id"],
            "instrument": "OPTIDX", "expiryFlag": expiry_flag, "expiryCode": expiry_code, "strike": strike,
            "drvOptionType": "CALL" if side.upper() == "CE" else "PUT",
            "requiredData": ["open", "high", "low", "close", "volume", "strike", "spot"],
            "fromDate": day.isoformat(), "toDate": upto.isoformat(),
        }).get("data") or {}
        out += _bars(data.get(side.lower()) or {})
        day = upto
    return out


def heikin_ashi(bars: list[tuple]) -> list[dict]:
    """HA candles of (start, open, high, low, close, ...) bars, with the strategy's flags:
    green, and solid (green with no lower wick)."""
    out: list[dict] = []
    for start, open_, high, low, close, *_ in bars:
        ha_close = (open_ + high + low + close) / 4
        ha_open = (open_ + close) / 2 if not out else (out[-1]["open"] + out[-1]["close"]) / 2
        ha_high, ha_low = max(high, ha_open, ha_close), min(low, ha_open, ha_close)
        green = ha_close > ha_open
        out.append({"start": start, "open": ha_open, "high": ha_high, "low": ha_low, "close": ha_close,
                    "green": green, "solid": green and ha_low >= ha_open})
    return out
