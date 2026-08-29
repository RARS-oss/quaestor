"""Deterministic risk gates for quaestor — the last line of defense before any order.

``judge()`` evaluates a TradeIntent against the signed policy (configs/policy.yaml)
and returns a RiskVerdict listing every gate that was consulted. The module is pure
and deterministic: no I/O, no clock reads (``now`` is passed in), no randomness —
identical inputs always produce the identical verdict, which is what makes the
bulla-receipted decision step replayable and auditable.

Alpaca facts encoded here (verified 2026-08-28):
- Paper-only operation: orders may only ever target https://paper-api.alpaca.markets.
  The gate fails closed if the policy dict carries a live trading base or live flag.
- mleg orders: max 4 legs, ratio_qty values coprime across legs (GCD == 1), unique
  leg symbols, net limit_price signed per strategy unit (positive = debit, negative
  = credit), every short leg must be covered within the same order (defined-risk
  only — naked shorts are rejected), TIF is always "day".
- Options quotes come from the free "indicative" feed (a randomized OPRA
  derivative): greeks/IV/OI may be None for illiquid contracts. The sane-price band
  (|limit| within 0.5x..1.5x of the chain's net mid) and the per-leg spread-quality
  gate absorb that quote noise while still catching fat-finger limits.
- Option symbols are OCC format: ROOT + YYMMDD + C/P + strike*1000 zero-padded to 8.

Unit conventions: policy percentages are percent points (10 means 10%), and
``portfolio_state`` day_pnl_pct / week_pnl_pct use the same convention (-3.2 means
down 3.2% versus the day/week open equity).

``portfolio_state`` shape::

    {"halted_today": bool, "day_pnl_pct": float, "open_position_count": int,
     "underlying_exposure": {root: usd}, "week_pnl_pct": float}

CLOSE intents (Structure.CLOSE) bypass the gates that exist to limit NEW risk
(per-trade cap, daily/weekly halts, concurrency, concentration, entry timing, and
the entry liquidity screens: spread quality / open interest / min leg price) —
blocking an exit is itself a risk. They keep every sanity gate: paper_gate,
structure_allowed, defined_risk, sane_limit_price, qty_positive, mleg_rules.
Bypassed gates still appear in the verdict as ok=True with a "skipped" detail so
the audit trail shows every gate was consulted.
"""
from __future__ import annotations

import math
from datetime import date, datetime, time as dtime
from functools import reduce
from typing import Any
from zoneinfo import ZoneInfo

from quaestor.models import (
    AccountSnapshot,
    Leg,
    PositionIntent,
    RiskCheck,
    RiskVerdict,
    Side,
    Structure,
    TradeIntent,
)

ET = ZoneInfo("America/New_York")
PAPER_BASE = "https://paper-api.alpaca.markets"

# Structures whose net limit must be a debit (limit_price > 0).
_DEBIT_STRUCTURES = frozenset(
    {Structure.LONG_CALL, Structure.LONG_PUT, Structure.VERTICAL_DEBIT, Structure.STRADDLE}
)
# Structures whose net limit must be a credit (limit_price < 0).
_CREDIT_STRUCTURES = frozenset({Structure.VERTICAL_CREDIT})

_EPS = 1e-9
SANE_PRICE_BAND = (0.5, 1.5)  # |limit| must be within this multiple of |net mid|
MAX_MLEG_LEGS = 4             # Alpaca mleg hard limit


# --------------------------------------------------------------------------- #
# small pure helpers
# --------------------------------------------------------------------------- #

def _as_et(now: datetime) -> datetime:
    """Normalize the injected timestamp to ET. Naive datetimes are assumed ET."""
    if now.tzinfo is None:
        return now.replace(tzinfo=ET)
    return now.astimezone(ET)


def _parse_hhmm(s: str) -> dtime:
    hh, mm = str(s).split(":")
    return dtime(int(hh), int(mm))


def _sign(x: float) -> int:
    return 1 if x > 0 else (-1 if x < 0 else 0)


