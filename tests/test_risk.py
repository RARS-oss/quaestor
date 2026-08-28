"""Unit tests for quaestor.risk — every gate proven on both its pass and fail path.

Deterministic, no network, no clock reads: `now` is always injected, chains are
hand-built fixtures shaped like data.MarketData.option_chain() output, and the
real configs/policy.yaml is loaded (preferring quaestor.config.load_policy(),
falling back to yaml+hashlib when config.py is not present yet).

Run from the repo root: python -m pytest tests/test_risk.py -q
"""
from __future__ import annotations

import copy
import hashlib
import math
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest
import yaml

from quaestor import risk
from quaestor.models import (
    AccountSnapshot,
    Leg,
    PositionIntent,
    RiskVerdict,
    Side,
    Structure,
    TradeIntent,
)

ET = ZoneInfo("America/New_York")
REPO_ROOT = Path(__file__).resolve().parents[1]

# OCC symbols used throughout (SPY weekly calls/puts expiring 2026-09-04)
C650 = "SPY260904C00650000"
C655 = "SPY260904C00655000"
C660 = "SPY260904C00660000"
C665 = "SPY260904C00665000"
C670 = "SPY260904C00670000"
P650 = "SPY260904P00650000"

# A calm Tuesday mid-morning inside the contest window, well before all cutoffs.
NOW = datetime(2026, 9, 1, 10, 30, tzinfo=ET)

EXPECTED_CHECK_ORDER = [
    "paper_gate", "structure_allowed", "defined_risk", "per_trade_cap",
    "daily_halt", "weekly_halt", "concurrency", "concentration",
    "spread_quality", "open_interest", "leg_price_min", "timing",
    "sane_limit_price", "qty_positive", "mleg_rules",
]


# --------------------------------------------------------------------------- #
# fixtures and builders
# --------------------------------------------------------------------------- #

def _load_policy_fallback() -> dict:
    raw = (REPO_ROOT / "configs" / "policy.yaml").read_bytes()
    pol = yaml.safe_load(raw)
    pol["digest"] = hashlib.sha256(raw).hexdigest()
    return pol


def _load_policy() -> dict:
    try:
        from quaestor.config import load_policy  # written by a parallel module
        pol = load_policy()
        if isinstance(pol, dict) and pol.get("digest"):
            return pol
    except Exception:
        pass
    return _load_policy_fallback()


@pytest.fixture(scope="session")
def policy() -> dict:
    return _load_policy()


@pytest.fixture()
def account() -> AccountSnapshot:
    return AccountSnapshot(
        equity=100_000.0,
        cash=100_000.0,
        buying_power=200_000.0,
        options_buying_power=100_000.0,
        options_approved_level=3,
        options_trading_level=3,
        positions=[],
    )


def snap(bid: float, ask: float, *, oi: int | None = 500,
         delta: float | None = 0.4) -> dict[str, Any]:
    """One normalized option snapshot as data.option_chain() returns them."""
    mid = round((bid + ask) / 2.0, 4)
    return {
        "bid": bid, "ask": ask, "mid": mid, "last": mid,
        "iv": 0.14, "delta": delta, "gamma": 0.02, "theta": -0.05,
        "vega": 0.08, "oi": oi, "t": 1756000000.0,
    }


def make_chain() -> dict[str, dict]:
    return {
        C650: snap(1.98, 2.02, oi=1500, delta=0.45),
        C655: snap(0.99, 1.01, oi=900, delta=0.25),
        C660: snap(0.49, 0.50, oi=700, delta=0.15),
        C665: snap(0.24, 0.25, oi=400, delta=0.10),
        C670: snap(0.12, 0.12, oi=300, delta=0.06),
        P650: snap(1.48, 1.52, oi=1200, delta=-0.45),
    }


def fresh_state(**overrides: Any) -> dict[str, Any]:
    state: dict[str, Any] = {
        "halted_today": False,
        "day_pnl_pct": 0.0,
        "open_position_count": 0,
        "underlying_exposure": {},
        "week_pnl_pct": 0.0,
    }
    state.update(overrides)
    return state


