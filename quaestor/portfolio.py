"""Portfolio state: day/week P&L tracking, halt flag, exposure — persisted as JSON.

Role: the restart-safe memory of the agent (runs/portfolio_state.json). Rolls day
and week boundaries in US/Eastern, computes day_pnl_pct against day-open equity,
latches the daily-loss halt flag, and produces the exact ``portfolio_state`` dict
shape that risk.judge() consumes.

Alpaca facts encoded here:
- The trading day is defined in US/Eastern (zoneinfo America/New_York); day P&L is
  measured against the first equity snapshot seen on each ET calendar date.
- Position dicts from GET /v2/positions carry ``asset_class`` == "us_option" for
  options and numeric fields (market_value, ...) as strings — cast before math.
- Option symbols are OCC format (ROOT + YYMMDD + C/P + strike*1000); the underlying
  root is the leading alphabetic run of the symbol, so per-underlying exposure
  groups option positions by that root (a plain equity symbol maps to itself).
"""
from __future__ import annotations

import json
import os
import re
import time
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from quaestor.models import AccountSnapshot

ET = ZoneInfo("America/New_York")

_DEFAULT_DAILY_LOSS_HALT_PCT = 15.0
_DEFAULT_WEEKLY_LOSS_HALT_PCT = 30.0
_OCC_ROOT_RE = re.compile(r"^[A-Za-z]+")


def _num(value: Any, default: float = 0.0) -> float:
    """Cast Alpaca's string/None numerics to float, defaulting on junk."""
    if value is None or value == "":
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def occ_root(symbol: str) -> str:
    """Leading alphabetic run of an OCC option symbol == the underlying root.
    'SPY260904C00650000' -> 'SPY'; a plain equity symbol maps to itself."""
    match = _OCC_ROOT_RE.match(symbol or "")
    return match.group(0).upper() if match else ""