def _occ_valid(symbol: str) -> bool:
    """True when symbol parses as OCC: ROOT + YYMMDD + C/P + 8-digit strike*1000."""
    if len(symbol) < 16:
        return False
    strike, cp, exp, root = symbol[-8:], symbol[-9], symbol[-15:-9], symbol[:-15]
    return strike.isdigit() and cp in "CP" and exp.isdigit() and root.isalnum()


def _occ_root(symbol: str) -> str:
    return symbol[:-15]


def _occ_type(symbol: str) -> str:
    return symbol[-9]


def _leg_mid(snap: dict[str, Any] | None) -> float | None:
    """Best mid for one leg's snapshot dict; None when no usable quote exists."""
    if snap is None:
        return None
    mid = snap.get("mid")
    if isinstance(mid, (int, float)) and math.isfinite(mid) and mid > 0:
        return float(mid)
    bid, ask = snap.get("bid"), snap.get("ask")
    if isinstance(bid, (int, float)) and isinstance(ask, (int, float)):
        computed = (float(bid) + float(ask)) / 2.0
        if math.isfinite(computed) and computed > 0:
            return computed
    return None


def _net_mid(intent: TradeIntent, chain: dict[str, dict]) -> float | None:
    """Signed net mid per strategy unit: +mid for BUY legs, -mid for SELL legs,
    weighted by ratio_qty. None when any leg lacks a usable quote."""
    total = 0.0
    for leg in intent.legs:
        mid = _leg_mid(chain.get(leg.symbol))
        if mid is None:
            return None
        signed = mid if leg.side is Side.BUY else -mid
        total += signed * leg.ratio_qty
    return total


def _skip_for_close(name: str) -> RiskCheck:
    return RiskCheck(
        name=name, ok=True,
        detail="skipped: close intent — gate applies to opening trades only",
    )


# --------------------------------------------------------------------------- #
# the gates — one small pure function per check, each returns a RiskCheck
# --------------------------------------------------------------------------- #

def check_paper_gate(policy: dict) -> RiskCheck:
    """Refuse anything that is not the paper endpoint. config.load_settings()
    fail-closes at startup; this re-asserts it inside the receipted decision.
    The agent injects policy["trading_base"] = settings.trading_base after load;
    an absent key defaults to the paper base (the only base Settings allows)."""
    name = "paper_gate"
    base = str(policy.get("trading_base", PAPER_BASE))
    live_flag = bool(policy.get("live_trade", False))
    if live_flag:
        return RiskCheck(name, False, "live_trade flag is set — paper-only agent refuses")
    if "paper-api.alpaca.markets" not in base:
        return RiskCheck(name, False, f"trading base {base!r} is not the paper endpoint")
    return RiskCheck(name, True, f"paper endpoint confirmed ({base})")


def check_structure_allowed(intent: TradeIntent, policy: dict) -> RiskCheck:
    name = "structure_allowed"
    allowed = [str(s) for s in policy["structures"].get("allowed", [])]
    if intent.structure.value not in allowed:
        return RiskCheck(
            name, False,
            f"structure {intent.structure.value!r} not in allowed set {allowed}",
        )
    return RiskCheck(name, True, f"structure {intent.structure.value!r} allowed")