def make_vertical(**overrides: Any) -> TradeIntent:
    """Happy-path debit call vertical: buy 650C / sell 655C, net mid +1.00."""
    kwargs: dict[str, Any] = dict(
        underlying="SPY",
        structure=Structure.VERTICAL_DEBIT,
        legs=[
            Leg(C650, Side.BUY, 1, PositionIntent.BUY_TO_OPEN),
            Leg(C655, Side.SELL, 1, PositionIntent.SELL_TO_OPEN),
        ],
        qty=2,
        limit_price=1.05,
        thesis="test momentum debit vertical",
        max_loss_usd=210.0,   # debit * 100 * qty
        expiry="2026-09-04",
        is_0dte=False,
    )
    kwargs.update(overrides)
    return TradeIntent(**kwargs)


def make_credit_vertical(**overrides: Any) -> TradeIntent:
    """Covered credit vertical: sell 650C / buy 655C, net mid -1.00, width 5."""
    kwargs: dict[str, Any] = dict(
        underlying="SPY",
        structure=Structure.VERTICAL_CREDIT,
        legs=[
            Leg(C650, Side.SELL, 1, PositionIntent.SELL_TO_OPEN),
            Leg(C655, Side.BUY, 1, PositionIntent.BUY_TO_OPEN),
        ],
        qty=1,
        limit_price=-0.95,
        thesis="test credit vertical",
        max_loss_usd=405.0,   # (width - credit) * 100 * qty
        expiry="2026-09-04",
        is_0dte=False,
    )
    kwargs.update(overrides)
    return TradeIntent(**kwargs)


def make_close(**overrides: Any) -> TradeIntent:
    """Close a long 650/655 call vertical: sell-to-close / buy-to-close, net mid -1.00."""
    kwargs: dict[str, Any] = dict(
        underlying="SPY",
        structure=Structure.CLOSE,
        legs=[
            Leg(C650, Side.SELL, 1, PositionIntent.SELL_TO_CLOSE),
            Leg(C655, Side.BUY, 1, PositionIntent.BUY_TO_CLOSE),
        ],
        qty=2,
        limit_price=-0.95,
        thesis="test exit of long vertical",
        max_loss_usd=0.0,
        expiry="2026-09-04",
        is_0dte=False,
    )
    kwargs.update(overrides)
    return TradeIntent(**kwargs)


def run_judge(
    intent: TradeIntent,
    policy: dict,
    account: AccountSnapshot,
    *,
    state: dict | None = None,
    chain: dict[str, dict] | None = None,
    now: datetime = NOW,
) -> RiskVerdict:
    return risk.judge(
        intent,
        policy=policy,
        account=account,
        portfolio_state=state if state is not None else fresh_state(),
        chain=chain if chain is not None else make_chain(),
        now=now,
    )


def get_check(verdict: RiskVerdict, name: str):
    return next(c for c in verdict.checks if c.name == name)


# --------------------------------------------------------------------------- #
# the composed verdict
# --------------------------------------------------------------------------- #

def test_happy_path_vertical_fully_approved(policy, account):
    intent = make_vertical()
    verdict = run_judge(intent, policy, account)
    assert verdict.approved, [c.detail for c in verdict.checks if not c.ok]
    assert all(c.ok for c in verdict.checks)
    assert [c.name for c in verdict.checks] == EXPECTED_CHECK_ORDER
    assert verdict.intent_id == intent.intent_id
    assert verdict.reasons == []


def test_policy_digest_stamped_into_verdict(policy, account):
    verdict = run_judge(make_vertical(), policy, account)
    assert verdict.policy_digest == policy["digest"]
    assert len(verdict.policy_digest) == 64  # sha256 hex


def test_rejected_verdict_lists_reasons(policy, account):
    verdict = run_judge(make_vertical(qty=0), policy, account)
    assert not verdict.approved
    assert verdict.reasons  # non-empty explanations for the audit log


# --------------------------------------------------------------------------- #
# paper_gate
# --------------------------------------------------------------------------- #

def test_paper_gate_passes_by_default(policy, account):
    verdict = run_judge(make_vertical(), policy, account)
    assert get_check(verdict, "paper_gate").ok


