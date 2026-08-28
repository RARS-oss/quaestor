# quaestor — architecture spec (v1, 2026-08-29)

**Mission:** autonomous options-trading agent for the Alpaca AI Trading Agents Hackathon
(deadline Sep 4 2026 11:00 EDT). Every trading decision is executed with a cryptographically
signed audit trail (bulla receipts), deterministic risk gates, and ZK risk-cap proofs.
Judged on: P&L, technology implementation (Trading API + MCP/CLI), originality, presentation.

## Runtime topology

- Dev/edit on Windows: `C:\Users\Daniil\Desktop\alpaca-hack\quaestor` (this repo).
- Execution in WSL2 Ubuntu: venv `~/hack/venv` (python 3.12, alpaca-py 0.44), repo visible at
  `/mnt/c/Users/Daniil/Desktop/alpaca-hack/quaestor`.
- Binaries in WSL: `~/hack/bin/alpaca` (official CLI v0.0.14, pinned),
  `~/.cache/hack-target/release/bulla`, `~/.cache/hack-target-sbx/release/sbx`.
- The agent loop runs in WSL. Decision/execution steps run inside bulla cells (signed receipts).

## The decision cycle (agent.py)

Every N minutes during market hours (default 5), plus event-triggered runs from `configs/calendar.yaml`:

1. `clock` — market open? session phase? catalyst due? final-day flatten due?
2. `broker.account_snapshot()` — equity, options BP, positions. Update `portfolio` state (daily P&L vs day-open equity).
3. `data` — refresh underlying snapshots/bars for the universe; option chains + greeks where needed.
4. `signals` + `sentiment` — compute features. `strategy.decide(ctx) -> list[TradeIntent]`.
5. `risk.judge(intent, ctx) -> RiskVerdict` for each intent. Rejected intents are logged, never executed.
6. `broker.execute(intent) -> ExecutionReport` for approved intents (idempotent, marketable-limit, repost loop).
7. `audit.record_cycle(CycleRecord)` — write runs/ contract; `receipts` wraps steps 4–6 in a bulla cell.

State that must survive restarts lives in files under `runs/` and `receipts/` (JSON/JSONL) — no DB.

## Module contracts (implement exactly these signatures)

All shared types come from `quaestor/models.py` (already written — READ IT FIRST).
Config loading via `quaestor/config.py`. Nothing else imports os.environ directly.

### config.py
```python
@dataclass
class Settings:  # loaded from env (+ .env), fail-closed
    api_key: str; api_secret: str
    paper: bool                    # MUST be True; raise SystemExit if base url resolves to live
    trading_base: str = "https://paper-api.alpaca.markets"
    data_base: str = "https://data.alpaca.markets"
    featherless_key: str = ""      # optional
    repo_root: Path; runs_dir: Path; receipts_dir: Path
def load_settings() -> Settings
def load_policy() -> dict         # configs/policy.yaml parsed + "digest": sha256 hex of raw bytes
def load_calendar() -> dict       # configs/calendar.yaml parsed
```
Env var names: `ALPACA_API_KEY`, `ALPACA_SECRET_KEY` (Alpaca CLI/MCP convention), `FEATHERLESS_API_KEY`.
`ALPACA_LIVE_TRADE` must be ABSENT/false — if set truthy, refuse to start (fail-closed paper gate).

### clock.py
```python
ET = ZoneInfo("America/New_York")
def now_et() -> datetime
def is_market_open_now(cal: TradingCalendarLike | None = None) -> bool   # simple ET schedule fallback
def session_phase(dt) -> str      # "pre" | "open" | "power_hour" | "close" | "closed"
def minutes_to_close(dt) -> float
def parse_et_hhmm(s: str, on_date: date) -> datetime
def due_catalysts(calendar: dict, dt, fired: set[str]) -> list[dict]   # events whose time passed, not yet fired
def is_final_day(dt, policy: dict) -> bool
def past_cutoff(dt, policy: dict, key: str) -> bool   # e.g. key="no_new_0dte_after_et"
```
Pure functions, no network. Weekend/holiday aware for THIS week only (Labor Day Sep 7 is after deadline; all Aug 28–Sep 4 weekdays trade).

