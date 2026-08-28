"""Audit trail writer — the runs/ contract every judge-facing artifact reads.

Creates one session directory per process (runs/<YYYYMMDD-HHMMSS>-session/, stamped in
US/Eastern because sessions align with market days) and maintains inside it:

- cycles.jsonl          append-only, one CycleRecord per line
- cycle-<id>.json       full per-cycle document (atomic write)
- orders.json           JSON array of {action, timestamp, request, response, x_request_id},
                        rewritten atomically (tmp + os.replace) on every append
- order_log.csv         flat log with fixed columns for spreadsheet triage
- positions_snapshot.json  latest-wins account/positions snapshot (atomic write)
- summary.json          dashboard aggregate produced by summary()

Alpaca facts encoded here:
- Every state-mutating Alpaca call is logged together with its X-Request-ID response
  header — that id is what Alpaca support needs to trace an order server-side.
- order_log.csv mirrors the Alpaca order object fields: client_order_id is the
  idempotency key (never reused across logical orders), time_in_force is always "day"
  for options, and mleg limit_price is the SIGNED net per strategy unit
  (positive = debit, negative = credit) — recorded exactly as submitted.
- Timestamps in records are UTC (ISO 8601); only the session directory name is ET.
"""
from __future__ import annotations

import csv
import json
import os
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from quaestor.models import AccountSnapshot, CycleRecord

try:  # WSL/Linux has the system tz database; bare Windows python may lack tzdata
    from zoneinfo import ZoneInfo

    _ET: timezone | Any = ZoneInfo("America/New_York")
except Exception:  # pragma: no cover - Windows dev box without tzdata
    _ET = timezone(timedelta(hours=-4), "ET")  # EDT; the contest window is all DST

CSV_COLUMNS: list[str] = [
    "timestamp", "action", "order_id", "client_order_id", "symbol", "side", "qty",
    "type", "limit_price", "tif", "status", "filled_qty", "filled_avg_price", "error",
]

__all__ = ["AuditTrail", "CSV_COLUMNS"]


def _utc_iso() -> str:
    return datetime.now(tz=timezone.utc).isoformat(timespec="milliseconds")


def _safe_name(fragment: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]", "_", fragment) or "unnamed"


def _atomic_write_json(path: Path, obj: Any) -> None:
    """Write JSON via tmp file + os.replace so readers never see a torn file."""
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(obj, indent=2, default=str), encoding="utf-8")
    os.replace(tmp, path)


