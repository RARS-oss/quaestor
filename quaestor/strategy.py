"""The quaestor brain: three deterministic playbooks -> list[TradeIntent].

Role: step 4 of the decision cycle. decide(ctx) reads signals, sentiment,
option chains and open positions and proposes fully-specified TradeIntents.
It NEVER talks to the network; risk.judge() gates every intent afterwards and
broker.execute() does the marketable-limit dance.

A regime-gated BARBELL: sell defined-risk premium (income) on machine-classified
range days, buy convexity on trend days, stand down in a storm — so the book has
a source of P&L in a quiet week AND right-tail convexity on moves. Entries are
gated per underlying by quaestor.regime (the one component with a documented live
P&L effect); the aggregate-risk gate in risk.judge hard-bounds the whole book.

Playbooks:
1b. Income sleeve (RANGE days only): 0-1DTE iron condor, shorts ~0.18d, defined
   risk, net credit; take 55% / stop 2.2x / 0DTE curfew. The quiet-week earner.
The convex sleeve (TREND days, all defined-risk long premium):
0. High-conviction directional long (strength >= 0.78, non-opposing sentiment):
   a single near-the-money 0-1 DTE call/put — convex upside, sized to the
   conviction cap. The bet that makes iks.
1. Catalyst play (tag from ctx.due_events, e.g. NFP_OPEN_PLAY): DIRECTIONAL long
   when a lean is confirmed (higher-EV than paying for both sides), else a long
   ATM 0DTE straddle. Sized to the catalyst cap.
2. Core momentum debit vertical (SPY/QQQ, medium conviction >= 0.55): buy
   ~0.45-delta / sell ~0.25-delta, 0-3 DTE, sized to the per-trade cap.
3. Exit management: CLOSE intents when unrealized <= -50% of debit (stop),
   >= +150% (let convex winners run), 0DTE near policy flat_0dte_by_et, or the
   ALL_CASH event is due (final day -> close everything, no new entries).
decide() picks at most ONE fresh entry per underlying per cycle, priority
catalyst > conviction > vertical.

Max-loss math (per spec): debit vertical -> debit*100*qty; credit vertical ->
(width-credit)*100*qty; straddle/single -> debit*100*qty.

Alpaca facts encoded:
- OCC option symbols: ROOT + YYMMDD + C/P + strike*1000 zero-padded to 8 digits.
  Strategy only SELECTS symbols already present in the normalized chain
  (data.option_chain, indicative feed) — it never constructs new ones.
- mleg rules: max 4 legs (we use 2), ratio_qty coprime across legs (all 1:1
  here), NET limit_price signed (+debit / -credit), TIF=day (orders.py).
- Indicative-feed greeks/iv can be None on illiquid strikes; every selector
  guards and falls back (ATM by call/put mid parity when deltas are missing).
- Paper fills only at marketable NBBO-touch prices: strategy quotes chain
  mids/touches; orders.marketable_limit applies the execution buffer.
- CLOSE intents reverse the position side with explicit *_TO_CLOSE
  position_intent; sell-to-close carries a negative (credit) limit_price,
  buy-to-close a positive (debit) one; the price comes from the current chain
  touch (bid to sell, ask to buy).
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from statistics import NormalDist
from typing import TYPE_CHECKING, Any, Iterator
from zoneinfo import ZoneInfo

from quaestor import regime as regime_mod
from quaestor.models import (
    AccountSnapshot,
    Leg,
    PositionIntent,
    Side,
    Structure,
    TradeIntent,
)
from quaestor.signals import Signal

if TYPE_CHECKING:  # pragma: no cover - other slices; types only
    from quaestor.config import Settings
    from quaestor.portfolio import PortfolioState

ET = ZoneInfo("America/New_York")

# --- playbook constants ------------------------------------------------------
CORE_UNDERLYINGS: tuple[str, ...] = ("SPY", "QQQ")
MIN_VERTICAL_STRENGTH: float = 0.55       # medium conviction -> defined-risk vertical
CONVICTION_STRENGTH: float = 0.78         # high conviction -> a convex directional LONG
CATALYST_LEAN_STRENGTH: float = 0.45      # a lean this strong turns a catalyst directional
VERTICAL_LONG_DELTA: float = 0.45
VERTICAL_SHORT_DELTA: float = 0.25
CONVICTION_DELTA: float = 0.45            # directional long strike (near-the-money convexity)
VERTICAL_TARGET_DTE: int = 1
VERTICAL_MAX_DTE: int = 3
ATM_DELTA: float = 0.50
STOP_PLPC: float = -0.5          # close at -50% of debit
TARGET_PLPC: float = 1.5         # let convex winners run further (+150% of debit)
# Income sleeve (range days): sell defined-risk premium, take 55%, stop 2.2x credit.
CONDOR_SHORT_DELTA: float = 0.18     # short strikes ~1x expected move
CONDOR_LONG_DELTA: float = 0.08      # protection wings further OTM
MIN_CONDOR_CREDIT: float = 0.15      # absolute floor: skip sub-15c junk (VIX ~14)
MIN_CONDOR_CREDIT_FRAC: float = 0.20  # AND credit must be >= 20% of the wing width.
#   Penny premium is the quiet-week trap (mine #3): a $5-wide condor for $0.20 risks
#   $4.80 to make $0.20. The width-relative floor rejects that — a $5 condor now needs
#   >= $1.00 credit (risk $4 for $1, ~25% on risk) or we don't sell it at all.
INCOME_TAKE_FRAC: float = 0.55       # buy back once 55% of the credit is captured
INCOME_STOP_MULT: float = 2.2        # stop when it costs 2.2x the credit to close
FLAT_0DTE_LEAD_MIN: int = 10     # start flattening 0DTE this many min before deadline
MIN_0DTE_RUNWAY_MIN: int = 40    # a NEW 0DTE income position needs at least this long
                                 # before the flatten window opens, or it exists only to
                                 # pay the spread twice: measured 2026-09-01, a condor
                                 # entered 15:06 was curfew-closed 15:16 for a net -$64 —
                                 # collected 0.29, paid 0.31, ten minutes of life.
FLATTEN_TAG: str = "ALL_CASH"
# IM/RM gate on the long catalyst STRADDLE: only pay for both sides of event vol when
# it is genuinely cheap vs realized. IM = straddle price / spot (the move the chain is
# pricing); RM = recent realized daily move. Buy iff IM < IM_RM_CHEAP * RM. Otherwise
# the straddle just bleeds theta (the quiet-day trap). This guards ONLY the straddle —
# a directional catalyst lean is a separate edge and is unaffected. On a true binary
# (NFP) IM is justly high, so this correctly makes us take a directional shot or sit,
# rather than overpay for vol that usually crushes.
IM_RM_CHEAP: float = 0.85
RM_FLOOR_PCT: float = 0.008      # fallback realized daily move when we have no data (0.8%)
# Single-stock EARNINGS IV-crush harvest (the sell-side of the IM/RM rule, applied ONLY
# where the overpricing is documented and defined-risk can bound the gap): before an AMC
# report we SELL a defined-risk iron condor on the reporting name and let the post-report
# IV crush pay us. Fires only in the pre-close window, only when vol is genuinely RICH
# (implied move > IM_RM_RICH x realized), sized to the small income cap, wings cap the loss.
EARNINGS_HARVEST: dict[str, str] = {"AVGO_EARNINGS": "AVGO"}   # calendar tag -> underlying
# Enter in the afternoon (elevated pre-earnings IV) with margin before risk's 15:45
# no-new-positions cutoff — so a live cycle actually lands the harvest before the close.
EARNINGS_ENTRY_AFTER_MIN: int = 15 * 60          # 15:00 ET onward
IM_RM_RICH: float = 1.30         # harvest only when implied move exceeds 1.3x realized
# Which calendar tags fire the straddle playbook, and on which underlying.
# Macro events that genuinely move the INDEX we trade -> straddle on that index.
# Deliberately NO single-stock earnings here: AVGO_EARNINGS used to map to QQQ, but
# Broadcom moving +-5% barely moves QQQ (+-0.25%), so a QQQ straddle can never pay for
# the AVGO move -- a dead bet. Trading AVGO directly does not help either: an AMC report
# (16:05, after the 16:00 close) can't be played by the intraday due-catalyst machinery,
# and buying the +-5% straddle is overpriced event vol the IM/RM gate would correctly
# skip. The only +EV AVGO play is HARVESTING the post-report IV crush (sell defined-risk),
# which carries real gap risk and is a deliberate opt-in, not an auto-trade. So we sit out
# AVGO earnings rather than place a bet that cannot win. (Kept in the calendar for
# gap-risk awareness on the QQQ book, not as a trade trigger.)
STRADDLE_PLAYS: dict[str, str] = {
    "NFP_OPEN_PLAY": "SPY",
    "NFP": "SPY",
    "ADP": "SPY",
    "JOLTS": "SPY",
    "CLAIMS": "SPY",
    "ISM_MFG": "SPY",
    "ISM_SVC": "SPY",
}

_OCC_ROOT_RE = re.compile(r"[A-Z][A-Z0-9]{0,5}")
_OCC_TAIL_RE = re.compile(r"(\d{6})([CP])(\d{8})")


@dataclass
class Context:
    """Everything decide() needs, assembled by agent.py each cycle."""
    settings: "Settings"
    policy: dict
    calendar: dict
    account: AccountSnapshot
    portfolio: "PortfolioState"
    signals: dict[str, Signal]
    sentiment: dict[str, float]
    chains: dict[str, dict[str, dict]]       # underlying -> {occ_symbol: quote}
    contracts: dict[str, list[dict]]         # underlying -> discovered contracts
    now: datetime
    due_events: list[dict]
    regimes: dict[str, str] = field(default_factory=dict)   # underlying -> trend/range/storm
    realized_moves: dict[str, float] = field(default_factory=dict)  # underlying -> realized daily move (frac)


def decide(ctx: Context) -> list[TradeIntent]:
    """Run all playbooks. Exit intents first; ALL_CASH suppresses new entries.

    Entry priority per underlying (aggressive convex barbell): a catalyst play
    beats a high-conviction directional long, which beats the medium-conviction
    defined-risk vertical. At most one NEW entry per underlying per cycle — the
    aggregate-risk gate in risk.judge bounds the whole book's worst case."""
    intents: list[TradeIntent] = []
    all_cash = any(str(ev.get("tag") or "") == FLATTEN_TAG for ev in ctx.due_events or [])
    # The daily loss halt is an ENTRY gate by default: it blocks new positions but
    # lets the open book ride to the curfew. With account.flatten_on_daily_halt the
    # latch also closes the book, turning the halt into a real stop.
    halt_flat = (
        bool(_halted_today(ctx))
        and bool(ctx.policy.get("account", {}).get("flatten_on_daily_halt", False))
    )
    intents.extend(_exit_intents(ctx, all_cash, halt_flat))
    if all_cash:
        return intents

    # Regime gates entries per underlying (the one component with a documented live
    # P&L effect): STORM -> stand down; RANGE -> sell premium (income sleeve), never
    # buy naive long debits; TREND/UNKNOWN -> long convexity. Catalyst plays are
    # event-driven and override the trend/range split, but a STORM suppresses all
    # new entries. Priority per underlying: catalyst > income > conviction > vertical.
    chosen: list[TradeIntent] = []
    seen: set[str] = set()
    groups = (_catalyst_plays(ctx), _earnings_harvest(ctx), _income_condors(ctx),
              _conviction_directionals(ctx), _core_verticals(ctx))
    for group in groups:
        for it in group:
            if it.underlying in seen:      # one fresh entry per underlying per cycle
                continue
            reg = (ctx.regimes or {}).get(it.underlying, regime_mod.UNKNOWN)
            if reg == regime_mod.STORM:
                continue                   # stand down entirely in a storm
            is_income = it.structure is Structure.VERTICAL_CREDIT
            # A genuine calendar-EVENT play overrides the regime split; "CONVICTION"
            # is only a sizing tag, not an event, so it is still gated by regime.
            is_event = bool(it.catalyst_tag) and it.catalyst_tag != "CONVICTION"
            if is_event:
                allow = True               # events fire on any non-storm regime
            elif is_income:
                allow = reg == regime_mod.RANGE      # sell premium only on a range day
            else:
                allow = reg == regime_mod.TREND      # buy premium only on a confirmed trend
            if not allow:                  # UNKNOWN (pre-classification) -> wait, no blind entry
                continue
            chosen.append(it)
            seen.add(it.underlying)
    intents.extend(chosen)
    return intents


