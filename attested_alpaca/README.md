# attested-alpaca

**Verifiable execution for any Alpaca agent.** Wrap your credentials; every order
comes back with a cryptographically signed, hermetically-sealed receipt of the
exact exchange conversation — so an autonomous agent's track record is something
you can *verify*, not just trust.

```python
from attested_alpaca import AttestedAlpaca

aa = AttestedAlpaca(api_key, secret_key)          # paper by default
order = aa.submit_sealed({                          # placed inside a sealed cell
    "symbol": "SPY260904C00650000", "qty": "1", "side": "buy",
    "type": "limit", "limit_price": "0.50", "time_in_force": "day",
    "position_intent": "buy_to_open", "client_order_id": "demo-1",
}, prove_risk_usd=250.0)

print(order.status, order.receipt_path)            # canceled, receipts/sealed-*.json
print(aa.verify(order.receipt_path))               # True — signature + seal + chain
```

## Why

Autonomous agents are starting to trade on brokerage APIs. The problem nobody has
closed: **you cannot trust what an agent actually did.** "+40% this month" is
unverifiable — the run could be cherry-picked, backdated, executed with lookahead,
or simply faked. That's the same trust crisis that broke agent benchmarks in 2026,
now with money on the line.

attested-alpaca makes an agent's execution record **tamper-evident and
independently verifiable**:

- **Sealed placement.** Each order is placed from inside a hermetic, no-root Linux
  cell over a *mediated* egress tunnel. TLS terminates in the cell — your keys
  never reach the broker in plaintext — and the exact request/response transcript
  is hash-chained into the receipt. The isolation seal stays HELD.
- **Signed receipts.** Every cycle is Ed25519-signed and chained into a
  tamper-evident ledger. Edit one field and the signature rejects it; drop one
  cycle and the chain breaks.
- **Zero-knowledge risk proofs.** Each order can carry a Bulletproofs range proof
  that its worst-case loss is under a hard cap — verifiable *without* revealing the
  position size.
- **External anchor.** The ledger head is witnessed to an append-only external log
  (opaque hashes only), so even truncating the newest losing day is caught.
- **Offline verification.** `verify()` (and a browser-only WASM verifier) re-check
  any receipt with no backend and no trust in the runner beyond its public key.

## The primitive

This is the trust layer an **agent marketplace** or **copy-trading of AI agents**
would build on: a provable, portable performance record. Follow, fund, copy, or
audit an autonomous agent on the strength of a signature — not its operator's word.

## API

| Method | Does |
|---|---|
| `submit_sealed(payload, prove_risk_usd=None)` | Place one order in a sealed cell → `AttestedOrder` (+ optional ZK proof) |
| `submit_batch_sealed(payloads)` | Place several in one cell / one receipt |
| `verify(receipt_path) -> bool` | Offline signature + digest + chain + seal check |
| `prove_risk_cap(max_loss_usd)` / `verify_risk_proof(proof)` | ZK risk-cap proof |
| `anchor_head() -> dict` | Witness the ledger head externally |
| `track_record() -> dict` | Counts + chain heads of the signed record so far |
| `export_bundle(out_dir=None) -> Path` | Self-contained offline-verifiable proof bundle |

## Status

Reference implementation, paper-trading only. Built on the
[bulla](https://github.com/RARS-oss/bulla) hermetic-receipt sandbox; `quaestor`
is the reference autonomous options agent that uses it. See
`examples/minimal_agent.py` for a ~30-line adopter.

## Disclosure

Not investment advice. Options trading involves significant risk. Paper-trading
results are simulated and do not represent actual trading. See
[Alpaca's disclosures](https://alpaca.markets/disclosures).
