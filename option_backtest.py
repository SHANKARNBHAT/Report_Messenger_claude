"""
ATM option backtest of the HA IDX trades: each closed NIFTY / BANKNIFTY futures trade
(ha_index.py) replayed as buying 1 lot of the at-the-money option instead, at the same
entry and exit times and index levels.

  * Long -> buy the ATM call (CE); short -> buy the ATM put (PE). Option buying only.
  * Strike: the index at entry rounded to STRIKE_STEP.
  * Expiry: NIFTY weekly (the Tuesday on or after the trade day), BANKNIFTY monthly (the
    last Tuesday of the month, on or after the trade day). NSE holidays, which move an
    expiry a day earlier, are not counted.
  * Premium: Black-Scholes, with the India VIX at entry as the implied volatility
    (x BNF_IV_RATIO for BANKNIFTY), else FALLBACK_IV. An estimate, not traded prices:
    real premiums, skew and the IV moving during the trade are not modelled.
  * Spread: bought HALF_SPREAD points above the model price, sold HALF_SPREAD below.
  * Charges: NSE option rates (OPTION_CHARGES), STT 0.15% of the sell premium from
    1 April 2026.
"""

from __future__ import annotations

import math
from datetime import date, datetime, time, timedelta

STRIKE_STEP = {"NIFTY": 50, "BANKNIFTY": 100}
HALF_SPREAD = {"NIFTY": 0.5, "BANKNIFTY": 1.5}  # premium points paid on each side
FALLBACK_IV = {"NIFTY": 13.0, "BANKNIFTY": 15.0}  # % a year, when no India VIX
BNF_IV_RATIO = 1.15  # BANKNIFTY's ATM IV runs above NIFTY's (the VIX)
RATE = 0.06  # risk-free rate, a year
EXPIRY_CLOSE = time(15, 30)
MIN_PREMIUM = 0.05  # the tick
VIX_YAHOO = "^INDIAVIX"

OPTION_CHARGES = {
    "brokerage_per_order": 20.0,  # flat ₹20 an executed order
    "stt_sell": 0.0015,           # STT on the sell premium, 0.15% from 1 April 2026
    "exchange": 0.0003503,        # NSE option transaction charges, on premium, both sides
    "sebi": 0.000001,             # ₹10 per crore, both sides
    "stamp_buy": 0.00003,         # stamp duty on the buy premium
    "gst": 0.18,                  # on brokerage, exchange and SEBI charges
}


def charges(buy_value: float, sell_value: float) -> float:
    """Brokerage and taxes of an option round trip, on the premiums paid and received."""
    rates = OPTION_CHARGES
    brokerage = 2 * rates["brokerage_per_order"]
    exchange = (buy_value + sell_value) * rates["exchange"]
    sebi = (buy_value + sell_value) * rates["sebi"]
    stt = sell_value * rates["stt_sell"]
    stamp = buy_value * rates["stamp_buy"]
    return brokerage + exchange + sebi + stt + stamp + (brokerage + exchange + sebi) * rates["gst"]


def expiry(symbol: str, day: date) -> date:
    """The option expiry a trade on `day` uses: NIFTY's weekly, BANKNIFTY's monthly (Tuesdays)."""
    if symbol == "NIFTY":
        return day + timedelta(days=(1 - day.weekday()) % 7)
    for ahead in (0, 1):
        year, month = divmod(day.year * 12 + day.month - 1 + ahead, 12)
        month += 1
        following = date(year + month // 12, month % 12 + 1, 1)
        last = following - timedelta(days=1)
        tuesday = last - timedelta(days=(last.weekday() - 1) % 7)
        if tuesday >= day:
            return tuesday
    raise ValueError(day)


def _cdf(x: float) -> float:
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


def price(spot: float, strike: float, years: float, iv: float, call: bool) -> float:
    """Black-Scholes premium; `iv` a fraction a year. At or past expiry, the intrinsic value."""
    if years <= 0 or iv <= 0:
        return max(spot - strike, 0.0) if call else max(strike - spot, 0.0)
    sd = iv * math.sqrt(years)
    d1 = (math.log(spot / strike) + (RATE + iv * iv / 2) * years) / sd
    d2 = d1 - sd
    discount = math.exp(-RATE * years)
    if call:
        return spot * _cdf(d1) - strike * discount * _cdf(d2)
    return strike * discount * _cdf(-d2) - spot * _cdf(-d1)


def vix_at(vix_bars: list | None, seconds: float) -> float | None:
    """The India VIX of the last 5-minute bar started at or before `seconds`."""
    if not vix_bars:
        return None
    level = None
    for start, _, _, _, close in vix_bars:
        if start > seconds:
            break
        level = close
    return level


def replay(trade: dict, tz, vix_bars: list | None = None) -> dict:
    """A closed futures trade (symbol, side, qty, entry, exit, entry_time, exit_time in ms)
    as the ATM option bought at its entry and sold at its exit."""
    symbol = trade["symbol"]
    call = trade["side"] == "BUY"
    entered = datetime.fromtimestamp(trade["entry_time"] / 1000, tz)
    exited = datetime.fromtimestamp(trade["exit_time"] / 1000, tz)
    strike = round(float(trade["entry"]) / STRIKE_STEP[symbol]) * STRIKE_STEP[symbol]
    expires = expiry(symbol, entered.date())
    end = datetime.combine(expires, EXPIRY_CLOSE, tz)
    year = 365 * 86400

    vix = vix_at(vix_bars, entered.timestamp())
    iv = (vix * (BNF_IV_RATIO if symbol == "BANKNIFTY" else 1)) if vix else FALLBACK_IV[symbol]
    model_in = price(float(trade["entry"]), strike, (end - entered).total_seconds() / year, iv / 100, call)
    model_out = price(float(trade["exit"]), strike, (end - exited).total_seconds() / year, iv / 100, call)
    bought = model_in + HALF_SPREAD[symbol]
    sold = max(model_out - HALF_SPREAD[symbol], MIN_PREMIUM)

    qty = int(trade["qty"])
    gross = (sold - bought) * qty
    cost = charges(bought * qty, sold * qty)
    return {
        "Option": f"{symbol} {strike} {'CE' if call else 'PE'} {expires:%d %b}",
        "IV %": round(iv, 1),
        "IV from": "India VIX" if vix else "fallback",
        "Days to expiry": round((end - entered).total_seconds() / 86400, 1),
        "Bought": round(bought, 2),
        "Sold": round(sold, 2),
        "Option gross": round(gross, 2),
        "Option charges": round(cost, 2),
        "Option net": round(gross - cost, 2),
    }
