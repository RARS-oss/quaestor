"""bulla receipts integration — a signed, verifiable audit cell around every decision cycle.

Role
----
``ReceiptPress`` seals each quaestor decision cycle into a *bulla* hermetic cell and
maintains bulla's tamper-evident run ledger. The v1 shape ("hermetic attestation of
bytes"): the decision function runs OUTSIDE bulla (it needs live Alpaca data), its
exact input and output bytes are written into a fresh cell work dir, and bulla then
runs ``sha256sum input.json decision.json`` inside a fully hermetic cell (no network,
deterministic env) — so the signed receipt binds the exact bytes of what the agent
saw and what it decided, with SEAL HELD. Full in-cell API submission is the v2
upgrade (see patches/bulla/README.md).

bulla v0 CLI facts encoded here (verified against the local clone):
- Subcommands used: ``run`` / ``verify`` / ``keygen``.
- ``run`` flags: ``--work DIR --out PATH --key PATH --allow-net --nondeterministic
  --wall-ms N --ledger PATH --json``; the command comes after ``--``.
- There is NO ``--env`` flag: the deterministic profile strips host env vars entirely
  (hermit-core fixed env), so API keys cannot reach a hermetic cell in v0. v1 therefore
  never needs them inside the cell — no network call happens in there at all.
- ``--key`` and ``--ledger`` are REFUSED if they resolve inside the ``--work`` dir
  (trust material must be outside the cell-writable mount). Hence the layout below.
- ``verify`` exits 0 iff the receipt is intact (signature + body digest + event chain);
  it exits 2 otherwise.

Alpaca facts encoded
--------------------
The decision payload sealed here is exactly what ``risk.judge`` saw (account snapshot,
indicative-feed option quotes) and what ``orders.build_order_payload`` produced (mleg
payloads with signed net limit_price, TIF=day, idempotent client_order_id). Alpaca API
keys NEVER enter the cell work dir: secrets flow only through config.Settings and the
cell runs no network code (per the "never written into receipt work dirs" standard).

Layout (under ``receipts_dir``)
-------------------------------
    receipts/signing.seed        Ed25519 seed (``bulla keygen``) — OUTSIDE any cell
    receipts/ledger.jsonl        bulla's hash-chained run ledger — OUTSIDE any cell
    receipts/<cycle_id>.json     the signed receipt (``--out``)
    receipts/cells/<cycle_id>/   the cell ``--work`` dir: input.json + decision.json

WSL bridge
----------
The bulla binary lives inside WSL (``~/.cache/hack-target/release/bulla``). On Windows,
invocations are wrapped as ``wsl -d Ubuntu -- bash -lc '<cmd>'`` with ``C:\\`` paths
translated to ``/mnt/c/...``. If bulla is unavailable anywhere, an UNSIGNED
"receipt-lite" JSON is written instead — the trading loop never dies because of the
receipt layer.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable

if TYPE_CHECKING:  # pragma: no cover — avoid a runtime dependency on config.py
    from quaestor.config import Settings

# Inside WSL; expanduser'd (Linux) or tilde-expanded by `bash -lc` (Windows bridge).
BULLA: str = "~/.cache/hack-target/release/bulla"
WSL_DISTRO: str = "Ubuntu"
CELL_WALL_MS: int = 60_000
RECEIPT_LITE_SCHEMA: str = "quaestor.receipt-lite.v1"
CELL_INPUT_SCHEMA: str = "quaestor.cell-input.v1"
# The command sealed inside the hermetic cell: attest the exact bytes of both files.
CELL_COMMAND: tuple[str, ...] = ("/bin/sh", "-c", "sha256sum input.json decision.json")

_SUBPROCESS_TIMEOUT_S: float = 120.0
_WIN_DRIVE_RE = re.compile(r"^([A-Za-z]):[\\/](.*)$")
_CYCLE_ID_SAFE_RE = re.compile(r"[^A-Za-z0-9._-]")


class BullaUnavailableError(RuntimeError):
    """bulla (or the WSL bridge) could not be invoked — degrade to receipt-lite."""


@dataclass
class BullaResult:
    """Outcome of one bulla CLI invocation."""

    returncode: int
    stdout: str
    stderr: str


# An invoker receives the POSIX-side bulla argv (argv[0] is the bulla binary path)
# and returns a BullaResult. The default invoker adds the WSL bridge on Windows.
# Tests inject a fake invoker to assert composed args without any subprocess.
BullaInvoker = Callable[[list[str]], BullaResult]


def to_wsl_path(path: str | Path) -> str:
    """Translate a Windows path to its WSL view: ``C:\\a\\b`` -> ``/mnt/c/a/b``.

    Non-drive paths pass through with backslashes normalized to forward slashes.
    """
    s = str(path)
    m = _WIN_DRIVE_RE.match(s)
    if m:
        drive = m.group(1).lower()
        rest = m.group(2).replace("\\", "/")
        return f"/mnt/{drive}/{rest}" if rest else f"/mnt/{drive}"
    return s.replace("\\", "/")


def wsl_bridge(bulla_argv: list[str]) -> list[str]:
    """Wrap a POSIX bulla argv for execution from Windows via WSL.

    Shape: ``["wsl", "-d", "Ubuntu", "--", "bash", "-lc", <shell command>]``.
    argv[0] is left unquoted when it starts with ``~`` so bash expands it to the
    WSL home; every other token is shell-quoted.
    """
    if not bulla_argv:
        raise ValueError("empty bulla argv")
    head, *rest = bulla_argv
    head_s = head if head.startswith("~") else shlex.quote(head)
    shell_cmd = " ".join([head_s, *(shlex.quote(a) for a in rest)])
    return ["wsl", "-d", WSL_DISTRO, "--", "bash", "-lc", shell_cmd]


def _utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _write_json(path: Path, payload: dict[str, Any]) -> bytes:
    """Write deterministic JSON bytes (sorted keys) and return them."""
    raw = (json.dumps(payload, sort_keys=True, indent=2, default=str) + "\n").encode("utf-8")
    path.write_bytes(raw)
    return raw


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _sanitize_cycle_id(cycle_id: str) -> str:
    """Filesystem-safe cycle id (no separators, no traversal)."""
    safe = _CYCLE_ID_SAFE_RE.sub("_", cycle_id.strip())
    return safe or "cycle"


class ReceiptPress:
    """Produces signed bulla receipts (or UNSIGNED receipt-lite fallbacks) per cycle."""

    def __init__(
        self,
        settings: "Settings | Any",
        receipts_dir: Path,
        key_path: Path | None = None,
        *,
        bulla_invoker: BullaInvoker | None = None,
        windows: bool | None = None,
    ) -> None:
        self.settings = settings
        self.receipts_dir = Path(receipts_dir)
        self.cells_dir = self.receipts_dir / "cells"
        self.key_path = Path(key_path) if key_path is not None else self.receipts_dir / "signing.seed"
        self.ledger_path = self.receipts_dir / "ledger.jsonl"
        self._windows: bool = (os.name == "nt") if windows is None else windows
        self._invoker: BullaInvoker = bulla_invoker if bulla_invoker is not None else self._run_bulla

        self.receipts_dir.mkdir(parents=True, exist_ok=True)
        self.cells_dir.mkdir(parents=True, exist_ok=True)
        # bulla refuses --key/--ledger inside the --work dir; fail early and loudly here.
        for label, p in (("key_path", self.key_path), ("ledger_path", self.ledger_path)):
            if self._is_inside(p, self.cells_dir):
                raise ValueError(
                    f"{label} {p} resolves inside the cells dir {self.cells_dir}; "
                    "bulla refuses trust material inside a cell --work dir"
                )

    # -- public API ------------------------------------------------------------------

    def attested_cycle(
        self, cycle_id: str, decision_fn: Callable[[], dict]
    ) -> tuple[dict, Path]:
        """Run one decision, then seal its exact input/decision bytes in a bulla cell.

        v1 flow:
        1. fresh cell work dir ``receipts/cells/<cycle_id>/`` gets ``input.json``;
        2. ``decision_fn`` runs OUTSIDE bulla (live Alpaca calls happen here) and its
           dict is written to ``decision.json`` (if it carries an ``"inputs"`` key,
           those are folded into ``input.json`` too);
        3. ``bulla run`` executes ``sha256sum input.json decision.json`` hermetically
           (no --allow-net, no --nondeterministic) -> signed SEAL HELD receipt at
           ``receipts/<cycle_id>.json`` + a ledger entry.

        Exceptions from ``decision_fn`` propagate (the agent loop owns that policy);
        ANY failure in the receipt machinery degrades to an UNSIGNED receipt-lite.
        Returns ``(decision_dict, receipt_path)``.
        """
        cid = _sanitize_cycle_id(cycle_id)
        cell_dir = self.cells_dir / cid
        if cell_dir.exists():
            shutil.rmtree(cell_dir)
        cell_dir.mkdir(parents=True)

        input_path = cell_dir / "input.json"
        decision_path = cell_dir / "decision.json"
        input_payload: dict[str, Any] = {
            "schema": CELL_INPUT_SCHEMA,
            "cycle_id": cid,
            "created_utc": _utc_iso(),
        }
        _write_json(input_path, input_payload)

        decision_raw = decision_fn()  # outside bulla by design (v1); may raise
        decision: dict[str, Any] = (
            decision_raw if isinstance(decision_raw, dict) else {"decision": decision_raw}
        )
        if "inputs" in decision:
            input_payload["inputs"] = decision["inputs"]
            _write_json(input_path, input_payload)
        _write_json(decision_path, decision)

        receipt_path = self.receipts_dir / f"{cid}.json"
        try:
            self._ensure_key()
            argv: list[str] = [
                BULLA,
                "run",
                "--work", self._posix(cell_dir),
                "--out", self._posix(receipt_path),
                "--key", self._posix(self.key_path),
                "--ledger", self._posix(self.ledger_path),
                "--wall-ms", str(CELL_WALL_MS),
                "--json",
                "--",
                *CELL_COMMAND,
            ]
            result = self._invoker(argv)
            if result.returncode != 0:
                raise BullaUnavailableError(
                    f"bulla run exited {result.returncode}: "
                    f"{(result.stderr or result.stdout).strip()[:400]}"
                )
            if not receipt_path.exists():
                raise BullaUnavailableError("bulla run reported success but wrote no receipt")
        except Exception as exc:  # the receipt layer must never kill the trading loop
            receipt_path = self._write_receipt_lite(
                cid, cell_dir, input_path, decision_path, reason=str(exc)
            )
        return decision, receipt_path

    def verify(self, receipt_path: Path) -> bool:
        """True iff ``bulla verify`` exits 0 on this receipt (intact + signed).

        Receipt-lite files (UNSIGNED marker) are never verifiable -> False.
        Any bulla failure -> False (fail closed on trust claims).
        """
        receipt_path = Path(receipt_path)
        try:
            data = json.loads(receipt_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return False
        if data.get("UNSIGNED") or data.get("schema") == RECEIPT_LITE_SCHEMA:
            return False
        try:
            result = self._invoker([BULLA, "verify", self._posix(receipt_path)])
        except Exception:
            return False
        return result.returncode == 0

    def ledger_summary(self) -> dict[str, Any]:
        """Parse ``receipts/ledger.jsonl`` and count UNSIGNED receipt-lite files."""
        entries: list[dict[str, Any]] = []
        if self.ledger_path.exists():
            for line in self.ledger_path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except ValueError:
                    continue
                if isinstance(obj, dict):
                    entries.append(obj)

        seal_held = sum(1 for e in entries if e.get("seal_ok") is True)
        unsigned_files: list[str] = []
        for p in sorted(self.receipts_dir.glob("*.json")):
            try:
                d = json.loads(p.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if isinstance(d, dict) and (
                d.get("UNSIGNED") or d.get("schema") == RECEIPT_LITE_SCHEMA
            ):
                unsigned_files.append(p.name)

        last: dict[str, Any] = entries[-1] if entries else {}
        return {
            "ledger_path": str(self.ledger_path),
            "attempts": len(entries),
            "seal_held": seal_held,
            "seal_broken": len(entries) - seal_held,
            "last_seq": last.get("seq"),
            "last_hash": last.get("hash"),
            "unsigned_receipts": len(unsigned_files),
            "unsigned_files": unsigned_files,
        }

    # -- internals -------------------------------------------------------------------

    def _posix(self, path: Path) -> str:
        """The path string as bulla (inside WSL) must see it."""
        return to_wsl_path(path) if self._windows else str(path)

    @staticmethod
    def _is_inside(path: Path, ancestor: Path) -> bool:
        try:
            Path(path).resolve().relative_to(Path(ancestor).resolve())
            return True
        except ValueError:
            return False

    def _ensure_key(self) -> None:
        """``bulla keygen --key receipts/signing.seed`` on first use (best-effort:
        ``bulla run`` also creates a missing seed itself)."""
        if self.key_path.exists():
            return
        self.key_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            self._invoker([BULLA, "keygen", "--key", self._posix(self.key_path)])
        except Exception:
            pass  # bulla run will create the seed; if bulla is absent, run fails -> lite

    def _run_bulla(self, bulla_argv: list[str]) -> BullaResult:
        """Default invoker: direct subprocess on Linux/WSL, wsl-bridged on Windows."""
        if self._windows:
            cmd = wsl_bridge(bulla_argv)
        else:
            binary = Path(bulla_argv[0]).expanduser()
            if not binary.exists():
                raise BullaUnavailableError(f"bulla binary not found: {binary}")
            cmd = [str(binary), *bulla_argv[1:]]
        try:
            proc = subprocess.run(  # noqa: S603 — fixed binary, no shell on Linux path
                cmd, capture_output=True, text=True, timeout=_SUBPROCESS_TIMEOUT_S
            )
        except FileNotFoundError as exc:  # wsl.exe (or the binary) missing
            raise BullaUnavailableError(str(exc)) from exc
        except subprocess.TimeoutExpired as exc:
            raise BullaUnavailableError(f"bulla invocation timed out: {exc}") from exc
        if proc.returncode == 127:  # bash -lc: command not found inside WSL
            raise BullaUnavailableError(
                (proc.stderr or proc.stdout).strip() or "bulla not found in WSL (exit 127)"
            )
        return BullaResult(proc.returncode, proc.stdout, proc.stderr)

    def _write_receipt_lite(
        self,
        cycle_id: str,
        cell_dir: Path,
        input_path: Path,
        decision_path: Path,
        reason: str,
    ) -> Path:
        """Unsigned fallback receipt with a loud marker — better than dying silently."""
        receipt = {
            "schema": RECEIPT_LITE_SCHEMA,
            "UNSIGNED": True,
            "warning": (
                "UNSIGNED receipt-lite — bulla was unavailable; there is NO "
                "cryptographic seal on this cycle. Hashes below are self-reported."
            ),
            "reason": reason,
            "cycle_id": cycle_id,
            "created_utc": _utc_iso(),
            "cell_dir": str(cell_dir),
            "input_sha256": _sha256_file(input_path),
            "decision_sha256": _sha256_file(decision_path),
        }
        path = self.receipts_dir / f"{cycle_id}.json"
        _write_json(path, receipt)
        return path
