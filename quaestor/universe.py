"""Tradable-contract discovery and selection for quaestor.

Role: turn "SPY, this week, ~3% around spot" into concrete OCC contracts, filter them
against the risk policy's liquidity gates, and pick expiries/strikes for the playbooks.

Alpaca facts encoded here (verified 2026-08-28):
- Option contracts are discovered via GET /v2/options/contracts on the TRADING host
  (TradingClient.get_option_contracts) — NOT the data host.
- On the trading host, strike_price_gte / strike_price_lte are STRINGS (the data host
  takes floats — see quaestor/data.py). We format them explicitly.
- The endpoint's default expiration window is "this week" — explicit
  expiration_date_gte/lte bounds are ALWAYS passed; callers must supply both.
- Open interest lives ONLY on these trading-host contract records (the data-host
  snapshot has no OI), so liquidity filtering joins contracts (OI) with the chain
  snapshot (quotes/greeks) by OCC symbol.
- Results are paginated via next_page_token; we loop until exhausted, throttled to
  stay under the 200 req/min trading-API limit.
- Greeks/IV in chain snapshots are Optional (may be None for illiquid contracts):
  strike picking by delta skips None-greek rows instead of crashing.
"""
from __future__ import annotations

import time
from datetime import date, datetime, timedelta
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

from alpaca.trading.client import TradingClient
from alpaca.trading.enums import AssetStatus, ContractType
from alpaca.trading.requests import GetOptionContractsRequest

from quaestor.data import MarketData

if TYPE_CHECKING:  # avoid a hard import-order dependency on quaestor.config
    from quaestor.config import Settings

# Core underlyings; catalyst plays may extend this (e.g. "AVGO" around earnings).
UNDERLYINGS: list[str] = ["SPY", "QQQ"]

_ET = ZoneInfo("America/New_York")

# Trading API is also limited to 200 req/min — same 0.35 s spacing as the data side.
_MIN_CALL_INTERVAL_S: float = 0.35
_PAGE_LIMIT: int = 1000

_last_call: float = time.monotonic() - _MIN_CALL_INTERVAL_S


def _throttle() -> None:
    """Minimum gap between trading-API REST calls (monotonic clock)."""
    global _last_call
    wait = _last_call + _MIN_CALL_INTERVAL_S - time.monotonic()
    if wait > 0:
        time.sleep(wait)
    _last_call = time.monotonic()


def _to_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _to_int(value: Any) -> int | None:
    f = _to_float(value)
    return None if f is None else int(f)


def _contract_type(type_: str) -> ContractType:
    first = type_.strip()[:1].lower()
    if first == "c":
        return ContractType.CALL
    if first == "p":
        return ContractType.PUT
    raise ValueError(f"unknown option type: {type_!r} (expected call/put)")


def _normalize_contract(c: Any) -> dict[str, Any]:
    """OptionContract model (or raw dict) -> plain dict the rest of the agent consumes."""

    def get(name: str) -> Any:
        if isinstance(c, dict):
            return c.get(name)
        return getattr(c, name, None)

    exp = get("expiration_date")
    if isinstance(exp, date):
        expiry = exp.isoformat()
    else:
        expiry = str(exp)[:10] if exp else ""

    ctype = get("type")
    type_str = ctype.value if hasattr(ctype, "value") else (str(ctype).lower() if ctype else "")

    style = get("style")
    style_str = style.value if hasattr(style, "value") else (str(style).lower() if style else "")

    return {
        "symbol": str(get("symbol") or ""),
        "underlying": str(get("underlying_symbol") or ""),
        "expiry": expiry,
        "type": type_str,
        "strike": _to_float(get("strike_price")),
        "open_interest": _to_int(get("open_interest")),
        "close_price": _to_float(get("close_price")),
        "tradable": bool(get("tradable")),
        "style": style_str,
        "size": _to_int(get("size")) or 100,
    }