def test_paper_gate_fails_on_live_base_or_flag(policy, account):
    live = copy.deepcopy(policy)
    live["trading_base"] = "https://api.alpaca.markets"
    verdict = run_judge(make_vertical(), live, account)
    assert not verdict.approved
    assert not get_check(verdict, "paper_gate").ok

    flagged = copy.deepcopy(policy)
    flagged["live_trade"] = True
    verdict2 = run_judge(make_vertical(), flagged, account)
    assert not get_check(verdict2, "paper_gate").ok


# --------------------------------------------------------------------------- #
# structure_allowed
# --------------------------------------------------------------------------- #

def test_structure_allowed_pass_and_fail(policy, account):
    assert get_check(run_judge(make_vertical(), policy, account), "structure_allowed").ok

    narrowed = copy.deepcopy(policy)
    narrowed["structures"]["allowed"] = ["long_call", "long_put"]
    verdict = run_judge(make_vertical(), narrowed, account)
    assert not verdict.approved
    assert not get_check(verdict, "structure_allowed").ok


# --------------------------------------------------------------------------- #
# defined_risk
# --------------------------------------------------------------------------- #

def test_defined_risk_rejects_infinite_max_loss(policy, account):
    verdict = run_judge(make_vertical(max_loss_usd=float("inf")), policy, account)
    assert not get_check(verdict, "defined_risk").ok


def test_defined_risk_rejects_nonpositive_max_loss_on_open(policy, account):
    verdict = run_judge(make_vertical(max_loss_usd=0.0), policy, account)
    assert not get_check(verdict, "defined_risk").ok


def test_defined_risk_rejects_naked_short(policy, account):
    naked = make_credit_vertical(
        legs=[Leg(C650, Side.SELL, 1, PositionIntent.SELL_TO_OPEN)],
        limit_price=-2.0,
        max_loss_usd=500.0,  # a lie — the cover check must catch it regardless
    )
    verdict = run_judge(naked, policy, account)
    assert not verdict.approved
    check = get_check(verdict, "defined_risk")
    assert not check.ok
    assert "naked" in check.detail.lower()


def test_defined_risk_accepts_covered_credit_vertical(policy, account):
    verdict = run_judge(make_credit_vertical(), policy, account)
    assert get_check(verdict, "defined_risk").ok
    assert verdict.approved, verdict.reasons


def test_defined_risk_rejects_uncovered_ratio_spread(policy, account):
    ratio = make_credit_vertical(
        legs=[
            Leg(C650, Side.BUY, 1, PositionIntent.BUY_TO_OPEN),
            Leg(C655, Side.SELL, 2, PositionIntent.SELL_TO_OPEN),
        ],
        limit_price=-0.10,
        max_loss_usd=400.0,
    )
    verdict = run_judge(ratio, policy, account)
    assert not get_check(verdict, "defined_risk").ok


# --------------------------------------------------------------------------- #
# per_trade_cap (catalyst-aware)
# --------------------------------------------------------------------------- #

def test_per_trade_cap_default_pass_and_fail(policy, account):
    # default cap: 10% of 100k = $10,000
    ok = run_judge(make_vertical(max_loss_usd=9_800.0), policy, account)
    assert get_check(ok, "per_trade_cap").ok

    bad = run_judge(make_vertical(max_loss_usd=10_500.0), policy, account)
    assert not bad.approved
    assert not get_check(bad, "per_trade_cap").ok


def test_per_trade_cap_catalyst_allows_larger_size(policy, account):
    # catalyst cap: 20% of 100k = $20,000
    intent = make_vertical(max_loss_usd=15_000.0, catalyst_tag="NFP")
    verdict = run_judge(intent, policy, account)
    check = get_check(verdict, "per_trade_cap")
    assert check.ok
    assert "NFP" in check.detail

    # same size without the tag fails the default cap
    untagged = run_judge(make_vertical(max_loss_usd=15_000.0), policy, account)
    assert not get_check(untagged, "per_trade_cap").ok

    # and the catalyst cap is still a cap
    huge = run_judge(make_vertical(max_loss_usd=25_000.0, catalyst_tag="NFP"), policy, account)
    assert not get_check(huge, "per_trade_cap").ok


# --------------------------------------------------------------------------- #
# daily_halt / weekly_halt
# --------------------------------------------------------------------------- #

