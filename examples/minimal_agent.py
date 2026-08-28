"""A minimal verifiable Alpaca agent — ~30 lines, adopts attested-alpaca.

Shows the whole point: any agent can place an order that comes back with a
signed, sealed receipt anyone can verify — without trusting the agent's operator.

Run (WSL, inside the repo, with ALPACA_API_KEY/ALPACA_SECRET_KEY set):
    ~/hack/venv/bin/python examples/minimal_agent.py
"""
import uuid

from attested_alpaca import from_env

# 1. Wrap your Alpaca paper credentials.
aa = from_env(receipts_dir="receipts")
print("sealed execution available:", aa.available())

# 2. Decide (here: a deliberately unfillable, safe demo order). Your real agent
#    would compute this from its strategy + risk gates.
order_payload = {
    "symbol": "SPY260904C00825000", "qty": "1", "side": "buy", "type": "limit",
    "limit_price": "0.01", "time_in_force": "day", "position_intent": "buy_to_open",
    "client_order_id": "minimal-" + uuid.uuid4().hex[:10],
}

# 3. Place it INSIDE a sealed cell — and prove the risk cap in zero knowledge.
order = aa.submit_sealed(order_payload, label="minimal", prove_risk_usd=1.0)
print(f"status={order.status}  order_id={order.order_id[:8]}…  calls={len(order.request_ids)}")
print(f"receipt: {order.receipt_path}")
if order.zk_proof:
    print(f"zk risk proof: worst-case loss < 2^16 USD, commitment {order.zk_proof['commitment'][:16]}…")

# 4. Anyone can verify the receipt offline — signature, digest, seal, chain.
print("receipt verifies:", aa.verify(order.receipt_path) if order.receipt_path else False)

# 5. The run's verifiable track record, and a shareable proof bundle.
print("track record:", aa.track_record())
bundle = aa.export_bundle()
print("proof bundle:", bundle)