# --- playbook 1b: income sleeve (range days only) — sell defined-risk premium --

def _iron_condor_intent(ctx: Context, u: str, chain: dict[str, dict], expiry: date, *,
                        catalyst_tag: str, thesis_label: str,
                        snapshot_extra: dict[str, Any] | None = None) -> TradeIntent | None:
    """Build ONE defined-risk iron condor on `u` at `expiry`: sell ~0.18d call+put, buy
    ~0.08d wings, net credit, sized to the small income cap with a width-relative min
    credit. Shared by the range-day income sleeve and the earnings IV-crush harvest so
    both size and gate premium selling identically. Returns None if it doesn't qualify."""
    equity = float(ctx.account.equity)
    dte = (expiry - _as_et(ctx.now).date()).days

    def _sub(typ: str) -> dict[str, dict]:
        return {occ: q for occ, q in chain.items()
                if (m := _occ_meta(occ)) is not None and m["expiry"] == expiry
                and m["type"] == typ and _mid(q) is not None}
    calls, puts = _sub("C"), _sub("P")
    # Read spot and one-sigma off the chain so the condor still builds when the
    # feed sends no greeks — the 0DTE case, where delta is null across the board.
    spot = _implied_spot(chain, expiry)
    sigma_t = _sigma_t_from_chain(chain, expiry, spot)
    sc = _choose_strike(calls, CONDOR_SHORT_DELTA, "C", spot=spot, sigma_t=sigma_t)
    sp = _choose_strike(puts, CONDOR_SHORT_DELTA, "P", spot=spot, sigma_t=sigma_t)
    if sc is None or sp is None:
        return None
    sc_k, sp_k = _occ_meta(sc)["strike"], _occ_meta(sp)["strike"]
    lc = _choose_strike({k: v for k, v in calls.items()
                         if _occ_meta(k)["strike"] > sc_k}, CONDOR_LONG_DELTA, "C",
                        spot=spot, sigma_t=sigma_t)
    lp = _choose_strike({k: v for k, v in puts.items()
                         if _occ_meta(k)["strike"] < sp_k}, CONDOR_LONG_DELTA, "P",
                        spot=spot, sigma_t=sigma_t)
    if lc is None or lp is None:
        return None
    credit = round((_mid(calls[sc]) + _mid(puts[sp]))
                   - (_mid(calls[lc]) + _mid(puts[lp])), 2)
    call_w = _occ_meta(lc)["strike"] - sc_k
    put_w = sp_k - _occ_meta(lp)["strike"]
    width = max(call_w, put_w)
    # mine #3: reject penny premium. Credit must clear BOTH an absolute floor and a
    # fraction of the width, or the risk/reward is junk and we don't sell it.
    min_credit = max(MIN_CONDOR_CREDIT, MIN_CONDOR_CREDIT_FRAC * width)
    if credit < min_credit:
        return None
    max_loss_per = width - credit
    if max_loss_per <= 0:
        return None
    # mine #1: premium selling must be SMALL. Size to the income cap (~2.5% of equity),
    # NOT the 12% directional cap — one adverse move can't erase weeks of credit.
    qty = _size_by_cap(equity, ctx.policy, max_loss_per, income=True)
    if qty < 1:
        return None
    snap = {"credit": credit, "call_width": call_w, "put_width": put_w}
    snap.update(snapshot_extra or {})
    return TradeIntent(
        underlying=u,
        structure=Structure.VERTICAL_CREDIT,
        legs=[
            Leg(sc, Side.SELL, 1, PositionIntent.SELL_TO_OPEN),
            Leg(lc, Side.BUY, 1, PositionIntent.BUY_TO_OPEN),
            Leg(sp, Side.SELL, 1, PositionIntent.SELL_TO_OPEN),
            Leg(lp, Side.BUY, 1, PositionIntent.BUY_TO_OPEN),
        ],
        qty=qty,
        limit_price=-credit,               # net credit -> negative signed limit
        thesis=(f"{thesis_label}: iron condor shorts ~{CONDOR_SHORT_DELTA:.2f}d, "
                f"credit {credit:.2f}, defined risk (max loss {max_loss_per:.2f}/unit)"),
        max_loss_usd=round(max_loss_per * 100.0 * qty, 2),
        catalyst_tag=catalyst_tag,
        is_0dte=(dte == 0),
        expiry=expiry.isoformat(),
        signal_snapshot=snap,
    )