def check_defined_risk(intent: TradeIntent, policy: dict) -> RiskCheck:
    """max_loss_usd must be finite, and every SELL_TO_OPEN leg must be covered by
    BUY_TO_OPEN legs of the same OCC root and option type within the same order
    (Alpaca mleg covers shorts in-order; a naked short is undefined risk)."""
    name = "defined_risk"
    is_close = intent.structure is Structure.CLOSE
    ml = intent.max_loss_usd
    if not isinstance(ml, (int, float)) or not math.isfinite(ml):
        return RiskCheck(name, False, f"max_loss_usd is not finite ({ml!r})")
    if is_close:
        if ml < 0:
            return RiskCheck(name, False, f"max_loss_usd negative ({ml})")
    elif ml <= 0:
        return RiskCheck(name, False, f"max_loss_usd must be > 0 for opening trades ({ml})")

    if policy["structures"].get("defined_risk_only", True):
        shorts: dict[tuple[str, str], int] = {}
        covers: dict[tuple[str, str], int] = {}
        for leg in intent.legs:
            if not _occ_valid(leg.symbol):
                return RiskCheck(name, False, f"leg symbol {leg.symbol!r} is not valid OCC")
            key = (_occ_root(leg.symbol), _occ_type(leg.symbol))
            if leg.position_intent is PositionIntent.SELL_TO_OPEN:
                shorts[key] = shorts.get(key, 0) + leg.ratio_qty
            elif leg.position_intent is PositionIntent.BUY_TO_OPEN:
                covers[key] = covers.get(key, 0) + leg.ratio_qty
        for key, short_qty in shorts.items():
            if covers.get(key, 0) < short_qty:
                return RiskCheck(
                    name, False,
                    f"naked short: {short_qty} short {key[1]} on {key[0]} covered by "
                    f"{covers.get(key, 0)} long — defined_risk_only forbids it",
                )
    return RiskCheck(name, True, f"defined risk: worst case ${ml:.2f}, all shorts covered")


def check_per_trade_cap(intent: TradeIntent, policy: dict, account: AccountSnapshot) -> RiskCheck:
    """Worst-case loss capped at a % of current equity; catalyst-tagged intents
    (intent.catalyst_tag != \"\") get the larger conviction cap."""
    name = "per_trade_cap"
    if intent.structure is Structure.CLOSE:
        return _skip_for_close(name)
    pt = policy["per_trade"]
    if intent.structure in _CREDIT_STRUCTURES:
        # Premium selling is sized small regardless of any catalyst tag — this is the
        # independent backstop for mine #1 (an oversized income condor is the sleeve's
        # deadliest failure). Default 2.5% if the policy predates the income cap.
        cap_pct = float(pt.get("max_loss_pct_income", 2.5))
        which = "income"
    elif intent.catalyst_tag:
        cap_pct = float(pt["max_loss_pct_catalyst"])
        which = f"catalyst {intent.catalyst_tag!r}"
    else:
        cap_pct = float(pt["max_loss_pct_default"])
        which = "default"
    cap_usd = account.equity * cap_pct / 100.0
    if intent.max_loss_usd > cap_usd + _EPS:
        return RiskCheck(
            name, False,
            f"max_loss ${intent.max_loss_usd:.2f} exceeds {which} cap ${cap_usd:.2f} "
            f"({cap_pct:g}% of equity ${account.equity:.2f})",
        )
    return RiskCheck(
        name, True,
        f"max_loss ${intent.max_loss_usd:.2f} within {which} cap ${cap_usd:.2f} ({cap_pct:g}%)",
    )


def check_aggregate_risk(intent: TradeIntent, policy: dict, account: AccountSnapshot,
                         portfolio_state: dict) -> RiskCheck:
    """The 'cannot blow up' gate: total premium-at-risk (already-deployed +
    this intent's worst-case loss) must stay under account.max_open_risk_pct of
    equity. Since every position is defined-risk long premium, this hard-bounds
    the worst possible loss of the whole book — so we can push convexity
    aggressively without ever being able to zero the account."""
    name = "aggregate_risk"
    if intent.structure is Structure.CLOSE:
        return _skip_for_close(name)
    acct = policy.get("account", {})
    cap_pct = float(acct.get("max_open_risk_pct", 55))
    cap_usd = account.equity * cap_pct / 100.0
    deployed = float(portfolio_state.get("open_risk_usd", 0.0))
    projected = deployed + intent.max_loss_usd
    if projected > cap_usd + _EPS:
        return RiskCheck(
            name, False,
            f"open premium-at-risk ${projected:.2f} (${deployed:.2f} live + "
            f"${intent.max_loss_usd:.2f} new) exceeds budget ${cap_usd:.2f} ({cap_pct:g}% of equity)",
        )
    return RiskCheck(
        name, True,
        f"premium-at-risk ${projected:.2f} within budget ${cap_usd:.2f} ({cap_pct:g}%)",
    )


