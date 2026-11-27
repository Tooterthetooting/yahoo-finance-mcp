"""Extra tools for hosting on cloud servers where Yahoo throttles some endpoints.

On Render, Yahoo answers "Too Many Requests" for the quoteSummary endpoint that
powers Ticker.info (get_stock_info) and for news. The price chart endpoint and the
fundamentals-timeseries endpoint (financial statements, valuation measures) keep
working. This module:

  * adds get_valuation_snapshot: price stats from the chart endpoint plus
    valuation ratios (P/E, forward P/E, PEG, P/S, P/B, EV/EBITDA ...) from the
    fundamentals-timeseries endpoint, cached for 15 minutes;
  * replaces get_stock_info so that, when Yahoo refuses Ticker.info, it returns
    the snapshot instead of an error.

It does not change server.py; http_server.py imports it.
"""

import asyncio
import json
import math
import time

import pandas as pd
import yfinance as yf

from server import normalize_ticker, yfinance_server

_CACHE_SECONDS = 15 * 60
_cache: dict[str, tuple[float, dict]] = {}


def _num(value):
    """Turn numpy/pandas numbers into plain floats; drop NaN."""
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    return None if math.isnan(f) or math.isinf(f) else round(f, 4)


def _price_section(company: yf.Ticker) -> dict:
    hist = company.history(period="1y", interval="1d", auto_adjust=False)
    if hist is None or hist.empty:
        raise ValueError("no price history returned")
    close = hist["Close"].dropna()
    last_date = close.index[-1]
    section = {
        "lastClose": _num(close.iloc[-1]),
        "lastCloseDate": last_date.strftime("%Y-%m-%d"),
        "previousClose": _num(close.iloc[-2]) if len(close) > 1 else None,
        "fiftyTwoWeekHigh": _num(hist["High"].max()),
        "fiftyTwoWeekLow": _num(hist["Low"].min()),
        "fiftyDayAverage": _num(close.tail(50).mean()),
        "twoHundredDayAverage": _num(close.tail(200).mean()) if len(close) >= 200 else None,
        "averageVolume3Month": _num(hist["Volume"].tail(63).mean()),
        "oneYearChangePercent": _num((close.iloc[-1] / close.iloc[0] - 1) * 100),
    }
    if section["previousClose"]:
        section["dayChangePercent"] = _num(
            (section["lastClose"] / section["previousClose"] - 1) * 100
        )
    if "Dividends" in hist.columns:
        ttm_div = hist["Dividends"].sum()
        section["dividendsLast12Months"] = _num(ttm_div)
        if ttm_div and section["lastClose"]:
            section["trailingDividendYieldPercent"] = _num(ttm_div / section["lastClose"] * 100)
    meta = getattr(company, "history_metadata", None) or {}
    for key in ("currency", "exchangeName", "longName", "instrumentType"):
        if meta.get(key):
            section[key] = meta[key]
    return section


def _valuation_section(company: yf.Ticker) -> dict:
    df = company.get_valuation_measures(freq="quarterly", periods=4)
    if df is None or df.empty:
        return {}
    out = {}
    for column in df.columns:
        values = {str(k): _num(v) for k, v in df[column].items()}
        out[str(column)] = {k: v for k, v in values.items() if v is not None}
    return out


def _build_snapshot(ticker: str) -> dict:
    symbol = normalize_ticker(ticker)
    cached = _cache.get(symbol)
    if cached and time.time() - cached[0] < _CACHE_SECONDS:
        return cached[1]
    company = yf.Ticker(symbol)
    snapshot = {"symbol": symbol, "source": "Yahoo Finance chart + fundamentals-timeseries"}
    try:
        snapshot["price"] = _price_section(company)
    except Exception as e:  # keep going: valuation may still work
        snapshot["priceError"] = str(e)
    try:
        snapshot["valuation"] = _valuation_section(company)
        if not snapshot["valuation"]:
            snapshot["valuationError"] = "Yahoo returned no valuation measures"
    except Exception as e:
        snapshot["valuationError"] = str(e)
    snapshot["notes"] = (
        "'Current' valuation ratios are Yahoo's latest trailing values; dated columns are "
        "quarter-end values. Prices are daily closes, not real-time quotes."
    )
    if "price" in snapshot or snapshot.get("valuation"):
        _cache[symbol] = (time.time(), snapshot)
    return snapshot


async def get_valuation_snapshot(ticker: str) -> str:
    snapshot = await asyncio.to_thread(_build_snapshot, ticker)
    return json.dumps(snapshot, default=str)


async def get_stock_info(ticker: str) -> str:
    def _info():
        return yf.Ticker(normalize_ticker(ticker)).info

    try:
        info = await asyncio.to_thread(_info)
        if info and "symbol" in info and "quoteType" in info:
            return json.dumps(info, default=str)
        reason = "Yahoo returned no stock info"
    except Exception as e:
        reason = str(e)
    snapshot = dict(await asyncio.to_thread(_build_snapshot, ticker))  # copy: don't alter cache
    snapshot["fallbackReason"] = (
        f"Full stock info unavailable ({reason}); returning price and valuation snapshot instead."
    )
    return json.dumps(snapshot, default=str)


def register() -> None:
    yfinance_server.add_tool(
        get_valuation_snapshot,
        name="get_valuation_snapshot",
        description="""Get a price and valuation snapshot for a stock from Yahoo Finance:
last close, day change, 52-week high/low, 50/200-day averages, average volume, trailing
dividend yield, and valuation measures (Market Cap, Enterprise Value, Trailing P/E,
Forward P/E, PEG, Price/Sales, Price/Book, EV/Revenue, EV/EBITDA) for now and the last
4 quarter-ends. Prefer this over get_stock_info for valuation questions.

Args:
    ticker: str
        The ticker symbol, e.g. "AAPL"
""",
    )
    yfinance_server.remove_tool("get_stock_info")
    yfinance_server.add_tool(
        get_stock_info,
        name="get_stock_info",
        description="""Get stock information for a given ticker symbol from yahoo finance (price,
company details, financial metrics, ownership, analyst coverage). If Yahoo refuses the full
info request, returns the price and valuation snapshot from get_valuation_snapshot instead.

Args:
    ticker: str
        The ticker symbol of the stock to get information for, e.g. "AAPL"
""",
    )