def _enough_0dte_runway(policy: dict, now_et: datetime) -> bool:
    """Is there at least MIN_0DTE_RUNWAY_MIN before the 0DTE flatten window opens?

    The entry cutoff (no_new_0dte_after_et) and the flatten lead leave a gap in
    which a brand-new condor lives for minutes and is then force-closed — all
    spread, no theta. Same class of churn as the final-day buffer, fixed the same
    way: as code, so the frozen policy digest stays untouched.
    """
    try:
        hh, mm = _flat_0dte_str(policy).split(":")
        deadline = now_et.replace(hour=int(hh), minute=int(mm), second=0, microsecond=0)
    except (ValueError, AttributeError):
        return True
    flatten_start = deadline - timedelta(minutes=FLAT_0DTE_LEAD_MIN)
    return now_et <= flatten_start - timedelta(minutes=MIN_0DTE_RUNWAY_MIN)


def _past_0dte_entry_cutoff(policy: dict, now_et: datetime) -> bool:
    """True once policy.timing.no_new_0dte_after_et has passed.

    Lets a playbook stop PROPOSING a 0DTE entry the risk gate would only reject,
    so it can fall through to the next expiry instead of wasting the cycle.
    """
    raw = str(((policy or {}).get("timing") or {}).get("no_new_0dte_after_et") or "15:10")
    try:
        hh, mm = raw.split(":")
        cutoff = now_et.replace(hour=int(hh), minute=int(mm), second=0, microsecond=0)
    except (ValueError, AttributeError):
        return False
    return now_et >= cutoff


def _income_condors(ctx: Context) -> list[TradeIntent]:
    """0-1DTE iron condor on a machine-classified RANGE day: sell ~0.18d call/put, buy
    further-OTM wings for defined risk, net credit. The only source of P&L in a quiet
    week — turns a theta-bleeding day into income. Exits at 55% credit / 2.2x stop /
    0DTE curfew (handled in exit management)."""
    out: list[TradeIntent] = []
    for u in CORE_UNDERLYINGS:
        if (ctx.regimes or {}).get(u, regime_mod.UNKNOWN) != regime_mod.RANGE:
            continue
        if _has_option_position(ctx.account.positions, u):
            continue                       # don't stack income on an existing book
        chain = (ctx.chains or {}).get(u) or {}
        contracts = (ctx.contracts or {}).get(u) or []
        if not chain or not contracts:
            continue
        # Walk the sleeve's whole 0-1DTE window rather than betting everything on
        # the nearest expiry: if today's chain cannot produce a qualifying condor
        # (thin quotes, penny credit, no greeks) tomorrow's still can, and a range
        # day with no trade at all is the worst outcome available.
        seen: set[date] = set()
        # Past the 0DTE entry cutoff a same-day condor is dead on arrival at the
        # timing gate, so don't spend the cycle on it — go straight to tomorrow.
        # Unless income.same_day_only is set, in which case there IS no tomorrow:
        # a position held overnight cannot be stopped out, and the stop is the
        # entire risk control on a short condor. Past the cutoff we simply stand
        # down rather than buy gap risk we have no way to manage.
        past_cutoff = (_past_0dte_entry_cutoff(ctx.policy, _as_et(ctx.now))
                       or not _enough_0dte_runway(ctx.policy, _as_et(ctx.now)))
        same_day_only = bool((ctx.policy.get("income") or {}).get("same_day_only", False))
        if same_day_only:
            targets: tuple[int, ...] = () if past_cutoff else (0,)
        else:
            targets = (1,) if past_cutoff else (0, 1)
        for target_dte in targets:
            expiry = _choose_expiry(contracts, target_dte, ctx.now)
            if expiry is None or expiry in seen:
                continue
            seen.add(expiry)
            dte = (expiry - _as_et(ctx.now).date()).days
            if dte < 0 or dte > 1:
                continue
            it = _iron_condor_intent(ctx, u, chain, expiry, catalyst_tag="",
                                     thesis_label=f"{u} range-day income (0-{dte}DTE)",
                                     snapshot_extra={"regime": "range"})
            if it is not None:
                out.append(it)
                break
    return out


def _earnings_today(calendar: dict | None, tag: str, today: date) -> bool:
    """True if `tag` is scheduled on `today` in the calendar."""
    for day in (calendar or {}).get("week", []) or []:
        raw = day.get("date")
        try:
            d = raw if isinstance(raw, date) else date.fromisoformat(str(raw))
        except (ValueError, TypeError):
            continue
        if d != today:
            continue
        for ev in day.get("events", []) or []:
            if str(ev.get("tag", "")) == tag:
                return True
    return False


def earnings_underlyings_today(calendar: dict | None, day: date) -> set[str]:
    """Reporting names the agent must fetch data for today so the harvest can see their
    chain + realized move. Called by agent.py to extend the data-fetch universe."""
    return {u for tag, u in EARNINGS_HARVEST.items() if _earnings_today(calendar, tag, day)}


def _earnings_harvest(ctx: Context) -> list[TradeIntent]:
    """Single-stock earnings IV-crush harvest: before an AMC report, SELL a defined-risk
    iron condor on the reporting name (e.g. AVGO) and let the post-report IV crush pay us.
    Earnings implied vol is systematically overpriced; we collect it, and the wings cap the
    loss even on a big gap. Fires ONLY (a) in the pre-close window on the earnings day,
    (b) when vol is genuinely RICH (implied move > 1.3x realized — so we never sell cheap
    vol), and (c) on a non-storm tape (decide()'s storm gate). Sized to the small income
    cap: a bounded, opt-in bet, not a naked short."""
    out: list[TradeIntent] = []
    et = _as_et(ctx.now)
    if et.hour * 60 + et.minute < EARNINGS_ENTRY_AFTER_MIN:
        return out                                  # sell peak IV only near the close
    today = et.date()
    rich = float(((ctx.policy or {}).get("catalyst") or {}).get("im_rm_rich", IM_RM_RICH))
    for tag, u in EARNINGS_HARVEST.items():
        if not _earnings_today(ctx.calendar, tag, today):
            continue
        if _has_option_position(ctx.account.positions, u):
            continue
        chain = (ctx.chains or {}).get(u) or {}
        contracts = (ctx.contracts or {}).get(u) or []
        if not chain or not contracts:
            continue
        expiry = _choose_expiry(contracts, 1, ctx.now)     # nearest ~1DTE...
        if expiry is None or (expiry - today).days < 1:
            continue                                # ...must survive PAST the AMC report
        pair = _atm_pair(chain, expiry)
        if pair is None:
            continue
        call_sym, _put_sym, call_q, put_q = pair
        cm, pm = _mid(call_q), _mid(put_q)
        meta = _occ_meta(call_sym)
        spot_ref = meta["strike"] if meta else None
        if cm is None or pm is None or not spot_ref:
            continue
        im = (cm + pm) / spot_ref                   # ATM straddle / spot ~ implied move
        rm = _num((ctx.realized_moves or {}).get(u))
        if rm is None or rm <= 0 or im <= rich * rm:
            continue                                # only harvest genuinely RICH vol
        it = _iron_condor_intent(
            ctx, u, chain, expiry, catalyst_tag=tag,
            thesis_label=f"{u} {tag} IV-crush harvest (IM {im:.3f} > {rich:g}x RM {rm:.3f})",
            snapshot_extra={"harvest": True, "implied_move": round(im, 5),
                            "realized_move": round(rm, 5), "im_rm_ratio": round(im / rm, 3)})
        if it is not None:
            out.append(it)
    return out


