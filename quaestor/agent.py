"""The autonomous decision loop — wires every quaestor module into the 7-step cycle.

Cycle (every N minutes during market hours, plus catalyst-triggered early runs):
  1. clock      — session phase, due catalysts (calendar.yaml), final-day flatten timing
  2. broker     — account snapshot -> portfolio state refresh (day P&L vs day-open equity)
  3. data       — stock snapshots/bars for the universe; option chains for the universe
                  plus every underlying we hold positions in
  4. signals + sentiment -> strategy.decide(Context) -> TradeIntents
  5. risk.judge per intent (fail-closed: an exception judging == rejection)
  6. broker.execute for approved intents (idempotent client_order_id, marketable limit,
     cancel/repost loop; fresh quotes come from a chain_refresh callback when supported)
  7. audit.record_cycle + receipts.attested_cycle (bulla) — receipts are OPTIONAL:
     the loop never dies because of the receipt layer.

Every step is individually try/except'd: one failing step degrades gracefully
(sentiment -> {}, chain -> {}, judge exception -> rejected) and run_cycle() never raises.

Alpaca facts encoded here:
- Market hours are US/Eastern; options orders are TIF="day" only, so the loop sleeps
  until the next 9:30 ET open when the market is closed (weekends skipped; all
  Aug 28 - Sep 4 2026 weekdays are trading days).
- GET /v2/options/contracts defaults its expiration window to "this week", so every
  chain/contract fetch passes explicit expiry_gte/expiry_lte bounds (today .. +3 DTE
  for entries, extended to cover held positions' expiries for exits).
- Paper fills are marketable-at-NBBO-touch on the randomized "indicative" options feed;
  execution is delegated to broker.execute (repost loop, 403 = buying power no-retry,
  422 = bad payload no-retry, 429 honors Retry-After, X-Request-ID collected).
- client_order_id is the idempotency key: submission/lookup discipline lives in broker,
  and this module logs each intent -> ExecutionReport pair through audit.log_order.
"""
from __future__ import annotations

import inspect
import signal as _signal
import sys
import time
import uuid
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

from quaestor import clock
from quaestor import orders as orders_mod
from quaestor import risk as risk_mod
from quaestor import sentiment as sentiment_mod
from quaestor import signals as signals_mod
from quaestor import strategy as strategy_mod
from quaestor import universe as universe_mod
from quaestor.audit import AuditTrail
from quaestor.broker import Broker
from quaestor.config import Settings, load_calendar, load_policy, load_settings
from quaestor.data import MarketData
from quaestor.models import (
    AccountSnapshot,
    CycleRecord,
    ExecutionReport,
    RiskCheck,
    RiskVerdict,
    TradeIntent,
)
from quaestor.portfolio import PortfolioState
from quaestor.receipts import ReceiptPress
from quaestor.zk import ZkProver

try:  # WSL/Linux has the system tz database; bare Windows python may lack tzdata
    from zoneinfo import ZoneInfo

    _ET_FALLBACK: timezone | Any = ZoneInfo("America/New_York")
except Exception:  # pragma: no cover - Windows dev box without tzdata
    _ET_FALLBACK = timezone(timedelta(hours=-4), "ET")  # EDT; contest window is all DST
_ENTRY_DTE_WINDOW_DAYS = 3          # playbooks trade 0-3 DTE
_SLEEP_CHUNK_S = 15.0               # responsiveness of the stop flag / catalyst watch

__all__ = ["Agent", "build_agent"]


def build_agent() -> "Agent":
    """Load settings/policy/calendar and wire a ready-to-run Agent."""
    settings = load_settings()
    return Agent(settings, load_policy(), load_calendar())


