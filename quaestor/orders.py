"""Order construction for quaestor: OCC symbol codec, Alpaca order payloads,
and marketable-limit pricing.

Pure functions — no network, no I/O. broker.py submits what this module builds.

Alpaca facts encoded here (verified 2026-08-28, do not re-derive):
- Option symbols are OCC format: ROOT (1-6 alnum chars) + YYMMDD + C/P + strike*1000
  zero-padded to 8 digits, e.g. SPY260904C00650000.
- Options TIF is always "day"; qty is whole contracts (no notional, no extended hours).
- Multi-leg ("mleg") orders: order_class="mleg", max 4 legs, leg symbols unique,
  ratio_qty values coprime across legs (GCD == 1), every short (sell_to_open) leg must
  be covered by a long leg within the SAME order (defined risk), limit/market only.
- mleg limit_price is the signed NET per strategy unit: positive = debit paid,
  negative = credit received. The sign is part of the contract — never flip it.
- Single-leg option orders carry NO order_class field (plain {symbol, qty, side, ...}).
- Numeric fields travel as strings in the JSON payload: qty = str(int),
  limit_price formatted to exactly 2 decimals (sign preserved), ratio_qty = str(int).
- client_order_id is the idempotency key (models.new_client_order_id) — unique per
  attempt; retries look orders up by it instead of resubmitting blind.
- Paper fills are marketable-at-NBBO-touch: a limit resting inside the spread does NOT
  fill until the quote crosses it, and the free "indicative" options feed adds small
  random noise. Hence marketable limits: single buy = ask + buffer, single sell =
  bid - buffer, mleg net = sum of signed touches + mleg buffer (for a credit that moves
  the demanded credit toward zero — less credit demanded, more fillable).
"""
from __future__ import annotations

import math
import re
from datetime import date, datetime
from decimal import Decimal, ROUND_HALF_UP
from typing import Any

from quaestor.models import Leg, PositionIntent, Side, TradeIntent, new_client_order_id

__all__ = ["build_order_payload", "marketable_limit", "occ_parse", "occ_build"]

_CENT = Decimal("0.01")
_ROOT_RE = re.compile(r"^[A-Z][A-Z0-9]{0,5}$", re.ASCII)
_TAIL_RE = re.compile(r"^(\d{6})([CP])(\d{8})$", re.ASCII)
_TYPE_MAP = {"C": "C", "CALL": "C", "P": "P", "PUT": "P"}


# --------------------------------------------------------------------------- OCC codec

def occ_parse(symbol: str) -> dict[str, Any]:
    """Parse an OCC option symbol into {root, expiry: date, type: "C"|"P", strike: float}.

    The trailing 15 characters are fixed-width (YYMMDD + C/P + 8 strike digits);
    everything before them is the root. Raises ValueError on any malformed input.
    """
    if not isinstance(symbol, str):
        raise ValueError(f"OCC symbol must be a string, got {type(symbol).__name__}")
    s = symbol.strip().upper()
    if not 16 <= len(s) <= 21:
        raise ValueError(
            f"invalid OCC symbol {symbol!r}: length {len(s)} outside 16..21 "
            "(root 1-6 chars + 15-char tail)"
        )
    root, tail = s[:-15], s[-15:]
    if not _ROOT_RE.match(root):
        raise ValueError(f"invalid OCC symbol {symbol!r}: bad root {root!r}")
    m = _TAIL_RE.match(tail)
    if not m:
        raise ValueError(
            f"invalid OCC symbol {symbol!r}: tail {tail!r} is not YYMMDD + C/P + 8 strike digits"
        )
    yymmdd, cp, strike_digits = m.group(1), m.group(2), m.group(3)
    try:
        expiry = datetime.strptime(yymmdd, "%y%m%d").date()
    except ValueError as exc:
        raise ValueError(f"invalid OCC symbol {symbol!r}: bad expiry date {yymmdd!r}") from exc
    strike_millis = int(strike_digits)
    if strike_millis <= 0:
        raise ValueError(f"invalid OCC symbol {symbol!r}: strike must be positive")
    return {"root": root, "expiry": expiry, "type": cp, "strike": strike_millis / 1000.0}