def check_daily_halt(intent: TradeIntent, policy: dict, portfolio_state: dict) -> RiskCheck:
    """No NEW positions once day P&L is at/below -daily_loss_halt_pct or the
    halted flag is latched. Closes stay allowed — de-risking is never blocked."""
    name = "daily_halt"
    if intent.structure is Structure.CLOSE:
        return _skip_for_close(name)
    halt_pct = float(policy["account"]["daily_loss_halt_pct"])
    day_pnl = float(portfolio_state.get("day_pnl_pct", 0.0))
    if bool(portfolio_state.get("halted_today", False)):
        return RiskCheck(name, False, "trading halted for the day (halted_today latched)")
    if day_pnl <= -halt_pct:
        return RiskCheck(
            name, False,
            f"day P&L {day_pnl:+.2f}% at/below halt threshold -{halt_pct:g}%",
        )
    return RiskCheck(name, True, f"day P&L {day_pnl:+.2f}% above halt threshold -{halt_pct:g}%")


def check_weekly_halt(intent: TradeIntent, policy: dict, portfolio_state: dict) -> RiskCheck:
    """Hard stop for the rest of the contest once week P&L breaches the weekly cap."""
    name = "weekly_halt"
    if intent.structure is Structure.CLOSE:
        return _skip_for_close(name)
    halt_pct = float(policy["account"]["weekly_loss_halt_pct"])
    week_pnl = float(portfolio_state.get("week_pnl_pct", 0.0))
    if bool(portfolio_state.get("halted_week", False)):
        return RiskCheck(
            name, False,
            "weekly hard stop latched — no new positions for the rest of the contest",
        )
    if week_pnl <= -halt_pct:
        return RiskCheck(
            name, False,
            f"week P&L {week_pnl:+.2f}% at/below weekly hard stop -{halt_pct:g}%",
        )
    return RiskCheck(name, True, f"week P&L {week_pnl:+.2f}% above weekly hard stop -{halt_pct:g}%")


def check_concurrency(intent: TradeIntent, policy: dict, portfolio_state: dict) -> RiskCheck:
    name = "concurrency"
    if intent.structure is Structure.CLOSE:
        return _skip_for_close(name)
    max_pos = int(policy["account"]["max_concurrent_positions"])
    open_count = int(portfolio_state.get("open_position_count", 0))
    if open_count >= max_pos:
        return RiskCheck(
            name, False,
            f"{open_count} open positions >= max_concurrent_positions {max_pos}",
        )
    return RiskCheck(name, True, f"{open_count} open positions < limit {max_pos}")


def check_concentration(
    intent: TradeIntent, policy: dict, account: AccountSnapshot, portfolio_state: dict,
    chain: dict[str, dict] | None = None,
) -> RiskCheck:
    """Projected per-underlying exposure must stay under the underlying's %-of-equity cap.

    The held side (portfolio.underlying_exposure) measures GROSS per-leg
    |market_value|, so the projection must use the same metric: sum of per-leg
    mids * ratio * 100 * qty. Net premium (|limit|) understates a spread's
    measured exposure ~3x and would certify caps the fill immediately breaches.
    Falls back to |limit| only when a leg mid is unavailable."""
    name = "concentration"
    if intent.structure is Structure.CLOSE:
        return _skip_for_close(name)
    caps = policy["account"]["max_underlying_concentration_pct"]
    cap_pct = float(caps.get(intent.underlying, caps.get("default", 100)))
    cap_usd = account.equity * cap_pct / 100.0
    current = float(portfolio_state.get("underlying_exposure", {}).get(intent.underlying, 0.0))
    gross_unit: float | None = 0.0
    for leg in intent.legs:
        mid = _leg_mid((chain or {}).get(leg.symbol))
        if mid is None:
            gross_unit = None
            break
        gross_unit += abs(mid) * leg.ratio_qty
    if gross_unit is None:
        added = abs(intent.limit_price) * 100.0 * intent.qty  # fallback: net premium
    else:
        added = gross_unit * 100.0 * intent.qty
    projected = current + added
    if projected > cap_usd + _EPS:
        return RiskCheck(
            name, False,
            f"{intent.underlying} projected exposure ${projected:.2f} "
            f"(${current:.2f} held + ${added:.2f} new) exceeds cap ${cap_usd:.2f} ({cap_pct:g}%)",
        )
    return RiskCheck(
        name, True,
        f"{intent.underlying} projected exposure ${projected:.2f} within cap ${cap_usd:.2f} ({cap_pct:g}%)",
    )


