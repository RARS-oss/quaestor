"""Force one approved intent through the agent's sealed execution path (real order)."""
import os

os.environ["QUAESTOR_SEALED"] = "1"

from quaestor.agent import build_agent
from quaestor.models import (CycleRecord, Leg, PositionIntent, RiskCheck, RiskVerdict,
                             Side, Structure, TradeIntent)

agent = build_agent()
assert agent.sealed_executor is not None, "sealed executor not active"

# A deep-OTM SPY call next Friday at $0.01 — accepted, never fills, cancelled in-cell.
sym = "SPY260904C00825000"
intent = TradeIntent(
    underlying="SPY", structure=Structure.LONG_CALL,
    legs=[Leg(sym, Side.BUY, 1, PositionIntent.BUY_TO_OPEN)],
    qty=1, limit_price=0.01, thesis="agent-sealed smoke", max_loss_usd=1.0,
)
verdict = RiskVerdict(approved=True, checks=[RiskCheck("smoke", True, "forced")],
                      policy_digest="smoke", intent_id=intent.intent_id)
chain = {sym: {"bid": 0.00, "ask": 0.02, "mid": 0.01}}

rec = CycleRecord(cycle_id="agentsealed-smoke", started_at=0.0)
agent._execute_sealed(rec, [(intent, verdict)], {"SPY": chain}, {}, rec.notes)

print("notes:", rec.notes)
print("executions:", rec.executions)
print("receipt_path:", rec.receipt_path)
assert rec.receipt_path and "sealed-" in rec.receipt_path, "no sealed receipt"
assert rec.executions and rec.executions[0]["status"] in ("canceled", "filled", "partially_filled")
assert rec.executions[0]["request_ids"], "no x-request-id"
print("AGENT_SEALED_SMOKE_OK")