def test_daily_halt_blocks_at_threshold(policy, account):
    verdict = run_judge(make_vertical(), policy, account,
                        state=fresh_state(day_pnl_pct=-15.0))
    assert not verdict.approved
    assert not get_check(verdict, "daily_halt").ok


def test_daily_halt_blocks_when_latched(policy, account):
    verdict = run_judge(make_vertical(), policy, account,
                        state=fresh_state(halted_today=True))
    assert not get_check(verdict, "daily_halt").ok


def test_daily_halt_passes_above_threshold(policy, account):
    verdict = run_judge(make_vertical(), policy, account,
                        state=fresh_state(day_pnl_pct=-14.9))
    assert get_check(verdict, "daily_halt").ok


def test_weekly_halt_pass_and_fail(policy, account):
    ok = run_judge(make_vertical(), policy, account,
                   state=fresh_state(week_pnl_pct=-29.0))
    assert get_check(ok, "weekly_halt").ok

    bad = run_judge(make_vertical(), policy, account,
                    state=fresh_state(week_pnl_pct=-30.0))
    assert not bad.approved
    assert not get_check(bad, "weekly_halt").ok


# --------------------------------------------------------------------------- #
# concurrency
# --------------------------------------------------------------------------- #

def test_concurrency_pass_and_fail(policy, account):
    ok = run_judge(make_vertical(), policy, account,
                   state=fresh_state(open_position_count=2))
    assert get_check(ok, "concurrency").ok

    full = run_judge(make_vertical(), policy, account,
                     state=fresh_state(open_position_count=3))
    assert not full.approved
    assert not get_check(full, "concurrency").ok


# --------------------------------------------------------------------------- #
# concentration
# --------------------------------------------------------------------------- #

def test_concentration_pass_and_fail_named_underlying(policy, account):
    # SPY cap 60% of 100k = $60,000; new order adds |1.05| * 100 * 2 = $210
    ok = run_judge(make_vertical(), policy, account,
                   state=fresh_state(underlying_exposure={"SPY": 59_700.0}))
    assert get_check(ok, "concentration").ok

    bad = run_judge(make_vertical(), policy, account,
                    state=fresh_state(underlying_exposure={"SPY": 59_900.0}))
    assert not bad.approved
    assert not get_check(bad, "concentration").ok


def test_concentration_default_cap_for_unlisted_underlying(policy, account):
    # default cap 40% = $40,000 for a root without an explicit cap
    intent = make_vertical(underlying="IWM")
    state = fresh_state(underlying_exposure={"IWM": 39_900.0})
    check = risk.check_concentration(intent, policy, account, state)
    assert not check.ok

    check_ok = risk.check_concentration(
        intent, policy, account, fresh_state(underlying_exposure={"IWM": 39_700.0})
    )
    assert check_ok.ok


# --------------------------------------------------------------------------- #
# spread_quality
# --------------------------------------------------------------------------- #

def test_spread_quality_rejects_wide_market(policy, account):
    chain = make_chain()
    chain[C650] = snap(1.80, 2.20, oi=1500)  # 0.40 / 2.00 = 20% >> 3%
    verdict = run_judge(make_vertical(), policy, account, chain=chain)
    assert not verdict.approved
    assert not get_check(verdict, "spread_quality").ok


def test_spread_quality_rejects_missing_quote(policy, account):
    chain = make_chain()
    del chain[C655]
    verdict = run_judge(make_vertical(), policy, account, chain=chain)
    assert not get_check(verdict, "spread_quality").ok


def test_spread_quality_passes_tight_market(policy, account):
    verdict = run_judge(make_vertical(), policy, account)
    assert get_check(verdict, "spread_quality").ok


# --------------------------------------------------------------------------- #
# open_interest
# --------------------------------------------------------------------------- #

def test_open_interest_rejects_thin_contract(policy, account):
    chain = make_chain()
    chain[C655] = snap(0.99, 1.01, oi=50)
    verdict = run_judge(make_vertical(), policy, account, chain=chain)
    assert not verdict.approved
    assert not get_check(verdict, "open_interest").ok


