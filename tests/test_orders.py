"""Unit tests for quaestor.orders — deterministic, fixture-driven, no network.

Covers: OCC round-trip (incl. SPY260904C00650000 and a 5-char root), single-leg
payload shape (no order_class), debit vertical payload, credit vertical with a
NEGATIVE limit_price string, coprime rejection (2:4), uncovered-short rejection,
marketable_limit math on synthetic chains (incl. the credit case), and missing
quotes raising ValueError.
"""
from __future__ import annotations

from datetime import date

import pytest

from quaestor.models import (
    Leg,
    PositionIntent,
    Side,
    Structure,
    TradeIntent,
    new_client_order_id,
)
from quaestor.orders import build_order_payload, marketable_limit, occ_build, occ_parse

# ------------------------------------------------------------------------------ fixtures

POLICY: dict = {"execution": {"marketable_buffer_usd": 0.02, "mleg_buffer_usd": 0.05}}

CALL_650 = "SPY260904C00650000"
CALL_655 = "SPY260904C00655000"
PUT_640 = "SPY260904P00640000"
GOOGL_PUT = "GOOGL261218P00185500"

CHAIN: dict[str, dict] = {
    CALL_650: {"bid": 1.00, "ask": 1.10, "mid": 1.05},
    CALL_655: {"bid": 0.40, "ask": 0.48, "mid": 0.44},
}


def _leg(
    symbol: str,
    side: Side,
    position_intent: PositionIntent,
    ratio: int = 1,
) -> Leg:
    return Leg(symbol=symbol, side=side, ratio_qty=ratio, position_intent=position_intent)


def _intent(
    structure: Structure,
    legs: list[Leg],
    qty: int = 1,
    limit: float = 1.0,
) -> TradeIntent:
    return TradeIntent(
        underlying="SPY",
        structure=structure,
        legs=legs,
        qty=qty,
        limit_price=limit,
        thesis="unit-test fixture",
        max_loss_usd=100.0,
    )


def _long_call(symbol: str = CALL_650, limit: float = 1.0) -> TradeIntent:
    return _intent(
        Structure.LONG_CALL,
        [_leg(symbol, Side.BUY, PositionIntent.BUY_TO_OPEN)],
        limit=limit,
    )


def _debit_vertical_legs() -> list[Leg]:
    return [
        _leg(CALL_650, Side.BUY, PositionIntent.BUY_TO_OPEN),
        _leg(CALL_655, Side.SELL, PositionIntent.SELL_TO_OPEN),
    ]


def _credit_vertical_legs() -> list[Leg]:
    return [
        _leg(CALL_650, Side.SELL, PositionIntent.SELL_TO_OPEN),
        _leg(CALL_655, Side.BUY, PositionIntent.BUY_TO_OPEN),
    ]


# ----------------------------------------------------------------------------- OCC codec


def test_occ_parse_spy() -> None:
    assert occ_parse(CALL_650) == {
        "root": "SPY",
        "expiry": date(2026, 9, 4),
        "type": "C",
        "strike": 650.0,
    }


def test_occ_build_spy() -> None:
    assert occ_build("SPY", date(2026, 9, 4), "C", 650.0) == CALL_650


def test_occ_roundtrip_five_char_root() -> None:
    parsed = occ_parse(GOOGL_PUT)
    assert parsed == {
        "root": "GOOGL",
        "expiry": date(2026, 12, 18),
        "type": "P",
        "strike": 185.5,
    }
    rebuilt = occ_build(parsed["root"], parsed["expiry"], parsed["type"], parsed["strike"])
    assert rebuilt == GOOGL_PUT


def test_occ_roundtrip_fractional_strike() -> None:
    symbol = occ_build("QQQ", date(2026, 9, 2), "P", 570.5)
    assert symbol == "QQQ260902P00570500"
    assert occ_parse(symbol)["strike"] == 570.5