class AuditTrail:
    """Session-scoped audit writer. One instance == one runs/<stamp>-session/ directory."""

    def __init__(self, runs_dir: Path) -> None:
        self.runs_dir = Path(runs_dir)
        self.runs_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(tz=_ET).strftime("%Y%m%d-%H%M%S")
        session_dir = self.runs_dir / f"{stamp}-session"
        n = 2
        while session_dir.exists():
            session_dir = self.runs_dir / f"{stamp}-session-{n}"
            n += 1
        session_dir.mkdir(parents=True)
        self.session_dir: Path = session_dir

        self.cycles_path: Path = session_dir / "cycles.jsonl"
        self.orders_path: Path = session_dir / "orders.json"
        self.order_csv_path: Path = session_dir / "order_log.csv"
        self.positions_path: Path = session_dir / "positions_snapshot.json"
        self.summary_path: Path = session_dir / "summary.json"

        self._orders: list[dict[str, Any]] = []
        _atomic_write_json(self.orders_path, self._orders)
        with self.order_csv_path.open("w", newline="", encoding="utf-8") as fh:
            csv.writer(fh).writerow(CSV_COLUMNS)

    # ------------------------------------------------------------------ cycles

    def record_cycle(self, rec: CycleRecord) -> Path:
        """Append the cycle to cycles.jsonl and write the per-cycle JSON document."""
        doc = rec.to_dict()
        line = json.dumps(doc, default=str)
        with self.cycles_path.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")
        per_cycle = self.session_dir / f"cycle-{_safe_name(rec.cycle_id)}.json"
        _atomic_write_json(per_cycle, doc)
        return per_cycle

    # ------------------------------------------------------------------ orders

    def log_order(self, action: str, request: dict, response: dict, request_id: str) -> None:
        """Record one mutating Alpaca exchange: JSON array (atomic rewrite) + CSV row."""
        entry: dict[str, Any] = {
            "action": action,
            "timestamp": _utc_iso(),
            "request": request if isinstance(request, dict) else {"raw": str(request)},
            "response": response if isinstance(response, dict) else {"raw": str(response)},
            "x_request_id": request_id or "",
        }
        self._orders.append(entry)
        _atomic_write_json(self.orders_path, self._orders)
        row = self._order_csv_fields(action, request, response)
        with self.order_csv_path.open("a", newline="", encoding="utf-8") as fh:
            csv.writer(fh).writerow(row)

    def _order_csv_fields(self, action: str, request: Any, response: Any) -> list[str]:
        req: dict[str, Any] = request if isinstance(request, dict) else {}
        resp: dict[str, Any] = response if isinstance(response, dict) else {}
        # An ExecutionReport nests the final Alpaca order under "raw" — that order
        # carries the ACTUALLY submitted limit_price/side/type/tif after reposts,
        # which must win over the intent's original decision-time price.
        raw: dict[str, Any] = resp.get("raw") if isinstance(resp.get("raw"), dict) else {}

        def pick(*keys: str) -> str:
            for source in (raw, resp, req):
                for key in keys:
                    value = source.get(key)
                    if value not in (None, ""):
                        return str(value)
            return ""

        symbol = pick("symbol")
        if not symbol:
            legs = raw.get("legs") or resp.get("legs") or req.get("legs") or []
            if isinstance(legs, list):
                symbol = "|".join(
                    str(leg.get("symbol", "")) for leg in legs if isinstance(leg, dict)
                )
        error = ""
        for source in (resp, req):
            if isinstance(source, dict) and source.get("error"):
                error = str(source["error"])
                break
        if not error and "id" not in resp and resp.get("message"):
            error = str(resp["message"])

        return [
            _utc_iso(),
            action,
            pick("id", "order_id"),
            pick("client_order_id"),
            symbol,
            pick("side"),
            pick("qty"),
            pick("type", "order_type"),
            pick("limit_price"),
            pick("time_in_force", "tif"),
            pick("status"),
            pick("filled_qty"),
            pick("filled_avg_price"),
            error,
        ]

    # --------------------------------------------------------------- positions

    def snapshot_positions(self, account: AccountSnapshot) -> None:
        """Latest-wins snapshot of the account + raw positions (atomic write)."""
        _atomic_write_json(
            self.positions_path,
            {"written_at": _utc_iso(), "account": account.to_dict()},
        )

    # ----------------------------------------------------------------- summary

    def summary(self) -> dict[str, Any]:
        """Aggregate this session: equity curve, verdict/execution/order counts.

        Writes summary.json (atomic) for the dashboard and returns the same dict.
        "realized_pnl" is the paper-equity delta over the session's cycles — on
        Alpaca paper, equity marks positions to market, so this includes
        unrealized MTM until positions are flattened.
        """
        cycles: list[dict[str, Any]] = []
        if self.cycles_path.exists():
            for line in self.cycles_path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    cycles.append(json.loads(line))
                except json.JSONDecodeError:
                    continue

        equity_points: list[dict[str, Any]] = []
        intents = approved = rejected = receipts = 0
        exec_status: dict[str, int] = {}
        for cycle in cycles:
            acct = cycle.get("account") or {}
            if isinstance(acct, dict) and acct.get("equity") is not None:
                try:
                    equity_points.append(
                        {"t": cycle.get("started_at"), "equity": float(acct["equity"])}
                    )
                except (TypeError, ValueError):
                    pass
            intents += len(cycle.get("intents") or [])
            for verdict in cycle.get("verdicts") or []:
                if isinstance(verdict, dict) and verdict.get("approved"):
                    approved += 1
                else:
                    rejected += 1
            for execution in cycle.get("executions") or []:
                status = "unknown"
                if isinstance(execution, dict):
                    status = str(execution.get("status") or "unknown")
                exec_status[status] = exec_status.get(status, 0) + 1
            if cycle.get("receipt_path"):
                receipts += 1

        first_equity = equity_points[0]["equity"] if equity_points else None
        last_equity = equity_points[-1]["equity"] if equity_points else None
        pnl = (last_equity - first_equity) if (first_equity is not None and last_equity is not None) else 0.0

        orders_by_action: dict[str, int] = {}
        for entry in self._orders:
            action = str(entry.get("action") or "unknown")
            orders_by_action[action] = orders_by_action.get(action, 0) + 1

        out: dict[str, Any] = {
            "generated_at": _utc_iso(),
            "session_dir": str(self.session_dir),
            "cycles": len(cycles),
            "equity_points": equity_points,
            "equity_open": first_equity,
            "equity_last": last_equity,
            "realized_pnl": round(pnl, 2),
            "intents": intents,
            "verdicts": {"approved": approved, "rejected": rejected},
            "executions": exec_status,
            "receipts": receipts,
            "orders_logged": len(self._orders),
            "orders_by_action": orders_by_action,
        }
        _atomic_write_json(self.summary_path, out)
        return out