### data.py  (alpaca-py wrappers; feed pinned)
```python
class MarketData:
    def __init__(self, settings: Settings): ...   # StockHistoricalDataClient, OptionHistoricalDataClient
    def stock_snapshot(self, symbols: list[str]) -> dict[str, dict]      # feed=IEX
    def stock_bars(self, symbols, timeframe="5Min", lookback_minutes=390) -> dict[str, list[dict]]
    def option_chain(self, underlying: str, *, expiry_lte: str, expiry_gte: str,
                     strike_gte: float | None = None, strike_lte: float | None = None,
                     type_: str | None = None) -> dict[str, dict]
        # OptionChainRequest, feed=OptionsFeed.INDICATIVE hardcoded; returns OCC symbol -> snapshot dict
        # snapshot dict normalized: {bid, ask, mid, last, iv, delta, gamma, theta, vega, oi(optional), t}
        # greeks/iv can be None for illiquid contracts — pass through as None, callers guard.
    def option_snapshots(self, occ_symbols: list[str]) -> dict[str, dict]   # max 100 per call, chunk
    def news(self, symbols: list[str], limit=20) -> list[dict]
```
Respect 200 req/min: naive time-based throttle (min interval between calls) is enough.

### universe.py
```python
UNDERLYINGS = ["SPY", "QQQ"]                     # core; catalyst plays may add e.g. "AVGO"
def discover_contracts(settings, underlying, *, expiry_gte, expiry_lte, strike_band_pct=0.03,
                       type_: str | None = None) -> list[dict]
    # GET /v2/options/contracts via TradingClient.get_option_contracts (strike_price_* are STRINGS here)
    # NOTE: default expiration window is "this week" — ALWAYS pass explicit expiry_gte/lte.
def filter_tradable(contracts, chain: dict[str, dict], policy: dict) -> list[dict]
    # apply: min_open_interest, max_spread_quality ((ask-bid)/mid), min_leg_price; drop missing-quote rows
def nearest_expiry(contracts, target_dte: int) -> str      # YYYY-MM-DD
def pick_strike(chain, target_delta: float, type_: str) -> str | None   # OCC symbol by |delta| proximity
```

### signals.py
```python
@dataclass
class Signal:
    underlying: str; direction: int          # +1 bullish, -1 bearish, 0 flat
    strength: float                          # 0..1
    features: dict[str, float]               # ret_5m, ret_30m, vwap_dev, range_pos, rv, ...
def compute(bars: dict[str, list[dict]], snapshots: dict[str, dict]) -> dict[str, Signal]
```
Deterministic momentum/mean-reversion features from 5-min bars. No look-ahead. Unit-testable on fixtures.

