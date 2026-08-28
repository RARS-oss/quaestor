"""The quaestor brain: three deterministic playbooks -> list[TradeIntent].

Role: step 4 of the decision cycle. decide(ctx) reads signals, sentiment,
option chains and open positions and proposes fully-specified TradeIntents.
It NEVER talks to the network; risk.judge() gates every intent afterwards and
broker.execute() does the marketable-limit dance.

Playbooks (each returns [] when its conditions are not met):
1. Core momentum debit vertical (SPY/QQQ): signal.strength >= 0.6 and
   direction != 0 -> buy ~0.45-delta / sell ~0.25-delta, same expiry (0-3 DTE),
   qty sized so max_loss ~= policy per-trade cap. Skipped when a position is
   already open on that underlying+direction.
2. Catalyst straddle (tag from ctx.due_events, e.g. NFP_OPEN_PLAY): long ATM
   0DTE straddle (BUY call + BUY put, same strike/expiry, ratio 1:1), sized to
   the catalyst cap, catalyst_tag set; exits handled by flatten rules.
3. Exit management: CLOSE intents when unrealized <= -50% of debit (stop),
   >= +100% (target), 0DTE near policy flat_0dte_by_et, or the ALL_CASH event
   is due (final day -> close everything, no new entries).

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

import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import TYPE_CHECKING, Any, Iterator
from zoneinfo import ZoneInfo

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
MIN_VERTICAL_STRENGTH: float = 0.6
VERTICAL_LONG_DELTA: float = 0.45
VERTICAL_SHORT_DELTA: float = 0.25
VERTICAL_TARGET_DTE: int = 1
VERTICAL_MAX_DTE: int = 3
ATM_DELTA: float = 0.50
STOP_PLPC: float = -0.5          # close at -50% of debit
TARGET_PLPC: float = 1.0         # close at +100% of debit
FLAT_0DTE_LEAD_MIN: int = 10     # start flattening 0DTE this many min before deadline
FLATTEN_TAG: str = "ALL_CASH"
# Which calendar tags fire the straddle playbook, and on which underlying.
STRADDLE_PLAYS: dict[str, str] = {
    "NFP_OPEN_PLAY": "SPY",
    "NFP": "SPY",
    "ADP": "SPY",
    "JOLTS": "SPY",
    "CLAIMS": "SPY",
    "ISM_MFG": "SPY",
    "ISM_SVC": "SPY",
    "AVGO_EARNINGS": "QQQ",
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


def decide(ctx: Context) -> list[TradeIntent]:
    """Run all playbooks. Exit intents first; ALL_CASH suppresses new entries."""
    intents: list[TradeIntent] = []
    all_cash = any(str(ev.get("tag") or "") == FLATTEN_TAG for ev in ctx.due_events or [])
    intents.extend(_exit_intents(ctx, all_cash))
    if all_cash:
        return intents
    intents.extend(_catalyst_straddles(ctx))
    intents.extend(_core_verticals(ctx))
    return intents


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


# --- playbook 2: catalyst straddle -------------------------------------------

def _catalyst_straddles(ctx: Context) -> list[TradeIntent]:
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

def _exit_intents(ctx: Context, all_cash: bool) -> list[TradeIntent]:
    out: list[TradeIntent] = []
    now = _as_et(ctx.now)
    for pos in _option_positions(ctx.account.positions):
        plpc = _pos_plpc(pos)
        reason = ""
        if all_cash:
            reason = "ALL_CASH: final-day flatten before submission deadline"
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


def _choose_strike(chain: dict[str, dict], target_delta: float, opt_type: str) -> str | None:
    """universe.pick_strike when importable and its answer validates, else local."""
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
    return _pick_strike_local(chain, target_delta, opt_type)


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


def _size_by_cap(equity: float, policy: dict, unit_debit: float, *, catalyst: bool) -> int:
    """Contracts (strategy units) so that unit_debit*100*qty <= per-trade cap."""
    per_trade = (policy or {}).get("per_trade") or {}
    key = "max_loss_pct_catalyst" if catalyst else "max_loss_pct_default"
    pct = _num(per_trade.get(key))
    if pct is None:
        pct = 20.0 if catalyst else 10.0
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