def test_occ_parse_rejects_malformed() -> None:
    for bad in (
        "",
        "SPY",
        "SPY260904X00650000",       # bad type char
        "SPY26090C400650000",       # non-digit inside date field
        "SPY260904C0065000",        # 7 strike digits
        "TOOLONGROOT260904C00650000",  # root > 6 chars
        "SPY260931C00650000",       # impossible calendar date
        "SPY260904C00000000",       # zero strike
    ):
        with pytest.raises(ValueError):
            occ_parse(bad)


def test_occ_build_rejects_bad_type() -> None:
    with pytest.raises(ValueError):
        occ_build("SPY", date(2026, 9, 4), "X", 650.0)


# ----------------------------------------------------------------------- payload: single


def test_single_leg_payload_shape() -> None:
    intent = _intent(
        Structure.LONG_CALL,
        [_leg(CALL_650, Side.BUY, PositionIntent.BUY_TO_OPEN)],
        qty=2,
        limit=1.234,
    )
    payload = build_order_payload(intent, attempt=0)
    assert payload == {
        "symbol": CALL_650,
        "qty": "2",
        "side": "buy",
        "type": "limit",
        "limit_price": "1.23",
        "time_in_force": "day",
        "position_intent": "buy_to_open",
        "client_order_id": new_client_order_id(intent.intent_id, 0),
    }
    assert "order_class" not in payload
    assert "legs" not in payload
    assert isinstance(payload["qty"], str)
    assert isinstance(payload["limit_price"], str)


def test_single_leg_rejects_zero_limit_and_negative_buy() -> None:
    # Zero is always invalid; a negative (signed-net credit) limit is only valid
    # on a SELL leg — a negative BUY makes no sense under the signed convention.
    with pytest.raises(ValueError, match="nonzero"):
        build_order_payload(_long_call(limit=0.0), attempt=0)
    with pytest.raises(ValueError, match="BUY leg"):
        build_order_payload(_long_call(limit=-1.0), attempt=0)


def test_single_leg_sell_to_close_accepts_signed_negative_limit() -> None:
    # Project convention: sell-to-close a long carries a NEGATIVE signed-net limit
    # (strategy.py / cli.py produce these); Alpaca's payload is unsigned with the
    # direction on side/position_intent. Regression for the review finding that
    # every long-position exit died in build_order_payload.
    intent = _intent(
        Structure.CLOSE,
        [_leg(CALL_650, Side.SELL, PositionIntent.SELL_TO_CLOSE)],
        qty=1,
        limit=-1.20,
    )
    payload = build_order_payload(intent, attempt=0)
    assert payload["side"] == "sell"
    assert payload["position_intent"] == "sell_to_close"
    assert payload["limit_price"] == "1.20"  # abs() serialized, sign carried by side


def test_marketable_limit_preserves_negative_sign_for_close() -> None:
    # broker._requote and cli.py discard a requote whose sign flips vs the intent;
    # marketable_limit must therefore preserve the signed-net convention.
    intent = _intent(
        Structure.CLOSE,
        [_leg(CALL_650, Side.SELL, PositionIntent.SELL_TO_CLOSE)],
        qty=1,
        limit=-1.20,
    )
    chain = {CALL_650: {"bid": 1.20, "ask": 1.30, "mid": 1.25}}
    policy = {"execution": {"marketable_buffer_usd": 0.02}}
    price = marketable_limit(intent, chain, policy)
    assert price == pytest.approx(-1.18)  # bid - buffer, sign preserved


def test_qty_must_be_whole_and_positive() -> None:
    with pytest.raises(ValueError, match="qty"):
        build_order_payload(
            _intent(
                Structure.LONG_CALL,
                [_leg(CALL_650, Side.BUY, PositionIntent.BUY_TO_OPEN)],
                qty=0,
            ),
            attempt=0,
        )


# ------------------------------------------------------------------------- payload: mleg