def discover_contracts(
    settings: "Settings",
    underlying: str,
    *,
    expiry_gte: str,
    expiry_lte: str,
    strike_band_pct: float = 0.03,
    type_: str | None = None,
) -> list[dict]:
    """List active option contracts for `underlying` inside explicit expiry bounds.

    Uses TradingClient.get_option_contracts (trading host). Strike bounds are computed
    as spot * (1 +/- strike_band_pct) from a fresh IEX stock snapshot and sent as
    STRINGS (trading-host convention). Explicit expiry_gte/expiry_lte ('YYYY-MM-DD')
    are mandatory because the endpoint defaults to "this week" otherwise.
    If no spot price is available, the strike filter is omitted (full chain returned).
    Paginates via next_page_token until exhausted.
    """
    strike_gte_s: str | None = None
    strike_lte_s: str | None = None
    if strike_band_pct is not None and strike_band_pct > 0:
        md = MarketData(settings)
        snap = md.stock_snapshot([underlying]).get(underlying) or {}
        spot = snap.get("price")
        if spot is not None and spot > 0:
            strike_gte_s = f"{spot * (1.0 - strike_band_pct):.2f}"
            strike_lte_s = f"{spot * (1.0 + strike_band_pct):.2f}"

    client = TradingClient(
        api_key=settings.api_key,
        secret_key=settings.api_secret,
        paper=settings.paper,
    )

    contracts: list[dict] = []
    page_token: str | None = None
    while True:
        _throttle()
        req = GetOptionContractsRequest(
            underlying_symbols=[underlying],
            status=AssetStatus.ACTIVE,
            expiration_date_gte=date.fromisoformat(expiry_gte),
            expiration_date_lte=date.fromisoformat(expiry_lte),
            strike_price_gte=strike_gte_s,
            strike_price_lte=strike_lte_s,
            type=_contract_type(type_) if type_ else None,
            limit=_PAGE_LIMIT,
            page_token=page_token,
        )
        resp = client.get_option_contracts(req)
        if isinstance(resp, dict):
            batch = resp.get("option_contracts") or []
            page_token = resp.get("next_page_token")
        else:
            batch = getattr(resp, "option_contracts", None) or []
            page_token = getattr(resp, "next_page_token", None)
        contracts.extend(_normalize_contract(c) for c in batch)
        if not page_token:
            break
    return contracts


def filter_tradable(
    contracts: list[dict], chain: dict[str, dict], policy: dict
) -> list[dict]:
    """Apply the policy liquidity gates; join trading-host OI with data-host quotes.

    Keeps a contract only when:
    - it is tradable and has a chain snapshot with BOTH bid and ask (mid computable) —
      missing-quote rows are dropped;
    - open_interest >= per_trade.min_open_interest (trading-host OI; None counts as 0);
    - (ask - bid) / mid <= per_trade.max_spread_quality;
    - mid >= per_trade.min_leg_price (no sub-nickel junk).

    Returns copies of the contract dicts enriched with the live quote:
    bid/ask/mid/spread_quality, and "oi" mirroring open_interest.
    """
    per_trade = policy.get("per_trade", {})
    min_oi = float(per_trade.get("min_open_interest", 0))
    max_spread = float(per_trade.get("max_spread_quality", 1.0))
    min_price = float(per_trade.get("min_leg_price", 0.0))

    out: list[dict] = []
    for contract in contracts:
        if not contract.get("tradable", True):
            continue
        snap = chain.get(contract.get("symbol", ""))
        if not snap:
            continue
        bid = snap.get("bid")
        ask = snap.get("ask")
        mid = snap.get("mid")
        if bid is None or ask is None or mid is None or mid <= 0:
            continue
        oi = contract.get("open_interest")
        if float(oi if oi is not None else 0) < min_oi:
            continue
        spread_quality = (ask - bid) / mid
        if spread_quality > max_spread:
            continue
        if mid < min_price:
            continue
        enriched = dict(contract)
        enriched.update(
            {
                "bid": bid,
                "ask": ask,
                "mid": mid,
                "spread_quality": round(spread_quality, 6),
                "oi": contract.get("open_interest"),
            }
        )
        out.append(enriched)
    return out


def nearest_expiry(contracts: list[dict], target_dte: int) -> str:
    """Expiry ('YYYY-MM-DD') among `contracts` closest to today(ET) + target_dte days.

    Ties break toward the EARLIER expiry (less premium at risk in the contest window).
    Raises ValueError when no contract carries a parseable expiry.
    """
    today = datetime.now(_ET).date()
    target = today + timedelta(days=int(target_dte))

    expiries: set[date] = set()
    for contract in contracts:
        raw = contract.get("expiry") or ""
        try:
            expiries.add(date.fromisoformat(str(raw)[:10]))
        except ValueError:
            continue
    if not expiries:
        raise ValueError("nearest_expiry: no contracts with a valid expiry date")

    best = min(expiries, key=lambda d: (abs((d - target).days), d))
    return best.isoformat()


def _occ_type(occ_symbol: str) -> str | None:
    """'C' or 'P' from an OCC symbol (ROOT + YYMMDD + C/P + 8-digit strike), else None."""
    if len(occ_symbol) < 10:
        return None
    ch = occ_symbol[-9]
    return ch if ch in ("C", "P") else None


def pick_strike(chain: dict[str, dict], target_delta: float, type_: str) -> str | None:
    """OCC symbol of the `type_` contract whose |delta| is closest to |target_delta|.

    Greeks in the indicative-feed snapshots are Optional — rows with delta None are
    skipped, never crashed on. Returns None when no candidate has a usable delta.
    """
    want = _contract_type(type_).value[:1].upper()  # 'C' | 'P'
    target = abs(float(target_delta))

    best_symbol: str | None = None
    best_err = float("inf")
    for symbol, snap in chain.items():
        if _occ_type(symbol) != want:
            continue
        delta = snap.get("delta")
        if delta is None:
            continue
        try:
            err = abs(abs(float(delta)) - target)
        except (TypeError, ValueError):
            continue
        if err < best_err:
            best_err = err
            best_symbol = symbol
    return best_symbol
