"""Deterministic momentum feature engine: 5-minute bars -> per-underlying Signal.

Role: step 4 of the decision cycle. Turns normalized 5-min equity bars
(data.MarketData.stock_bars) plus optional snapshot dicts (data.stock_snapshot)
into Signal objects that strategy.decide() consumes.

Alpaca facts encoded:
- Bars come from the free IEX feed, so history can be sparse/short early in the
  session and fields can be missing — every feature degrades gracefully with
  fewer than 7 bars and with None/absent values (None-safe by construction).
- Snapshot dicts may carry a fresher last-trade price ("last"/"latest_trade")
  and the official daily bar high/low ("daily_bar") — both optional, guarded.
- No look-ahead: only the completed data passed in is used. Pure functions,
  no network, fully deterministic (unit-testable on fixtures).

Features per symbol:
  ret_5m    — return over the last completed 5-min interval
  ret_30m   — return over the last ~30 minutes (6 intervals; earliest bar if fewer)
  vwap_dev  — deviation of current price from session VWAP (volume-weighted;
              falls back to mean close when volume is absent)
  range_pos — position of current price in the session/day range, 0..1
  rv_30m    — realized vol of the last 5-min returns (up to 6), annualized
Score: weighted sum of z-scored directional features (z via realized per-bar
sigma, fallback fixed scale; each z clamped to +/-3). direction = sign(score)
if |score| >= 0.3 else 0; strength = min(1, |score|).
"""
from __future__ import annotations

import math
import statistics
from dataclasses import dataclass
from typing import Any

# --- tuning constants (deterministic, documented) ---------------------------
DIRECTION_THRESHOLD: float = 0.3
Z_CLAMP: float = 3.0
FALLBACK_SIGMA_5M: float = 0.001          # ~0.1% per 5-min bar when rv unavailable
RV_WINDOW_BARS: int = 6                   # 6 x 5min = 30 minutes
ANNUALIZATION: float = math.sqrt(78.0 * 252.0)   # 78 five-min bars/day, 252 days
WEIGHTS: dict[str, float] = {
    "ret_5m": 0.35,
    "ret_30m": 0.25,
    "vwap_dev": 0.25,
    "range_pos": 0.15,
}

_FEATURE_KEYS = ("ret_5m", "ret_30m", "vwap_dev", "range_pos", "rv_30m", "score")


@dataclass
class Signal:
    """Directional read on one underlying, with the raw features that drove it."""
    underlying: str
    direction: int                  # +1 bullish, -1 bearish, 0 flat
    strength: float                 # 0..1
    features: dict[str, float]      # ret_5m, ret_30m, vwap_dev, range_pos, rv_30m, score


def compute(bars: dict[str, list[dict]], snapshots: dict[str, dict]) -> dict[str, Signal]:
    """Compute a Signal for every symbol present in bars or snapshots.

    Deterministic: same inputs -> same outputs. Symbols with no usable bars get
    a flat Signal (direction 0, strength 0, zeroed features).
    """
    bars = bars or {}
    snapshots = snapshots or {}
    out: dict[str, Signal] = {}
    for sym in sorted(set(bars) | set(snapshots)):
        sym_bars = bars.get(sym) or []
        snap = snapshots.get(sym) or {}
        out[sym] = _signal_for(sym, sym_bars, snap if isinstance(snap, dict) else {})
    return out


# --- internals ---------------------------------------------------------------

def _num(v: Any) -> float | None:
    """Coerce to a finite float or None. Accepts int/float/str."""
    if v is None or isinstance(v, bool):
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(f):
        return None
    return f


def _bar_val(bar: dict, *keys: str) -> float | None:
    for k in keys:
        if k in bar:
            f = _num(bar.get(k))
            if f is not None:
                return f
    return None


def _snap_price(snap: dict) -> float | None:
    """Best-effort latest trade price out of a snapshot dict (shape-tolerant)."""
    for k in ("last", "price", "last_price"):
        f = _num(snap.get(k))
        if f is not None and f > 0:
            return f
    for k in ("latest_trade", "latestTrade"):
        sub = snap.get(k)
        if isinstance(sub, dict):
            f = _num(sub.get("p")) or _num(sub.get("price"))
            if f is not None and f > 0:
                return f
    return None