def test_debit_vertical_payload() -> None:
    intent = _intent(Structure.VERTICAL_DEBIT, _debit_vertical_legs(), qty=3, limit=0.8)
    payload = build_order_payload(intent, attempt=1, zk_prefix="deadbeefcafef00d")
    assert payload == {
        "order_class": "mleg",
        "qty": "3",
        "type": "limit",
        "limit_price": "0.80",
        "time_in_force": "day",
        "legs": [
            {
                "symbol": CALL_650,
                "ratio_qty": "1",
                "side": "buy",
                "position_intent": "buy_to_open",
            },
            {
                "symbol": CALL_655,
                "ratio_qty": "1",
                "side": "sell",
                "position_intent": "sell_to_open",
            },
        ],
        "client_order_id": new_client_order_id(intent.intent_id, 1, "deadbeefcafef00d"),
    }
    assert "symbol" not in payload
    assert "side" not in payload
    assert "position_intent" not in payload


def test_credit_vertical_negative_limit_price_string() -> None:
    intent = _intent(Structure.VERTICAL_CREDIT, _credit_vertical_legs(), qty=1, limit=-0.85)
    payload = build_order_payload(intent, attempt=0)
    assert payload["order_class"] == "mleg"
    assert payload["limit_price"] == "-0.85"
    assert payload["time_in_force"] == "day"


def test_coprime_rejection_2_to_4() -> None:
    legs = [
        _leg(CALL_650, Side.BUY, PositionIntent.BUY_TO_OPEN, ratio=2),
        _leg(CALL_655, Side.SELL, PositionIntent.SELL_TO_OPEN, ratio=4),
    ]
    intent = _intent(Structure.VERTICAL_DEBIT, legs, limit=0.8)
    with pytest.raises(ValueError, match="coprime"):
        build_order_payload(intent, attempt=0)


def test_uncovered_short_rejection_wrong_type() -> None:
    # A short CALL "covered" only by a long PUT is naked upside risk — must reject.
    legs = [
        _leg(CALL_655, Side.SELL, PositionIntent.SELL_TO_OPEN),
        _leg(PUT_640, Side.BUY, PositionIntent.BUY_TO_OPEN),
    ]
    intent = _intent(Structure.VERTICAL_CREDIT, legs, limit=-0.5)
    with pytest.raises(ValueError, match="uncovered short"):
        build_order_payload(intent, attempt=0)


def test_uncovered_short_rejection_ratio_exceeds_long() -> None:
    # 1x2 ratio spread: coprime passes (gcd(1,2)==1) but one short is naked.
    legs = [
        _leg(CALL_650, Side.BUY, PositionIntent.BUY_TO_OPEN, ratio=1),
        _leg(CALL_655, Side.SELL, PositionIntent.SELL_TO_OPEN, ratio=2),
    ]
    intent = _intent(Structure.VERTICAL_CREDIT, legs, limit=-0.1)
    with pytest.raises(ValueError, match="uncovered short"):
        build_order_payload(intent, attempt=0)


def test_duplicate_leg_symbols_rejected() -> None:
    legs = [
        _leg(CALL_650, Side.BUY, PositionIntent.BUY_TO_OPEN),
        _leg(CALL_650, Side.SELL, PositionIntent.SELL_TO_OPEN),
    ]
    intent = _intent(Structure.VERTICAL_DEBIT, legs, limit=0.5)
    with pytest.raises(ValueError, match="unique"):
        build_order_payload(intent, attempt=0)


def test_more_than_four_legs_rejected() -> None:
    strikes = (650.0, 655.0, 660.0, 665.0, 670.0)
    legs = [
        _leg(
            occ_build("SPY", date(2026, 9, 4), "C", k),
            Side.BUY,
            PositionIntent.BUY_TO_OPEN,
        )
        for k in strikes
    ]
    intent = _intent(Structure.STRADDLE, legs, limit=1.0)
    with pytest.raises(ValueError, match="at most 4 legs"):
        build_order_payload(intent, attempt=0)