### sentiment.py (optional path — NEVER blocks trading)
```python
def score_headlines(headlines: list[dict], settings) -> dict[str, float]   # symbol -> -1..1
```
Featherless `openai/gpt-oss-20b` via openai SDK (base_url https://api.featherless.ai/v1), temperature 0,
strict JSON prompt. On ANY error (no key, 402, 429, timeout, bad JSON) return {} — neutral fallback.

### strategy.py
```python
@dataclass
class Context:   # everything decide() needs, assembled by agent.py
    settings: Settings; policy: dict; calendar: dict
    account: AccountSnapshot; portfolio: "PortfolioState"
    signals: dict[str, Signal]; sentiment: dict[str, float]
    chains: dict[str, dict[str, dict]]       # underlying -> chain
    contracts: dict[str, list[dict]]         # underlying -> discovered contracts
    now: datetime; due_events: list[dict]
def decide(ctx: Context) -> list[TradeIntent]
```
Playbooks (encode all; each returns [] when conditions not met):
1. **Core momentum debit vertical** (SPY/QQQ): signal.strength >= 0.6 and direction != 0 →
   buy ~0.45-delta, sell ~0.25-delta same expiry (0–3 DTE), qty sized so max_loss ≈ policy per-trade cap.
   Skip if position already open on that underlying+direction.
2. **Catalyst straddle** (tag from due_events, e.g. NFP_OPEN_PLAY): long ATM straddle 0DTE,
   sized to catalyst cap, catalyst_tag set; exit handled by flatten rules.
3. **Exit management:** for open option positions produce CLOSE intents when: unrealized <= -50% of debit
   (stop), >= +100% (target), 0DTE near flat_0dte_by_et, or ALL_CASH event due (final day) → close everything.
Max-loss math: debit vertical → debit*100*qty; credit vertical → (width - credit)*100*qty; straddle/single → debit*100*qty.

### risk.py (pure, deterministic — THE gates that go in the one-pager)
```python
def judge(intent: TradeIntent, *, policy: dict, account: AccountSnapshot,
          portfolio_state: dict, chain: dict[str, dict], now: datetime) -> RiskVerdict
```
Checks (each -> RiskCheck; approved = all ok):
paper_gate (base is paper), structure_allowed, defined_risk (max_loss_usd finite & legs cover shorts),
per_trade_cap (max_loss <= equity * cap%, catalyst-aware), daily_halt (day P&L above -daily_loss_halt_pct),
concurrency (< max_concurrent_positions), concentration (per-underlying notional cap),
spread_quality (every leg (ask-bid)/mid <= max), open_interest, leg_price_min,
timing (0DTE cutoff, no_new_positions_after_et, final-day all-cash), sane_limit_price
(sign matches structure; |limit| within [0.5x, 1.5x] of chain mid net), qty_positive, mleg_rules
(<=4 legs, coprime ratio_qtys, unique symbols).
CLOSE intents bypass position-count/concentration/timing-entry checks but keep sanity checks.
`policy_digest` = sha256 of configs/policy.yaml bytes (from config.load_policy()["digest"]).

### orders.py
```python
def build_order_payload(intent: TradeIntent, attempt: int, zk_prefix: str = "") -> dict
    # single-leg: {symbol, qty, side, type: "limit", limit_price, time_in_force: "day", position_intent, client_order_id}
    # mleg:       {order_class: "mleg", qty, type: "limit", limit_price (signed), time_in_force: "day",
    #              legs: [leg.to_alpaca()...], client_order_id}
    # numbers serialized as strings where Alpaca expects strings; validate mleg constraints, raise ValueError early.
def marketable_limit(intent: TradeIntent, chain: dict[str, dict], policy: dict) -> float
    # single long: ask + buffer; single sell: bid - buffer
    # mleg net: sum(touch per leg direction) +/- mleg_buffer (debit: +, credit: raise |credit| toward mid by buffer)
    # round to cents; NEVER flip the sign of intent.limit_price.
def occ_parse(symbol: str) -> dict   # {root, expiry: date, type: "C"|"P", strike: float}
def occ_build(root: str, expiry: date, type_: str, strike: float) -> str
```

### broker.py (execution engine — raw REST via httpx, alpaca-py only for streams later)
```python
class Broker:
    def __init__(self, settings: Settings): ...    # httpx.Client, headers APCA-API-KEY-ID / APCA-API-SECRET-KEY
    def account_snapshot(self) -> AccountSnapshot  # GET /v2/account + /v2/positions
    def positions(self) -> list[dict]
    def open_orders(self) -> list[dict]
    def submit(self, payload: dict) -> tuple[dict, str]        # POST /v2/orders -> (order, x_request_id)
    def get_by_client_id(self, cid: str) -> dict | None
    def cancel(self, order_id: str) -> None
    def close_position(self, symbol_or_id: str) -> dict        # DELETE /v2/positions/{...}
    def execute(self, intent: TradeIntent, chain: dict, policy: dict, zk_prefix: str = "") -> ExecutionReport
        # loop: build payload (fresh client_order_id per attempt) -> submit -> poll order until filled/
        # partially_filled/terminal; if unfilled after repost_after_s: cancel, re-quote from fresh chain touch,
        # resubmit (attempt+1), up to max_reposts. Partial fills: keep remainder working, report accurately.
        # On timeout/ambiguous network error: get_by_client_id BEFORE any resubmit (never double-send).
        # 403 = buying power (do NOT retry), 422 = bad payload (do NOT retry), 429 -> backoff honoring Retry-After.
        # Collect X-Request-ID from every response into report.request_ids.
```
Wash-trade guard: before submitting, cancel any open opposing order on the same contract (403 otherwise).

### portfolio.py
```python
class PortfolioState:   # persisted at runs/portfolio_state.json
    day_open_equity: float; week_open_equity: float; fired_events: set[str]
    realized_pnl_today: float; halted_today: bool
    def refresh(self, account: AccountSnapshot, now) -> None   # roll day boundaries (ET), compute day P&L
    def option_positions(self, account) -> list[dict]          # asset_class == "us_option"
    def underlying_exposure(self, account) -> dict[str, float] # |market_value| by OCC root
    def save(self) / load(cls, path)
```

### audit.py (alpaca-skills runs/ contract, extended)
```python
class AuditTrail:
    def __init__(self, runs_dir: Path): ...   # creates runs/<YYYYMMDD-HHMMSS>-session/
    def record_cycle(self, rec: CycleRecord) -> Path      # cycles.jsonl append + per-cycle JSON
    def log_order(self, action: str, request: dict, response: dict, request_id: str) -> None
        # orders.json (array of {action,timestamp,request,response,x_request_id}) + order_log.csv
        # csv columns: timestamp,action,order_id,client_order_id,symbol,side,qty,type,limit_price,tif,status,filled_qty,filled_avg_price,error
    def snapshot_positions(self, account: AccountSnapshot) -> None   # positions_snapshot.json (latest)
    def summary(self) -> dict    # equity curve points, realized P&L, counts — dashboard reads this
```

### receipts.py (bulla integration — the signature feature)
```python
BULLA = "~/.cache/hack-target/release/bulla"   # inside WSL; expanduser
class ReceiptPress:
    def __init__(self, settings, receipts_dir: Path, key_path: Path | None = None): ...
    def attested_cycle(self, cycle_id: str, decision_fn: Callable[[], dict]) -> tuple[dict, Path]
        # v1 ("honest mode"): serialize decision inputs to a work dir file, run
        #   bulla run --work <cell_dir> --allow-net --nondeterministic --wall-ms 120000
        #        --out receipts/<cycle_id>.json --ledger receipts/ledger.jsonl --key <pinned key> -- <python step>
        # The receipt honestly records SEAL BROKEN (net was on) but stays signed+verifiable.
        # v2 (after bulla patch): --egress-allow paper-api.alpaca.markets:443 ... keeps SEAL HELD.
    def verify(self, receipt_path: Path) -> bool               # subprocess: bulla verify, exit 0
    def ledger_summary(self) -> dict                           # parse receipts/ledger.jsonl
```
If bulla binary missing (e.g. running on Windows directly), degrade: write unsigned JSON "receipt-lite"
with a loud "UNSIGNED" marker — the loop must never die because of the receipt layer.
v1 pragmatic shape: the DECISION step (risk.judge + payload build) runs inside the cell reading a JSON
input file and writing a JSON output file; broker submission happens outside; the receipt binds
inputs+decision (stdout sha256). Full in-cell CLI submission is the v2 upgrade.

### agent.py + cli.py
```python
# agent.py
class Agent:
    def __init__(self, settings, policy, calendar): ...        # wires all modules
    def run_cycle(self) -> CycleRecord                          # the 7 steps above; never raises (catch, log, continue)
    def run_forever(self, interval_s: int = 300) -> None        # sleep-loop; respects market hours; SIGINT clean exit
# cli.py  (python -m quaestor <cmd>)
#   status   — account + positions + today P&L (works without market open)
#   once     — one decision cycle
#   loop     — run_forever
#   verify   — verify all receipts + ledger, print table
#   flatten  — emergency close-everything (still goes through risk CLOSE path + audit)
```
`__main__.py` dispatches to cli.main().

## Sealed live trading (killer feature) — operational notes
- The bulla egress broker creates a Unix socket under the cell `--work` dir.
  **drvfs (`/mnt/c`) does not support Unix sockets** — any cell that uses
  `--egress-allow` MUST have its work dir on a native Linux fs (ext4, e.g.
  `~/.cache/quaestor-cells/...`). The `--out` receipt / `--key` / `--ledger`
  may live on `/mnt/c` (ordinary file writes). Byte-attestation cells (the v1
  cycle receipts, no egress) work fine on `/mnt/c`.
- Sealed trade = `bulla run --work <ext4 cell> --egress-allow
  paper-api.alpaca.markets:443 --egress-allow data.alpaca.markets:443
  --nondeterministic --out <receipt> -- python3 /work/<agent-step>`. TLS
  terminates in-cell (keys never reach the broker in plaintext); the receipt's
  egress block carries one hash-chained CONNECT call per HTTPS request; seal
  stays HELD. `quaestor/sealed_transport.py` is the httpx transport the agent
  uses inside such a cell; `scripts/in_cell_probe.py` is the stdlib proof.
- Keep this OFF the critical P&L path: the autonomous week trades in the normal
  path; the sealed tunnel is demonstrated per-order (proven end-to-end) so an
  experimental tunnel bug can never halt trading.

## Non-negotiable coding standards
- Python 3.12, stdlib + httpx + pyyaml + alpaca-py + openai only. Type hints everywhere.
- No placeholder/TODO code — everything runnable. No network in unit tests (fixtures only).
- Every Alpaca request/response pair that mutates state goes through audit.log_order.
- Secrets only via config.Settings; never printed, never written into runs/ or receipts work dirs.
- Times: ET internally for market logic (zoneinfo), UTC timestamps in records.
- Every module: short docstring header explaining role + the Alpaca facts it encodes.

## Key Alpaca facts (verified — do not re-derive)
- Paper fills: marketable-at-NBBO-touch, size unchecked, 10% random partials, zero fees; limit inside
  spread does NOT fill until quote crosses. Always marketable limits + repost loop.
- Free options feed = "indicative" (randomized OPRA derivative) — buffers must absorb small quote noise.
- mleg: max 4 legs, coprime ratio_qty, net limit_price (+debit/−credit), shorts must be covered in-order,
  market/limit only, TIF=day. Single-leg options: market/limit/stop/stop_limit, TIF=day.
- GET /v2/options/contracts on TRADING host; strike filters are strings there, floats on data host.
  Default expiration filter is "this week" — always pass explicit bounds.
- Greeks/IV only in REST snapshots (Black-Scholes, may be None). 200 req/min both APIs.
- Rate limit 429 honors Retry-After. 403 on orders = buying power. 422 = bad payload.
- Wash-trade protection: opposing open orders on same symbol -> 403; cancel first.
- XSP/SPX European cash-settled options exist in paper (no assignment risk) but no index underlying data yet.