def check_spread_quality(intent: TradeIntent, policy: dict, chain: dict[str, dict]) -> RiskCheck:
    """(ask - bid) / mid must be <= max_spread_quality on EVERY leg. A leg with no
    usable quote fails closed — we never open into an unquotable contract."""
    name = "spread_quality"
    if intent.structure is Structure.CLOSE:
        return _skip_for_close(name)
    max_q = float(policy["per_trade"]["max_spread_quality"])
    # A leg also passes when its ABSOLUTE spread is tiny — a penny/2-cent spread on
    # a cheap far-OTM wing is fine to trade even though (ask-bid)/mid looks large.
    max_abs = float(policy["per_trade"].get("max_abs_spread", 0.05))
    worst = 0.0
    for leg in intent.legs:
        snap = chain.get(leg.symbol)
        if snap is None:
            return RiskCheck(name, False, f"no quote in chain for leg {leg.symbol}")
        bid, ask = snap.get("bid"), snap.get("ask")
        mid = _leg_mid(snap)
        if not isinstance(bid, (int, float)) or not isinstance(ask, (int, float)) or mid is None:
            return RiskCheck(name, False, f"unusable quote for leg {leg.symbol} (bid/ask/mid missing)")
        abs_spread = float(ask) - float(bid)
        quality = abs_spread / mid
        worst = max(worst, quality)
        if quality > max_q + _EPS and abs_spread > max_abs + _EPS:
            return RiskCheck(
                name, False,
                f"leg {leg.symbol} spread quality {quality:.4f} > max {max_q:g} "
                f"(bid {bid} / ask {ask})",
            )
    return RiskCheck(name, True, f"all legs spread quality <= {max_q:g} (worst {worst:.4f})")


def check_open_interest(intent: TradeIntent, policy: dict, chain: dict[str, dict]) -> RiskCheck:
    """Every leg with a reported OI must have OI >= min_open_interest. The
    indicative feed may omit OI (None) — that leg passes with a note; the
    universe filter is the primary OI screen."""
    name = "open_interest"
    if intent.structure is Structure.CLOSE:
        return _skip_for_close(name)
    min_oi = int(policy["per_trade"]["min_open_interest"])
    unreported: list[str] = []
    for leg in intent.legs:
        snap = chain.get(leg.symbol)
        if snap is None:
            return RiskCheck(name, False, f"no chain snapshot for leg {leg.symbol}")
        oi = snap.get("oi")
        if oi is None:
            unreported.append(leg.symbol)
            continue
        if int(oi) < min_oi:
            return RiskCheck(
                name, False, f"leg {leg.symbol} open interest {int(oi)} < min {min_oi}"
            )
    if unreported:
        return RiskCheck(
            name, True,
            f"OI >= {min_oi} where reported; unreported on indicative feed for {unreported}",
        )
    return RiskCheck(name, True, f"all legs open interest >= {min_oi}")


def check_leg_price_min(intent: TradeIntent, policy: dict, chain: dict[str, dict]) -> RiskCheck:
    """Every leg mid must be >= min_leg_price — no sub-nickel junk contracts."""
    name = "leg_price_min"
    if intent.structure is Structure.CLOSE:
        return _skip_for_close(name)
    min_price = float(policy["per_trade"]["min_leg_price"])
    for leg in intent.legs:
        mid = _leg_mid(chain.get(leg.symbol))
        if mid is None:
            return RiskCheck(name, False, f"no usable quote to price leg {leg.symbol}")
        if mid < min_price - _EPS:
            return RiskCheck(
                name, False, f"leg {leg.symbol} mid {mid:.4f} < min_leg_price {min_price:g}"
            )
    return RiskCheck(name, True, f"all leg mids >= min_leg_price {min_price:g}")