class PortfolioState:
    """Restart-safe P&L/exposure state, persisted at runs/portfolio_state.json."""

    def __init__(self, path: Path | str = Path("runs") / "portfolio_state.json",
                 policy: dict[str, Any] | None = None) -> None:
        self.path: Path = Path(path)
        self.policy: dict[str, Any] = policy or {}
        self.day_open_equity: float = 0.0
        self.week_open_equity: float = 0.0
        self.fired_events: set[str] = set()
        self.realized_pnl_today: float = 0.0
        self.halted_today: bool = False
        self.halted_week: bool = False   # latched for the REST OF THE CONTEST, never reset
        self.day_pnl_pct: float = 0.0
        self.week_pnl_pct: float = 0.0
        self._day_key: str = ""       # ET calendar date "YYYY-MM-DD"
        self._week_key: str = ""      # ISO week "YYYY-Www"
        self._last_account: AccountSnapshot | None = None

    # ------------------------------------------------------------------ refresh

    def refresh(self, account: AccountSnapshot, now: datetime) -> None:
        """Roll ET day/week boundaries, recompute P&L pcts, latch the halt flag.

        Persists to disk after every refresh so a crash/restart mid-day keeps the
        same day-open equity (and therefore the same halt math).
        """
        et_now = self._to_et(now)
        day_key = et_now.date().isoformat()
        iso = et_now.date().isocalendar()
        week_key = f"{iso.year}-W{iso.week:02d}"
        equity = float(account.equity)

        if day_key != self._day_key:
            self._day_key = day_key
            self.day_open_equity = equity
            self.realized_pnl_today = 0.0
            self.halted_today = False
        if week_key != self._week_key:
            self._week_key = week_key
            self.week_open_equity = equity
        if self.day_open_equity <= 0:
            self.day_open_equity = equity
        if self.week_open_equity <= 0:
            self.week_open_equity = equity

        self.day_pnl_pct = (
            (equity - self.day_open_equity) / self.day_open_equity * 100.0
            if self.day_open_equity > 0 else 0.0
        )
        self.week_pnl_pct = (
            (equity - self.week_open_equity) / self.week_open_equity * 100.0
            if self.week_open_equity > 0 else 0.0
        )

        halt_pct = _num(
            self.policy.get("account", {}).get("daily_loss_halt_pct"),
            _DEFAULT_DAILY_LOSS_HALT_PCT,
        )
        if self.day_pnl_pct <= -halt_pct:
            self.halted_today = True   # latched: stays halted until the ET day rolls

        weekly_halt_pct = _num(
            self.policy.get("account", {}).get("weekly_loss_halt_pct"),
            _DEFAULT_WEEKLY_LOSS_HALT_PCT,
        )
        if self.week_pnl_pct <= -weekly_halt_pct:
            # "Hard stop for the rest of the contest": latched forever — a bounce
            # back above the threshold must NOT resume trading.
            self.halted_week = True

        self._last_account = account
        self.save()

    # ------------------------------------------------------------------ views

    def option_positions(self, account: AccountSnapshot) -> list[dict[str, Any]]:
        """Positions with asset_class == 'us_option' (raw Alpaca position dicts)."""
        return [p for p in account.positions if p.get("asset_class") == "us_option"]

    def underlying_exposure(self, account: AccountSnapshot) -> dict[str, float]:
        """abs(market_value) summed per underlying root across ALL positions."""
        exposure: dict[str, float] = {}
        for pos in account.positions:
            root = occ_root(str(pos.get("symbol") or ""))
            if not root:
                continue
            exposure[root] = exposure.get(root, 0.0) + abs(_num(pos.get("market_value")))
        return exposure

    def strategy_position_count(self, account: AccountSnapshot) -> int:
        """Count STRATEGY positions, not option leg rows.

        Alpaca returns one position row per contract, so a 2-leg vertical is 2 rows.
        Legs of every playbook structure (vertical, straddle, single) share the same
        (underlying root, expiry), so distinct (root, YYMMDD) pairs count strategies.
        """
        keys = set()
        for pos in self.option_positions(account):
            sym = str(pos.get("symbol") or "")
            if len(sym) >= 15:
                keys.add((occ_root(sym), sym[-15:-9]))
            else:
                keys.add((sym, ""))
        return len(keys)

    def to_state_dict(self) -> dict[str, Any]:
        """Exactly the portfolio_state shape risk.judge() consumes."""
        account = self._last_account
        return {
            "halted_today": self.halted_today,
            "halted_week": self.halted_week,
            "day_pnl_pct": self.day_pnl_pct,
            "open_position_count": self.strategy_position_count(account) if account else 0,
            "underlying_exposure": self.underlying_exposure(account) if account else {},
            "week_pnl_pct": self.week_pnl_pct,
        }

    # ------------------------------------------------------------------ persistence

    def save(self) -> None:
        """Atomic JSON write (tmp file + os.replace). Never raises into the loop."""
        data = {
            "day_open_equity": self.day_open_equity,
            "week_open_equity": self.week_open_equity,
            "fired_events": sorted(self.fired_events),
            "realized_pnl_today": self.realized_pnl_today,
            "halted_today": self.halted_today,
            "halted_week": self.halted_week,
            "day_pnl_pct": self.day_pnl_pct,
            "week_pnl_pct": self.week_pnl_pct,
            "day_key": self._day_key,
            "week_key": self._week_key,
            "saved_at": time.time(),
        }
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(self.path.suffix + ".tmp")
            tmp.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")
            os.replace(tmp, self.path)
        except OSError:
            pass  # persistence is best-effort; the trading loop must never die here

    @classmethod
    def load(cls, path: Path | str,
             policy: dict[str, Any] | None = None) -> "PortfolioState":
        """Load from JSON if present/parsable, else a fresh state bound to path."""
        state = cls(path, policy)
        file_path = Path(path)
        if not file_path.exists():
            return state
        try:
            data = json.loads(file_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return state
        if not isinstance(data, dict):
            return state
        state.day_open_equity = _num(data.get("day_open_equity"))
        state.week_open_equity = _num(data.get("week_open_equity"))
        state.fired_events = {str(t) for t in data.get("fired_events") or []}
        state.realized_pnl_today = _num(data.get("realized_pnl_today"))
        state.halted_today = bool(data.get("halted_today", False))
        state.halted_week = bool(data.get("halted_week", False))
        state.day_pnl_pct = _num(data.get("day_pnl_pct"))
        state.week_pnl_pct = _num(data.get("week_pnl_pct"))
        state._day_key = str(data.get("day_key") or "")
        state._week_key = str(data.get("week_key") or "")
        return state

    # ------------------------------------------------------------------ internals

    @staticmethod
    def _to_et(now: datetime) -> datetime:
        """Naive datetimes are assumed to already be ET; aware ones are converted."""
        if now.tzinfo is None:
            return now.replace(tzinfo=ET)
        return now.astimezone(ET)
