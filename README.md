<h1 align="center">quaestor</h1>

<p align="center"><b>An autonomous options-trading agent whose every trade is cryptographically signed, hermetically sealed, and independently verifiable.</b></p>

<p align="center">
  <img src="https://img.shields.io/badge/tests-161%20passing-2f8f5b" alt="tests">
  <img src="https://img.shields.io/badge/receipts-Ed25519%20signed-2f8f5b" alt="signed">
  <img src="https://img.shields.io/badge/verify-offline%20%2F%20in--browser-2bc4b2" alt="verify">
  <img src="https://img.shields.io/badge/trading-paper%20only-0e9c8c" alt="paper">
  <img src="https://img.shields.io/badge/license-MIT-0e9c8c" alt="license">
</p>

<p align="center">Alpaca AI Trading Agents Hackathon 2026 · options-native · fully autonomous</p>

---

## The claim every other agent can't make

Every agent in this hackathon will tell you it has risk management and a good week.
**quaestor can prove it.** Every decision runs through deterministic risk gates and
is executed **from inside a hermetic, no-root sandbox** that emits an **Ed25519-signed
receipt** of the exact exchange conversation — hash-chained into a tamper-evident
ledger and witnessed to an external anchor. Edit one number and the signature rejects
it. Drop one losing cycle and the chain breaks.

You don't have to trust our P&L. **You can verify it** — offline, in 30 seconds, with
no credentials:

```bash
make verify          # re-checks every receipt's signature + ledger chain, offline
```

…or open [`dashboard/verifier.html`](dashboard/verifier.html) in any browser, paste a
receipt, watch it verify — then change one character and watch it fail.

---

## 👩‍⚖️ For judges & reviewers — a guided tour

**Open [`dashboard/index.html`](dashboard/index.html) first** — the front door to every
verifiable surface. Then the fastest path to the parts that matter:

| You have | Do this | You'll see |
|---|---|---|
| **1 minute** | `make verify` (or open `dashboard/verifier.html`, click **Verify**) | Every signed receipt of the week re-checks, offline, no keys |
| **3 minutes** | Open `dashboard/track-record.html`, click **Verify**, then **Tamper** | A provable agent P&L record; the signature catching a forgery live |
| **5 minutes** | `make replay` · `bash scripts/red_team.sh` | Every decision re-derived from signed inputs; the four ways to fake results, each caught |
| **7 minutes** | Open `dashboard/marketplace.html` | "Alpaca Verified Agents" — the product this primitive unlocks |
| **10 minutes** | Read [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) + skim [`attested_alpaca/`](attested_alpaca/) | How any Alpaca agent adopts verifiable execution in a few lines |
| **15 minutes** | Read [`patches/bulla/`](patches/bulla/) — the sealed-egress tunnel | We patched a Rust sandbox so an agent trades *inside* the seal |

The boldest claim — *every trade placed from inside a cryptographically sealed cell* —
is the easiest to check. That's the point.

---

## What it does

- **Autonomous options trading** on Alpaca's paper API: defined-risk debit verticals on
  SPY/QQQ as the core flow, catalyst playbooks (ISM, ADP, the Sep-4 NFP print) for
  convexity — all multi-leg (`mleg`) orders with signed net limit prices.
- **Deterministic risk gates** (`quaestor/risk.py` + `configs/policy.yaml`): per-trade
  loss caps, daily & weekly halts, concentration and spread-quality gates, 0DTE cutoffs,
  final-day auto-flatten. The policy file's **sha256 is bound into every receipt** — you
  can't quietly loosen the rules without it showing in the audit trail.
- **Sealed live trading** (`quaestor/sealed_exec.py`): each order is placed from inside a
  hermetic no-root cell over a *mediated* egress tunnel — TLS terminates in the cell (keys
  never reach the broker), the exchange transcript is hash-chained into the signed receipt,
  the isolation seal stays **HELD**.
- **Zero-knowledge risk proofs** (`quaestor/zk.py`): a Bulletproofs range proof per order
  that its worst-case loss is under a hard cap, size hidden; the commitment rides inside the
  Alpaca `client_order_id`.