# --- playbook 0: high-conviction directional convexity -----------------------

def _conviction_directionals(ctx: Context) -> list[TradeIntent]:
    """The convex upside bet: on the STRONGEST momentum, buy a directional long
    option (near-the-money 0-1 DTE) instead of a capped spread — max loss is the
    premium, upside is convex. Sized to the conviction (catalyst) cap."""
    out: list[TradeIntent] = []
    for underlying in CORE_UNDERLYINGS:
        sig = (ctx.signals or {}).get(underlying)
        if sig is None or sig.direction == 0 or sig.strength < CONVICTION_STRENGTH:
            continue
        sent = (ctx.sentiment or {}).get(underlying)
        if sent is not None and (sent * sig.direction) < -0.3:   # sentiment strongly disagrees
            continue
        if _has_open_direction(ctx.account.positions, underlying, sig.direction):
            continue
        intent = _directional_long(ctx, underlying, sig.direction, tag="CONVICTION",
                                   note=f"high-conviction momentum (strength {sig.strength:.2f})")
        if intent is not None:
            out.append(intent)
    return out


# --- playbook 1: core momentum debit vertical --------------------------------

def _core_verticals(ctx: Context) -> list[TradeIntent]:
    out: list[TradeIntent] = []
    equity = float(ctx.account.equity)
    for underlying in CORE_UNDERLYINGS:
        sig = (ctx.signals or {}).get(underlying)
        if sig is None or sig.direction == 0 or sig.strength < MIN_VERTICAL_STRENGTH:
            continue
        if _has_open_direction(ctx.account.positions, underlying, sig.direction):
            continue
        chain = (ctx.chains or {}).get(underlying) or {}
        contracts = (ctx.contracts or {}).get(underlying) or []
        if not chain or not contracts:
            continue
        expiry = _choose_expiry(contracts, VERTICAL_TARGET_DTE, ctx.now)
        if expiry is None:
            continue
        dte = (expiry - _as_et(ctx.now).date()).days
        if dte < 0 or dte > VERTICAL_MAX_DTE:
            continue
        opt_type = "C" if sig.direction > 0 else "P"
        sub = {
            occ: q for occ, q in chain.items()
            if (m := _occ_meta(occ)) is not None
            and m["expiry"] == expiry and m["type"] == opt_type
            and _mid(q) is not None
        }
        buy_sym = _choose_strike(sub, VERTICAL_LONG_DELTA, opt_type)
        if buy_sym is None:
            continue
        sell_sym = _choose_strike(
            {k: v for k, v in sub.items() if k != buy_sym}, VERTICAL_SHORT_DELTA, opt_type
        )
        if sell_sym is None:
            continue
        mb, ms = _occ_meta(buy_sym), _occ_meta(sell_sym)
        assert mb is not None and ms is not None
        # sanity of moneyness ordering: debit vertical buys the nearer strike
        if opt_type == "C" and not mb["strike"] < ms["strike"]:
            continue
        if opt_type == "P" and not mb["strike"] > ms["strike"]:
            continue
        buy_mid, sell_mid = _mid(sub[buy_sym]), _mid(sub[sell_sym])
        assert buy_mid is not None and sell_mid is not None
        debit = buy_mid - sell_mid
        width = abs(ms["strike"] - mb["strike"])
        if debit <= 0 or debit >= width:      # mispriced/no-edge spread
            continue
        limit = round(debit, 2)
        if limit <= 0:
            continue
        qty = _size_by_cap(equity, ctx.policy, limit, catalyst=False)
        if qty < 1:
            continue
        snapshot: dict[str, Any] = dict(sig.features)
        snapshot["direction"] = float(sig.direction)
        snapshot["strength"] = float(sig.strength)
        sent = (ctx.sentiment or {}).get(underlying)
        if sent is not None:
            snapshot["sentiment"] = float(sent)
        word = "bullish" if sig.direction > 0 else "bearish"
        out.append(TradeIntent(
            underlying=underlying,
            structure=Structure.VERTICAL_DEBIT,
            legs=[
                Leg(buy_sym, Side.BUY, 1, PositionIntent.BUY_TO_OPEN),
                Leg(sell_sym, Side.SELL, 1, PositionIntent.SELL_TO_OPEN),
            ],
            qty=qty,
            limit_price=limit,
            thesis=(
                f"{underlying} momentum {word} (strength {sig.strength:.2f}): "
                f"buy ~{VERTICAL_LONG_DELTA:.2f}d / sell ~{VERTICAL_SHORT_DELTA:.2f}d "
                f"{'call' if opt_type == 'C' else 'put'} debit vertical exp {expiry.isoformat()}"
            ),
            max_loss_usd=round(limit * 100.0 * qty, 2),
            catalyst_tag="",
            is_0dte=(dte == 0),
            expiry=expiry.isoformat(),
            signal_snapshot=snapshot,
        ))
    return out


# --- directional long (shared convex builder) --------------------------------

def _directional_long(ctx: Context, underlying: str, direction: int, *, tag: str,
                      note: str, dte: int = VERTICAL_TARGET_DTE) -> TradeIntent | None:
    """A single-leg near-the-money long call/put — defined risk (max loss = debit),
    convex upside. Sized to the conviction (catalyst) cap."""
    equity = float(ctx.account.equity)
    chain = (ctx.chains or {}).get(underlying) or {}
    contracts = (ctx.contracts or {}).get(underlying) or []
    if not chain or not contracts:
        return None
    expiry = _choose_expiry(contracts, dte, ctx.now)
    if expiry is None:
        return None
    real_dte = (expiry - _as_et(ctx.now).date()).days
    if real_dte < 0 or real_dte > VERTICAL_MAX_DTE:
        return None
    opt_type = "C" if direction > 0 else "P"
    sub = {
        occ: q for occ, q in chain.items()
        if (m := _occ_meta(occ)) is not None
        and m["expiry"] == expiry and m["type"] == opt_type and _mid(q) is not None
    }
    sym = _choose_strike(sub, CONVICTION_DELTA, opt_type)
    if sym is None:
        return None
    mid = _mid(sub[sym])
    if mid is None or mid <= 0:
        return None
    limit = round(mid, 2)
    is_conviction = tag == "CONVICTION"
    qty = _size_by_cap(equity, ctx.policy, limit,
                       catalyst=not is_conviction, conviction=is_conviction)
    if qty < 1:
        return None
    word = "call" if opt_type == "C" else "put"
    snapshot: dict[str, Any] = {"direction": float(direction), "tag": tag, "note": note}
    sig = (ctx.signals or {}).get(underlying)
    if sig is not None:
        snapshot.update(sig.features)
        snapshot["strength"] = float(sig.strength)
    sent = (ctx.sentiment or {}).get(underlying)
    if sent is not None:
        snapshot["sentiment"] = float(sent)
    return TradeIntent(
        underlying=underlying,
        structure=Structure.LONG_CALL if opt_type == "C" else Structure.LONG_PUT,
        legs=[Leg(sym, Side.BUY, 1, PositionIntent.BUY_TO_OPEN)],
        qty=qty,
        limit_price=limit,
        thesis=f"{underlying} {tag}: long ~{CONVICTION_DELTA:.2f}d {word} "
               f"exp {expiry.isoformat()} — {note}",
        max_loss_usd=round(limit * 100.0 * qty, 2),
        catalyst_tag=tag,
        is_0dte=(real_dte == 0),
        expiry=expiry.isoformat(),
        signal_snapshot=snapshot,
    )


