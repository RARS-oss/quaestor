# Live-run runbook — the competition trading week

Trading days in the contest window: **Fri Aug 28** (kickoff, already passed),
then after the closed weekend **Mon Aug 31, Tue Sep 1, Wed Sep 2, Thu Sep 3, and
Fri Sep 4** (half day — submission deadline 11:00 ET). Regular session
**09:30–16:00 ET** (16:30–23:00 in Cyprus / UTC+3). Start the fresh competition
account at **Monday Aug 31's open** to capture the most P&L history. This is the
step-by-step for running quaestor live under seal, unattended, all week.

> One rule above all: **paper only.** `load_settings()` fail-closes if the base
> ever resolves to live or `ALPACA_LIVE_TRADE` is set. Never set that variable.

---

## T-minus: before Monday's open (weekend, ~15 min)

1. **Create the fresh competition paper account.** In the Alpaca dashboard →
   account switcher (top-left) → **Open New Paper Account** (starts at exactly
   **$100,000**). This is required by the rules — the submitted account must be
   brand-new with no prior history.
2. **Generate its API keys** (blue "Generate" on the paper Overview) — a `PK…`
   key + secret. Put them in `.env`:
   ```
   ALPACA_API_KEY=PK...        # the NEW competition account
   ALPACA_SECRET_KEY=...
   ```
   Keep the old dev keys somewhere else; do NOT commit `.env` (it is gitignored).
3. **Record the account ID** (e.g. `PA…`) — it goes in the final submission so
   judges can see the P&L. Save it in `docs/ONE-PAGER.md`.
4. **Start with a clean ledger** so the week's chain is pristine:
   ```bash
   # keep a backup of dev receipts, then clear for the competition run
   mv receipts receipts.dev-$(date +%s) 2>/dev/null; mkdir -p receipts
   rm -f runs/portfolio_state.json runs/heartbeat.json runs/HALT
   ```
5. **Go/no-go:**
   ```bash
   make preflight        # expect: PREFLIGHT: GO ✓, options L3, fresh $100k
   make test             # expect: all green
   python -m quaestor rehearse   # expect: ALL GREEN (data + order path live)
   ```

## T-0: Monday Aug 31 at (or just before) 09:30 ET

6. **Start the sealed loop** in a terminal that will stay up all week:
   ```bash
   QUAESTOR_SEALED=1 python -m quaestor loop --interval 300
   ```
   - Every 5 min during market hours it: snapshots the account → decides →
     risk-gates → places approved orders **inside a signed cell** → anchors the
     ledger head → writes a receipt. Off-hours it sleeps to the next open.
   - Leave the machine on and awake during 09:30–16:00 ET. (Disable sleep; if on
     a laptop, keep it plugged in.)
7. **Babysit the first 2–3 cycles.** The strategy has only been dress-rehearsed,
   not run in a live open session. Watch the console lines:
   `[quaestor] cycle …: intents=N approved=M executed=K`. In another terminal:
   ```bash
   python -m quaestor status        # equity / positions / today P&L
   cat runs/heartbeat.json          # liveness: state + last cycle time
   make verify                      # the week's receipts, offline
   ```

## Daily (each trading day)

- Morning: confirm the loop is still running (`heartbeat.json` recent) and
  `make preflight` is GO. Glance at overnight option activities.
- Anytime: `make verify` and `python -m quaestor replay` should stay green.
- The agent auto-flattens 0DTE by the policy cutoffs and halts on the daily loss
  gate; you don't need to intervene.

## Friday Sep 4 (final day)

- The policy's `final_day_all_cash_by_et: 10:30` makes the agent flatten to cash
  before the **11:00 ET submission deadline**, so the judged number is realized.
- Sanity: `python -m quaestor status` should show no open positions by ~10:35 ET.
- Then move to the submission checklist (below).

## Emergency controls

- **Stop trading immediately, cleanly:** `touch runs/HALT` (the loop finishes the
  current step and exits) — or Ctrl-C once (SIGINT finishes the cycle, then stops).
- **Flatten everything now:** `make flatten` (routes CLOSE intents through the
  risk + audit path).
- **The loop crashed / machine rebooted:** just restart step 6 — portfolio state,
  fired-events, and the ledger persist across restarts; the day-open equity and
  halt latches are preserved.
- **Circuit breaker:** after `loop.max_consecutive_errors` failing cycles in a
  row the loop stops itself (something is badly wrong) — investigate, then
  restart.

## Submission checklist (due Sep 4 11:00 ET)

- [ ] `git` public flip (or vendor bulla/sbx into quaestor so it's self-contained)
- [ ] Public GitHub repo URL
- [ ] Demo/Application URL (host `dashboard/index.html` — e.g. GitHub Pages)
- [ ] Alpaca **paper account ID** (the competition account)
- [ ] Video (≤5 min, MP4 link), slide deck (PDF), 16:9 cover image
- [ ] One-page write-up (AI logic / risk gates / Alpaca infra) with final P&L
- [ ] Up to 5 build-in-public post links (X + LinkedIn, tag @lablabai @AlpacaHQ)
- [ ] Submit from the team dashboard on lablab.ai