def _snap_day_high_low(snap: dict) -> tuple[float | None, float | None]:
    for k in ("daily_bar", "dailyBar", "day_bar"):
        sub = snap.get(k)
        if isinstance(sub, dict):
            hi = _bar_val(sub, "h", "high")
            lo = _bar_val(sub, "l", "low")
            return (hi if hi and hi > 0 else None, lo if lo and lo > 0 else None)
    return (None, None)


def _clamp(z: float, bound: float = Z_CLAMP) -> float:
    return max(-bound, min(bound, z))


def _zero_features() -> dict[str, float]:
    return {k: 0.0 for k in _FEATURE_KEYS}


def _signal_for(sym: str, sym_bars: list[dict], snap: dict) -> Signal:
    closes: list[float] = []
    highs: list[float] = []
    lows: list[float] = []
    vols: list[float] = []
    for bar in sym_bars:
        if not isinstance(bar, dict):
            continue
        c = _bar_val(bar, "c", "close")
        if c is None or c <= 0:
            continue
        h = _bar_val(bar, "h", "high")
        l = _bar_val(bar, "l", "low")
        v = _bar_val(bar, "v", "volume")
        closes.append(c)
        highs.append(h if (h is not None and h > 0) else c)
        lows.append(l if (l is not None and l > 0) else c)
        vols.append(v if (v is not None and v >= 0) else 0.0)

    if not closes:
        return Signal(underlying=sym, direction=0, strength=0.0, features=_zero_features())

    cur = _snap_price(snap) or closes[-1]

    # returns
    base5 = closes[-2] if len(closes) >= 2 else closes[-1]
    ret_5m = cur / base5 - 1.0 if base5 > 0 else 0.0
    base30 = closes[-7] if len(closes) >= 7 else closes[0]
    ret_30m = cur / base30 - 1.0 if base30 > 0 else 0.0

    # session vwap
    tot_v = sum(vols)
    if tot_v > 0:
        vwap = sum(((h + l + c) / 3.0) * v for h, l, c, v in zip(highs, lows, closes, vols)) / tot_v
    else:
        vwap = sum(closes) / len(closes)
    vwap_dev = cur / vwap - 1.0 if vwap > 0 else 0.0

    # day range position
    snap_hi, snap_lo = _snap_day_high_low(snap)
    day_hi = max(highs + ([snap_hi] if snap_hi else []) + [cur])
    day_lo = min(lows + ([snap_lo] if snap_lo else []) + [cur])
    range_pos = (cur - day_lo) / (day_hi - day_lo) if day_hi > day_lo else 0.5
    range_pos = max(0.0, min(1.0, range_pos))

    # realized vol of last <=6 five-minute returns, annualized
    rets = [closes[i] / closes[i - 1] - 1.0 for i in range(1, len(closes)) if closes[i - 1] > 0]
    rets = rets[-RV_WINDOW_BARS:]
    sigma_5m = statistics.pstdev(rets) if len(rets) >= 2 else 0.0
    rv_30m = sigma_5m * ANNUALIZATION

    # z-scores: scale returns by realized per-bar sigma (fallback fixed scale)
    sig5 = sigma_5m if sigma_5m > 1e-9 else FALLBACK_SIGMA_5M
    sig30 = sig5 * math.sqrt(6.0)
    z = {
        "ret_5m": _clamp(ret_5m / sig5),
        "ret_30m": _clamp(ret_30m / sig30),
        "vwap_dev": _clamp(vwap_dev / sig30),
        "range_pos": _clamp((range_pos - 0.5) * 2.0, 1.0),
    }
    score = sum(WEIGHTS[k] * z[k] for k in WEIGHTS)

    direction = 0
    if abs(score) >= DIRECTION_THRESHOLD:
        direction = 1 if score > 0 else -1
    strength = min(1.0, abs(score))

    features = {
        "ret_5m": ret_5m,
        "ret_30m": ret_30m,
        "vwap_dev": vwap_dev,
        "range_pos": range_pos,
        "rv_30m": rv_30m,
        "score": score,
    }
    return Signal(underlying=sym, direction=direction, strength=strength, features=features)