# --- playbook 2: catalyst play (directional when confirmed, else straddle) ----

def _catalyst_plays(ctx: Context) -> list[TradeIntent]:
    out: list[TradeIntent] = []
    equity = float(ctx.account.equity)
    played: set[str] = set()
    for ev in ctx.due_events or []:
        tag = str(ev.get("tag") or "")
        underlying = STRADDLE_PLAYS.get(tag)
        if underlying is None or underlying in played:
            continue
        if _has_open_straddle(ctx.account.positions, underlying):
            continue
        # Directional when we have a confirmed lean (signal + non-opposing sentiment):
        # a directional long is higher-EV than paying for both sides of a straddle.
        sig = (ctx.signals or {}).get(underlying)
        sent = (ctx.sentiment or {}).get(underlying)
        if (sig is not None and sig.direction != 0 and sig.strength >= CATALYST_LEAN_STRENGTH
                and not (sent is not None and (sent * sig.direction) < -0.2)):
            lean = _directional_long(
                ctx, underlying, sig.direction, tag=tag,
                note=f"{tag} directional lean (strength {sig.strength:.2f})", dte=0)
            if lean is not None:
                out.append(lean)
                played.add(underlying)
                continue
        chain = (ctx.chains or {}).get(underlying) or {}
        contracts = (ctx.contracts or {}).get(underlying) or []
        if not chain or not contracts:
            continue
        expiry = _choose_expiry(contracts, 0, ctx.now)
        if expiry is None or (expiry - _as_et(ctx.now).date()).days != 0:
            continue                          # playbook demands a true 0DTE
        pair = _atm_pair(chain, expiry)
        if pair is None:
            continue
        call_sym, put_sym, call_q, put_q = pair
        call_mid, put_mid = _mid(call_q), _mid(put_q)
        assert call_mid is not None and put_mid is not None
        limit = round(call_mid + put_mid, 2)
        if limit <= 0:
            continue
        # IM/RM gate: don't overpay for event vol on the pure long straddle. IM is the
        # straddle's priced move (its price over spot ~ the ATM strike); RM is the recent
        # realized daily move. If vol is not clearly cheap, standing aside beats bleeding
        # theta on a quiet day. (Directional leans above are unaffected.)
        cat_pol = (ctx.policy or {}).get("catalyst") or {}
        cheap = float(cat_pol.get("im_rm_cheap", IM_RM_CHEAP))
        rm = _num((ctx.realized_moves or {}).get(underlying))
        if rm is None or rm <= 0:
            rm = float(cat_pol.get("rm_floor_pct_frac", RM_FLOOR_PCT))
        atm_meta = _occ_meta(call_sym)
        spot_ref = atm_meta["strike"] if atm_meta else None
        im = (limit / spot_ref) if spot_ref else None
        if im is not None and im >= cheap * rm:
            continue                          # event vol not cheap -> no long straddle
        qty = _size_by_cap(equity, ctx.policy, limit, catalyst=True)
        if qty < 1:
            continue
        sig = (ctx.signals or {}).get(underlying)
        snapshot: dict[str, Any] = dict(sig.features) if sig is not None else {}
        snapshot.update({
            "catalyst": tag,
            "event_desc": str(ev.get("desc") or ""),
            "call_mid": call_mid,
            "put_mid": put_mid,
            "implied_move": round(im, 5) if im is not None else None,
            "realized_move": round(rm, 5),
            "im_rm_ratio": round(im / rm, 3) if (im is not None and rm) else None,
        })
        cd, pd = _num(call_q.get("delta")), _num(put_q.get("delta"))
        if cd is not None:
            snapshot["call_delta"] = cd
        if pd is not None:
            snapshot["put_delta"] = pd
        out.append(TradeIntent(
            underlying=underlying,
            structure=Structure.STRADDLE,
            legs=[
                Leg(call_sym, Side.BUY, 1, PositionIntent.BUY_TO_OPEN),
                Leg(put_sym, Side.BUY, 1, PositionIntent.BUY_TO_OPEN),
            ],
            qty=qty,
            limit_price=limit,
            thesis=(
                f"{tag}: long ATM 0DTE straddle on {underlying} exp {expiry.isoformat()} "
                f"— convexity into the catalyst, exit via flatten rules"
            ),
            max_loss_usd=round(limit * 100.0 * qty, 2),
            catalyst_tag=tag,
            is_0dte=True,
            expiry=expiry.isoformat(),
            signal_snapshot=snapshot,
        ))
        played.add(underlying)
    return out


# --- playbook 3: exit management ---------------------------------------------

def _halted_today(ctx: Context) -> bool:
    """Has the daily loss halt latched? Read from the portfolio the cycle judged."""
    pf = getattr(ctx, "portfolio", None)
    if pf is None:
        return False
    if isinstance(pf, dict):
        return bool(pf.get("halted_today", False))
    return bool(getattr(pf, "halted_today", False))


def _exit_intents(ctx: Context, all_cash: bool, halt_flat: bool = False) -> list[TradeIntent]:
    """Exit management. Spreads exit as ONE mleg order (both legs reversed) —
    per-leg exits would strand the loser or leave a naked short the account
    cannot hold (Alpaca 403s an uncovering sell). Residual unpaired positions
    use the single-leg path, shorts first (buy_to_close before sell_to_close)."""
    out: list[TradeIntent] = []
    now = _as_et(ctx.now)
    pairs, singles = _pair_spreads(_option_positions(ctx.account.positions))
    force_flat = all_cash or halt_flat
    force_reason = ("ALL_CASH: final-day flatten before submission deadline" if all_cash
                    else "DAILY HALT: loss halt latched — flattening the book")

    for long_pos, short_pos in pairs:
        intent = _spread_close_intent(ctx, long_pos, short_pos, now, force_flat, force_reason)
        if intent is not None:
            out.append(intent)

    # Shorts first: a buy_to_close must never queue behind a sell that would
    # temporarily uncover it.
    for pos in sorted(singles, key=lambda p: _num(p.get("qty")) or 0.0):
        plpc = _pos_plpc(pos)
        reason = ""
        if force_flat:
            reason = force_reason
        elif plpc is not None and plpc <= STOP_PLPC:
            reason = f"stop: unrealized {plpc:+.0%} <= -50% of debit"
        elif plpc is not None and plpc >= TARGET_PLPC:
            reason = f"target: unrealized {plpc:+.0%} >= +100% of debit"
        elif _is_0dte_position(pos, now) and _near_0dte_flatten(ctx.policy, now):
            reason = f"0DTE flatten window before {_flat_0dte_str(ctx.policy)} ET"
        if not reason:
            continue
        intent = _close_intent(ctx, pos, reason, plpc)
        if intent is not None:
            out.append(intent)
    return out