# -------------------------------------------------------------------- marketable_limit


def test_marketable_limit_single_buy_is_ask_plus_buffer() -> None:
    intent = _long_call()
    assert marketable_limit(intent, CHAIN, POLICY) == pytest.approx(1.12)  # 1.10 + 0.02


def test_marketable_limit_single_sell_is_bid_minus_buffer() -> None:
    intent = _intent(
        Structure.CLOSE,
        [_leg(CALL_650, Side.SELL, PositionIntent.SELL_TO_CLOSE)],
        limit=1.0,
    )
    assert marketable_limit(intent, CHAIN, POLICY) == pytest.approx(0.98)  # 1.00 - 0.02


def test_marketable_limit_mleg_debit() -> None:
    intent = _intent(Structure.VERTICAL_DEBIT, _debit_vertical_legs(), limit=0.7)
    # net = ask(650)=1.10 - bid(655)=0.40 = 0.70; debit -> +0.05 buffer = 0.75
    assert marketable_limit(intent, CHAIN, POLICY) == pytest.approx(0.75)


def test_marketable_limit_mleg_credit_moves_toward_zero() -> None:
    intent = _intent(Structure.VERTICAL_CREDIT, _credit_vertical_legs(), limit=-0.6)
    # net = -bid(650)=-1.00 + ask(655)=0.48 = -0.52; credit -> +0.05 = -0.47
    # (less credit demanded than the touch net => more fillable), sign preserved.
    price = marketable_limit(intent, CHAIN, POLICY)
    assert price == pytest.approx(-0.47)
    assert price < 0


def test_marketable_limit_rounds_half_up() -> None:
    chain = {CALL_650: {"bid": 1.00, "ask": 1.095}}
    assert marketable_limit(_long_call(), chain, POLICY) == pytest.approx(1.12)  # 1.115 -> 1.12


def test_marketable_limit_credit_rounds_half_up_away_from_zero() -> None:
    chain = {
        CALL_650: {"bid": 1.325, "ask": 1.40},
        CALL_655: {"bid": 0.35, "ask": 0.40},
    }
    intent = _intent(Structure.VERTICAL_CREDIT, _credit_vertical_legs(), limit=-0.8)
    # net = -1.325 + 0.40 = -0.925; +0.05 = -0.875 -> half-up on magnitude -> -0.88
    assert marketable_limit(intent, chain, POLICY) == pytest.approx(-0.88)


def test_marketable_limit_never_flips_credit_sign() -> None:
    chain = {
        CALL_650: {"bid": 0.44, "ask": 0.50},
        CALL_655: {"bid": 0.38, "ask": 0.42},
    }
    intent = _intent(Structure.VERTICAL_CREDIT, _credit_vertical_legs(), limit=-0.02)
    # net = -0.44 + 0.42 = -0.02; +0.05 buffer would flip to +0.03 -> clamp to -0.01
    assert marketable_limit(intent, chain, POLICY) == pytest.approx(-0.01)


def test_marketable_limit_missing_symbol_raises() -> None:
    with pytest.raises(ValueError, match=PUT_640):
        marketable_limit(_long_call(symbol=PUT_640), CHAIN, POLICY)


def test_marketable_limit_none_side_quote_raises() -> None:
    chain = {CALL_650: {"bid": 1.00, "ask": None}}
    with pytest.raises(ValueError, match="ask"):
        marketable_limit(_long_call(), chain, POLICY)


def test_marketable_limit_mleg_missing_leg_quote_raises() -> None:
    chain = {CALL_650: {"bid": 1.00, "ask": 1.10}}  # CALL_655 absent
    intent = _intent(Structure.VERTICAL_DEBIT, _debit_vertical_legs(), limit=0.7)
    with pytest.raises(ValueError, match=CALL_655):
        marketable_limit(intent, chain, POLICY)