def occ_build(root: str, expiry: date, type_: str, strike: float) -> str:
    """Build an OCC option symbol from parts. Inverse of occ_parse.

    type_ accepts "C"/"P" (also "call"/"put", any case). Strike is encoded as
    strike*1000, rounded half-up, zero-padded to 8 digits.
    """
    r = str(root).strip().upper()
    if not _ROOT_RE.match(r):
        raise ValueError(f"invalid OCC root {root!r}: need 1-6 alphanumeric chars starting with a letter")
    if not isinstance(expiry, date):
        raise ValueError(f"expiry must be a datetime.date, got {type(expiry).__name__}")
    t = _TYPE_MAP.get(str(type_).strip().upper())
    if t is None:
        raise ValueError(f"invalid option type {type_!r}: expected 'C' or 'P'")
    strike_millis = int((Decimal(str(strike)) * 1000).to_integral_value(rounding=ROUND_HALF_UP))
    if strike_millis <= 0:
        raise ValueError(f"invalid strike {strike!r}: must be positive")
    if strike_millis > 99_999_999:
        raise ValueError(f"invalid strike {strike!r}: exceeds 8-digit OCC field")
    return f"{r}{expiry.strftime('%y%m%d')}{t}{strike_millis:08d}"


# ------------------------------------------------------------------ price serialization

def _to_cents(x: Decimal) -> Decimal:
    """Round to cents, half-up on the magnitude (ties go away from zero), sign preserved."""
    return x.quantize(_CENT, rounding=ROUND_HALF_UP)


def _fmt_price(x: float) -> str:
    """Serialize a limit price the way Alpaca expects: string, exactly 2 decimals, signed."""
    return f"{_to_cents(Decimal(str(x))):.2f}"


def _whole_qty(qty: Any) -> int:
    """Validate qty is a whole number >= 1 and return it as int."""
    if isinstance(qty, bool) or not isinstance(qty, (int, float)):
        raise ValueError(f"qty must be a whole number of contracts >= 1, got {qty!r}")
    if isinstance(qty, float):
        if not qty.is_integer():
            raise ValueError(f"qty must be a whole number of contracts, got {qty!r}")
        qty = int(qty)
    if qty < 1:
        raise ValueError(f"qty must be >= 1, got {qty}")
    return qty


# ----------------------------------------------------------------------- order payloads

def build_order_payload(intent: TradeIntent, attempt: int, zk_prefix: str = "") -> dict[str, Any]:
    """Build the POST /v2/orders JSON payload for an intent.

    single-leg: {symbol, qty, side, type: "limit", limit_price, time_in_force: "day",
                 position_intent, client_order_id} — NO order_class.
    mleg:       {order_class: "mleg", qty, type: "limit", limit_price (signed net),
                 time_in_force: "day", legs: [...], client_order_id}.

    Validates early and raises ValueError with a precise message on:
    non-whole/non-positive qty, malformed leg symbols or ratio_qtys, more than 4 legs,
    duplicate leg symbols, non-coprime ratio_qtys, uncovered sell_to_open legs, and
    limit prices with an impossible sign / that round to $0.00.
    """
    if isinstance(attempt, bool) or not isinstance(attempt, int) or attempt < 0:
        raise ValueError(f"attempt must be an int >= 0, got {attempt!r}")
    legs = intent.legs
    if not legs:
        raise ValueError(f"intent {intent.intent_id} has no legs")
    qty = _whole_qty(intent.qty)

    parsed: list[dict[str, Any]] = []
    for leg in legs:
        info = occ_parse(leg.symbol)  # raises ValueError on malformed symbols
        rq = leg.ratio_qty
        if isinstance(rq, bool) or not isinstance(rq, int) or rq < 1:
            raise ValueError(f"leg {leg.symbol}: ratio_qty must be an int >= 1, got {rq!r}")
        parsed.append(info)

    client_order_id = new_client_order_id(intent.intent_id, attempt, zk_prefix)
    limit = float(intent.limit_price)

    if len(legs) == 1:
        leg = legs[0]
        if limit <= 0:
            raise ValueError(
                f"single-leg limit_price must be > 0, got {limit} "
                "(signed net prices apply to mleg orders only)"
            )
        price = _fmt_price(limit)
        if price == "0.00":
            raise ValueError(f"limit_price {limit} rounds to $0.00")
        return {
            "symbol": leg.symbol,
            "qty": str(qty * leg.ratio_qty),
            "side": leg.side.value,
            "type": "limit",
            "limit_price": price,
            "time_in_force": "day",
            "position_intent": leg.position_intent.value,
            "client_order_id": client_order_id,
        }

    # ---- mleg constraints (Alpaca rejects violations with 422; we refuse earlier) ----
    if len(legs) > 4:
        raise ValueError(f"mleg orders support at most 4 legs, got {len(legs)}")
    symbols = [leg.symbol for leg in legs]
    if len(set(symbols)) != len(symbols):
        dupes = sorted({s for s in symbols if symbols.count(s) > 1})
        raise ValueError(f"mleg leg symbols must be unique, duplicated: {dupes}")
    ratios = [leg.ratio_qty for leg in legs]
    gcd = math.gcd(*ratios)
    if gcd != 1:
        raise ValueError(
            f"mleg ratio_qtys {ratios} must be coprime (gcd == 1), got gcd={gcd}; "
            "reduce the ratio to lowest terms"
        )
    for leg, info in zip(legs, parsed):
        if leg.position_intent is not PositionIntent.SELL_TO_OPEN:
            continue
        covered = any(
            other is not leg
            and other.side is Side.BUY
            and other_info["root"] == info["root"]
            and other_info["type"] == info["type"]
            and other_info["expiry"] == info["expiry"]
            and other.ratio_qty >= leg.ratio_qty
            for other, other_info in zip(legs, parsed)
        )
        if not covered:
            raise ValueError(
                f"uncovered short leg {leg.symbol}: every sell_to_open leg needs a buy leg "
                f"of the same type and expiry with ratio_qty >= {leg.ratio_qty} in the same "
                "order (defined-risk vertical shape)"
            )
    if limit == 0:
        raise ValueError("mleg limit_price must be nonzero: positive = debit, negative = credit")
    price = _fmt_price(limit)
    if price in ("0.00", "-0.00"):
        raise ValueError(f"limit_price {limit} rounds to $0.00")
    return {
        "order_class": "mleg",
        "qty": str(qty),
        "type": "limit",
        "limit_price": price,
        "time_in_force": "day",
        "legs": [leg.to_alpaca() for leg in legs],
        "client_order_id": client_order_id,
    }