def _pair_spreads(
    positions: list[dict],
) -> tuple[list[tuple[dict, dict]], list[dict]]:
    """Match long and short option positions into vertical-spread pairs.

    Alpaca has no spread-position object — a vertical opened as one mleg order
    comes back as two independent rows. Legs of our verticals share (root,
    expiry, type) with opposite signs, so pair within those groups (sorted by
    strike for determinism). Unpairable rows are returned as singles."""
    groups: dict[tuple[str, str, str], list[dict]] = {}
    singles: list[dict] = []
    for pos in positions:
        meta = _occ_meta(str(pos.get("symbol") or ""))
        if meta is None:
            singles.append(pos)
            continue
        key = (meta["root"], meta["expiry"].isoformat(), meta["type"])
        groups.setdefault(key, []).append(pos)

    pairs: list[tuple[dict, dict]] = []
    for rows in groups.values():
        longs = sorted((p for p in rows if (_num(p.get("qty")) or 0.0) > 0),
                       key=lambda p: str(p.get("symbol")))
        shorts = sorted((p for p in rows if (_num(p.get("qty")) or 0.0) < 0),
                        key=lambda p: str(p.get("symbol")))
        n = min(len(longs), len(shorts))
        pairs.extend(zip(longs[:n], shorts[:n]))
        singles.extend(longs[n:])
        singles.extend(shorts[n:])
    return pairs, singles


def _short_strike_touched(ctx: Context, short_pos: dict, now: datetime) -> bool:
    """Has price reached the strike we are short on this side?

    Spot is read off the option chain itself (where call and put mids meet), so
    this needs no extra market-data call and survives a feed that nulls greeks —
    the same trick the strike selection uses. A short call is tested from above,
    a short put from below. Unknown spot means unknown answer: return False and
    leave the decision to the other exit rules rather than guessing.
    """
    sym = str(short_pos.get("symbol") or "")
    meta = _occ_meta(sym)
    if meta is None:
        return False
    chain = (ctx.chains or {}).get(meta["root"]) or {}
    spot = _implied_spot(chain, meta["expiry"]) if chain else None
    if not spot or spot <= 0:
        return False
    strike = float(meta["strike"])
    return spot >= strike if meta["type"] == "C" else spot <= strike


def _spread_close_intent(
    ctx: Context, long_pos: dict, short_pos: dict, now: datetime, all_cash: bool,
    flat_reason: str = "ALL_CASH: final-day flatten before submission deadline",
) -> TradeIntent | None:
    """One mleg CLOSE for a paired vertical, judged on SPREAD-level P&L."""
    long_sym = str(long_pos.get("symbol") or "")
    short_sym = str(short_pos.get("symbol") or "")
    meta = _occ_meta(long_sym)
    if meta is None:
        return None
    units = min(abs(int(_num(long_pos.get("qty")) or 0)),
                abs(int(_num(short_pos.get("qty")) or 0)))
    if units < 1:
        return None

    # Spread-level economics per unit (contract = 100 shares).
    net_cost = (_num(long_pos.get("avg_entry_price")) or 0.0) - \
               (_num(short_pos.get("avg_entry_price")) or 0.0)
    long_q = _find_quote(ctx.chains or {}, long_sym, meta["root"])
    short_q = _find_quote(ctx.chains or {}, short_sym, meta["root"])
    long_touch = _first_price(long_q.get("bid"), _mid(long_q), long_q.get("last"),
                              long_pos.get("current_price"))
    short_touch = _first_price(short_q.get("ask"), _mid(short_q), short_q.get("last"),
                               short_pos.get("current_price"))
    if long_touch is None or short_touch is None:
        # No usable quotes: fall back to per-unit net from position market values.
        mv = (_num(long_pos.get("market_value")) or 0.0) + \
             (_num(short_pos.get("market_value")) or 0.0)
        net_value = mv / (100.0 * units) if units else None
    else:
        net_value = long_touch - short_touch

    is_credit = net_cost < -1e-6      # opened for a net credit (income sleeve)
    spread_plpc: float | None = None
    if net_value is not None and net_cost > 0:
        spread_plpc = (net_value - net_cost) / net_cost

    is_0dte = meta["expiry"] == now.date()
    reason = ""
    if all_cash:
        reason = flat_reason
    elif is_credit and net_value is not None:
        # Credit spread: profit as the position decays toward zero. captured = how
        # much of the credit we keep if we buy it back now; cost_to_close = -net_value.
        credit_recv = -net_cost
        cost_to_close = -net_value                     # >=0 when the spread still has value
        captured = 1.0 - (cost_to_close / credit_recv) if credit_recv > 0 else 0.0
        if captured >= INCOME_TAKE_FRAC:
            reason = f"income target: captured {captured:+.0%} of credit"
        elif _short_strike_touched(ctx, short_pos, now):
            # The credit stop is deliberately wide — 2.2x on a 0.80 credit means
            # not acting until the buy-back costs 1.76 — so on its own it lets a
            # tested side run a long way first. Price reaching the strike we sold
            # is the earlier and more honest signal that this side is wrong, and
            # it does not depend on quotes that go noisy in the pennies.
            reason = f"short strike {_occ_meta(short_sym)['strike']:g} touched"
        elif cost_to_close >= INCOME_STOP_MULT * credit_recv:
            reason = f"income stop: cost {cost_to_close:.2f} >= {INCOME_STOP_MULT:g}x credit"
        elif is_0dte and _near_0dte_flatten(ctx.policy, now):
            reason = f"0DTE flatten window before {_flat_0dte_str(ctx.policy)} ET"
    elif spread_plpc is not None and spread_plpc <= STOP_PLPC:
        reason = f"stop: spread {spread_plpc:+.0%} <= -50% of debit"
    elif spread_plpc is not None and spread_plpc >= TARGET_PLPC:
        reason = f"target: spread {spread_plpc:+.0%} >= +150% of debit"
    elif is_0dte and _near_0dte_flatten(ctx.policy, now):
        reason = f"0DTE flatten window before {_flat_0dte_str(ctx.policy)} ET"
    if not reason:
        return None

    # Signed net for the close: receive long_touch, pay short_touch. Receiving
    # (normal debit-vertical close) => credit => negative limit.
    if long_touch is not None and short_touch is not None:
        net_close = round(long_touch - short_touch, 2)
    elif net_value is not None:
        net_close = round(net_value, 2)
    else:
        return None
    limit = -max(0.01, net_close) if net_close > 0 else max(0.01, -net_close)

    legs = [
        Leg(long_sym, Side.SELL, 1, PositionIntent.SELL_TO_CLOSE),
        Leg(short_sym, Side.BUY, 1, PositionIntent.BUY_TO_CLOSE),
    ]
    snapshot: dict[str, Any] = {
        "reason": reason,
        "net_cost": net_cost,
        "net_value": net_value,
        "long_symbol": long_sym,
        "short_symbol": short_sym,
    }
    if spread_plpc is not None:
        snapshot["spread_plpc"] = spread_plpc
    return TradeIntent(
        underlying=meta["root"],
        structure=Structure.CLOSE,
        legs=legs,
        qty=units,
        limit_price=limit,
        thesis=f"CLOSE spread {long_sym}/{short_sym} x{units}: {reason}",
        # Closing at a debit costs that debit; closing at a credit risks ~nothing.
        max_loss_usd=round((limit if limit > 0 else 0.01) * 100.0 * units, 2),
        catalyst_tag="",
        is_0dte=is_0dte,
        expiry=meta["expiry"].isoformat(),
        signal_snapshot=snapshot,
    )


