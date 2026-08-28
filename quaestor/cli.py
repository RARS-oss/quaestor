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


# ---------------------------------------------------------------------- verify

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
    sub.add_parser("flatten", help="emergency: close every open option position (audited)")
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