def check_timing(intent: TradeIntent, policy: dict, now: datetime) -> RiskCheck:
    """Entry-timing gates (ET): no new 0DTE at/after no_new_0dte_after_et, no new
    positions at/after no_new_positions_after_et, and on/after the final contest
    day's all-cash cutoff nothing new opens at all. An intent expiring today is
    treated as 0DTE even if the is_0dte flag was not set."""
    name = "timing"
    if intent.structure is Structure.CLOSE:
        return _skip_for_close(name)
    t = policy["timing"]
    et_now = _as_et(now)
    final_day = date.fromisoformat(str(t["final_day"]))
    all_cash_cutoff = _parse_hhmm(t["final_day_all_cash_by_et"])
    no_new_cutoff = _parse_hhmm(t["no_new_positions_after_et"])
    no_0dte_cutoff = _parse_hhmm(t["no_new_0dte_after_et"])

    if et_now.date() > final_day:
        return RiskCheck(
            name, False, f"past final contest day {final_day.isoformat()} — all-cash, no new positions"
        )
    if et_now.date() == final_day and et_now.time() >= all_cash_cutoff:
        return RiskCheck(
            name, False,
            f"final-day all-cash cutoff {t['final_day_all_cash_by_et']} ET reached — no new positions",
        )
    if et_now.time() >= no_new_cutoff:
        return RiskCheck(
            name, False,
            f"past no_new_positions_after_et {t['no_new_positions_after_et']} ET "
            f"(now {et_now.strftime('%H:%M')} ET)",
        )
    is_0dte = intent.is_0dte or (intent.expiry == et_now.date().isoformat())
    if is_0dte and et_now.time() >= no_0dte_cutoff:
        return RiskCheck(
            name, False,
            f"0DTE entry past no_new_0dte_after_et {t['no_new_0dte_after_et']} ET "
            f"(now {et_now.strftime('%H:%M')} ET)",
        )
    return RiskCheck(name, True, f"entry timing ok at {et_now.strftime('%H:%M')} ET")


def check_sane_limit_price(intent: TradeIntent, chain: dict[str, dict]) -> RiskCheck:
    """The net limit must be finite and non-zero, its sign must match the
    structure (mleg convention: positive = debit, negative = credit), and when
    the chain provides a net mid, sign(limit) must equal sign(net mid) with
    |limit| within [0.5x, 1.5x] of |net mid| — wide enough for marketable-limit
    buffers on the noisy indicative feed, tight enough to stop fat fingers."""
    name = "sane_limit_price"
    lp = intent.limit_price
    if not isinstance(lp, (int, float)) or not math.isfinite(lp) or lp == 0:
        return RiskCheck(name, False, f"limit_price {lp!r} is not a finite non-zero net price")
    if intent.structure in _DEBIT_STRUCTURES and lp < 0:
        return RiskCheck(
            name, False,
            f"{intent.structure.value} must be a net debit (limit > 0), got {lp}",
        )
    if intent.structure in _CREDIT_STRUCTURES and lp > 0:
        return RiskCheck(
            name, False,
            f"{intent.structure.value} must be a net credit (limit < 0), got {lp}",
        )
    net_mid = _net_mid(intent, chain)
    if net_mid is None or abs(net_mid) < _EPS:
        return RiskCheck(
            name, True,
            f"limit {lp:+.2f} sign consistent with structure; net mid unavailable — band check skipped",
        )
    if _sign(lp) != _sign(net_mid):
        return RiskCheck(
            name, False,
            f"limit {lp:+.2f} sign contradicts chain net mid {net_mid:+.2f}",
        )
    lo, hi = SANE_PRICE_BAND
    ratio = abs(lp) / abs(net_mid)
    if not (lo - _EPS <= ratio <= hi + _EPS):
        return RiskCheck(
            name, False,
            f"|limit| {abs(lp):.2f} is {ratio:.2f}x |net mid| {abs(net_mid):.2f} — "
            f"outside sane band [{lo}x, {hi}x]",
        )
    return RiskCheck(
        name, True,
        f"limit {lp:+.2f} within [{lo}x, {hi}x] of net mid {net_mid:+.2f} ({ratio:.2f}x)",
    )