def _close_intent(ctx: Context, pos: dict, reason: str, plpc: float | None) -> TradeIntent | None:
    """Single-leg CLOSE: side reversed, *_TO_CLOSE, qty from position, limit
    from the current chain touch (bid to sell a long, ask to buy back a short)."""
    sym = str(pos.get("symbol") or "")
    meta = _occ_meta(sym)
    if meta is None:
        return None
    qty_f = _num(pos.get("qty"))
    if qty_f is None:
        qty_f = _num(pos.get("qty_available"))
    if qty_f is None:
        return None
    qty = abs(int(qty_f))
    if qty < 1:
        return None
    side_s = str(pos.get("side") or "").lower()
    is_long = side_s == "long" if side_s in ("long", "short") else qty_f > 0

    quote = _find_quote(ctx.chains or {}, sym, meta["root"])
    if is_long:
        touch = _first_price(quote.get("bid"), _mid(quote), quote.get("last"),
                             pos.get("current_price"), pos.get("avg_entry_price"))
    else:
        touch = _first_price(quote.get("ask"), _mid(quote), quote.get("last"),
                             pos.get("current_price"), pos.get("avg_entry_price"))
    price = max(0.01, round(touch if touch is not None else 0.01, 2))
    limit = -price if is_long else price     # sell-to-close = credit (<0)

    leg = Leg(
        symbol=sym,
        side=Side.SELL if is_long else Side.BUY,
        ratio_qty=1,
        position_intent=PositionIntent.SELL_TO_CLOSE if is_long else PositionIntent.BUY_TO_CLOSE,
    )
    snapshot: dict[str, Any] = {"reason": reason, "position_qty": qty_f, "touch": price}
    if plpc is not None:
        snapshot["unrealized_plpc"] = plpc
    return TradeIntent(
        underlying=meta["root"],
        structure=Structure.CLOSE,
        legs=[leg],
        qty=qty,
        limit_price=limit,
        thesis=f"CLOSE {sym} x{qty}: {reason}",
        max_loss_usd=round(abs(limit) * 100.0 * qty, 2),   # cash at stake on the close
        catalyst_tag="",
        is_0dte=(meta["expiry"] == _as_et(ctx.now).date()),
        expiry=meta["expiry"].isoformat(),
        signal_snapshot=snapshot,
    )


# --- selection helpers (universe.py preferred, local fallbacks) --------------

def _choose_expiry(contracts: list[dict], target_dte: int, now: datetime) -> date | None:
    """universe.nearest_expiry when importable/valid, else local equivalent."""
    try:
        from quaestor import universe
        raw = universe.nearest_expiry(contracts, target_dte)
        return date.fromisoformat(str(raw))
    except Exception:
        pass
    return _nearest_expiry_local(contracts, target_dte, _as_et(now).date())


def _nearest_expiry_local(contracts: list[dict], target_dte: int, ref: date) -> date | None:
    best: date | None = None
    best_err: int | None = None
    for c in contracts or []:
        if not isinstance(c, dict):
            continue
        raw = c.get("expiration_date") or c.get("expiry") or c.get("expiration")
        try:
            d = date.fromisoformat(str(raw))
        except (TypeError, ValueError):
            continue
        err = abs((d - ref).days - target_dte)
        if best is None or best_err is None or err < best_err or (err == best_err and d < best):
            best, best_err = d, err
    return best


def _choose_strike(chain: dict[str, dict], target_delta: float, opt_type: str, *,
                   spot: float | None = None, sigma_t: float | None = None) -> str | None:
    """universe.pick_strike when importable and its answer validates, else local
    by delta, else — when the chain carries no greeks — by moneyness.

    Callers that can supply `spot` and `sigma_t` keep working on a chain the feed
    stripped of deltas; callers that cannot behave exactly as before.
    """
    if not chain:
        return None
    try:
        from quaestor import universe
        sym = universe.pick_strike(chain, target_delta, "call" if opt_type == "C" else "put")
        if isinstance(sym, str) and sym in chain:
            m = _occ_meta(sym)
            if m is not None and m["type"] == opt_type:
                return sym
    except Exception:
        pass
    by_delta = _pick_strike_local(chain, target_delta, opt_type)
    if by_delta is not None:
        return by_delta
    return _pick_strike_by_moneyness(chain, target_delta, opt_type, spot, sigma_t)


def _pick_strike_local(chain: dict[str, dict], target_delta: float, opt_type: str) -> str | None:
    """OCC symbol whose |delta| is closest to target. Ties break by symbol."""
    want = "C" if str(opt_type).upper().startswith("C") else "P"
    best: str | None = None
    best_err = float("inf")
    for occ in sorted(chain):
        q = chain[occ]
        m = _occ_meta(occ)
        if m is None or m["type"] != want or not isinstance(q, dict):
            continue
        d = _num(q.get("delta"))
        if d is None:
            continue
        err = abs(abs(d) - target_delta)
        if err < best_err:
            best, best_err = occ, err
    return best


def _implied_spot(chain: dict[str, dict], expiry: date) -> float | None:
    """Forward price implied by the chain itself — the strike where the call and
    put mids are closest, since put-call parity is tightest at the money.

    Lets strike selection work off the option chain alone: no extra market-data
    call, and no dependence on greeks the feed may not send.
    """
    by_strike: dict[float, dict[str, float]] = {}
    for occ, q in chain.items():
        m = _occ_meta(occ)
        mid = _mid(q) if isinstance(q, dict) else None
        if m is None or m["expiry"] != expiry or mid is None:
            continue
        by_strike.setdefault(m["strike"], {})[m["type"]] = mid
    pairs = [(abs(sides["C"] - sides["P"]), strike)
             for strike, sides in by_strike.items()
             if "C" in sides and "P" in sides]
    if not pairs:
        return None
    pairs.sort()
    return pairs[0][1]


def _sigma_t_from_chain(chain: dict[str, dict], expiry: date,
                        spot: float | None) -> float | None:
    """One-sigma move to this expiry, as a fraction of spot, read off the ATM
    straddle: straddle ~= sqrt(2/pi) * S * sigma*sqrt(T).

    Priced from mids only, so it survives a feed that nulls every greek.
    """
    if not spot or spot <= 0:
        return None
    pair = _atm_pair(chain, expiry)
    if pair is None:
        return None
    _c_sym, _p_sym, c_q, p_q = pair
    straddle = (_mid(c_q) or 0.0) + (_mid(p_q) or 0.0)
    if straddle <= 0:
        return None
    return straddle / (0.7978845608 * spot)


def _pick_strike_by_moneyness(chain: dict[str, dict], target_delta: float,
                              opt_type: str, spot: float | None,
                              sigma_t: float | None) -> str | None:
    """Strike selection for a chain that carries no greeks at all.

    An OTM option's |delta| is approximately N(-x), where x is its log-moneyness
    in standard deviations, so the strike that would carry `target_delta` sits
    x = Phi^-1(1 - target_delta) sigmas out.

    This is not a nicety: the free indicative feed nulls delta on the ENTIRE 0DTE
    chain (measured 2026-08-31: 0 of 526 SPY quotes carried one), which left the
    range-day income sleeve unable to build a condor and the agent unable to
    trade at all on a range day.
    """
    if not chain or not spot or spot <= 0 or not sigma_t or sigma_t <= 0:
        return None
    td = min(max(float(target_delta), 1e-4), 0.4999)
    x = NormalDist().inv_cdf(1.0 - td)
    want = "C" if str(opt_type).upper().startswith("C") else "P"
    target_k = spot * math.exp(x * sigma_t if want == "C" else -x * sigma_t)
    best: str | None = None
    best_err = float("inf")
    for occ in sorted(chain):
        q = chain[occ]
        m = _occ_meta(occ)
        if m is None or m["type"] != want or not isinstance(q, dict) or _mid(q) is None:
            continue
        err = abs(m["strike"] - target_k)
        if err < best_err:
            best, best_err = occ, err
    return best


