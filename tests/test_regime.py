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


def test_diagnostics_agrees_with_classify_and_exposes_features() -> None:
    # mine #2: diagnostics() must return the SAME label as classify() plus the
    # features behind it, so a live cycle can log WHY (not just the verdict).
    trend = _bars([660.0 + i * 0.6 for i in range(12)])
    d = R.diagnostics(trend, {}, trend[-1]["c"])
    assert d["label"] == R.classify(trend, {}, trend[-1]["c"]) == R.TREND
    assert d["net_move"] is not None and d["net_move"] > 0
    assert d["vwap_dev"] is not None
    assert d["rvol"] is not None
    assert d["reason"]                      # human-readable why-string for the log

    # the UNKNOWN early-tape case still carries a reason and null features
    thin = R.diagnostics(_bars([660.0, 661.0]), {}, 661.0)
    assert thin["label"] == R.UNKNOWN
    assert thin["net_move"] is None
    assert "tape" in thin["reason"]
