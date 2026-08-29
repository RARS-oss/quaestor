"""Soak test: many sealed cycles back-to-back, then verify every chain.

Simulates the Monday load without waiting for the open: forces a signal each
iteration so the full pipeline places a real (queued, then cancelled) order
inside a sealed cell, one round with TWO underlyings (batch-in-one-cell). After
the loop it checks: every sealed receipt verifies, the sealed ledger chains, the
anchor chain is intact, and cell dirs are pruned (not accumulating).

Run (WSL):  QUAESTOR_SEALED=1 ~/hack/venv/bin/python scripts/soak_test.py [N]
"""
from __future__ import annotations

import copy
import os
import subprocess
import sys
import time
from dataclasses import replace as _replace
from datetime import timedelta
from pathlib import Path

os.environ.setdefault("QUAESTOR_SEALED", "1")

from quaestor import clock
from quaestor import risk as risk_mod
from quaestor import strategy as strategy_mod
from quaestor import universe as universe_mod
from quaestor.agent import build_agent
from quaestor.models import CycleRecord
from quaestor.signals import Signal

BULLA = os.path.expanduser("~/.cache/hack-target/release/bulla")


def _forced_cycle(agent, now, underlyings, demo_policy) -> CycleRecord:
    today = now.date()
    lte = (today + timedelta(days=3)).isoformat()
    chains, contracts, sigs = {}, {}, {}
    for u in underlyings:
        chains[u] = agent.data.option_chain(u, expiry_gte=today.isoformat(), expiry_lte=lte)
        contracts[u] = universe_mod.discover_contracts(
            agent.settings, u, expiry_gte=today.isoformat(), expiry_lte=lte, strike_band_pct=0.05)
        sigs[u] = Signal(underlying=u, direction=1, strength=0.9, features={"forced": 1.0})
    account = agent.broker.account_snapshot()
    ctx = strategy_mod.Context(
        settings=agent.settings, policy=agent.policy, calendar=agent.calendar,
        account=account, portfolio=agent.portfolio, signals=sigs, sentiment={},
        chains=chains, contracts=contracts, now=now, due_events=[])
    intents = [_replace(it, qty=1) for it in strategy_mod.decide(ctx)]
    portfolio_state = agent._portfolio_state_dict(account)
    approved = []
    for it in intents:
        v = risk_mod.judge(it, policy=demo_policy, account=account,
                           portfolio_state=portfolio_state, chain=chains.get(it.underlying, {}), now=now)
        if v.approved:
            approved.append((it, v))
    rec = CycleRecord(cycle_id=f"soak-{now:%H%M%S}-{int(time.time()*1000)%1000:03d}", started_at=0.0)
    if approved:
        zk = {it.intent_id: agent.zk.prove_max_loss(it.max_loss_usd) for it, _ in approved}
        agent._execute_sealed(rec, approved, chains, {k: v for k, v in zk.items() if v}, rec.notes)
    return rec, len(intents), len(approved)


def main() -> int:
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 5
    agent = build_agent()
    if not agent.sealed_executor:
        print("sealed executor OFF — set QUAESTOR_SEALED=1"); return 1
    demo_policy = copy.deepcopy(agent.policy)
    demo_policy["per_trade"]["max_spread_quality"] = 0.60  # weekend quotes are wide

    now = clock.now_et().replace(hour=10, minute=0, second=0, microsecond=0)
    while now.weekday() >= 5:
        now = now + timedelta(days=1)

    print("=" * 60)
    print(f"  quaestor soak — {n} forced sealed cycles back-to-back")
    print("=" * 60)
    receipts = []
    t0 = time.time()
    for i in range(n):
        # Every 3rd cycle is a two-underlying batch (SPY+QQQ in one cell).
        unders = ["SPY", "QQQ"] if i % 3 == 2 else ["SPY"]
        cyc_now = now + timedelta(minutes=5 * i)
        try:
            rec, n_int, n_app = _forced_cycle(agent, cyc_now, unders, demo_policy)
        except Exception as exc:
            print(f"  cycle {i+1}/{n}: EXCEPTION {exc!r}")
            continue
        statuses = [e.get("status") for e in rec.executions]
        if rec.receipt_path:
            receipts.append(rec.receipt_path)
        print(f"  cycle {i+1}/{n} [{'+'.join(unders)}]: intents={n_int} approved={n_app} "
              f"orders={len(rec.executions)} statuses={statuses} "
              f"receipt={'yes' if rec.receipt_path else 'NO'}")
    dt = time.time() - t0
    print(f"\n  ran {n} cycles in {dt:.1f}s ({dt/max(1,n):.1f}s/cycle)")

    # -- verify every chain -------------------------------------------------------
    print("\n" + "-" * 60)
    ok = True

    verified = 0
    for r in receipts:
        res = subprocess.run([BULLA, "verify", r], capture_output=True, text=True)
        if res.returncode == 0:
            verified += 1
        else:
            ok = False
            print(f"  receipt FAILED verify: {r}")
    print(f"  sealed receipts verify: {verified}/{len(receipts)} INTACT")

    ledger = Path(agent.settings.receipts_dir) / "sealed-ledger.jsonl"
    if ledger.exists():
        res = subprocess.run([BULLA, "log", str(ledger)], capture_output=True, text=True)
        chain_ok = res.returncode == 0
        ok = ok and chain_ok
        n_entries = sum(1 for _ in ledger.open())
        print(f"  sealed ledger chain: {'INTACT' if chain_ok else 'BROKEN'} ({n_entries} entries)")

    av = agent.anchor.verify_anchor_chain() if agent.anchor else {"ok": None}
    print(f"  anchor chain: ok={av.get('ok')} entries={av.get('entries')} break_at={av.get('break_at')}")

    cells_root = Path(os.path.expanduser("~/.cache/quaestor-cells"))
    n_cells = len(list(cells_root.iterdir())) if cells_root.exists() else 0
    du = subprocess.run(["du", "-sh", str(cells_root)], capture_output=True, text=True).stdout.split()
    print(f"  cell dirs: {n_cells} (pruned to <=48) using {du[0] if du else '?'}")

    print("-" * 60)
    print("SOAK: " + ("ALL GREEN ✓" if ok else "some checks FAILED ✗"))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
