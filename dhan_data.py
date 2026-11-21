"""
NIFTY and BANKNIFTY option data from Dhan (DhanHQ API v2), for the option chain page
(option_chain_page.py) and the planned ATM option HA strategy; also F&O stocks (stock()).

Credentials, read from the environment (GitHub Actions secrets) or else st.secrets:
  DHAN_CLIENT_ID      always
  DHAN_PIN and        your Dhan PIN and the TOTP secret key (the text under the QR code shown
  DHAN_TOTP_SECRET    when TOTP was set up): with these the access token is made automatically
                      (auth.dhan.co generateAccessToken, valid 24 hours), cached in .dhan_token.json
                      (git-ignored) and made again when Dhan refuses it.
  DHAN_ACCESS_TOKEN   optional: a token made by hand on web.dhan.co (Profile -> Trading & Data
                      APIs), used until it stops working. Needed only without PIN + TOTP secret.
Dhan keeps one token active at a time and makes at most one every 2 minutes, so a new token
from one place (the dashboard, a GitHub run, this PC) ends the others; each makes a fresh one
when its own is refused.

  * expiries()            the index's option expiries Dhan lists (YYYY-MM-DD)
  * option_chain(expiry)  every strike's CE and PE: LTP, OI, volume, IV, greeks, security id
  * candles(security_id)  intraday candles of one option contract (today's included)
  * atm_candles(side)     past candles of the rolling ATM CE / PE (Dhan's expired-options
                          data: the strike follows the index, so no contract list is needed)

Dhan allows one option chain request every 3 seconds; callers cache (the page does).
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import os
import re
import struct
import time
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import requests

try:  # trust the Windows certificate store too (antivirus HTTPS scanning, e.g. Norton, on the home PC)
    import truststore

    truststore.inject_into_ssl()
except ImportError:
    pass

API = "https://api.dhan.co/v2"
IST = ZoneInfo("Asia/Kolkata")
# BANKNIFTY has monthly expiries only (NSE stopped its weekly ones in Nov 2024).
NIFTY = {"name": "NIFTY", "id": 13, "segment": "IDX_I", "step": 50, "expiry_flag": "WEEK", "instrument": "OPTIDX"}
BANKNIFTY = {"name": "BANKNIFTY", "id": 25, "segment": "IDX_I", "step": 100, "expiry_flag": "MONTH",
             "instrument": "OPTIDX"}
INDICES = {i["name"]: i for i in (NIFTY, BANKNIFTY)}


def stock(name: str, security_id: int, step: float) -> dict:
    """An F&O stock as an underlying: its NSE equity security id on Dhan and its regular strike
    interval (both from strike_steps.json). Stock options expire monthly."""
    return {"name": name, "id": int(security_id), "segment": "NSE_EQ", "step": step,
            "expiry_flag": "MONTH", "instrument": "OPTSTK"}
TIMEOUT = 15


class DhanError(RuntimeError):
    pass


AUTH = "https://auth.dhan.co/app/generateAccessToken"
TOKEN_CACHE = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".dhan_token.json")
AUTH_ERRORS = re.compile(r"\b(dh-901|807|808|809)\b|invalid token|token expired|token invalid|"
                         r"access token|authentication failed")
_token: dict = {}  # this process's token: {"token": str, "expires": epoch seconds}


def secret(name: str) -> str:
    """A setting from the environment, else Streamlit secrets ("" when missing)."""
    value = os.environ.get(name, "")
    if not value:
        try:
            import streamlit as st

            value = str(st.secrets.get(name, ""))
        except Exception:  # no Streamlit, or no secrets file
            pass
    return value.strip()


def totp(key: str, at: float | None = None, digits: int = 6, step: int = 30) -> str:
    """The current RFC 6238 TOTP code for a base32 secret key (what the authenticator app shows)."""
    key = key.replace(" ", "").upper()
    raw = base64.b32decode(key + "=" * (-len(key) % 8))
    counter = int((time.time() if at is None else at) // step)
    mac = hmac.new(raw, struct.pack(">Q", counter), hashlib.sha1).digest()
    offset = mac[-1] & 0x0F
    code = (struct.unpack(">I", mac[offset:offset + 4])[0] & 0x7FFFFFFF) % 10 ** digits
    return str(code).zfill(digits)


def can_make_token() -> bool:
    return bool(secret("DHAN_CLIENT_ID") and secret("DHAN_PIN") and secret("DHAN_TOTP_SECRET"))


def _expiry_epoch(text: str | None) -> float:
    try:
        return datetime.fromisoformat(str(text)).replace(tzinfo=IST).timestamp()
    except ValueError:
        return time.time() + 23 * 3600


def new_token() -> str:
    """Make a fresh access token with the client id, PIN and the current TOTP code, and cache it."""
    client, pin, key = secret("DHAN_CLIENT_ID"), secret("DHAN_PIN"), secret("DHAN_TOTP_SECRET")
    if not (client and pin and key):
        raise DhanError("DHAN_CLIENT_ID, DHAN_PIN and DHAN_TOTP_SECRET are needed to make a token.")
    try:
        code = totp(key)
    except (ValueError, binascii.Error) as e:
        raise DhanError("DHAN_TOTP_SECRET is not a valid TOTP secret key (the letters and digits "
                        "shown under the QR code).") from e
    try:
        reply = requests.post(AUTH, params={"dhanClientId": client, "pin": pin, "totp": code},
                              headers={"Accept": "application/json"}, timeout=TIMEOUT)
    except requests.RequestException as e:
        raise DhanError(f"Dhan login did not answer: {e}") from e
    try:
        data = reply.json()
    except ValueError:
        data = {}
    token = data.get("accessToken") if isinstance(data, dict) else None
    if reply.status_code != 200 or not token:
        detail = (data.get("message") or data.get("errorMessage") or data.get("remarks") or data) \
            if isinstance(data, dict) and data else reply.text[:200]
        if "2 minutes" in str(detail):
            raise DhanError("The Dhan token was just renewed somewhere else (another copy of this app, "
                            "e.g. one running on the PC, or GitHub). Dhan keeps one token at a time and "
                            "makes at most one every 2 minutes: refresh in 2 minutes.")
        raise DhanError(f"Dhan did not make a token (check DHAN_PIN and DHAN_TOTP_SECRET; at most one "
                        f"token every 2 minutes): HTTP {reply.status_code} {detail}")
    _token.clear()
    _token.update(token=token, expires=_expiry_epoch(data.get("expiryTime")))
    try:
        with open(TOKEN_CACHE, "w", encoding="utf-8") as f:
            json.dump(_token, f)
    except OSError:  # read-only disk: the token still lives in this process
        pass
    return token


def _cached_token() -> str:
    if not _token:
        try:
            with open(TOKEN_CACHE, encoding="utf-8") as f:
                _token.update(json.load(f))
        except (OSError, ValueError):
            pass
    if _token.get("token") and _token.get("expires", 0) > time.time() + 300:
        return _token["token"]
    return ""


def credentials(fresh: bool = False) -> tuple[str, str]:
    """(client id, access token): the cached token, else DHAN_ACCESS_TOKEN, else a new one made
    with PIN + TOTP. fresh=True skips straight to making a new one."""
    client = secret("DHAN_CLIENT_ID")
    if not client:
        raise DhanError("DHAN_CLIENT_ID is not set.")
    if fresh:
        return client, new_token()
    token = _cached_token() or secret("DHAN_ACCESS_TOKEN")
    if token:
        return client, token
    if can_make_token():
        return client, new_token()
    raise DhanError("Set DHAN_PIN and DHAN_TOTP_SECRET (or DHAN_ACCESS_TOKEN) for Dhan.")


def post(path: str, body: dict, _retry: bool = True) -> dict:
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
        text = f"{detail} {data.get('errorCode', '') if isinstance(data, dict) else ''}".lower()
        if "806" in text or "not subscribed" in text:
            raise DhanError(f"Dhan says the Data APIs plan is not active on this account (web.dhan.co -> "
                            f"Trading & Data APIs; then make a new access token): {detail}")
        if reply.status_code == 401 or AUTH_ERRORS.search(text):
            if _retry and can_make_token():
                credentials(fresh=True)
                return post(path, body, _retry=False)
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


def candles(security_id: int | str, minutes: int = 15, days: int = 5, instrument: str = "OPTIDX",
            segment: str = "NSE_FNO") -> list[tuple]:
    """Intraday candles of one NSE option contract (or, with segment and instrument, any security:
    underlying_candles), oldest first, from `days` ago up to now."""
    now = datetime.now(IST)
    data = post("/charts/intraday", {
        "securityId": str(security_id), "exchangeSegment": segment, "instrument": instrument,
        "interval": str(minutes), "oi": False,
        "fromDate": (now - timedelta(days=days)).strftime("%Y-%m-%d 09:15:00"),
        "toDate": now.strftime("%Y-%m-%d %H:%M:%S"),
    })
    return _bars(data)


def underlying_candles(underlying: dict = NIFTY, minutes: int = 15, days: int = 5) -> list[tuple]:
    """Intraday candles of the index or stock itself (not an option), oldest first."""
    instrument = "INDEX" if underlying["segment"] == "IDX_I" else "EQUITY"
    return candles(underlying["id"], minutes, days, instrument=instrument, segment=underlying["segment"])


def atm_candles(side: str, start: date, end: date, minutes: int = 15, strike: str = "ATM",
                underlying: dict = NIFTY, expiry_flag: str | None = None, expiry_code: int = 1) -> list[tuple]:
    """Past candles of the rolling ATM (or "ATM+1" ...) call (side "CE") or put ("PE"), from
    `start` to `end` (not included), today included. Dhan serves one day a call (its toDate is
    inclusive), so this asks day by day, skipping weekends, a little under its 5 calls a second."""
    out: list[tuple] = []
    day = start
    while day < end:
        if day.weekday() < 5:
            data = post("/charts/rollingoption", {
                "exchangeSegment": "NSE_FNO", "interval": str(minutes), "securityId": str(underlying["id"]),
                "instrument": underlying.get("instrument", "OPTIDX"), "expiryFlag": expiry_flag or underlying["expiry_flag"],
                "expiryCode": str(expiry_code), "strike": strike,
                "drvOptionType": "CALL" if side.upper() == "CE" else "PUT",
                "requiredData": ["open", "high", "low", "close", "volume", "strike", "spot"],
                "fromDate": day.isoformat(), "toDate": day.isoformat(),
            }).get("data") or {}
            out += [b for b in _bars(data.get(side.lower()) or {})
                    if datetime.fromtimestamp(b[0], IST).date() == day]
            time.sleep(0.25)
        day += timedelta(days=1)
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


if __name__ == "__main__":  # python dhan_data.py token: make a token now and check it works
    import sys

    if sys.argv[1:] == ["token"]:
        new_token()
        print("New Dhan token made, valid until",
              datetime.fromtimestamp(_token["expires"], IST).strftime("%d %b %Y %H:%M"), "IST")
        print("NIFTY expiries:", expiries()[:3])
