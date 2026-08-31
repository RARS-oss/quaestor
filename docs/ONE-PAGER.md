# quaestor — one-page write-up (DRAFT v1, will be finalized Sep 3-4 with live results)

**Team:** RARS-oss (solo) · **Account ID:** `PA3MOH6DEEEH` (fresh paper account, opened 2026-08-31, funded at exactly $100,000)
**Repo:** github.com/RARS-oss/quaestor · **Demo:** _<dashboard URL>_

## AI logic

quaestor is a fully autonomous options agent. Every 5 minutes during market
hours it: snapshots the account → pulls SPY/QQQ bars, option chains and greeks
(free indicative feed) → computes deterministic momentum features (5m/30m
returns, VWAP deviation, range position, realized vol) → a strategy layer
proposes **defined-risk trades only**: 0-3 DTE debit verticals in the signal
direction (buy ~0.45Δ / sell ~0.25Δ), plus scheduled **catalyst playbooks**
(ISM, ADP, and the Sep 4 NFP report — an ATM straddle at the 9:30 open, closed
by 10:30, all positions realized to cash before the 11:00 deadline).
No LLM sits on the trade path at runtime — decisions are deterministic and
replayable; intelligence went into the design, auditability into the runtime.

## Risk gates (enforced, then *proven*)

Deterministic gates judge every intent before execution: per-trade worst-case
loss ≤ 12% of equity (22% for tagged catalysts, 2.5% for premium-selling income),
total open premium-at-risk ≤ 55% of equity, ≤5 concurrent positions, a daily
halt at −12% that also **flattens the open book**, a −30% weekly floor,
per-underlying concentration caps, spread-quality ≤3%, open-interest and price
floors, 0DTE time cutoffs, final-day auto-flatten. The daily halt is evaluated
once per 5-minute cycle, so the realized stop lands modestly below its trigger —
the setting is chosen for where it lands, not where it reads.
The policy lives in `configs/policy.yaml`; its **sha256 digest is signed into
every receipt**, so silently loosening the rules mid-week is cryptographically
visible. Each order additionally carries a **zero-knowledge Bulletproofs range
proof** ("worst-case loss < 2^16 USD") whose Pedersen commitment prefix is
embedded in the Alpaca `client_order_id` — the order on the exchange is bound
to its own risk proof.

## Alpaca infrastructure

Trading API (REST) for orders/positions/account — multi-leg `mleg` orders with
signed net limit prices, idempotent `client_order_id` with lookup-before-retry,
marketable-limit + cancel/repost execution tuned to the paper fill model
(NBBO-touch fills, 10% random partials); every `X-Request-ID` archived.
Market Data API: IEX stock feed + indicative options feed (chains, greeks, IV).
**Alpaca CLI v0.0.14** (pinned) is invoked by the agent itself on every cycle —
`alpaca clock`, run headless off `APCA_API_KEY_ID` — and its verbatim answer,
with a sha256 over the raw bytes, is **sealed into that cycle's signed receipt**.
Alpaca's clock is reconciled against the agent's own; a disagreement is recorded
rather than silently resolved. So "this agent uses Alpaca's tooling" is not a
claim in this document — it is checkable in the receipts, like everything else
here. The **Alpaca MCP server** (`alpaca-mcp-server` 2.3.0, pinned to
`fastmcp==3.1.0` in `.mcp.json`) is the interactive operator surface. Multi-leg
entries deliberately go through the Trading API directly rather than the MCP
server, whose `mleg` path is broken (#97) — and every entry this agent makes is
a four-leg condor. The audit trail follows
Alpaca's own `alpaca-skills` runs/ contract (orders.json, order_log.csv,
position snapshots) — extended with signed receipts.

## The verifiable-agent layer (what's new)

Every decision cycle is sealed in a hermetic no-root Linux cell (user/pid/mount/
net namespaces, pivot_root, seccomp) from our pre-existing open-source project
**bulla**: the cycle's exact input and decision bytes are hashed inside the cell
and the outcome is **Ed25519-signed** into a receipt, hash-chained into a weekly
ledger. `make verify` re-checks the entire week offline with no credentials.
Edit any receipt → signature fails. Drop any cycle → chain breaks.
Judges can try both live in the dashboard's **Tamper Playground**.

*P&L results section to be completed after the trading week.*
