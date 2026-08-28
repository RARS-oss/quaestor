"""Market data access for quaestor — thin, throttled wrappers over alpaca-py historical clients.

Role: give the rest of the agent normalized, None-safe dicts for stock snapshots/bars,
option chains/snapshots (with greeks), and news — never raw SDK models.

Alpaca facts encoded here (verified 2026-08-28):
- Stock data feed is pinned to IEX (DataFeed.IEX) — the free feed; SIP is not available on
  the free tier for recent data.
- Options data feed is pinned to OptionsFeed.INDICATIVE — the free options feed is a
  randomized derivative of OPRA, so quotes carry small noise (execution buffers absorb it).
- Greeks and implied volatility exist ONLY in REST snapshots (Black-Scholes based) and are
  Optional — illiquid contracts come back with greeks/iv = None. We pass None through;
  callers must guard. mid is None whenever bid or ask is missing.
- Open interest is NOT in the data-host option snapshot; it lives on the trading-host
  contracts endpoint (see quaestor/universe.py). The normalized snapshot carries "oi": None
  as a stable key; universe merges real OI from contracts.
- Rate limit is 200 req/min on the data API. A naive monotonic-clock throttle with a
  minimum 0.35 s gap between REST calls keeps us safely under it.
- Option snapshot requests accept at most 100 symbols per call — we chunk.
- The news endpoint takes `symbols` as a single comma-joined STRING (not a list) and its
  page limit is 50 items.
"""
from __future__ import annotations

import re
import time
from datetime import date, datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any

from alpaca.data.enums import DataFeed, OptionsFeed
from alpaca.data.historical.news import NewsClient
from alpaca.data.historical.option import OptionHistoricalDataClient
from alpaca.data.historical.stock import StockHistoricalDataClient
from alpaca.data.requests import (
    NewsRequest,
    OptionChainRequest,
    OptionSnapshotRequest,
    StockBarsRequest,
    StockSnapshotRequest,
)
from alpaca.data.timeframe import TimeFrame, TimeFrameUnit
from alpaca.trading.enums import ContractType

if TYPE_CHECKING:  # avoid a hard import-order dependency on quaestor.config
    from quaestor.config import Settings

# Minimum spacing between REST calls (200 req/min limit -> 0.3 s floor; 0.35 s = margin).
MIN_CALL_INTERVAL_S: float = 0.35

# Option snapshot endpoint hard cap on symbols per request.
SNAPSHOT_CHUNK: int = 100

# News endpoint page limit.
NEWS_MAX_LIMIT: int = 50

_TIMEFRAME_UNITS: dict[str, TimeFrameUnit] = {
    "min": TimeFrameUnit.Minute,
    "minute": TimeFrameUnit.Minute,
    "hour": TimeFrameUnit.Hour,
    "hr": TimeFrameUnit.Hour,
    "day": TimeFrameUnit.Day,
    "week": TimeFrameUnit.Week,
    "month": TimeFrameUnit.Month,
}


def _parse_timeframe(spec: str) -> TimeFrame:
    """Parse a timeframe string like '5Min', '1Hour', '1Day' into an alpaca TimeFrame."""
    m = re.fullmatch(r"\s*(\d+)\s*([A-Za-z]+)\s*", spec)
    if not m:
        raise ValueError(f"unparseable timeframe: {spec!r}")
    amount = int(m.group(1))
    unit_key = m.group(2).lower().rstrip("s")
    unit = _TIMEFRAME_UNITS.get(unit_key)
    if unit is None:
        raise ValueError(f"unknown timeframe unit in {spec!r}")
    return TimeFrame(amount, unit)


def _to_float(value: Any) -> float | None:
    """None-safe float conversion; returns None for None/unparseable values."""
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _iso(ts: Any) -> str | None:
    """Timestamp -> ISO-8601 string (or None)."""
    if ts is None:
        return None
    if isinstance(ts, datetime):
        return ts.isoformat()
    return str(ts)


def _get(obj: Any, name: str) -> Any:
    """Read an attribute from an SDK model OR a key from a raw dict."""
    if isinstance(obj, dict):
        return obj.get(name)
    return getattr(obj, name, None)


def _normalize_bar(bar: Any) -> dict[str, Any]:
    return {
        "t": _iso(_get(bar, "timestamp")),
        "o": _to_float(_get(bar, "open")),
        "h": _to_float(_get(bar, "high")),
        "l": _to_float(_get(bar, "low")),
        "c": _to_float(_get(bar, "close")),
        "v": _to_float(_get(bar, "volume")),
        "vw": _to_float(_get(bar, "vwap")),
        "n": _to_float(_get(bar, "trade_count")),
    }