def check_qty_positive(intent: TradeIntent) -> RiskCheck:
    """qty is whole strategy units (contracts x ratio_qty); must be an int >= 1."""
    name = "qty_positive"
    if not isinstance(intent.qty, int) or isinstance(intent.qty, bool) or intent.qty < 1:
        return RiskCheck(name, False, f"qty must be a whole number >= 1, got {intent.qty!r}")
    return RiskCheck(name, True, f"qty {intent.qty} is a positive whole number")


def check_mleg_rules(intent: TradeIntent) -> RiskCheck:
    """Alpaca mleg constraints: at most 4 legs, unique OCC symbols, each
    ratio_qty an int >= 1, ratio_qtys coprime across legs (GCD == 1). A
    single-leg order must have ratio_qty == 1 (qty carries the size)."""
    name = "mleg_rules"
    legs = intent.legs
    if not legs:
        return RiskCheck(name, False, "intent has no legs")
    if len(legs) > MAX_MLEG_LEGS:
        return RiskCheck(name, False, f"{len(legs)} legs > Alpaca mleg max {MAX_MLEG_LEGS}")
    symbols = [leg.symbol for leg in legs]
    if len(set(symbols)) != len(symbols):
        return RiskCheck(name, False, f"duplicate leg symbols: {symbols}")
    for leg in legs:
        if not _occ_valid(leg.symbol):
            return RiskCheck(name, False, f"leg symbol {leg.symbol!r} is not valid OCC format")
        if not isinstance(leg.ratio_qty, int) or isinstance(leg.ratio_qty, bool) or leg.ratio_qty < 1:
            return RiskCheck(
                name, False, f"leg {leg.symbol} ratio_qty {leg.ratio_qty!r} must be an int >= 1"
            )
    ratios = [leg.ratio_qty for leg in legs]
    if len(legs) == 1:
        if ratios[0] != 1:
            return RiskCheck(
                name, False, f"single-leg order must use ratio_qty 1, got {ratios[0]}"
            )
        return RiskCheck(name, True, "single leg, valid OCC symbol, ratio_qty 1")
    gcd = reduce(math.gcd, ratios)
    if gcd != 1:
        return RiskCheck(
            name, False, f"ratio_qtys {ratios} share GCD {gcd} — must be coprime (GCD 1)"
        )
    return RiskCheck(
        name, True,
        f"{len(legs)} legs, unique OCC symbols, ratio_qtys {ratios} coprime",
    )


# --------------------------------------------------------------------------- #
# the composed verdict
# --------------------------------------------------------------------------- #

def judge(
    intent: TradeIntent,
    *,
    policy: dict,
    account: AccountSnapshot,
    portfolio_state: dict,
    chain: dict[str, dict],
    now: datetime,
) -> RiskVerdict:
    """Run every gate against one intent and return the full verdict.

    approved is True only when every check is ok. All gates always run (no
    short-circuit) so the audit trail records the complete picture. The
    policy digest (sha256 of configs/policy.yaml bytes, provided by
    config.load_policy() under the "digest" key) is stamped into the verdict.
    """
    checks: list[RiskCheck] = [
        check_paper_gate(policy),
        check_structure_allowed(intent, policy),
        check_defined_risk(intent, policy),
        check_per_trade_cap(intent, policy, account),
        check_aggregate_risk(intent, policy, account, portfolio_state),
        check_daily_halt(intent, policy, portfolio_state),
        check_weekly_halt(intent, policy, portfolio_state),
        check_concurrency(intent, policy, portfolio_state),
        check_concentration(intent, policy, account, portfolio_state, chain),
        check_spread_quality(intent, policy, chain),
        check_open_interest(intent, policy, chain),
        check_leg_price_min(intent, policy, chain),
        check_timing(intent, policy, now),
        check_sane_limit_price(intent, chain),
        check_qty_positive(intent),
        check_mleg_rules(intent),
    ]
    return RiskVerdict(
        approved=all(c.ok for c in checks),
        checks=checks,
        policy_digest=str(policy.get("digest", "")),
        intent_id=intent.intent_id,
    )