# -------------------------------------------------------------------- marketable limits

def _touch(chain: dict[str, dict], leg: Leg) -> Decimal:
    """NBBO touch a marketable order must reach: ask when buying, bid when selling.

    Raises ValueError when the needed quote is missing, None, or non-positive
    (a zero bid/ask means there is no marketable touch on that side).
    """
    quote = chain.get(leg.symbol)
    if not quote:
        raise ValueError(f"no quote for {leg.symbol} in chain")
    side_key = "ask" if leg.side is Side.BUY else "bid"
    value = quote.get(side_key)
    if value is None:
        raise ValueError(f"missing {side_key} quote for {leg.symbol}")
    touch = Decimal(str(value))
    if touch <= 0:
        raise ValueError(f"non-positive {side_key} ({value}) for {leg.symbol}: no marketable touch")
    return touch


def marketable_limit(intent: TradeIntent, chain: dict[str, dict], policy: dict) -> float:
    """Compute a marketable limit price from current chain quotes + policy buffers.

    single buy  -> ask + marketable_buffer_usd
    single sell -> bid - marketable_buffer_usd
    mleg net    -> sum(sign * touch * ratio_qty) + mleg_buffer_usd, where sign = +1 buy /
                   -1 sell and touch = ask if buying else bid. For a debit that pays the
                   buffer over the touch net; for a credit (net < 0) it moves the demanded
                   credit toward zero — less credit demanded, more fillable.

    Rounds half-up to cents, preserves the sign of intent.limit_price (a credit intent
    never becomes a debit price and vice versa), and raises ValueError when any needed
    quote is missing.
    """
    if not intent.legs:
        raise ValueError(f"intent {intent.intent_id} has no legs")
    execution = (policy or {}).get("execution", {}) or {}

    if not intent.is_multileg:
        leg = intent.legs[0]
        buffer = Decimal(str(execution.get("marketable_buffer_usd", 0.02)))
        touch = _touch(chain, leg)
        raw = touch + buffer if leg.side is Side.BUY else touch - buffer
        price = _to_cents(raw)
        if price < _CENT:  # a sell into a thin bid must still be a positive limit
            price = _CENT
        return float(price)

    buffer = Decimal(str(execution.get("mleg_buffer_usd", 0.05)))
    net = Decimal("0")
    for leg in intent.legs:
        sign = Decimal(1) if leg.side is Side.BUY else Decimal(-1)
        net += sign * _touch(chain, leg) * Decimal(leg.ratio_qty)
    price = _to_cents(net + buffer)
    if intent.is_credit:
        if price > -_CENT:  # never flip a credit into a debit
            price = -_CENT
    else:
        if price < _CENT:  # never flip a debit into a credit
            price = _CENT
    return float(price)