def _normalize_option_snapshot(snap: Any) -> dict[str, Any]:
    """OptionsSnapshot -> {bid, ask, mid, last, iv, delta, gamma, theta, vega, oi, t}.

    Every field is Optional on the wire; mid is None whenever bid or ask is missing.
    oi is always None here (open interest comes from the trading-host contracts endpoint).
    """
    bid: float | None = None
    ask: float | None = None
    last: float | None = None
    t: Any = None

    quote = _get(snap, "latest_quote")
    if quote is not None:
        bid = _to_float(_get(quote, "bid_price"))
        ask = _to_float(_get(quote, "ask_price"))
        t = _get(quote, "timestamp")

    trade = _get(snap, "latest_trade")
    if trade is not None:
        last = _to_float(_get(trade, "price"))
        if t is None:
            t = _get(trade, "timestamp")

    mid: float | None = None
    if bid is not None and ask is not None:
        mid = round((bid + ask) / 2.0, 4)

    delta = gamma = theta = vega = None
    greeks = _get(snap, "greeks")
    if greeks is not None:
        delta = _to_float(_get(greeks, "delta"))
        gamma = _to_float(_get(greeks, "gamma"))
        theta = _to_float(_get(greeks, "theta"))
        vega = _to_float(_get(greeks, "vega"))

    return {
        "bid": bid,
        "ask": ask,
        "mid": mid,
        "last": last,
        "iv": _to_float(_get(snap, "implied_volatility")),
        "delta": delta,
        "gamma": gamma,
        "theta": theta,
        "vega": vega,
        "oi": None,
        "t": _iso(t),
    }


def _normalize_stock_snapshot(snap: Any) -> dict[str, Any]:
    """Stock Snapshot -> flat dict with best-effort 'price' plus bars."""
    bid: float | None = None
    ask: float | None = None
    last: float | None = None
    t: Any = None

    quote = _get(snap, "latest_quote")
    if quote is not None:
        bid = _to_float(_get(quote, "bid_price"))
        ask = _to_float(_get(quote, "ask_price"))
        t = _get(quote, "timestamp")

    trade = _get(snap, "latest_trade")
    if trade is not None:
        last = _to_float(_get(trade, "price"))
        if t is None:
            t = _get(trade, "timestamp")

    mid: float | None = None
    if bid is not None and ask is not None:
        mid = round((bid + ask) / 2.0, 4)

    minute_bar = _get(snap, "minute_bar")
    daily_bar = _get(snap, "daily_bar")
    prev_daily_bar = _get(snap, "previous_daily_bar")

    minute = _normalize_bar(minute_bar) if minute_bar is not None else None
    daily = _normalize_bar(daily_bar) if daily_bar is not None else None
    prev_daily = _normalize_bar(prev_daily_bar) if prev_daily_bar is not None else None

    # Best-effort reference price: last trade > quote mid > minute close > daily close.
    price = last
    if price is None:
        price = mid
    if price is None and minute is not None:
        price = minute.get("c")
    if price is None and daily is not None:
        price = daily.get("c")

    return {
        "price": price,
        "bid": bid,
        "ask": ask,
        "mid": mid,
        "last": last,
        "minute_bar": minute,
        "daily_bar": daily,
        "prev_daily_bar": prev_daily,
        "t": _iso(t),
    }


def _normalize_news_item(item: Any) -> dict[str, Any]:
    symbols = _get(item, "symbols") or []
    return {
        "id": _get(item, "id"),
        "headline": _get(item, "headline") or "",
        "summary": _get(item, "summary") or "",
        "author": _get(item, "author") or "",
        "source": _get(item, "source") or "",
        "url": _get(item, "url") or "",
        "symbols": list(symbols),
        "created_at": _iso(_get(item, "created_at")),
        "updated_at": _iso(_get(item, "updated_at")),
    }


def _contract_type(type_: str) -> ContractType:
    """Normalize 'call'/'put'/'C'/'P' (any case) to a ContractType."""
    first = type_.strip()[:1].lower()
    if first == "c":
        return ContractType.CALL
    if first == "p":
        return ContractType.PUT
    raise ValueError(f"unknown option type: {type_!r} (expected call/put)")


