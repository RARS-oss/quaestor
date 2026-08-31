"""Command-line interface: python -m quaestor {status|once|loop|verify|flatten}.

Subcommands:
- status   account + positions + today's P&L table (rich if installed, else plain text)
- once     run exactly one decision cycle and print what happened
- loop     Agent.run_forever with --interval seconds (default 300)
- verify   walk receipts/ verifying every bulla receipt + print the ledger summary
- flatten  emergency close-everything: builds CLOSE TradeIntents for every open option
           position and routes them through risk.judge + broker.execute — never a raw
           close_position for option structures, EXCEPT the documented v1 fallback:
           when no quote data is available for a contract, DELETE /v2/positions/{symbol}
           is used and the fallback is logged through audit.

Alpaca facts encoded here:
- GET /v2/account and /v2/positions work around the clock, so `status` never needs the
  market to be open.
- Options orders are TIF="day", limit only for our flows; flatten closes shorts
  (buy_to_close) BEFORE longs (sell_to_close) so no uncovered short is left behind.
- TradeIntent.limit_price is the SIGNED net per unit: closing a long is a credit
  (negative), closing a short is a debit (positive); marketable prices are built from
  the NBBO touch +/- policy execution.marketable_buffer_usd because paper fills happen
  at the touch (indicative feed adds quote noise the buffer absorbs).
- The wash-trade guard (cancel opposing open orders first, else 403) lives in broker.
"""
from __future__ import annotations

import argparse
import inspect
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

try:  # WSL/Linux has the system tz database; bare Windows python may lack tzdata
    from zoneinfo import ZoneInfo

    _ET: timezone | Any = ZoneInfo("America/New_York")
except Exception:  # pragma: no cover - Windows dev box without tzdata
    _ET = timezone(timedelta(hours=-4), "ET")  # EDT; the contest window is all DST
_OCC_RE = re.compile(r"^([A-Z][A-Z0-9]{0,5})(\d{6})([CP])(\d{8})$")

__all__ = ["main"]


# --------------------------------------------------------------------- helpers

