"""Watch the WHOLE pipeline fire once, end to end, with a forced signal.

The market is closed on weekends, so the strategy normally produces 0 intents and
we never see the full live path. This drives one complete cycle with a forced
strong signal + a market-open clock, so the agent actually: decides -> risk-gates
-> builds a marketable order -> SEALS it in a bulla cell (real order, queues while
closed, cancelled in-cell) -> writes a signed receipt -> anchors the ledger head.

Safe: runs on whatever account .env points at (use the dev account), the order
queues (market closed) and is cancelled inside the cell.

Run (WSL):  QUAESTOR_SEALED=1 ~/hack/venv/bin/python scripts/demo_full_cycle.py
"""
from __future__ import annotations

import os
from datetime import datetime, timedelta

os.environ.setdefault("QUAESTOR_SEALED", "1")

from quaestor import clock
from quaestor import orders as orders_mod
from quaestor import risk as risk_mod
from quaestor import strategy as strategy_mod
from quaestor import universe as universe_mod
from quaestor.agent import build_agent
from quaestor.models import CycleRecord
from quaestor.signals import Signal
from quaestor.zk import ZkProver


def banner(step: str, msg: str) -> None:
    print(f"\n\033[1m[{step}]\033[0m {msg}")


def main() -> int:
    agent = build_agent()
    print("=" * 66)
    print("  quaestor — full-cycle dry run (forced signal, market closed)")
    print("=" * 66)
    print(f"  sealed executor: {'ON' if agent.sealed_executor else 'OFF (set QUAESTOR_SEALED=1)'}")

    # A Monday-open clock so the strategy's timing gates allow an entry.
    now = clock.now_et().replace(hour=10, minute=0, second=0, microsecond=0)
    while now.weekday() >= 5:  # roll to the next weekday
        now = now + timedelta(days=1)
    banner("1 clock", f"pretending it is {now:%A %H:%M} ET (market-open window)")

    banner("2 account", "snapshotting the account...")
    account = agent.broker.account_snapshot()
    print(f"    equity ${account.equity:,.0f} · options L{account.options_trading_level} · sealed path")

    banner("3 data", "pulling a live SPY option chain + contracts...")
    today = now.date()
    lte = (today + timedelta(days=3)).isoformat()
    chains = {"SPY": agent.data.option_chain("SPY", expiry_gte=today.isoformat(), expiry_lte=lte)}
    contracts = {"SPY": universe_mod.discover_contracts(
        agent.settings, "SPY", expiry_gte=today.isoformat(), expiry_lte=lte, strike_band_pct=0.05)}
    print(f"    SPY: {len(chains['SPY'])} chain quotes, {len(contracts['SPY'])} contracts")

    # FORCE a strong bullish signal so the core momentum-vertical playbook fires.
    banner("4 signal", "\033[33mFORCING a strong bullish SPY signal\033[0m (this is the dry-run injection)")
    sigs = {"SPY": Signal(underlying="SPY", direction=1, strength=0.9,
                          features={"forced": 1.0})}

    banner("5 strategy", "strategy.decide(...) — what does the agent want to do?")
    ctx = strategy_mod.Context(
        settings=agent.settings, policy=agent.policy, calendar=agent.calendar,
        account=account, portfolio=agent.portfolio, signals=sigs, sentiment={},
        chains=chains, contracts=contracts, now=now, due_events=[])
    intents = list(strategy_mod.decide(ctx))
    if not intents:
        print("    strategy produced 0 intents on this (weekend/stale) chain — "
              "nothing to seal. Try again during a live session.")
        return 0
    # Dry-run tweaks: cap size to 1 lot (fast/safe) and — because weekend quotes
    # are artificially wide — relax ONLY the spread-quality gate so we can walk
    # the full chain through to the seal. Every other gate stays real.
    import copy
    demo_policy = copy.deepcopy(agent.policy)
    demo_policy["per_trade"]["max_spread_quality"] = 0.60
    print("    \033[33m(dry-run: capping to 1 lot; relaxing only the spread gate — "
          "weekend quotes are wide)\033[0m")
    from dataclasses import replace as _replace
    intents = [_replace(it, qty=1) for it in intents]
    for it in intents:
        print(f"    → {it.structure.value} {it.underlying} x{it.qty} "
              f"limit {it.limit_price:+.2f} — {it.thesis[:70]}")

    banner("6 risk", "risk.judge(...) — deterministic gates (policy sha256 in every receipt)")
    portfolio_state = agent._portfolio_state_dict(account)
    approved = []
    for it in intents:
        v = risk_mod.judge(it, policy=demo_policy, account=account,
                           portfolio_state=portfolio_state,
                           chain=chains.get(it.underlying, {}), now=now)
        mark = "\033[32mAPPROVED\033[0m" if v.approved else "\033[31mREJECTED\033[0m"
        print(f"    {mark} {it.intent_id}: "
              + ("; ".join(v.reasons) if not v.approved else "all gates passed"))
        if v.approved:
            approved.append((it, v))
    if not approved:
        print("    all intents rejected by the risk gates — nothing to seal.")
        return 0

    banner("7 zk", "minting a zero-knowledge risk-cap proof per approved order...")
    zk_proofs = {}
    for it, _ in approved:
        proof = agent.zk.prove_max_loss(it.max_loss_usd)
        if proof:
            zk_proofs[it.intent_id] = proof
            print(f"    {it.intent_id}: worst-case loss < 2^16 USD, "
                  f"commitment {proof['commitment'][:16]}…")

    banner("8 SEAL", "\033[36mplacing the order INSIDE a hermetic bulla cell over the tunnel\033[0m")
    rec = CycleRecord(cycle_id=f"fullcycle-{now:%H%M%S}", started_at=0.0)
    agent._execute_sealed(rec, approved, chains, zk_proofs, rec.notes)
    for e in rec.executions:
        print(f"    order {e.get('client_order_id')}: status={e.get('status')} "
              f"filled={e.get('filled_qty')} · {len(e.get('request_ids', []))} sealed API calls")
    if rec.receipt_path:
        print(f"    signed receipt: {rec.receipt_path}")

    banner("9 verify", "re-checking the sealed receipt offline...")
    if rec.receipt_path and agent.receipts:
        ok = agent.receipts.verify(rec.receipt_path)
        print(f"    bulla verify: {'INTACT ✓' if ok else 'FAILED ✗'}")

    banner("done", "the whole machine fired: decide → risk → zk → SEAL → receipt. "
                    "This is exactly what runs Monday at the open.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
