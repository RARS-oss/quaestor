"""Live smoke for SealedExecutor: execute a real unfillable order in a sealed cell."""
import json

from quaestor.config import load_settings
from quaestor.sealed_exec import SealedExecutor

settings = load_settings()
ex = SealedExecutor(settings, settings.receipts_dir)
print("available:", ex.available())

# A deep-OTM SPY call at $0.01 — accepted, never fills, cancelled inside the cell.
payload = {
    "symbol": "SPY260904C00820000", "qty": "1", "side": "buy", "type": "limit",
    "limit_price": "0.01", "time_in_force": "day", "position_intent": "buy_to_open",
    "client_order_id": "sealedexec-smoke-01",
}
results, receipt = ex.execute_cycle("smoke-cycle", [payload], poll_seconds=4)
print("receipt:", receipt)
print("results:", json.dumps(results, indent=2))
assert receipt is not None and receipt.exists(), "no receipt produced"
assert results and results[0]["status"] in ("canceled", "filled", "partially_filled"), results
assert results[0]["request_ids"], "no x-request-id captured"
print("SEALED_EXEC_SMOKE_OK")