def test_open_interest_tolerates_unreported_oi(policy, account):
    # indicative feed frequently omits OI — pass with a note, don't dead-stop
    chain = make_chain()
    chain[C655] = snap(0.99, 1.01, oi=None)
    verdict = run_judge(make_vertical(), policy, account, chain=chain)
    check = get_check(verdict, "open_interest")
    assert check.ok
    assert "unreported" in check.detail.lower()


def test_open_interest_passes_liquid_chain(policy, account):
    assert get_check(run_judge(make_vertical(), policy, account), "open_interest").ok


# --------------------------------------------------------------------------- #
# leg_price_min
# --------------------------------------------------------------------------- #

def test_leg_price_min_rejects_subnickel_leg(policy, account):
    chain = make_chain()
    chain[C655] = snap(0.02, 0.04, oi=900)  # mid 0.03 < 0.05
    verdict = run_judge(make_vertical(), policy, account, chain=chain)
    assert not verdict.approved
    assert not get_check(verdict, "leg_price_min").ok


def test_leg_price_min_passes_normal_legs(policy, account):
    assert get_check(run_judge(make_vertical(), policy, account), "leg_price_min").ok


# --------------------------------------------------------------------------- #
# timing
# --------------------------------------------------------------------------- #

def test_timing_blocks_0dte_after_cutoff(policy, account):
    late = datetime(2026, 9, 1, 15, 20, tzinfo=ET)  # past 15:10
    intent = make_vertical(is_0dte=True, expiry="2026-09-01")
    verdict = run_judge(intent, policy, account, now=late)
    assert not verdict.approved
    assert not get_check(verdict, "timing").ok


def test_timing_allows_0dte_before_cutoff(policy, account):
    early = datetime(2026, 9, 1, 14, 0, tzinfo=ET)
    intent = make_vertical(is_0dte=True, expiry="2026-09-01")
    verdict = run_judge(intent, policy, account, now=early)
    assert get_check(verdict, "timing").ok


def test_timing_infers_0dte_from_expiry(policy, account):
    # expiry == today makes it 0DTE even when the flag was forgotten
    late = datetime(2026, 9, 4, 10, 0, tzinfo=ET)
    intent = make_vertical(is_0dte=False, expiry="2026-09-04")
    # 10:00 on final day is before all cutoffs -> ok
    assert get_check(run_judge(intent, policy, account, now=late), "timing").ok
    # but the same intent at 15:20 on 2026-09-01 with expiry that day is blocked
    verdict = run_judge(
        make_vertical(is_0dte=False, expiry="2026-09-01"),
        policy, account, now=datetime(2026, 9, 1, 15, 20, tzinfo=ET),
    )
    assert not get_check(verdict, "timing").ok


def test_timing_blocks_all_new_entries_late_day(policy, account):
    late = datetime(2026, 9, 1, 15, 50, tzinfo=ET)  # past 15:45
    verdict = run_judge(make_vertical(), policy, account, now=late)
    assert not get_check(verdict, "timing").ok

    just_before = datetime(2026, 9, 1, 15, 44, tzinfo=ET)
    assert get_check(run_judge(make_vertical(), policy, account, now=just_before), "timing").ok


def test_timing_final_day_all_cash(policy, account):
    # final day 2026-09-04, all-cash by 10:30 ET
    after = datetime(2026, 9, 4, 10, 45, tzinfo=ET)
    verdict = run_judge(make_vertical(), policy, account, now=after)
    assert not verdict.approved
    assert not get_check(verdict, "timing").ok

    nfp_window = datetime(2026, 9, 4, 9, 35, tzinfo=ET)  # post-NFP open play still allowed
    assert get_check(run_judge(make_vertical(), policy, account, now=nfp_window), "timing").ok

    post_contest = datetime(2026, 9, 5, 9, 35, tzinfo=ET)
    assert not get_check(run_judge(make_vertical(), policy, account, now=post_contest), "timing").ok


def test_timing_naive_datetime_treated_as_et(policy, account):
    naive_late = datetime(2026, 9, 1, 15, 50)  # no tzinfo -> assumed ET
    verdict = run_judge(make_vertical(), policy, account, now=naive_late)
    assert not get_check(verdict, "timing").ok