- **Deterministic replay** (`quaestor/replay.py`): each decision seals its full risk-gate
  inputs, so `make replay` re-derives every approve/reject verdict from signed data — proof
  the agent's decisions are reproducible, not arbitrary.
- **External anchor** (`quaestor/anchor.py`): the ledger head is witnessed to an append-only
  log — so even truncating the newest losing day is caught.
- **Verify anywhere**: `bulla verify` offline, or the same check compiled to **WebAssembly**
  running entirely in a judge's browser (`dashboard/verifier.html`).
- **Proof bundle** (`quaestor bundle`): packages the whole week — receipts + ledgers + anchor
  + the WASM verifier + an index — for offline verification by anyone.

## Architecture

```
        market          decide                 gate                 execute
   ┌────────────┐   ┌────────────┐   ┌──────────────────┐   ┌──────────────────┐
   │ data.py    │──▶│ signals /   │──▶│ risk.py           │──▶│ sealed_exec.py    │──▶ Alpaca
   │ universe.py│   │ strategy    │   │ policy.yaml (hash)│   │  (inside a cell)  │   paper API
   └────────────┘   └────────────┘   └────────┬─────────┘   └────────┬─────────┘
                                              │                       │
                                        ┌─────▼───────────────────────▼─────┐
                                        │ signed receipt · ZK proof · ledger │
                                        │ external anchor · WASM verifier    │
                                        └────────────────────────────────────┘
```

The agent loop (`agent.py`) runs every 5 minutes during market hours in WSL2. The Alpaca
**CLI** (pinned v0.0.14) and **MCP server** cover the required integration surface; orders
go through the Trading API with every `X-Request-ID` archived.

## Quickstart

```bash
# WSL2 Ubuntu (unprivileged user namespaces required for the sealed receipts)
bash scripts/setup-wsl.sh          # sysctls, builds bulla/sbx, verifies the toolchain
cp .env.example .env               # add your Alpaca PAPER keys (PK...)

python -m quaestor status          # account + positions
python -m quaestor rehearse        # dress rehearsal on live data — no trading
python -m quaestor once            # one decision cycle (mints a signed receipt)
QUAESTOR_SEALED=1 python -m quaestor loop   # autonomous, every trade sealed

python -m quaestor verify          # verify all receipts + the ledger
python -m quaestor bundle          # export the offline proof bundle
streamlit run dashboard/app.py     # judge dashboard (Agent / Receipts / Tamper / About)
```

Trading runs **only** against `paper-api.alpaca.markets`; the config layer fail-closes if a
live endpoint or `ALPACA_LIVE_TRADE` is ever detected.

## It's a primitive, not just an app

The verifiable-execution layer is extracted as **[`attested-alpaca`](attested_alpaca/)** — a
thin, agent-agnostic facade any Alpaca agent can adopt:

```python
from attested_alpaca import AttestedAlpaca
aa = AttestedAlpaca(api_key, secret_key)                 # paper by default
order = aa.submit_sealed(payload, prove_risk_usd=250.0)  # placed inside a sealed cell
assert aa.verify(order.receipt_path)                     # signature + seal + chain
```

This is the trust primitive an **agent marketplace** or **copy-trading of AI agents** would
build on: a provable, portable track record. Follow, fund, or audit an autonomous agent on
the strength of a signature — not its operator's word. See
[`examples/minimal_agent.py`](examples/minimal_agent.py).

## Provenance

quaestor's agent, strategy, and integration code was written during the hackathon
(Aug 28 – Sep 4 2026). It deliberately builds on two of our own pre-existing MIT-licensed
research projects, **bulla** (hermetic sandbox + signed receipts) and **sbx** (typed-verdict
executor, used in our dev loop) — verifiable-governance infrastructure we researched for AI
agents, now pointed at the highest-stakes agent use case there is: money.

## Disclosures

Not investment advice. Options trading involves significant risk and is not suitable for all
investors. This project runs exclusively in Alpaca's paper-trading environment — simulated
results do not represent actual trading. See [Alpaca's disclosures](https://alpaca.markets/disclosures)
and [Characteristics and Risks of Standardized Options](https://www.theocc.com/company-information/documents-and-archives/options-disclosure-document).
