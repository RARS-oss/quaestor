"""Day-regime classifier: trend / range / storm.

The single component with a documented live P&L effect (Alpaca's own study:
regime-aligned trades +1.62% vs +0.21% against) is gating entries by the day's
regime. quaestor's long-premium playbooks bleed theta on quiet range days; a
premium-selling income sleeve is only safe on range days. This module labels the
day so the strategy can: sell premium in RANGE, buy premium/convexity in TREND,
and stand down in a STORM.

Pure and deterministic — features from 5-min bars + the option chain (ATM IV and
the expected move from the ATM straddle), no network. A GEX-style label is a weak
predictor on its own; what matters is the coarse regime bucket, computed from the
free indicative feed.
"""
from __future__ import annotations

import math
import statistics
from typing import Any

TREND = "trend"
RANGE = "range"
STORM = "storm"
UNKNOWN = "unknown"

# Thresholds (documented heuristics; conservative so we only ADD income on a
# confident range read and only SUPPRESS entries on a confident storm).
STORM_RVOL = 0.011          # per-5min realized vol (~ >45% annualized) -> storm
STORM_ATM_IV = 0.35         # ATM implied vol -> storm
TREND_NET_MOVE = 0.004      # >=0.40% net move since the open
TREND_VWAP_DEV = 0.0025     # >=0.25% from session VWAP, same sign -> directional
RANGE_NET_MOVE = 0.0025     # <0.25% net move
RANGE_VWAP_DEV = 0.002      # within 0.20% of VWAP
MIN_BARS = 6                # need at least ~30 min of tape before classifying


def _num(v: Any) -> float | None:
    try:
        f = float(v)
        return f if math.isfinite(f) else None
    except (TypeError, ValueError):
        return None


def expected_move_pct(chain: dict[str, dict], spot: float) -> float | None:
    """Expected move as a fraction of spot from the nearest ATM call+put straddle."""
    if not chain or spot <= 0:
        return None
    best: tuple[float, float] | None = None      # (|strike-spot|, call_mid+put_mid)
    by_strike: dict[tuple[str, float], dict] = {}
    for occ, q in chain.items():
        mid = _num(q.get("mid"))
        if mid is None or mid <= 0:
            continue
        k = _strike_of(occ)
        typ = _type_of(occ)
        if k is None or typ is None:
            continue
        by_strike[(typ, k)] = q
    strikes = sorted({k for (_t, k) in by_strike})
    for k in strikes:
        c = by_strike.get(("C", k))
        p = by_strike.get(("P", k))
        if not c or not p:
            continue
        cm, pm = _num(c.get("mid")), _num(p.get("mid"))
        if cm is None or pm is None:
            continue
        d = abs(k - spot)
        if best is None or d < best[0]:
            best = (d, cm + pm)
    if best is None:
        return None
    return best[1] / spot


def atm_iv(chain: dict[str, dict], spot: float) -> float | None:
    """IV of the strike nearest spot (any type), if present."""
    best: tuple[float, float] | None = None
    for occ, q in chain.items():
        iv = _num(q.get("iv"))
        k = _strike_of(occ)
        if iv is None or k is None:
            continue
        d = abs(k - spot)
        if best is None or d < best[0]:
            best = (d, iv)
    return best[1] if best else None


def diagnostics(bars: list[dict], chain: dict[str, dict], spot: float) -> dict[str, Any]:
    """Same computation as classify(), but returns the features alongside the label
    so callers can log WHY a regime was chosen — not just the verdict. This is how
    we watch the classifier on a live day (mine #2: never trust it blind): every
    cycle records net_move / vwap_dev / rvol / iv next to the label it produced."""
    closes = [c for b in bars if (c := _num(b.get("c"))) is not None]
    if len(closes) < MIN_BARS or spot <= 0:
        return {"label": UNKNOWN, "bars": len(closes), "net_move": None,
                "vwap_dev": None, "rvol": None, "iv": None, "exp_move": None,
                "reason": "insufficient tape" if len(closes) < MIN_BARS else "no spot"}

    open_px = closes[0]
    last = closes[-1]
    net_move = (last - open_px) / open_px if open_px else 0.0

    # session VWAP from typical price * volume
    num = den = 0.0
    for b in bars:
        c, h, l = _num(b.get("c")), _num(b.get("h")), _num(b.get("l"))
        v = _num(b.get("v")) or 0.0
        if c is None:
            continue
        tp = (c + (h or c) + (l or c)) / 3.0
        num += tp * v
        den += v
    vwap = (num / den) if den > 0 else last
    vwap_dev = (last - vwap) / vwap if vwap else 0.0

    rets = [(closes[i] - closes[i - 1]) / closes[i - 1]
            for i in range(1, len(closes)) if closes[i - 1]]
    rvol = statistics.pstdev(rets) if len(rets) >= 2 else 0.0

    iv = atm_iv(chain, spot)

    # STORM: violent tape or a big IV print -> stand down.
    if rvol >= STORM_RVOL or (iv is not None and iv >= STORM_ATM_IV):
        label, reason = STORM, f"rvol {rvol:.4f}>={STORM_RVOL} or iv {iv}>={STORM_ATM_IV}"
    # TREND: a real directional day (net move + one-sided vs VWAP) -> buy premium.
    elif abs(net_move) >= TREND_NET_MOVE and abs(vwap_dev) >= TREND_VWAP_DEV \
            and (net_move >= 0) == (vwap_dev >= 0):
        label, reason = TREND, f"net {net_move:+.4f} & vwap_dev {vwap_dev:+.4f} one-sided"
    # RANGE: contained, near VWAP -> sell premium.
    elif abs(net_move) < RANGE_NET_MOVE and abs(vwap_dev) < RANGE_VWAP_DEV:
        label, reason = RANGE, f"net {net_move:+.4f} & vwap_dev {vwap_dev:+.4f} contained"
    else:
        label, reason = UNKNOWN, f"net {net_move:+.4f} / vwap_dev {vwap_dev:+.4f} in no bucket"

    return {"label": label, "bars": len(closes), "net_move": net_move,
            "vwap_dev": vwap_dev, "rvol": rvol, "iv": iv,
            "exp_move": expected_move_pct(chain, spot), "reason": reason}


def classify(bars: list[dict], chain: dict[str, dict], spot: float) -> str:
    """Label the day for one underlying. Conservative: UNKNOWN until enough tape."""
    return diagnostics(bars, chain, spot)["label"]


# -- tiny OCC helpers (self-contained; no import cycle with strategy) ----------

def _strike_of(occ: str) -> float | None:
    if len(occ) < 15:
        return None
    try:
        return int(occ[-8:]) / 1000.0
    except ValueError:
        return None


def _type_of(occ: str) -> str | None:
    if len(occ) < 15:
        return None
    t = occ[-9]
    return t if t in ("C", "P") else None