class MarketData:
    """Throttled, feed-pinned access to Alpaca market data (stocks, options, news)."""

    def __init__(self, settings: "Settings") -> None:
        self._stocks = StockHistoricalDataClient(
            api_key=settings.api_key, secret_key=settings.api_secret
        )
        self._options = OptionHistoricalDataClient(
            api_key=settings.api_key, secret_key=settings.api_secret
        )
        self._news = NewsClient(api_key=settings.api_key, secret_key=settings.api_secret)
        self._last_call: float = time.monotonic() - MIN_CALL_INTERVAL_S

    def _throttle(self) -> None:
        """Enforce a minimum gap between REST calls (monotonic clock, sleep the deficit)."""
        wait = self._last_call + MIN_CALL_INTERVAL_S - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        self._last_call = time.monotonic()

    # ------------------------------------------------------------------ stocks

    def stock_snapshot(self, symbols: list[str]) -> dict[str, dict]:
        """Latest snapshot per symbol via the IEX feed.

        Returns {symbol: {price, bid, ask, mid, last, minute_bar, daily_bar,
        prev_daily_bar, t}}. Symbols with no data are omitted — use .get().
        """
        if not symbols:
            return {}
        self._throttle()
        req = StockSnapshotRequest(symbol_or_symbols=list(symbols), feed=DataFeed.IEX)
        resp = self._stocks.get_stock_snapshot(req)
        out: dict[str, dict] = {}
        items = resp.items() if isinstance(resp, dict) else []
        for symbol, snap in items:
            if snap is None:
                continue
            out[str(symbol)] = _normalize_stock_snapshot(snap)
        return out

    def stock_bars(
        self,
        symbols: list[str],
        timeframe: str = "5Min",
        lookback_minutes: int = 390,
    ) -> dict[str, list[dict]]:
        """Recent bars per symbol via the IEX feed (start = now - lookback_minutes, UTC).

        Returns {symbol: [{t, o, h, l, c, v, vw, n}, ...]} — every requested symbol is
        present, with [] when Alpaca returned nothing for it.
        """
        if not symbols:
            return {}
        self._throttle()
        start = datetime.now(timezone.utc) - timedelta(minutes=lookback_minutes)
        req = StockBarsRequest(
            symbol_or_symbols=list(symbols),
            timeframe=_parse_timeframe(timeframe),
            start=start,
            feed=DataFeed.IEX,
        )
        resp = self._stocks.get_stock_bars(req)
        data = getattr(resp, "data", None)
        if not isinstance(data, dict):
            data = resp if isinstance(resp, dict) else {}
        out: dict[str, list[dict]] = {symbol: [] for symbol in symbols}
        for symbol, bars in data.items():
            out[str(symbol)] = [_normalize_bar(b) for b in (bars or [])]
        return out

    # ----------------------------------------------------------------- options

    def option_chain(
        self,
        underlying: str,
        *,
        expiry_lte: str,
        expiry_gte: str,
        strike_gte: float | None = None,
        strike_lte: float | None = None,
        type_: str | None = None,
    ) -> dict[str, dict]:
        """Option chain snapshots for an underlying via the INDICATIVE feed.

        expiry bounds are 'YYYY-MM-DD' strings; strike bounds are FLOATS here (the data
        host takes floats — the trading-host contracts endpoint takes strings instead).
        Returns {occ_symbol: normalized snapshot dict}; greeks/iv may be None.
        """
        self._throttle()
        req = OptionChainRequest(
            underlying_symbol=underlying,
            feed=OptionsFeed.INDICATIVE,
            expiration_date_gte=date.fromisoformat(expiry_gte),
            expiration_date_lte=date.fromisoformat(expiry_lte),
            strike_price_gte=strike_gte,
            strike_price_lte=strike_lte,
            type=_contract_type(type_) if type_ else None,
        )
        resp = self._options.get_option_chain(req)
        out: dict[str, dict] = {}
        items = resp.items() if isinstance(resp, dict) else []
        for symbol, snap in items:
            if snap is None:
                continue
            out[str(symbol)] = _normalize_option_snapshot(snap)
        return out

    def option_snapshots(self, occ_symbols: list[str]) -> dict[str, dict]:
        """Snapshots for explicit OCC symbols, INDICATIVE feed, chunked <=100 per call."""
        out: dict[str, dict] = {}
        symbols = [s for s in occ_symbols if s]
        for i in range(0, len(symbols), SNAPSHOT_CHUNK):
            chunk = symbols[i : i + SNAPSHOT_CHUNK]
            self._throttle()
            req = OptionSnapshotRequest(
                symbol_or_symbols=chunk, feed=OptionsFeed.INDICATIVE
            )
            resp = self._options.get_option_snapshot(req)
            items = resp.items() if isinstance(resp, dict) else []
            for symbol, snap in items:
                if snap is None:
                    continue
                out[str(symbol)] = _normalize_option_snapshot(snap)
        return out

    # -------------------------------------------------------------------- news

    def news(self, symbols: list[str], limit: int = 20) -> list[dict]:
        """Latest news; `symbols` is comma-joined into a single STRING (Alpaca contract).

        Returns a list of {id, headline, summary, author, source, url, symbols,
        created_at, updated_at}. Empty symbol list -> market-wide news.
        """
        self._throttle()
        req = NewsRequest(
            symbols=",".join(symbols) if symbols else None,
            limit=min(max(int(limit), 1), NEWS_MAX_LIMIT),
        )
        resp = self._news.get_news(req)
        items: Any = []
        data = getattr(resp, "data", None)
        if isinstance(data, dict) and "news" in data:
            items = data.get("news") or []
        elif isinstance(resp, dict):
            items = resp.get("news") or []
        else:
            maybe = getattr(resp, "news", None)
            if maybe is not None:
                items = maybe
        return [_normalize_news_item(item) for item in items]