class Agent:
    """Owns one trading session: all module instances plus persisted portfolio state."""

    def __init__(self, settings: Settings, policy: dict, calendar: dict) -> None:
        self.settings = settings
        self.policy = policy
        self.calendar = calendar
        self.data = MarketData(settings)
        self.broker = Broker(settings)
        self.audit = AuditTrail(settings.runs_dir)
        self._portfolio_path: Path = Path(settings.runs_dir) / "portfolio_state.json"
        self.portfolio = self._load_portfolio()
        self.receipts: ReceiptPress | None
        try:
            self.receipts = ReceiptPress(settings, settings.receipts_dir)
        except Exception as exc:  # receipts must never block trading
            print(f"[quaestor] receipts unavailable: {exc!r}", file=sys.stderr)
            self.receipts = None
        self.zk = ZkProver()  # fail-open by design: proofs are None when unavailable
        self._stop = False

    # ------------------------------------------------------------- portfolio io

    def _load_portfolio(self) -> PortfolioState:
        try:
            return PortfolioState.load(self._portfolio_path)
        except Exception:
            pass
        try:
            return PortfolioState()
        except TypeError:
            # fallback if PortfolioState has no defaults: field order per spec
            return PortfolioState(0.0, 0.0, set(), 0.0, False)  # type: ignore[call-arg]

    def _save_portfolio(self, notes: list[str]) -> None:
        try:
            self.portfolio.save()
            return
        except TypeError:
            try:
                self.portfolio.save(self._portfolio_path)  # type: ignore[call-arg]
                return
            except Exception as exc:
                notes.append(f"portfolio.save failed: {exc!r}")
        except Exception as exc:
            notes.append(f"portfolio.save failed: {exc!r}")

    def _fired_events(self) -> set[str]:
        try:
            fired = getattr(self.portfolio, "fired_events", None)
            return set(fired) if fired else set()
        except Exception:
            return set()

    # ----------------------------------------------------------------- helpers

    def _now_et(self) -> datetime:
        try:
            return clock.now_et()
        except Exception:
            return datetime.now(tz=_ET_FALLBACK)

    def _market_open_now(self) -> bool:
        try:
            return bool(clock.is_market_open_now())
        except Exception:
            now = datetime.now(tz=_ET_FALLBACK)
            if now.weekday() >= 5:
                return False
            open_dt = now.replace(hour=9, minute=30, second=0, microsecond=0)
            close_dt = now.replace(hour=16, minute=0, second=0, microsecond=0)
            return open_dt <= now < close_dt

    def _option_positions(self, account: AccountSnapshot) -> list[dict[str, Any]]:
        try:
            positions = self.portfolio.option_positions(account)
            if isinstance(positions, list):
                return positions
        except Exception:
            pass
        return [
            p
            for p in list(account.positions or [])
            if isinstance(p, dict) and str(p.get("asset_class", "")) == "us_option"
        ]

    def _portfolio_state_dict(self, account: AccountSnapshot) -> dict[str, Any]:
        """Shape the persisted portfolio state as the plain dict risk.judge expects."""
        pf = self.portfolio

        def grab(name: str, default: Any) -> Any:
            try:
                value = getattr(pf, name)
            except Exception:
                return default
            return default if value is None else value

        opt_positions = self._option_positions(account)
        try:
            exposure = pf.underlying_exposure(account)
            if not isinstance(exposure, dict):
                exposure = {}
        except Exception:
            exposure = {}
        return {
            "day_open_equity": float(grab("day_open_equity", account.equity) or account.equity),
            "week_open_equity": float(grab("week_open_equity", account.equity) or account.equity),
            "realized_pnl_today": float(grab("realized_pnl_today", 0.0) or 0.0),
            "halted_today": bool(grab("halted_today", False)),
            "fired_events": sorted(str(t) for t in self._fired_events()),
            "open_option_positions": opt_positions,
            "open_positions_count": len(opt_positions),
            "underlying_exposure": exposure,
        }

    def _make_chain_refresh(self, intent: TradeIntent) -> Callable[[], dict[str, dict]]:
        """Callback the broker repost loop can use to re-quote from a fresh chain."""
        underlying = intent.underlying
        leg_symbols = [leg.symbol for leg in intent.legs]

        def refresh() -> dict[str, dict]:
            today = self._now_et().date()
            expiries: list[date] = []
            for sym in leg_symbols:
                try:
                    parsed = orders_mod.occ_parse(sym)
                    exp = parsed.get("expiry")
                    if isinstance(exp, date):
                        expiries.append(exp)
                except Exception:
                    continue
            gte = min(expiries) if expiries else today
            lte = max(expiries) if expiries else today + timedelta(days=_ENTRY_DTE_WINDOW_DAYS)
            return self.data.option_chain(
                underlying, expiry_gte=gte.isoformat(), expiry_lte=lte.isoformat()
            )

        return refresh

    def _execute(
        self, intent: TradeIntent, chain: dict[str, dict], zk_prefix: str = ""
    ) -> ExecutionReport:
        """broker.execute with the intent's ZK commitment prefix (binds order->proof)
        and, when supported, a chain_refresh callback."""
        kwargs: dict[str, Any] = {"zk_prefix": zk_prefix}
        try:
            if "chain_refresh" in inspect.signature(self.broker.execute).parameters:
                kwargs["chain_refresh"] = self._make_chain_refresh(intent)
        except (TypeError, ValueError):
            pass
        try:
            report = self.broker.execute(intent, chain, self.policy, **kwargs)
        except Exception as exc:
            report = ExecutionReport(
                intent_id=intent.intent_id,
                client_order_id="",
                status="error",
                error=f"broker.execute raised: {exc!r}",
            )
        request_id = report.request_ids[0] if report.request_ids else ""
        try:
            self.audit.log_order("execute", intent.to_dict(), report.to_dict(), request_id)
        except Exception:
            pass
        return report

    # -------------------------------------------------------------- the cycle

    def run_cycle(self) -> CycleRecord:
        """One full decision cycle. Never raises — every failure becomes a note."""
        cycle_id = f"c-{time.strftime('%Y%m%d-%H%M%S', time.gmtime())}-{uuid.uuid4().hex[:6]}"
        rec = CycleRecord(cycle_id=cycle_id, started_at=time.time())
        notes = rec.notes

        # -- step 1: clock ----------------------------------------------------
        now = self._now_et()
        fired = self._fired_events()
        due_events: list[dict[str, Any]] = []
        try:
            due_events = list(clock.due_catalysts(self.calendar, now, fired))
        except Exception as exc:
            notes.append(f"clock.due_catalysts failed: {exc!r}")
        market_open = False
        try:
            market_open = bool(clock.is_market_open_now())
            if not market_open:
                notes.append("market closed at cycle start (timing gates still apply)")
        except Exception as exc:
            notes.append(f"clock.is_market_open_now failed: {exc!r}")

        # -- step 2: account + portfolio state --------------------------------
        account: AccountSnapshot | None = None
        try:
            account = self.broker.account_snapshot()
            rec.account = account
        except Exception as exc:
            notes.append(f"broker.account_snapshot failed: {exc!r}")
        if account is None:
            notes.append("no account snapshot -> skipping decision steps this cycle")
            self._finish_cycle(rec, due_events, fired, notes)
            return rec
        try:
            self.portfolio.refresh(account, now)
        except Exception as exc:
            notes.append(f"portfolio.refresh failed: {exc!r}")
        try:
            self.audit.snapshot_positions(account)
        except Exception as exc:
            notes.append(f"audit.snapshot_positions failed: {exc!r}")

        # -- step 3: market data ----------------------------------------------
        core = list(dict.fromkeys(universe_mod.UNDERLYINGS))
        today = now.date()
        default_lte = today + timedelta(days=_ENTRY_DTE_WINDOW_DAYS)
        expiry_lte_by_root: dict[str, date] = {u: default_lte for u in core}
        opt_positions = self._option_positions(account)
        for pos in opt_positions:
            try:
                parsed = orders_mod.occ_parse(str(pos.get("symbol", "")))
                root = str(parsed.get("root", ""))
                exp = parsed.get("expiry")
                if not root:
                    continue
                current = expiry_lte_by_root.get(root, default_lte)
                if isinstance(exp, date) and exp > current:
                    current = exp
                expiry_lte_by_root[root] = current
            except Exception as exc:
                notes.append(f"occ_parse failed for position {pos.get('symbol')!r}: {exc!r}")
        underlyings = list(expiry_lte_by_root)

        snapshots: dict[str, dict] = {}
        bars: dict[str, list[dict]] = {}
        try:
            snapshots = self.data.stock_snapshot(core)
        except Exception as exc:
            notes.append(f"data.stock_snapshot failed: {exc!r}")
        try:
            bars = self.data.stock_bars(core)
        except Exception as exc:
            notes.append(f"data.stock_bars failed: {exc!r}")

        chains: dict[str, dict[str, dict]] = {}
        contracts: dict[str, list[dict]] = {}
        for underlying in underlyings:
            lte_iso = expiry_lte_by_root[underlying].isoformat()
            try:
                chains[underlying] = self.data.option_chain(
                    underlying, expiry_gte=today.isoformat(), expiry_lte=lte_iso
                )
            except Exception as exc:
                chains[underlying] = {}
                notes.append(f"data.option_chain({underlying}) failed: {exc!r}")
            try:
                contracts[underlying] = universe_mod.discover_contracts(
                    self.settings, underlying, expiry_gte=today.isoformat(), expiry_lte=lte_iso
                )
            except Exception as exc:
                contracts[underlying] = []
                notes.append(f"universe.discover_contracts({underlying}) failed: {exc!r}")

        # -- step 4: signals + sentiment -> strategy --------------------------
        sigs: dict[str, Any] = {}
        try:
            sigs = signals_mod.compute(bars, snapshots)
            if not isinstance(sigs, dict):
                sigs = {}
        except Exception as exc:
            sigs = {}
            notes.append(f"signals.compute failed: {exc!r}")
        senti: dict[str, float] = {}
        try:
            headlines = self.data.news(underlyings)
            senti = sentiment_mod.score_headlines(headlines, self.settings)
            if not isinstance(senti, dict):
                senti = {}
        except Exception:
            senti = {}  # sentiment NEVER blocks trading

        intents: list[TradeIntent] = []
        try:
            ctx = strategy_mod.Context(
                settings=self.settings,
                policy=self.policy,
                calendar=self.calendar,
                account=account,
                portfolio=self.portfolio,
                signals=sigs,
                sentiment=senti,
                chains=chains,
                contracts=contracts,
                now=now,
                due_events=due_events,
            )
            intents = list(strategy_mod.decide(ctx))
        except Exception as exc:
            notes.append(f"strategy.decide failed: {exc!r}")
        for intent in intents:
            try:
                rec.intents.append(intent.to_dict())
            except Exception:
                rec.intents.append({"intent_id": getattr(intent, "intent_id", "?"),
                                    "error": "intent.to_dict failed"})

        # -- step 5: risk gates (fail closed) ---------------------------------
        portfolio_state = self._portfolio_state_dict(account)
        judged: list[tuple[TradeIntent, RiskVerdict]] = []
        for intent in intents:
            try:
                verdict = risk_mod.judge(
                    intent,
                    policy=self.policy,
                    account=account,
                    portfolio_state=portfolio_state,
                    chain=chains.get(intent.underlying, {}),
                    now=now,
                )
            except Exception as exc:
                verdict = RiskVerdict(
                    approved=False,
                    checks=[RiskCheck("judge_exception", False, f"risk.judge raised: {exc!r}")],
                    policy_digest=str(self.policy.get("digest", "")),
                    intent_id=intent.intent_id,
                )
            judged.append((intent, verdict))
            rec.verdicts.append(verdict.to_dict())

        # -- zk: per approved intent, prove "max loss < 2^16 USD" in zero knowledge
        zk_proofs: dict[str, Any] = {}
        for intent, verdict in judged:
            if verdict.approved:
                proof = self.zk.prove_max_loss(intent.max_loss_usd)
                if proof is not None:
                    zk_proofs[intent.intent_id] = proof
                else:
                    notes.append(f"zk proof unavailable for intent {intent.intent_id}")

        # -- receipts: bind the decision (steps 4-5) in a bulla cell ----------
        # Every cycle is attested — a quiet cycle receipt proves the agent looked
        # at the market and chose to do nothing (discipline is part of the audit).
        if self.receipts is not None:
            decision_payload = {
                "cycle_id": cycle_id,
                "now_et": now.isoformat(),
                "policy_digest": str(self.policy.get("digest", "")),
                "due_events": due_events,
                "intents": rec.intents,
                "verdicts": rec.verdicts,
                "zk_proofs": zk_proofs,
                "inputs": {
                    "account": rec.account.to_dict() if rec.account else None,
                    "market_open": market_open,
                },
            }
            try:
                _, receipt_path = self.receipts.attested_cycle(
                    cycle_id, lambda: decision_payload
                )
                rec.receipt_path = str(receipt_path)
            except Exception as exc:
                notes.append(f"receipts.attested_cycle failed (continuing unsigned): {exc!r}")

        # -- step 6: execute approved intents ---------------------------------
        for intent, verdict in judged:
            if not verdict.approved:
                notes.append(
                    f"intent {intent.intent_id} rejected: {'; '.join(verdict.reasons) or 'unknown'}"
                )
                continue
            zk_prefix = ZkProver.commitment_prefix(zk_proofs.get(intent.intent_id))
            report = self._execute(intent, chains.get(intent.underlying, {}), zk_prefix)
            rec.executions.append(report.to_dict())

        # -- step 7: persist ---------------------------------------------------
        self._finish_cycle(rec, due_events, fired, notes)
        return rec

    def _finish_cycle(
        self,
        rec: CycleRecord,
        due_events: list[dict[str, Any]],
        fired: set[str],
        notes: list[str],
    ) -> None:
        for event in due_events:
            tag = str(event.get("tag", "")) if isinstance(event, dict) else ""
            if tag:
                fired.add(tag)
        try:
            self.portfolio.fired_events = fired
        except Exception as exc:
            notes.append(f"could not update fired_events: {exc!r}")
        self._save_portfolio(notes)
        try:
            self.audit.record_cycle(rec)
        except Exception as exc:
            print(f"[quaestor] audit.record_cycle failed: {exc!r}", file=sys.stderr)
        try:
            self.audit.summary()  # keep summary.json fresh for the dashboard
        except Exception:
            pass

    # ---------------------------------------------------------------- the loop

    def run_forever(self, interval_s: int = 300) -> None:
        """Cycle every interval_s during market hours; sleep to next open otherwise.

        SIGINT sets a stop flag so the current cycle finishes cleanly before exit.
        Catalyst events due mid-sleep trigger an early cycle.
        """
        self._stop = False
        previous_handler: Any = None

        def _on_signal(signum: int, frame: Any) -> None:  # noqa: ARG001
            self._stop = True
            print("\n[quaestor] stop requested — finishing current step...", file=sys.stderr)

        try:
            previous_handler = _signal.signal(_signal.SIGINT, _on_signal)
        except ValueError:
            previous_handler = None  # not on the main thread

        print(
            f"[quaestor] loop started: interval={interval_s}s "
            f"session={self.audit.session_dir}"
        )
        try:
            while not self._stop:
                try:
                    if self._market_open_now():
                        rec = self.run_cycle()
                        approved = sum(1 for v in rec.verdicts if v.get("approved"))
                        print(
                            f"[quaestor] cycle {rec.cycle_id}: intents={len(rec.intents)} "
                            f"approved={approved} executed={len(rec.executions)} "
                            f"notes={len(rec.notes)}"
                        )
                        self._sleep(float(interval_s), watch_events=True)
                    else:
                        wait = self._seconds_until_next_open()
                        print(
                            f"[quaestor] market closed — sleeping {int(wait)}s until next open"
                        )
                        self._sleep(wait)
                except KeyboardInterrupt:
                    self._stop = True
        finally:
            if previous_handler is not None:
                try:
                    _signal.signal(_signal.SIGINT, previous_handler)
                except (ValueError, TypeError):
                    pass
            self._save_portfolio([])
            print("[quaestor] loop stopped; portfolio state saved.")

    def _catalyst_due(self) -> bool:
        try:
            return bool(clock.due_catalysts(self.calendar, self._now_et(), self._fired_events()))
        except Exception:
            return False

    def _sleep(self, total_s: float, *, watch_events: bool = False) -> None:
        deadline = time.monotonic() + max(0.0, total_s)
        while not self._stop:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return
            try:
                time.sleep(min(_SLEEP_CHUNK_S, remaining))
            except KeyboardInterrupt:
                self._stop = True
                return
            if watch_events and self._catalyst_due():
                return  # event-triggered early cycle

    def _seconds_until_next_open(self) -> float:
        """Seconds to the next 9:30 ET weekday open (+15s so we wake inside the session)."""
        now = self._now_et()
        todays_open = now.replace(hour=9, minute=30, second=0, microsecond=0)
        if now.weekday() < 5 and now < todays_open:
            target = todays_open
        else:
            next_day = now + timedelta(days=1)
            while next_day.weekday() >= 5:
                next_day += timedelta(days=1)
            target = next_day.replace(hour=9, minute=30, second=0, microsecond=0)
        return max(30.0, (target - now).total_seconds() + 15.0)
