"""One read-only Alpaca CLI call per cycle, sealed into the decision receipt.

Role: make "this agent uses Alpaca's own tooling" a checkable fact rather than a
claim in a write-up. The contest requires the agent to use Alpaca's MCP server or
CLI; every other assertion this project makes is verifiable from a signed
receipt, and this one should be too. So each cycle shells out to the real
``alpaca`` binary, and its verbatim answer -- with a sha256 over the raw bytes --
is sealed alongside the decision.

The call is not decorative: the CLI's own view of the market clock is reconciled
against the agent's, and a disagreement is recorded in the cycle notes. Two
independent clocks that agree are worth more than one that is merely trusted.

Fail-open by design. A missing binary, a bad key, a timeout -- each is recorded
with its reason and never stops a trading cycle. The agent's execution path does
not run through the CLI (Alpaca's MCP ``mleg`` support is broken, #97, and every
entry this agent makes is a four-leg condor); orders continue to go through the
Trading API directly.

Auth note: the CLI reads ``APCA_API_KEY_ID`` / ``APCA_API_SECRET_KEY`` from the
environment, so no ``alpaca profile login`` is needed -- verified 2026-08-31
against the competition account.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover
    from quaestor.config import Settings

__all__ = ["resolve_cli", "probe_clock"]

_DEFAULT_CLI = Path.home() / "hack" / "bin" / "alpaca"
_TIMEOUT_S = 15.0
_MAX_STDOUT = 4096          # a clock payload is ~200B; cap keeps receipts small


def resolve_cli() -> str | None:
    """Path to the Alpaca CLI, or None when it is not installed.

    ``QUAESTOR_ALPACA_CLI`` overrides; otherwise the pinned install location,
    then whatever is on PATH.
    """
    override = os.environ.get("QUAESTOR_ALPACA_CLI", "").strip()
    if override:
        return override if Path(override).is_file() else None
    if _DEFAULT_CLI.is_file():
        return str(_DEFAULT_CLI)
    return shutil.which("alpaca")


def _cli_env(settings: "Settings") -> dict[str, str]:
    """Environment for the CLI: its own credential names, paper forced on."""
    env = dict(os.environ)
    env["APCA_API_KEY_ID"] = settings.api_key
    env["APCA_API_SECRET_KEY"] = settings.api_secret
    env["ALPACA_PAPER_TRADE"] = "true"
    return env


def probe_clock(settings: "Settings", timeout_s: float = _TIMEOUT_S) -> dict[str, Any]:
    """Run ``alpaca clock`` and return a sealable record of the invocation.

    Always returns a dict -- never raises. ``ok`` says whether the CLI answered;
    ``is_open`` is Alpaca's own answer, or None when it could not be read.
    """
    record: dict[str, Any] = {
        "tool": "alpaca-cli",
        "argv": ["alpaca", "clock"],
        "ok": False,
        "is_open": None,
    }

    binary = resolve_cli()
    if binary is None:
        record["error"] = "alpaca CLI not found (set QUAESTOR_ALPACA_CLI to its path)"
        return record
    record["binary"] = binary

    started = time.monotonic()
    try:
        proc = subprocess.run(                      # noqa: S603 - fixed argv, no shell
            [binary, "clock"],
            capture_output=True, text=True, timeout=timeout_s,
            env=_cli_env(settings), check=False,
        )
    except subprocess.TimeoutExpired:
        record["error"] = f"alpaca clock timed out after {timeout_s:g}s"
        record["duration_ms"] = int((time.monotonic() - started) * 1000)
        return record
    except OSError as exc:
        record["error"] = f"could not execute the CLI: {exc!r}"
        return record

    stdout = (proc.stdout or "")[:_MAX_STDOUT]
    record["duration_ms"] = int((time.monotonic() - started) * 1000)
    record["exit_code"] = proc.returncode
    record["stdout_sha256"] = hashlib.sha256(
        (proc.stdout or "").encode("utf-8")).hexdigest()
    record["stdout"] = stdout

    if proc.returncode != 0:
        # stderr can carry a credential hint; keep the reason, not the secret.
        record["error"] = (proc.stderr or "").strip()[:300] or "non-zero exit"
        return record

    try:
        payload = json.loads(stdout)
    except ValueError:
        record["error"] = "CLI output was not JSON"
        return record

    record["ok"] = True
    if isinstance(payload, dict):
        if isinstance(payload.get("is_open"), bool):
            record["is_open"] = payload["is_open"]
        for key in ("next_open", "next_close", "timestamp"):
            if isinstance(payload.get(key), str):
                record[key] = payload[key]
    return record


def reconcile(record: dict[str, Any], market_open: bool) -> str | None:
    """Note to append when Alpaca's clock disagrees with ours, else None.

    A disagreement is not made fatal: the agent's own clock stays authoritative
    (it is what the risk gates read), and the receipt carries both answers so the
    discrepancy is auditable after the fact rather than silently resolved.
    """
    if not record.get("ok"):
        return f"alpaca CLI probe failed: {record.get('error', 'unknown')}"
    theirs = record.get("is_open")
    if isinstance(theirs, bool) and theirs != market_open:
        return (f"clock disagreement: alpaca CLI says market_open={theirs}, "
                f"quaestor says {market_open} (ours is authoritative this cycle)")
    return None