# --------------------------------------------------------------------------- #
# sane_limit_price
# --------------------------------------------------------------------------- #

def test_sane_limit_price_happy(policy, account):
    verdict = run_judge(make_vertical(limit_price=1.05), policy, account)
    assert get_check(verdict, "sane_limit_price").ok


def test_sane_limit_price_rejects_fat_finger(policy, account):
    # net mid is +1.00; 2.00 is 2x — outside [0.5x, 1.5x]
    intent = make_vertical(limit_price=2.00, max_loss_usd=400.0)
    verdict = run_judge(intent, policy, account)
    assert not verdict.approved
    assert not get_check(verdict, "sane_limit_price").ok


def test_sane_limit_price_rejects_sign_mismatch_with_structure(policy, account):
    debit_negative = make_vertical(limit_price=-1.05)
    assert not get_check(run_judge(debit_negative, policy, account), "sane_limit_price").ok

    credit_positive = make_credit_vertical(limit_price=0.95)
    assert not get_check(run_judge(credit_positive, policy, account), "sane_limit_price").ok


def test_sane_limit_price_rejects_zero(policy, account):
    verdict = run_judge(make_vertical(limit_price=0.0), policy, account)
    assert not get_check(verdict, "sane_limit_price").ok


def test_sane_limit_price_band_edges_inclusive(policy, account):
    # net mid +1.00 -> 0.50 and 1.50 sit exactly on the band edges
    assert get_check(run_judge(make_vertical(limit_price=0.50), policy, account),
                     "sane_limit_price").ok
    assert get_check(run_judge(make_vertical(limit_price=1.50), policy, account),
                     "sane_limit_price").ok


def test_sane_limit_price_skips_band_when_mid_unavailable(policy, account):
    chain = make_chain()
    del chain[C655]  # net mid can no longer be computed
    verdict = run_judge(make_vertical(limit_price=1.05), policy, account, chain=chain)
    check = get_check(verdict, "sane_limit_price")
    assert check.ok
    assert "unavailable" in check.detail.lower()
    # structure sign is still enforced without a mid
    bad = run_judge(make_vertical(limit_price=-1.05), policy, account, chain=chain)
    assert not get_check(bad, "sane_limit_price").ok


# --------------------------------------------------------------------------- #
# qty_positive
# --------------------------------------------------------------------------- #

def test_qty_positive_pass_and_fail(policy, account):
    assert get_check(run_judge(make_vertical(qty=1, max_loss_usd=105.0), policy, account),
                     "qty_positive").ok
    verdict = run_judge(make_vertical(qty=0), policy, account)
    assert not verdict.approved
    assert not get_check(verdict, "qty_positive").ok


# --------------------------------------------------------------------------- #
# mleg_rules
# --------------------------------------------------------------------------- #

def test_mleg_rules_happy_vertical(policy, account):
    assert get_check(run_judge(make_vertical(), policy, account), "mleg_rules").ok


def test_mleg_rules_rejects_five_legs():
    five = make_vertical(legs=[
        Leg(C650, Side.BUY, 1, PositionIntent.BUY_TO_OPEN),
        Leg(C655, Side.SELL, 1, PositionIntent.SELL_TO_OPEN),
        Leg(C660, Side.BUY, 1, PositionIntent.BUY_TO_OPEN),
        Leg(C665, Side.SELL, 1, PositionIntent.SELL_TO_OPEN),
        Leg(C670, Side.BUY, 1, PositionIntent.BUY_TO_OPEN),
    ])
    assert not risk.check_mleg_rules(five).ok


def test_mleg_rules_rejects_non_coprime_ratios():
    bad = make_vertical(legs=[
        Leg(C650, Side.BUY, 2, PositionIntent.BUY_TO_OPEN),
        Leg(C655, Side.SELL, 4, PositionIntent.SELL_TO_OPEN),
    ])
    check = risk.check_mleg_rules(bad)
    assert not check.ok
    assert "coprime" in check.detail.lower()


def test_mleg_rules_accepts_coprime_ratios():
    good = make_vertical(legs=[
        Leg(C650, Side.BUY, 2, PositionIntent.BUY_TO_OPEN),
        Leg(C655, Side.SELL, 3, PositionIntent.SELL_TO_OPEN),
    ])
    assert risk.check_mleg_rules(good).ok


