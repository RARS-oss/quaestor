"""Restart recovery: portfolio state + ledgers survive an agent rebuild.

Simulates the runbook's 'loop crashed / machine rebooted -> restart' case: run a
cycle, then build a FRESH agent (as a new process would) and confirm it reloads
the same day-open equity / day key / fired events from disk, and that the ledger
+ anchor chains continue rather than resetting. Also sanity-checks the
next-open scheduler.
"""
from __future__ import annotations

import os
from pathlib import Path

from quaestor import clock
from quaestor.agent import build_agent


def main() -> int:
    print("=" * 58)
    print("  quaestor — restart recovery test")
    print("=" * 58)

    # 1. First agent: run one real cycle so portfolio state is written.
    a1 = build_agent()
    a1.run_cycle()
    d1 = a1.portfolio.day_open_equity
    key1 = a1.portfolio._day_key
    fired1 = set(a1.portfolio.fired_events)
    ledger = Path(a1.settings.receipts_dir) / "ledger.jsonl"
    n_ledger_1 = sum(1 for _ in ledger.open()) if ledger.exists() else 0
    print(f"  agent #1: day_open=${d1:,.2f} day_key={key1} fired={len(fired1)} "
          f"ledger_entries={n_ledger_1}")

    state_file = Path(a1.settings.runs_dir) / "portfolio_state.json"
    print(f"  portfolio_state.json exists: {state_file.exists()}")

    # 2. Rebuild a fresh agent (simulating a process restart) — it must load state.
    a2 = build_agent()
    d2 = a2.portfolio.day_open_equity
    key2 = a2.portfolio._day_key
    fired2 = set(a2.portfolio.fired_events)
    print(f"  agent #2 (fresh): day_open=${d2:,.2f} day_key={key2} fired={len(fired2)}")

    ok = True
    if abs(d1 - d2) > 1e-6:
        ok = False; print(f"  FAIL: day_open_equity not persisted ({d1} != {d2})")
    else:
        print("  PASS: day-open equity reloaded across restart")
    if key1 != key2:
        ok = False; print(f"  FAIL: day_key not persisted ({key1} != {key2})")
    else:
        print("  PASS: ET day key reloaded")
    if not fired1 <= fired2:
        ok = False; print("  FAIL: fired events lost on restart")
    else:
        print("  PASS: fired events preserved")

    # 3. Run another cycle on the restarted agent — ledger continues, not resets.
    a2.run_cycle()
    n_ledger_2 = sum(1 for _ in ledger.open()) if ledger.exists() else 0
    if n_ledger_2 >= n_ledger_1:
        print(f"  PASS: ledger continued ({n_ledger_1} -> {n_ledger_2} entries, chained)")
    else:
        ok = False; print(f"  FAIL: ledger shrank ({n_ledger_1} -> {n_ledger_2})")

    # 4. Anchor chain intact across the whole thing.
    if a2.anchor:
        av = a2.anchor.verify_anchor_chain()
        print(f"  anchor chain: ok={av.get('ok')} entries={av.get('entries')} "
              f"break_at={av.get('break_at')}")
        if av.get("entries", 0) and not av.get("ok"):
            ok = False

    # 5. Scheduler sanity: seconds to next open should be positive + reasonable.
    secs = a2._seconds_until_next_open()
    hrs = secs / 3600.0
    nxt = clock.now_et()
    print(f"  next-open scheduler: {secs:,.0f}s (~{hrs:.1f}h) from {nxt:%a %H:%M} ET")
    if not (0 < secs < 4 * 24 * 3600):
        ok = False; print("  FAIL: next-open time implausible")
    else:
        print("  PASS: next-open time plausible")

    print("=" * 58)
    print("RESTART RECOVERY: " + ("ALL GREEN ✓" if ok else "FAILED ✗"))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