def _atm_pair(chain: dict[str, dict], expiry: date) -> tuple[str, str, dict, dict] | None:
    """ATM call+put at the SAME strike/expiry, both quoted. Prefers the strike
    whose call |delta| is nearest 0.50; falls back to min |call_mid - put_mid|
    when deltas are missing (indicative feed can null them)."""
    by_strike: dict[float, dict[str, tuple[str, dict]]] = {}
    for occ in sorted(chain):
        q = chain[occ]
        m = _occ_meta(occ)
        if m is None or m["expiry"] != expiry or not isinstance(q, dict):
            continue
        if _mid(q) is None:
            continue
        by_strike.setdefault(m["strike"], {})[m["type"]] = (occ, q)
    pairs = [
        (strike, sides["C"], sides["P"])
        for strike, sides in sorted(by_strike.items())
        if "C" in sides and "P" in sides
    ]
    if not pairs:
        return None
    scored: list[tuple[float, float]] = []       # (delta_err, strike)
    for strike, (c_sym, c_q), _p in pairs:
        d = _num(c_q.get("delta"))
        if d is not None:
            scored.append((abs(abs(d) - ATM_DELTA), strike))
    if scored:
        scored.sort()
        chosen_strike = scored[0][1]
    else:
        parity = [
            (abs((_mid(c[1]) or 0.0) - (_mid(p[1]) or 0.0)), strike)
            for strike, c, p in pairs
        ]
        parity.sort()
        chosen_strike = parity[0][1]
    for strike, (c_sym, c_q), (p_sym, p_q) in pairs:
        if strike == chosen_strike:
            return c_sym, p_sym, c_q, p_q
    return None


# --- position/quote/policy helpers -------------------------------------------

def _option_positions(positions: list[dict] | None) -> Iterator[dict]:
    for p in positions or []:
        if not isinstance(p, dict):
            continue
        sym = str(p.get("symbol") or "")
        if p.get("asset_class") == "us_option" or _occ_meta(sym) is not None:
            yield p


def _has_option_position(positions: list[dict] | None, underlying: str) -> bool:
    """True if any open option position exists on this underlying root."""
    for p in _option_positions(positions):
        m = _occ_meta(str(p.get("symbol") or ""))
        if m is not None and m["root"] == underlying and (_num(p.get("qty")) or 0.0) != 0:
            return True
    return False


def _has_open_direction(positions: list[dict] | None, underlying: str, direction: int) -> bool:
    """Net entry-cost-weighted direction of open option positions on underlying:
    long call / short put lean bullish, long put / short call lean bearish."""
    net = 0.0
    any_pos = False
    for p in _option_positions(positions):
        m = _occ_meta(str(p.get("symbol") or ""))
        if m is None or m["root"] != underlying:
            continue
        qty_f = _num(p.get("qty"))
        if qty_f is None:
            continue
        side_s = str(p.get("side") or "").lower()
        if side_s == "long":
            signed = abs(qty_f)
        elif side_s == "short":
            signed = -abs(qty_f)
        else:
            signed = qty_f
        weight = _num(p.get("avg_entry_price"))
        weight = abs(weight) if weight else 1.0
        type_sign = 1.0 if m["type"] == "C" else -1.0
        net += type_sign * signed * weight
        any_pos = True
    if not any_pos:
        return False
    return (net > 0 and direction > 0) or (net < 0 and direction < 0)


def _has_open_straddle(positions: list[dict] | None, underlying: str) -> bool:
    has_call = has_put = False
    for p in _option_positions(positions):
        m = _occ_meta(str(p.get("symbol") or ""))
        if m is None or m["root"] != underlying:
            continue
        qty_f = _num(p.get("qty"))
        side_s = str(p.get("side") or "").lower()
        is_long = side_s == "long" if side_s in ("long", "short") else (qty_f or 0) > 0
        if not is_long:
            continue
        if m["type"] == "C":
            has_call = True
        else:
            has_put = True
    return has_call and has_put


def _pos_plpc(pos: dict) -> float | None:
    v = _num(pos.get("unrealized_plpc"))
    if v is not None:
        return v
    pl = _num(pos.get("unrealized_pl"))
    cb = _num(pos.get("cost_basis"))
    if pl is not None and cb is not None and abs(cb) > 1e-9:
        return pl / abs(cb)
    return None


def _is_0dte_position(pos: dict, now_et: datetime) -> bool:
    m = _occ_meta(str(pos.get("symbol") or ""))
    return m is not None and m["expiry"] == now_et.date()


def _flat_0dte_str(policy: dict) -> str:
    return str(((policy or {}).get("timing") or {}).get("flat_0dte_by_et") or "15:25")


def _near_0dte_flatten(policy: dict, now_et: datetime) -> bool:
    try:
        hh, mm = _flat_0dte_str(policy).split(":")
        deadline = now_et.replace(hour=int(hh), minute=int(mm), second=0, microsecond=0)
    except (ValueError, TypeError):
        return False
    return now_et >= deadline - timedelta(minutes=FLAT_0DTE_LEAD_MIN)


def _find_quote(chains: dict[str, dict[str, dict]], sym: str, root: str) -> dict:
    chain = chains.get(root)
    if isinstance(chain, dict) and isinstance(chain.get(sym), dict):
        return chain[sym]
    for chain in chains.values():
        if isinstance(chain, dict) and isinstance(chain.get(sym), dict):
            return chain[sym]
    return {}


def _size_by_cap(equity: float, policy: dict, unit_debit: float, *,
                 catalyst: bool = False, income: bool = False,
                 conviction: bool = False) -> int:
    """Contracts (strategy units) so that unit_debit*100*qty <= per-trade cap.

    Four caps, narrowest first: income (premium selling, ~2.5%), conviction
    (momentum longs, ~8%), catalyst (dated calendar events, ~22%), default
    (directional verticals, ~12%). income wins if set: selling insurance big is
    how a premium book blows up on one bad day. conviction beats catalyst because
    momentum is a continuous signal, not a scheduled trigger — sizing it like an
    event is what put 22% of the account into one 1DTE call on 2026-09-01."""
    per_trade = (policy or {}).get("per_trade") or {}
    if income:
        key, fallback = "max_loss_pct_income", 2.5
    elif conviction:
        key, fallback = "max_loss_pct_conviction", 8.0
    elif catalyst:
        key, fallback = "max_loss_pct_catalyst", 20.0
    else:
        key, fallback = "max_loss_pct_default", 10.0
    pct = _num(per_trade.get(key))
    if pct is None:
        pct = fallback
    cap_usd = equity * pct / 100.0
    per_unit = unit_debit * 100.0
    if per_unit <= 0:
        return 0
    return int(cap_usd // per_unit)


# --- primitives --------------------------------------------------------------

def _as_et(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=ET)
    return dt.astimezone(ET)


def _num(v: Any) -> float | None:
    if v is None or isinstance(v, bool):
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    if f != f or f in (float("inf"), float("-inf")):
        return None
    return f


def _first_price(*candidates: Any) -> float | None:
    for c in candidates:
        f = _num(c)
        if f is not None and f > 0:
            return f
    return None


def _mid(q: Any) -> float | None:
    """Usable mid price out of a normalized chain quote; None if unquotable."""
    if not isinstance(q, dict):
        return None
    m = _num(q.get("mid"))
    if m is not None and m > 0:
        return m
    bid, ask = _num(q.get("bid")), _num(q.get("ask"))
    if bid is not None and ask is not None and ask > 0 and ask >= bid >= 0:
        mm = (bid + ask) / 2.0
        if mm > 0:
            return mm
    last = _num(q.get("last"))
    if last is not None and last > 0:
        return last
    return None


def _occ_meta(symbol: str) -> dict[str, Any] | None:
    """Parse (never build) an OCC symbol: ROOT + YYMMDD + C/P + strike*1000 (8 digits)."""
    if not isinstance(symbol, str) or len(symbol) < 16:
        return None
    root, tail = symbol[:-15], symbol[-15:]
    if _OCC_ROOT_RE.fullmatch(root) is None:
        return None
    m = _OCC_TAIL_RE.fullmatch(tail)
    if m is None:
        return None
    yymmdd, cp, strike_raw = m.groups()
    try:
        expiry = datetime.strptime(yymmdd, "%y%m%d").date()
    except ValueError:
        return None
    return {"root": root, "expiry": expiry, "type": cp, "strike": int(strike_raw) / 1000.0}
