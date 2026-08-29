"""Tests for the day-regime classifier."""
from __future__ import annotations

from quaestor import regime as R


def _bars(closes: list[float]) -> list[dict]:
    return [{"c": c, "h": c, "l": c, "v": 1000.0} for c in closes]


def test_range_day_is_range() -> None:
    # tight oscillation around 660, no net move -> RANGE
    b = _bars([660.0 + (0.1 if i % 2 else -0.1) for i in range(12)])
    assert R.classify(b, {}, 660.0) == R.RANGE


def test_trend_day_is_trend() -> None:
    # steady climb ~+1% with price above VWAP -> TREND
    b = _bars([660.0 + i * 0.6 for i in range(12)])
    assert R.classify(b, {}, b[-1]["c"]) == R.TREND


def test_violent_tape_is_storm() -> None:
    # big alternating swings -> high realized vol -> STORM
    b = _bars([660.0 + (8.0 if i % 2 else -8.0) for i in range(12)])
    assert R.classify(b, {}, 660.0) == R.STORM


def test_high_iv_is_storm() -> None:
    b = _bars([660.0] * 12)
    chain = {"SPY260904C00660000": {"mid": 5.0, "iv": 0.45}}
    assert R.classify(b, chain, 660.0) == R.STORM


def test_too_few_bars_is_unknown() -> None:
    assert R.classify(_bars([660.0, 661.0, 662.0]), {}, 662.0) == R.UNKNOWN


def test_expected_move_from_atm_straddle() -> None:
    chain = {
        "SPY260904C00660000": {"mid": 3.0},
        "SPY260904P00660000": {"mid": 3.0},
    }
    em = R.expected_move_pct(chain, 660.0)
    assert em is not None
    assert abs(em - (6.0 / 660.0)) < 1e-6