def _f(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _money(value: Any) -> str:
    return f"${_f(value):,.2f}"


def _render_table(title: str, columns: list[str], rows: list[list[str]]) -> None:
    """Print with rich when available, otherwise as aligned plain text."""
    try:
        from rich.console import Console
        from rich.table import Table
    except ImportError:
        print(f"\n== {title} ==")
        widths = [len(str(c)) for c in columns]
        for row in rows:
            for i in range(len(columns)):
                cell = str(row[i]) if i < len(row) else ""
                widths[i] = max(widths[i], len(cell))
        header = "  ".join(str(c).ljust(widths[i]) for i, c in enumerate(columns))
        print(header)
        print("-" * len(header))
        for row in rows:
            print("  ".join((str(row[i]) if i < len(row) else "").ljust(widths[i])
                            for i in range(len(columns))))
        return
    table = Table(title=title)
    for column in columns:
        table.add_column(str(column))
    for row in rows:
        table.add_row(*[str(cell) for cell in row])
    Console().print(table)


def _now_et() -> datetime:
    try:
        from quaestor import clock
        return clock.now_et()
    except Exception:
        return datetime.now(tz=_ET)


def _occ(symbol: str) -> dict[str, Any] | None:
    """Parse an OCC symbol via orders.occ_parse, regex fallback if that fails."""
    try:
        from quaestor import orders as orders_mod
        parsed = orders_mod.occ_parse(symbol)
        if isinstance(parsed, dict) and parsed.get("root"):
            return parsed
    except Exception:
        pass
    match = _OCC_RE.match(symbol.strip().upper())
    if not match:
        return None
    from datetime import date
    yy, mm, dd = int(match.group(2)[0:2]), int(match.group(2)[2:4]), int(match.group(2)[4:6])
    try:
        expiry = date(2000 + yy, mm, dd)
    except ValueError:
        return None
    return {
        "root": match.group(1),
        "expiry": expiry,
        "type": match.group(3),
        "strike": int(match.group(4)) / 1000.0,
    }


def _execute_intent(
    broker: Any,
    intent: Any,
    chain: dict[str, dict],
    policy: dict,
    refresh: Callable[[], dict[str, dict]],
) -> Any:
    """broker.execute with zk_prefix="" plus chain_refresh when the broker supports it."""
    from quaestor.models import ExecutionReport

    kwargs: dict[str, Any] = {"zk_prefix": ""}
    try:
        if "chain_refresh" in inspect.signature(broker.execute).parameters:
            kwargs["chain_refresh"] = refresh
    except (TypeError, ValueError):
        pass
    try:
        return broker.execute(intent, chain, policy, **kwargs)
    except Exception as exc:
        return ExecutionReport(
            intent_id=intent.intent_id,
            client_order_id="",
            status="error",
            error=f"broker.execute raised: {exc!r}",
        )


# ---------------------------------------------------------------------- status

def cmd_status() -> int:
    from quaestor.broker import Broker
    from quaestor.config import load_settings

    settings = load_settings()
    broker = Broker(settings)
    account = broker.account_snapshot()

    day_open: float | None = None
    stale_day = ""
    state_path = settings.runs_dir / "portfolio_state.json"
    if state_path.exists():
        try:
            from quaestor import clock
            from quaestor.portfolio import PortfolioState
            state = PortfolioState.load(state_path)
            candidate = _f(getattr(state, "day_open_equity", 0.0))
            saved_day = str(getattr(state, "_day_key", ""))
            today_et = clock.now_et().date().isoformat()
            if candidate > 0 and saved_day == today_et:
                day_open = candidate
            else:
                stale_day = saved_day  # state predates today: Friday's open != today's
        except Exception:
            day_open = None

    account_rows: list[list[str]] = [
        ["mode", "PAPER" if settings.paper else "LIVE (!!)"],
        ["equity", _money(account.equity)],
        ["cash", _money(account.cash)],
        ["buying power", _money(account.buying_power)],
        ["options buying power", _money(account.options_buying_power)],
        ["options level", f"approved {account.options_approved_level} / "
                          f"trading {account.options_trading_level}"],
    ]
    if day_open is not None:
        pnl = account.equity - day_open
        account_rows.append(
            ["today P&L", f"{_money(pnl)} ({pnl / day_open * 100.0:+.2f}%)"]
        )
    else:
        detail = f"state from {stale_day}, no cycle yet today" if stale_day \
            else "no portfolio state yet"
        account_rows.append(["today P&L", f"n/a ({detail})"])
    _render_table("quaestor account", ["field", "value"], account_rows)

    positions = [p for p in list(account.positions or []) if isinstance(p, dict)]
    if not positions:
        print("no open positions.")
        return 0
    rows: list[list[str]] = []
    total_unrealized = 0.0
    for pos in positions:
        upl = _f(pos.get("unrealized_pl"))
        total_unrealized += upl
        rows.append([
            str(pos.get("symbol", "")),
            str(pos.get("asset_class", "")),
            str(pos.get("qty", "")),
            _money(pos.get("avg_entry_price")),
            _money(pos.get("current_price")),
            _money(pos.get("market_value")),
            f"{_money(upl)} ({_f(pos.get('unrealized_plpc')) * 100.0:+.2f}%)",
        ])
    rows.append(["TOTAL", "", "", "", "", "", _money(total_unrealized)])
    _render_table(
        "positions",
        ["symbol", "class", "qty", "avg entry", "current", "mkt value", "unrealized P&L"],
        rows,
    )
    return 0


# ------------------------------------------------------------------ once / loop

def cmd_once() -> int:
    from quaestor.agent import build_agent

    agent = build_agent()
    rec = agent.run_cycle()
    approved = sum(1 for v in rec.verdicts if isinstance(v, dict) and v.get("approved"))
    print(f"cycle {rec.cycle_id}")
    print(f"  intents:    {len(rec.intents)}")
    print(f"  verdicts:   {approved} approved / {len(rec.verdicts) - approved} rejected")
    print(f"  executions: {len(rec.executions)}")
    for execution in rec.executions:
        err = execution.get("error", "")
        print(
            f"    - {execution.get('client_order_id', '?')}: {execution.get('status', '')} "
            f"filled={execution.get('filled_qty', 0)} @ {execution.get('filled_avg_price', 0)}"
            + (f" err={err}" if err else "")
        )
    if rec.receipt_path:
        print(f"  receipt:    {rec.receipt_path}")
    for note in rec.notes:
        print(f"  note: {note}")
    print(f"  session:    {agent.audit.session_dir}")
    return 0


def cmd_loop(interval: int) -> int:
    from quaestor.agent import build_agent

    agent = build_agent()
    agent.run_forever(interval_s=max(30, int(interval)))
    return 0


# --------------------------------------------------------------------- rehearse

def cmd_replay(ref: str | None) -> int:
    """Re-derive sealed risk verdicts from signed inputs and confirm they reproduce.

    With no ref, replays every cycle that carries a replay block. Proves the
    agent's decisions are deterministic and reproducible from tamper-evident data,
    not arbitrary."""
    from quaestor.config import load_policy, load_settings
    from quaestor.replay import replay_cycle

    settings = load_settings()
    policy = load_policy()
    cells = settings.receipts_dir / "cells"

    refs: list[str]
    if ref:
        refs = [ref]
    else:
        refs = sorted(p.name for p in cells.glob("*") if (p / "decision.json").exists()) \
            if cells.exists() else []
    if not refs:
        print("replay: no sealed decision cycles found (run some cycles first).")
        return 0

    rows: list[list[str]] = []
    all_ok = True
    total_intents = reproduced = undecided = 0
    for r in refs:
        rep = replay_cycle(settings.receipts_dir, policy, r)
        if not rep.get("found"):
            rows.append([r[:22], "—", "—", rep.get("note", "not found")])
            continue
        results = rep.get("results", [])
        total_intents += len(results)
        reproduced += sum(1 for x in results if x.get("match"))
        # Determinism is the claim under test, and it is decided by whether the
        # verdicts re-derive — not by whether policy.yaml has since been edited.
        # A cycle we cannot decide (old receipt, no sealed policy, rules moved on)
        # is reported as undecided, never as a determinism failure.
        decidable = rep.get("decidable", rep.get("policy_digest_match", True))
        cycle_ok = bool(rep.get("all_match")) and decidable
        if not decidable:
            undecided += 1
        else:
            all_ok = all_ok and cycle_ok
        detail = f"{sum(1 for x in results if x.get('match'))}/{len(results)} verdicts"
        if not rep.get("policy_digest_match", True):
            detail += (" · policy changed, judged under the sealed copy"
                       if rep.get("judged_under") == "sealed" else
                       " · policy changed, no sealed copy")
        rows.append([
            str(rep.get("cycle_id", r))[:22],
            "✓" if rep.get("policy_digest_match") else "≠",
            "REPRODUCES" if cycle_ok else ("UNDECIDED" if not decidable else "MISMATCH"),
            detail,
        ])
    _render_table("quaestor replay — decisions re-derived from signed inputs",
                  ["cycle", "policy", "result", "detail"], rows)
    summary = (f"replay: {reproduced}/{total_intents} risk verdicts reproduced from "
               f"sealed inputs; ")
    summary += "all decidable cycles reproduce ✓" if all_ok else "some cycles did not reproduce ✗"
    if undecided:
        summary += (f" ({undecided} cycle(s) undecided — receipts predating the sealed "
                    f"policy body, judged under rules that have since changed)")
    print(summary)
    return 0 if all_ok else 1


def cmd_preflight() -> int:
    """Go/no-go check before the live competition run: keys, paper gate, options
    level, sealed toolchain, receipts writable, market clock. Prints a checklist
    and exits non-zero if anything critical is not ready."""
    from quaestor import clock
    from quaestor.broker import Broker
    from quaestor.config import load_calendar, load_policy, load_settings

    ok = True

    def check(label: str, passed: bool, detail: str = "", critical: bool = True) -> None:
        nonlocal ok
        if critical:
            ok = ok and passed
        mark = "PASS" if passed else ("FAIL" if critical else "warn")
        print(f"  [{mark}] {label}" + (f" — {detail}" if detail else ""))

    print("quaestor preflight — go/no-go for the live run\n" + "=" * 46)
    try:
        settings = load_settings()
        check("config loads + paper gate", settings.paper,
              f"trading_base={settings.trading_base}")
        check("API keys present", bool(settings.api_key and settings.api_secret),
              f"key={settings.api_key[:6]}…" if settings.api_key else "MISSING")
    except SystemExit as exc:
        check("config loads", False, f"load_settings refused: {exc}")
        print("preflight: NO-GO (config)")
        return 1

    try:
        with Broker(settings) as b:
            acct = b.account_snapshot()
            check("account reachable + active", acct.equity > 0,
                  f"${acct.equity:,.0f} equity")
            check("options level 3 (spreads)", acct.options_trading_level >= 3,
                  f"L{acct.options_trading_level}")

            # Freshness is a PRE-LAUNCH gate: the contest requires a brand-new
            # account, and equity alone cannot see it — an account that traded
            # back to flat still reads exactly $100,000. Look at positions and
            # order history too. Once our own run has begun the account
            # legitimately has history, so the check turns informational.
            started = any(settings.receipts_dir.glob("c-*.json"))
            try:
                positions = b.positions()
                history = b.recent_orders(limit=5, status="all")
            except Exception as exc:
                check("account history readable", False, repr(exc), critical=False)
            else:
                detail = (f"equity ${acct.equity:,.2f}, {len(positions)} position(s), "
                          f"{len(history)} order(s) in history")
                if started:
                    check("competition account (run already under way)", True,
                          detail + " — freshness gate was cleared at launch",
                          critical=False)
                else:
                    pristine = (abs(acct.equity - 100_000) < 1e-6
                                and not positions and not history)
                    check("fresh competition account — no prior history", pristine,
                          detail + ("" if pristine else
                                    " — the contest requires a brand-new $100,000 account"))
    except Exception as exc:
        check("account reachable", False, repr(exc))

    try:
        load_policy(); load_calendar()
        check("policy + calendar parse", True)
    except Exception as exc:
        check("policy + calendar parse", False, repr(exc))

    try:
        from quaestor.sealed_exec import SealedExecutor
        se = SealedExecutor(settings, settings.receipts_dir)
        check("sealed toolchain (bulla) available", se.available(),
              "QUAESTOR_SEALED will place every order inside a signed cell")
    except Exception as exc:
        check("sealed toolchain available", False, repr(exc), critical=False)

    try:
        probe = settings.receipts_dir / ".preflight"
        probe.write_text("ok", encoding="utf-8"); probe.unlink()
        check("receipts dir writable", True, str(settings.receipts_dir))
    except Exception as exc:
        check("receipts dir writable", False, repr(exc))

    try:
        now = clock.now_et()
        check("market clock", True,
              f"now {now:%Y-%m-%d %H:%M} ET, open={clock.is_market_open_now()}", critical=False)
    except Exception as exc:
        check("market clock", False, repr(exc), critical=False)

    print("=" * 46)
    print("PREFLIGHT: " + ("GO ✓ — cleared for the live run" if ok else "NO-GO ✗ — fix the FAILs above"))
    return 0 if ok else 1


def cmd_rehearse(place_order: bool = False) -> int:
    """Dress rehearsal: exercise the full decision path against LIVE data without
    trading (data -> signals -> strategy -> risk -> order payloads). With
    --place-order, also submit + cancel one real unfillable order to prove the
    execution path is live today. Safe to run when the market is closed."""
    from datetime import date, timedelta

    from quaestor import clock
    from quaestor import orders as orders_mod
    from quaestor import risk as risk_mod
    from quaestor import signals as signals_mod
    from quaestor import strategy as strategy_mod
    from quaestor import universe as universe_mod
    from quaestor.broker import Broker
    from quaestor.config import load_calendar, load_policy, load_settings
    from quaestor.data import MarketData
    from quaestor.models import (
        Leg, PositionIntent, Side, Structure, TradeIntent,
    )

    ok = True

    def check(label: str, passed: bool, detail: str = "") -> None:
        nonlocal ok
        ok = ok and passed
        mark = "PASS" if passed else "FAIL"
        line = f"  [{mark}] {label}"
        print(line + (f" — {detail}" if detail else ""))

    print("quaestor dress rehearsal\n" + "=" * 40)
    settings = load_settings()
    policy = load_policy()
    calendar = load_calendar()
    now = clock.now_et()
    print(f"  now (ET): {now.isoformat()}  market_open={clock.is_market_open_now()}")

    # 1. account / paper gate
    broker = Broker(settings)
    try:
        acct = broker.account_snapshot()
        check("account + paper gate", settings.paper and acct.equity > 0,
              f"{acct.equity:,.0f} equity, options L{acct.options_trading_level}, sealed={broker.sealed}")
        check("options level 3 (spreads)", acct.options_trading_level >= 3,
              f"L{acct.options_trading_level}")
    except Exception as exc:
        check("account snapshot", False, repr(exc))
        acct = None

    # 2. live market data
    data = MarketData(settings)
    core = list(dict.fromkeys(universe_mod.UNDERLYINGS))
    try:
        snaps = data.stock_snapshot(core)
        bars = data.stock_bars(core, timeframe="5Min", lookback_minutes=390)
        have_bars = sum(1 for u in core if bars.get(u))
        check("stock snapshots + bars", have_bars > 0,
              f"{have_bars}/{len(core)} underlyings have bars")
    except Exception as exc:
        check("stock data", False, repr(exc))
        snaps, bars = {}, {}

    # 3. option chain + greeks
    gte = now.date().isoformat()
    lte = (now.date() + timedelta(days=7)).isoformat()
    chains: dict[str, dict] = {}
    contracts: dict[str, list] = {}
    for u in core:
        try:
            contracts[u] = universe_mod.discover_contracts(
                settings, u, expiry_gte=gte, expiry_lte=lte, strike_band_pct=0.06)
            chains[u] = data.option_chain(u, expiry_gte=gte, expiry_lte=lte)
        except Exception as exc:
            check(f"option chain {u}", False, repr(exc))
            chains[u], contracts[u] = {}, []
    total_contracts = sum(len(c) for c in contracts.values())
    with_greeks = sum(
        1 for ch in chains.values() for q in ch.values() if q.get("delta") is not None)
    check("option chains discovered", total_contracts > 0,
          f"{total_contracts} contracts across {len(core)}")
    check("greeks present (may be sparse off-hours)", True,
          f"{with_greeks} quotes carry delta (informational)")

    # 4. full decision path (no execution)
    try:
        sigs = signals_mod.compute(bars, snaps)
        sig_desc = ", ".join(f"{u}:{s.direction:+d}@{s.strength:.2f}" for u, s in sigs.items())
        check("signals computed", len(sigs) > 0, sig_desc)
    except Exception as exc:
        check("signals", False, repr(exc))
        sigs = {}

    intents = []
    if acct is not None:
        try:
            ctx = strategy_mod.Context(
                settings=settings, policy=policy, calendar=calendar, account=acct,
                portfolio=_rehearsal_portfolio(settings, acct, now),
                signals=sigs, sentiment={}, chains=chains, contracts=contracts,
                now=now, due_events=list(clock.due_catalysts(calendar, now, set())),
            )
            intents = list(strategy_mod.decide(ctx))
            check("strategy.decide ran", True,
                  f"{len(intents)} intent(s) on current data "
                  f"(0 is normal when signals are flat / market closed)")
        except Exception as exc:
            check("strategy.decide", False, repr(exc))

    # judge + build payloads for whatever intents came out
    portfolio_state = {"halted_today": False, "halted_week": False, "day_pnl_pct": 0.0,
                       "week_pnl_pct": 0.0, "open_position_count": 0, "underlying_exposure": {}}
    for it in intents:
        try:
            verdict = risk_mod.judge(it, policy=policy, account=acct,
                                     portfolio_state=portfolio_state,
                                     chain=chains.get(it.underlying, {}), now=now)
            payload = orders_mod.build_order_payload(it, attempt=0)
            print(f"    intent {it.structure.value} {it.underlying}: "
                  f"approved={verdict.approved} legs={len(payload.get('legs', [payload]))} "
                  f"limit={payload.get('limit_price')}")
            if not verdict.approved:
                print(f"      rejected: {'; '.join(verdict.reasons)}")
        except Exception as exc:
            check(f"judge/build for {it.intent_id}", False, repr(exc))

    # 5. forced order-path proof (optional): a real, unfillable spread, cancelled at once
    if place_order and acct is not None:
        try:
            _prove_order_path(broker, settings, chains, contracts, now, check,
                              Leg, Side, PositionIntent, Structure, TradeIntent, orders_mod)
        except Exception as exc:
            check("order path (submit+cancel)", False, repr(exc))
    elif not place_order:
        print("  [skip] order path proof (pass --place-order to submit+cancel a real test order)")

    broker.close()
    print("=" * 40)
    print("REHEARSAL: " + ("ALL GREEN ✓" if ok else "some checks FAILED ✗"))
    return 0 if ok else 1


def _rehearsal_portfolio(settings, account, now):
    from quaestor.portfolio import PortfolioState
    pf = PortfolioState(settings.runs_dir / "portfolio_state.json")
    try:
        pf.refresh(account, now)
    except Exception:
        pass
    return pf


def _prove_order_path(broker, settings, chains, contracts, now, check,
                      Leg, Side, PositionIntent, Structure, TradeIntent, orders_mod):
    """Submit one real SPY debit vertical priced far below its true debit, confirm
    acceptance, cancel it, and VERIFY the cancel took.

    Two traps this walks around, both of which bit us live on 2026-08-31:
      * "$0.01 can't fill" is FALSE on Alpaca paper. Paper fills at the NBBO
        touch, so a $0.01 limit on penny-quoted deep-OTM strikes fills instantly.
        The legs are therefore chosen from strikes that carry a real ask, where a
        $0.01 net debit is nowhere near marketable.
      * A cancel that is not read back proves nothing: DELETE answers 422 for an
        already-filled order, which is exactly the case we must catch.
    """
    import time

    spy = contracts.get("SPY", [])
    quotes = chains.get("SPY", {})

    def _ask(sym: str) -> float | None:
        q = quotes.get(sym) or {}
        a = q.get("ask")
        try:
            return float(a) if a is not None else None
        except (TypeError, ValueError):
            return None

    calls = sorted(
        (c for c in spy if str(c.get("type")) == "call" and c.get("tradable", True)),
        key=lambda c: float(c.get("strike_price", 0)))
    if len(calls) < 2:
        check("order path (submit+cancel)", False, "not enough SPY call contracts")
        return

    # Adjacent strikes whose long leg carries real premium: the deeper-ITM leg must
    # be worth well over our $0.01 limit, so the spread cannot be marketable.
    pair = None
    for lo, hi in zip(calls, calls[1:]):
        a_lo, a_hi = _ask(lo["symbol"]), _ask(hi["symbol"])
        if a_lo is not None and a_hi is not None and a_lo >= 0.20 and a_lo > a_hi:
            pair = (lo, hi, a_lo - a_hi)
            break
    if pair is None:
        check("order path (submit+cancel)", False,
              "no SPY call pair with a real ask — refusing to send an order that could fill")
        return
    buy_c, sell_c, true_debit = pair

    intent = TradeIntent(
        underlying="SPY", structure=Structure.VERTICAL_DEBIT,
        legs=[Leg(buy_c["symbol"], Side.BUY, 1, PositionIntent.BUY_TO_OPEN),
              Leg(sell_c["symbol"], Side.SELL, 1, PositionIntent.SELL_TO_OPEN)],
        qty=1, limit_price=0.01,
        thesis="rehearsal — priced far under the real debit, cancel at once",
        max_loss_usd=1.0,
    )
    payload = orders_mod.build_order_payload(intent, attempt=1)
    order, rid = broker.submit(payload)
    accepted = str(order.get("status", "")) in ("accepted", "new", "pending_new", "held")
    check("order path: submit accepted", accepted,
          f"{buy_c['symbol']}/{sell_c['symbol']} limit=$0.01 vs real debit ≈${true_debit:.2f} "
          f"status={order.get('status')} rid={rid[:8]}")

    oid = str(order.get("id", ""))
    if not oid:
        check("order path: cancel verified", False, "broker returned no order id")
        return

    broker.cancel(oid)

    status, filled_qty = "", 0.0
    for _ in range(10):
        got = broker.get_order(oid) or {}
        status = str(got.get("status", ""))
        try:
            filled_qty = float(got.get("filled_qty") or 0)
        except (TypeError, ValueError):
            filled_qty = 0.0
        if status in ("canceled", "expired", "rejected", "filled", "done_for_day"):
            break
        time.sleep(0.5)

    check("order path: cancel verified", status in ("canceled", "expired", "rejected"),
          f"final status={status or 'unknown'}")

    if filled_qty > 0 or status == "filled":
        # The premise failed and we are now holding a real position on the
        # competition account. Say so loudly and flatten it rather than leaving it.
        flattened = []
        for leg in (buy_c["symbol"], sell_c["symbol"]):
            try:
                broker.close_position(leg)
                flattened.append(leg)
            except Exception:
                pass
        check("order path: rehearsal order did not fill", False,
              f"FILLED {filled_qty:g} — a rehearsal order must never fill; "
              f"flattened {len(flattened)}/2 leg(s), CHECK THE ACCOUNT")
    else:
        check("order path: rehearsal order did not fill", True,
              "no fill — the account is untouched")

def cmd_verify() -> int:
    from quaestor.config import load_settings
    from quaestor.receipts import ReceiptPress

    settings = load_settings()
    receipts_dir = settings.receipts_dir
    if not receipts_dir.exists():
        print(f"verify: no receipts directory at {receipts_dir}")
        return 0
    press = ReceiptPress(settings, receipts_dir)
    files = sorted(receipts_dir.glob("*.json"))
    if not files:
        print("verify: no receipts found.")
    rows: list[list[str]] = []
    bad = 0
    for path in files:
        try:
            ok = bool(press.verify(path))
            detail = "signature valid" if ok else "verification failed"
        except Exception as exc:
            ok = False
            detail = f"verify raised: {exc!r}"
        if not ok:
            bad += 1
        rows.append([path.name, "OK" if ok else "FAIL", detail])
    if rows:
        _render_table("bulla receipts", ["receipt", "verdict", "detail"], rows)
    try:
        ledger = press.ledger_summary()
        if isinstance(ledger, dict) and ledger:
            _render_table(
                "ledger summary", ["key", "value"],
                [[str(k), str(v)] for k, v in ledger.items()],
            )
    except Exception as exc:
        print(f"verify: ledger summary unavailable: {exc!r}")
    print(f"verify: {len(files) - bad}/{len(files)} receipts verified OK")
    return 1 if bad else 0


# ---------------------------------------------------------------------- anchor

def cmd_anchor() -> int:
    """Witness the run ledger's head into runs/anchor.jsonl (+ external push).

    Anchors the sealed ledger head when receipts/sealed-ledger.jsonl exists,
    otherwise the honest-mode receipts/ledger.jsonl. The witness line carries only
    opaque digests, so pushing it to a public repo leaks no strategy while making
    the ledger tail durable against truncation.
    """
    from quaestor.anchor import Anchor
    from quaestor.config import load_settings

    settings = load_settings()
    anchor = Anchor(settings)
    sealed = settings.receipts_dir / "sealed-ledger.jsonl"
    honest = settings.receipts_dir / "ledger.jsonl"
    ledger_path = sealed if sealed.exists() else honest

    entry = anchor.anchor_from_ledger(str(ledger_path))
    if not entry:
        print(f"anchor: no ledger to witness (looked at {sealed.name} then {honest.name})")
        return 1
    _render_table(
        f"anchor — witness of {ledger_path.name}",
        ["field", "value"],
        [[str(k), str(v)] for k, v in entry.items()],
    )
    import os as _os
    if anchor.push_external(entry):
        print("anchor: entry PUSHED to the external witness (durable off-box).")
    elif _os.environ.get("QUAESTOR_WITNESS_DIR") or _os.environ.get("QUAESTOR_WITNESS_REPO"):
        print("anchor: external push did NOT land — see stderr; the local witness "
              "was written but is not durable against truncation.")
    else:
        print("anchor: external witness not configured "
              "(set QUAESTOR_WITNESS_DIR + QUAESTOR_WITNESS_REPO to publish).")
    return 0


def cmd_anchor_verify() -> int:
    """Recompute the runs/anchor.jsonl witness chain and print the verdict."""
    from quaestor.anchor import Anchor
    from quaestor.config import load_settings

    settings = load_settings()
    anchor = Anchor(settings)
    result = anchor.verify_anchor_chain()
    _render_table(
        "anchor chain", ["field", "value"], [[str(k), str(v)] for k, v in result.items()]
    )
    if result.get("ok"):
        print(f"anchor-verify: chain intact over {result.get('entries')} entr"
              f"{'y' if result.get('entries') == 1 else 'ies'} ✓")
        return 0
    print(f"anchor-verify: chain BROKEN at entry {result.get('break_at')} ✗")
    return 1


# --------------------------------------------------------------------- flatten

def cmd_flatten() -> int:
    """Close every open option position through the risk CLOSE path, audited."""
    from quaestor import risk as risk_mod
    from quaestor.audit import AuditTrail
    from quaestor.broker import Broker
    from quaestor.config import load_policy, load_settings
    from quaestor.data import MarketData
    from quaestor.models import (
        CycleRecord,
        Leg,
        PositionIntent,
        RiskCheck,
        RiskVerdict,
        Side,
        Structure,
        TradeIntent,
    )

    settings = load_settings()
    policy = load_policy()
    broker = Broker(settings)
    data = MarketData(settings)
    audit = AuditTrail(settings.runs_dir)

    account = broker.account_snapshot()
    audit.snapshot_positions(account)
    positions = [
        p
        for p in list(account.positions or [])
        if isinstance(p, dict)
        and str(p.get("asset_class", "")) == "us_option"
        and abs(_f(p.get("qty"))) > 0
    ]
    if not positions:
        print("flatten: no open option positions.")
        return 0
    # Buy back shorts before selling longs: never leave a short leg uncovered.
    positions.sort(key=lambda p: _f(p.get("qty")))

    symbols = [str(p.get("symbol", "")) for p in positions]
    chain: dict[str, dict] = {}
    try:
        chain = data.option_snapshots(symbols)
        if not isinstance(chain, dict):
            chain = {}
    except Exception as exc:
        print(f"flatten: option_snapshots failed ({exc!r}) — falling back where needed")

    now = _now_et()
    buffer_usd = _f((policy.get("execution") or {}).get("marketable_buffer_usd"), 0.02)
    rec = CycleRecord(
        cycle_id=f"flatten-{time.strftime('%Y%m%d-%H%M%S', time.gmtime())}",
        started_at=time.time(),
        account=account,
    )
    rec.notes.append("CLI flatten: emergency close-everything")

    # Portfolio state for risk.judge (CLOSE bypasses most gates but judge wants the dict).
    day_open = account.equity
    realized = 0.0
    halted = False
    try:
        from quaestor.portfolio import PortfolioState
        state = PortfolioState.load(settings.runs_dir / "portfolio_state.json")
        day_open = _f(getattr(state, "day_open_equity", day_open), day_open) or day_open
        realized = _f(getattr(state, "realized_pnl_today", 0.0))
        halted = bool(getattr(state, "halted_today", False))
    except Exception:
        pass
    exposure: dict[str, float] = {}
    for pos in positions:
        parsed = _occ(str(pos.get("symbol", "")))
        root = str(parsed.get("root")) if parsed else str(pos.get("symbol", ""))
        exposure[root] = exposure.get(root, 0.0) + abs(_f(pos.get("market_value")))
    strategy_keys = set()
    for pos in positions:
        sym = str(pos.get("symbol") or "")
        strategy_keys.add((sym[-15:-9] if len(sym) >= 15 else "", _occ(sym) and _occ(sym).get("root")))
    portfolio_state: dict[str, Any] = {
        "day_open_equity": day_open,
        "week_open_equity": day_open,
        "realized_pnl_today": realized,
        "halted_today": halted,
        "fired_events": [],
        "open_option_positions": positions,
        # The key risk.judge reads (strategy count), plus the legacy raw row count:
        "open_position_count": len(strategy_keys),
        "open_positions_count": len(positions),
        "underlying_exposure": exposure,
    }

    def refresh() -> dict[str, dict]:
        return data.option_snapshots(symbols)

    rows: list[list[str]] = []
    failures = 0
    for pos in positions:
        symbol = str(pos.get("symbol", ""))
        signed_qty = _f(pos.get("qty"))
        qty = max(1, int(round(abs(signed_qty))))
        closing_long = signed_qty > 0
        snap = chain.get(symbol) or {}
        bid = _f(snap.get("bid"))
        ask = _f(snap.get("ask"))

        if (closing_long and bid <= 0) or (not closing_long and ask <= 0):
            # Documented v1 fallback: no quote data -> DELETE /v2/positions/{symbol}.
            request = {"symbol": symbol, "qty": qty, "reason": "no quote data for CLOSE intent"}
            try:
                response = broker.close_position(symbol)
                if not isinstance(response, dict):
                    response = {"raw": str(response)}
                audit.log_order("close_position_fallback", request, response, "")
                rec.notes.append(f"{symbol}: close_position fallback used (no quote data)")
                rows.append([symbol, "close_position", "-", str(qty), "sent (fallback)"])
            except Exception as exc:
                failures += 1
                audit.log_order("close_position_fallback", request, {"error": repr(exc)}, "")
                rows.append([symbol, "close_position", "-", str(qty), f"FAILED: {exc!r}"])
            continue

        if closing_long:
            side, position_intent = Side.SELL, PositionIntent.SELL_TO_CLOSE
            # Selling to close = credit -> negative signed net; marketable at bid - buffer.
            limit_price = -max(0.01, round(bid - buffer_usd, 2))
            max_loss = 0.0  # the closing order itself cannot lose more than the premium given up
        else:
            side, position_intent = Side.BUY, PositionIntent.BUY_TO_CLOSE
            # Buying back a short = debit -> positive signed net; marketable at ask + buffer.
            limit_price = round(ask + buffer_usd, 2)
            max_loss = limit_price * 100.0 * qty

        parsed = _occ(symbol) or {}
        expiry = parsed.get("expiry")
        expiry_iso = expiry.isoformat() if hasattr(expiry, "isoformat") else ""
        intent = TradeIntent(
            underlying=str(parsed.get("root") or symbol),
            structure=Structure.CLOSE,
            legs=[Leg(symbol=symbol, side=side, ratio_qty=1, position_intent=position_intent)],
            qty=qty,
            limit_price=limit_price,
            thesis="CLI flatten: close all open option positions",
            max_loss_usd=max_loss,
            is_0dte=bool(expiry_iso and expiry_iso == now.date().isoformat()),
            expiry=expiry_iso,
        )
        # Let orders.marketable_limit refine the price; never accept a sign flip.
        try:
            from quaestor import orders as orders_mod
            refined = float(orders_mod.marketable_limit(intent, chain, policy))
            if refined != 0.0 and (refined < 0) == (intent.limit_price < 0):
                intent.limit_price = refined
        except Exception:
            pass
        rec.intents.append(intent.to_dict())

        try:
            verdict = risk_mod.judge(
                intent,
                policy=policy,
                account=account,
                portfolio_state=portfolio_state,
                chain=chain,
                now=now,
            )
        except Exception as exc:
            verdict = RiskVerdict(
                approved=False,
                checks=[RiskCheck("judge_exception", False, f"risk.judge raised: {exc!r}")],
                policy_digest=str(policy.get("digest", "")),
                intent_id=intent.intent_id,
            )
        rec.verdicts.append(verdict.to_dict())
        if not verdict.approved:
            failures += 1
            reason = "; ".join(verdict.reasons) or "unknown"
            rows.append([symbol, "close intent", f"{intent.limit_price:+.2f}", str(qty),
                         f"REJECTED: {reason}"])
            continue

        report = _execute_intent(broker, intent, chain, policy, refresh)
        rec.executions.append(report.to_dict())
        request_id = report.request_ids[0] if report.request_ids else ""
        try:
            audit.log_order("execute", intent.to_dict(), report.to_dict(), request_id)
        except Exception:
            pass
        outcome = report.status or "unknown"
        if report.status not in ("filled", "partially_filled", "accepted", "new", "pending_new"):
            failures += 1
            if report.error:
                outcome = f"{outcome}: {report.error}"
        rows.append([symbol, "close intent", f"{intent.limit_price:+.2f}", str(qty), outcome])

    audit.record_cycle(rec)
    try:
        audit.summary()
    except Exception:
        pass
    _render_table("flatten results", ["symbol", "route", "net limit", "qty", "result"], rows)
    print(f"flatten: {len(positions) - failures}/{len(positions)} positions routed; "
          f"audit session {audit.session_dir}")
    return 1 if failures else 0


# ---------------------------------------------------------------------- bundle

def cmd_bundle(out: str | None = None) -> int:
    """Assemble the offline proof bundle and print its path + a one-line summary."""
    from pathlib import Path

    from quaestor.bundle import build_bundle, stats_from_receipts
    from quaestor.config import load_settings

    settings = load_settings()
    out_dir = Path(out).expanduser() if out else None
    bundle_dir = build_bundle(settings, out_dir)
    stats = stats_from_receipts(bundle_dir / "receipts")
    print(f"bundle: {bundle_dir}")
    print(
        f"  {stats['total']} receipts "
        f"({stats['sealed']} sealed / {stats['decision']} decision, "
        f"{stats['seal_held']} seal-held); open index.html to verify offline"
    )
    return 0


# ------------------------------------------------------------------------ main

def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="quaestor",
        description="quaestor — autonomous options-trading agent (Alpaca paper only).",
    )
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("status", help="account + positions + today's P&L (market can be closed)")
    sub.add_parser("once", help="run exactly one decision cycle")
    loop_parser = sub.add_parser("loop", help="run the autonomous loop (run_forever)")
    loop_parser.add_argument(
        "--interval", type=int, default=300, metavar="SECONDS",
        help="seconds between decision cycles during market hours (default: 300)",
    )
    sub.add_parser("verify", help="verify all bulla receipts + print the ledger summary")
    sub.add_parser("anchor", help="witness the ledger head into runs/anchor.jsonl (+ external push)")
    sub.add_parser("anchor-verify", help="recompute + verify the anchor.jsonl witness chain")
    bundle_parser = sub.add_parser(
        "bundle",
        help="package the week (receipts + verifier + index) for offline verification",
    )
    bundle_parser.add_argument(
        "--out", type=str, default=None, metavar="DIR",
        help="output folder for the bundle (default: runs/bundle)",
    )
    sub.add_parser("flatten", help="emergency: close every open option position (audited)")
    reh = sub.add_parser("rehearse", help="dress rehearsal: full decision path on live data, no trading")
    reh.add_argument("--place-order", action="store_true",
                     help="also submit + cancel one real unfillable order to prove the execution path")
    rep = sub.add_parser("replay", help="re-derive sealed risk verdicts from signed inputs (determinism proof)")
    rep.add_argument("ref", nargs="?", default=None,
                     help="a cycle id or receipt path; default: every sealed cycle")
    sub.add_parser("preflight", help="go/no-go readiness check before the live run")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        if args.command == "status":
            return cmd_status()
        if args.command == "once":
            return cmd_once()
        if args.command == "loop":
            return cmd_loop(args.interval)
        if args.command == "verify":
            return cmd_verify()
        if args.command == "anchor":
            return cmd_anchor()
        if args.command == "anchor-verify":
            return cmd_anchor_verify()
        if args.command == "bundle":
            return cmd_bundle(args.out)
        if args.command == "rehearse":
            return cmd_rehearse(place_order=args.place_order)
        if args.command == "replay":
            return cmd_replay(args.ref)
        if args.command == "preflight":
            return cmd_preflight()
        if args.command == "flatten":
            return cmd_flatten()
    except KeyboardInterrupt:
        print("\ninterrupted.")
        return 130
    except Exception as exc:
        print(f"quaestor: {args.command} failed: {exc!r}", file=sys.stderr)
        return 1
    print(f"quaestor: unknown command {args.command!r}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
