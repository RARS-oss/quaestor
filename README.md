<p align="center"><b>quaestor</b> — an autonomous options-trading agent whose every decision is a cryptographically signed, independently verifiable receipt.</p>

<p align="center">
  Alpaca AI Trading Agents Hackathon 2026 · paper trading · options-native · fully autonomous
</p>

---

## The claim other agents can't make

Every trading agent in this hackathon will tell you it has "risk management."
**quaestor can prove it.** Each decision cycle runs through deterministic risk
gates and is sealed in a hermetic [bulla](https://github.com/RARS-oss/bulla)
cell that emits an **Ed25519-signed receipt** — policy digest, input hashes,
decision bytes, event chain — appended to a **hash-chained ledger**. Delete or
edit any cycle of the trading week and the chain breaks. Forge a number and the
signature fails. And every order carries a **zero-knowledge Bulletproof** that
its worst-case loss is under a hard cap — verifiable without revealing our sizing.

You don't have to trust our P&L story. You can check it:

```bash
make verify        # offline: every receipt's signature + ledger chain + ZK proofs
```

## What it does

- **Autonomous options trading** on Alpaca's paper API: defined-risk debit
  verticals on SPY/QQQ as the core flow, catalyst playbooks (ISM, ADP, NFP)
  for convexity — all multi-leg (`mleg`) orders with signed net limit prices.
- **Deterministic risk gates** (`quaestor/risk.py` + `configs/policy.yaml`):
  per-trade loss caps, daily halt, concentration and spread-quality gates,
  0DTE cutoffs, final-day auto-flatten. The policy file's sha256 is bound into
  every verdict and receipt — changing the rules is visible in the audit trail.
- **Signed execution receipts** (`quaestor/receipts.py`): each cycle's exact
  input and decision bytes are attested inside a no-root hermetic Linux cell
  (namespaces + pivot_root + seccomp) — `SEAL HELD`, signed, ledger-chained.
- **ZK risk proofs** (`quaestor/zk.py`): per-order Bulletproofs range proof
  ("max loss < 2^16 USD") with the Pedersen commitment prefix embedded in the
  Alpaca `client_order_id` — the order on the exchange is bound to its proof.
- **Idempotent execution** (`quaestor/broker.py`): marketable-limit orders with
  cancel-and-repost, `client_order_id` lookup-before-retry, partial-fill
  tolerance — built for Alpaca's paper fill model (NBBO-touch, random partials).
- **Full audit trail** (`quaestor/audit.py`): the official
  [alpaca-skills](https://github.com/alpacahq/alpaca-skills) `runs/` contract
  (orders.json, order_log.csv, position snapshots), extended with receipts.
- **Judge dashboard** (`dashboard/app.py`): live equity/decisions/receipts —
  including a **Tamper Playground**: edit a real receipt and watch verification fail.

## Architecture

```
        ┌────────────┐   ┌──────────┐   ┌───────────┐   ┌──────────────┐
 market │ data.py    │──▶│signals /  │──▶│ risk.py    │──▶│ broker.py    │──▶ Alpaca
 (IEX + │ universe.py│   │strategy   │   │ policy.yaml│   │ orders.py    │   paper API
 indic.)└────────────┘   └──────────┘   └─────┬─────┘   └──────┬───────┘
                                              │                │
                                        ┌─────▼────────────────▼─────┐
                                        │ receipts.py — bulla cell    │
                                        │ Ed25519 receipt · ledger    │
                                        │ zk.py — Bulletproofs proof  │
                                        └────────────────────────────┘
```

The agent loop (`agent.py`) runs every 5 minutes during market hours in WSL2.
The Alpaca **CLI** (pinned v0.0.14) and **MCP server** cover the required
integration surface; order execution goes through the Trading API with every
`X-Request-ID` archived.

## Quickstart

```bash
# WSL2 Ubuntu (unprivileged user namespaces required for receipts)
bash scripts/setup-wsl.sh          # sysctls, builds bulla/sbx, verifies toolchain
cp .env.example .env               # add your Alpaca PAPER keys (PK...)
python -m quaestor status          # account + positions
python -m quaestor once            # one decision cycle (mints a receipt)
python -m quaestor loop            # autonomous mode
python -m quaestor verify          # verify all receipts + ledger
streamlit run dashboard/app.py     # judge dashboard
```

Trading runs **only** against `paper-api.alpaca.markets` — the config layer
fail-closes if a live endpoint or `ALPACA_LIVE_TRADE` is ever detected.

## Provenance (read this, judges)

quaestor's agent/strategy/integration code was written during the hackathon
(Aug 28 – Sep 4, 2026). It deliberately builds on two of our own pre-existing
MIT-licensed research projects, both published before the event:

- **[bulla](https://github.com/RARS-oss/bulla)** — hermetic no-root sandbox
  emitting signed, offline-verifiable execution receipts (+ bulla-zk Bulletproofs).
- **[sbx](https://github.com/RARS-oss/sbx)** — hermetic executor returning typed
  verdicts to coding agents (used in our dev loop for this project).

That's the point: verifiable governance infrastructure we researched for AI
agents, applied to the place it matters most — an autonomous agent that trades.

## Disclosures

Not investment advice. Options trading involves significant risk and is not
suitable for all investors. This project runs exclusively in Alpaca's paper
trading environment — simulated results do not represent actual trading. See
[Alpaca's disclosures](https://alpaca.markets/disclosures) and
[Characteristics and Risks of Standardized Options](https://www.theocc.com/company-information/documents-and-archives/options-disclosure-document).