def test_mleg_rules_rejects_duplicate_symbols():
    dup = make_vertical(legs=[
        Leg(C650, Side.BUY, 1, PositionIntent.BUY_TO_OPEN),
        Leg(C650, Side.SELL, 1, PositionIntent.SELL_TO_OPEN),
    ])
    assert not risk.check_mleg_rules(dup).ok


def test_mleg_rules_single_leg_needs_ratio_one():
    bad = make_vertical(legs=[Leg(C650, Side.BUY, 2, PositionIntent.BUY_TO_OPEN)])
    assert not risk.check_mleg_rules(bad).ok
    good = make_vertical(legs=[Leg(C650, Side.BUY, 1, PositionIntent.BUY_TO_OPEN)])
    assert risk.check_mleg_rules(good).ok


def test_mleg_rules_rejects_bad_occ_symbol():
    bad = make_vertical(legs=[
        Leg("SPY-NOT-OCC", Side.BUY, 1, PositionIntent.BUY_TO_OPEN),
        Leg(C655, Side.SELL, 1, PositionIntent.SELL_TO_OPEN),
    ])
    assert not risk.check_mleg_rules(bad).ok


# --------------------------------------------------------------------------- #
# CLOSE intents: bypass new-risk gates, keep sanity gates
# --------------------------------------------------------------------------- #

def test_close_approved_despite_halts_caps_and_cutoffs(policy, account):
    """Worst plausible book state: halted, at position and concentration limits,
    deep in the red, after the entry cutoff — flattening must still be allowed."""
    state = fresh_state(
        halted_today=True,
        day_pnl_pct=-20.0,
        week_pnl_pct=-35.0,
        open_position_count=3,
        underlying_exposure={"SPY": 70_000.0},
    )
    late = datetime(2026, 9, 1, 15, 50, tzinfo=ET)
    verdict = run_judge(make_close(), policy, account, state=state, now=late)
    assert verdict.approved, verdict.reasons
    for name in ("per_trade_cap", "daily_halt", "weekly_halt", "concurrency",
                 "concentration", "spread_quality", "open_interest",
                 "leg_price_min", "timing"):
        check = get_check(verdict, name)
        assert check.ok
        assert "skip" in check.detail.lower()


def test_close_keeps_paper_gate(policy, account):
    live = copy.deepcopy(policy)
    live["trading_base"] = "https://api.alpaca.markets"
    verdict = run_judge(make_close(), live, account)
    assert not verdict.approved
    assert not get_check(verdict, "paper_gate").ok


def test_close_keeps_sane_limit_price(policy, account):
    # closing the long vertical nets a credit (net mid -1.00): +0.95 is wrong-signed
    wrong_sign = run_judge(make_close(limit_price=0.95), policy, account)
    assert not wrong_sign.approved
    assert not get_check(wrong_sign, "sane_limit_price").ok

    # 5x the net mid is a fat finger even on a close
    fat = run_judge(make_close(limit_price=-5.0), policy, account)
    assert not get_check(fat, "sane_limit_price").ok


def test_close_keeps_qty_and_mleg_rules(policy, account):
    assert not run_judge(make_close(qty=0), policy, account).approved
    dup = make_close(legs=[
        Leg(C650, Side.SELL, 1, PositionIntent.SELL_TO_CLOSE),
        Leg(C650, Side.BUY, 1, PositionIntent.BUY_TO_CLOSE),
    ])
    verdict = run_judge(dup, policy, account)
    assert not verdict.approved
    assert not get_check(verdict, "mleg_rules").ok


def test_close_allows_zero_max_loss(policy, account):
    verdict = run_judge(make_close(max_loss_usd=0.0), policy, account)
    assert get_check(verdict, "defined_risk").ok
    assert verdict.approved, verdict.reasons


# --------------------------------------------------------------------------- #
# determinism
# --------------------------------------------------------------------------- #

def test_judge_is_deterministic(policy, account):
    intent = make_vertical()
    v1 = run_judge(intent, policy, account)
    v2 = run_judge(intent, policy, account)
    assert v1.to_dict() == v2.to_dict()
