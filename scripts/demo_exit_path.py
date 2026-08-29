"""Test the EXIT path end to end with a synthetic held position.

The market is closed, so we can't hold a real position — but the close/flatten
logic (stops, targets, 0DTE flatten, final-day ALL_CASH) is critical and has
never run end to end. This injects a synthetic long SPY debit vertical into the
account snapshot and drives the ALL_CASH flatten through: strategy pairs the
spread, emits ONE mleg CLOSE (both legs reversed), risk judges it, the payload
builds, and it seals in a cell (real order queues while closed, cancelled).

Run (WSL):  QUAESTOR_SEALED=1 ~/hack/venv/bin/python scripts/demo_exit_path.py
"""
from __future__ import annotations

import os
from datetime import timedelta

os.environ.setdefault("QUAESTOR_SEALED", "1")

from quaestor import clock
from quaestor import orders as orders_mod
from quaestor import risk as risk_mod
from quaestor import strategy as strategy_mod
from quaestor.agent import build_agent
from quaestor.models import CycleRecord

LONG = "SPY260904C00650000"   # long leg (we own +1)
SHORT = "SPY260904C00655000"  # short leg (we are -1) — together: a debit vertical


def synth_positions() -> list[dict]:
    """Alpaca-shaped option position rows for a held 650/655 call debit vertical."""
    return [
        {"symbol": LONG, "asset_class": "us_option", "side": "long",
         "qty": "1", "avg_entry_price": "2.00", "current_price": "1.20",
         "market_value": "120", "unrealized_pl": "-80", "unrealized_plpc": "-0.40"},
        {"symbol": SHORT, "asset_class": "us_option", "side": "short",
         "qty": "-1", "avg_entry_price": "1.00", "current_price": "0.55",
         "market_value": "-55", "unrealized_pl": "45", "unrealized_plpc": "0.45"},
    ]


def banner(s: str, m: str) -> None:
    print(f"\n\033[1m[{s}]\033[0m {m}")


def main() -> int:
    agent = build_agent()
    print("=" * 64)
    print("  quaestor — EXIT path test (synthetic held vertical, ALL_CASH)")
    print("=" * 64)

    now = clock.now_et().replace(hour=10, minute=30, second=0, microsecond=0)
    while now.weekday() >= 5:
        now = now + timedelta(days=1)

    banner("1 account", "snapshot + INJECT a synthetic long SPY 650/655 vertical")
    account = agent.broker.account_snapshot()
    account.positions = synth_positions()
    for p in account.positions:
        print(f"    holding {p['symbol']} qty {p['qty']} @ {p['avg_entry_price']} "
              f"(now {p['current_price']}, uPL {p['unrealized_plpc']})")

    banner("2 data", "live quotes for the held legs")
    today = now.date()
    chain = agent.data.option_chain("SPY", expiry_gte=today.isoformat(),
                                    expiry_lte=(today + timedelta(days=8)).isoformat())
    chains = {"SPY": chain}
    for s in (LONG, SHORT):
        q = chain.get(s, {})
        print(f"    {s}: bid {q.get('bid')} ask {q.get('ask')}")

    banner("3 strategy", "ALL_CASH flatten — expect ONE mleg CLOSE (both legs reversed)")
    ctx = strategy_mod.Context(
        settings=agent.settings, policy=agent.policy, calendar=agent.calendar,
        account=account, portfolio=agent.portfolio, signals={}, sentiment={},
        chains=chains, contracts={"SPY": []}, now=now,
        due_events=[{"tag": "ALL_CASH", "desc": "final-day flatten", "time": "10:30"}])
    intents = list(strategy_mod.decide(ctx))
    close_intents = [i for i in intents if i.structure.value == "close"]
    if not close_intents:
        print("    NO close intent produced — exit path did not fire. "
              f"(intents={[i.structure.value for i in intents]})")
        return 1
    for it in close_intents:
        legs = ", ".join(f"{l.symbol}:{l.side.value}/{l.position_intent.value}" for l in it.legs)
        print(f"    → CLOSE {it.underlying} x{it.qty} limit {it.limit_price:+.2f} "
              f"({'mleg' if it.is_multileg else 'single'}) [{legs}]")

    banner("4 risk", "risk.judge on the close (close bypasses entry gates, keeps sanity)")
    portfolio_state = agent._portfolio_state_dict(account)
    approved = []
    for it in close_intents:
        v = risk_mod.judge(it, policy=agent.policy, account=account,
                           portfolio_state=portfolio_state, chain=chain, now=now)
        print(f"    {'APPROVED' if v.approved else 'REJECTED'} {it.intent_id}: "
              + ("all gates passed" if v.approved else "; ".join(v.reasons)))
        if v.approved:
            approved.append((it, v))

    banner("5 payload", "build the Alpaca order payload for the close")
    for it, _ in approved:
        payload = orders_mod.build_order_payload(it, attempt=0)
        print(f"    {payload.get('order_class', 'simple')} qty {payload.get('qty')} "
              f"limit {payload.get('limit_price')} legs {len(payload.get('legs', [payload]))}")

    if approved:
        banner("6 SEAL", "\033[36msealing the close order inside a bulla cell\033[0m")
        rec = CycleRecord(cycle_id=f"exitpath-{now:%H%M%S}", started_at=0.0)
        agent._execute_sealed(rec, approved, chains, {}, rec.notes)
        for e in rec.executions:
            print(f"    close {e.get('client_order_id')}: status={e.get('status')} "
                  f"· {len(e.get('request_ids', []))} sealed calls")
        if rec.receipt_path and agent.receipts:
            print(f"    receipt {rec.receipt_path}")
            print(f"    verify: {'INTACT ✓' if agent.receipts.verify(rec.receipt_path) else 'FAILED ✗'}")

    banner("done", "exit path works: paired spread → reversed mleg CLOSE → risk → seal.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
