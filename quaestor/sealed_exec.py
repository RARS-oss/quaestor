"""Sealed execution: place approved orders from INSIDE a bulla cell.

This is Phase A — "the whole week under seal". The agent decides outside the
cell (strategy + risk need the venv), then hands the approved order payloads to
a hermetic cell that places them over the mediated egress tunnel. The real
exchange conversation for every order is hash-chained into a signed receipt, and
every cycle's receipt links into one week-long ledger. The submitted account's
entire P&L is therefore verifiable end to end, not a demo.

Contract mirrors broker.execute at the cycle level: given a list of approved
(intent, order-payload) pairs, run ONE cell that executes them all and return
per-order ExecutionReport-shaped dicts. Falls back to None (caller uses the
normal broker path) if the sealed toolchain is unavailable — sealed execution
must never silently drop trades.

bulla cell facts encoded (verified):
- The egress broker's Unix socket lives under --work; drvfs (/mnt/c) has no Unix
  sockets, so the cell work dir MUST be ext4 (~/.cache/quaestor-cells/...).
- --nondeterministic inherits the parent env (ALPACA_* keys reach the cell) and
  the seal still reports HELD; --egress-allow keeps net mediated + seal HELD.
- --key / --ledger must resolve OUTSIDE --work (bulla refuses them inside).
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

from quaestor.receipts import to_wsl_path, wsl_bridge

if TYPE_CHECKING:  # pragma: no cover
    from quaestor.config import Settings

BULLA = "~/.cache/hack-target/release/bulla"
IN_CELL_SCRIPT = "in_cell_execute.py"
# ext4 home for cells (Unix sockets don't work on drvfs /mnt/c).
CELL_ROOT_WSL = "~/.cache/quaestor-cells"
_SUBPROC_TIMEOUT_S = 120.0


class SealedExecutor:
    """Runs a cycle's approved orders inside a bulla cell over the sealed tunnel."""

    def __init__(self, settings: "Settings", receipts_dir: Path,
                 *, windows: bool | None = None) -> None:
        self.settings = settings
        self.receipts_dir = Path(receipts_dir)
        self._windows = (os.name == "nt") if windows is None else windows
        self.ledger_path = self.receipts_dir / "sealed-ledger.jsonl"
        self.key_path = self.receipts_dir / "signing.seed"
        # Where in_cell_execute.py lives on the host (repo scripts/), and its WSL view.
        self._script_host = Path(__file__).resolve().parents[1] / "scripts" / IN_CELL_SCRIPT

    def available(self) -> bool:
        """True if the bulla binary and the in-cell script are reachable."""
        if not self._script_host.exists():
            return False
        try:
            r = self._bulla(["--version"])
            return r.returncode == 0
        except Exception:
            return False

    def execute_cycle(
        self, cycle_id: str, orders: list[dict[str, Any]],
        *, poll_seconds: float = 8.0, poll_interval: float = 1.5,
    ) -> tuple[list[dict[str, Any]], Path | None]:
        """Execute all approved order payloads in one sealed cell.

        Returns (results, receipt_path). results is a list of dicts shaped like
        {client_order_id, order_id, status, filled_qty, filled_avg_price,
        request_ids, error}. receipt_path is the signed receipt for the cycle (or
        None on failure — caller then falls back to the normal broker path).
        """
        cid = _safe(cycle_id)
        # bulla runs via subprocess (no shell), so --work must be an absolute path,
        # not "~/..." (a literal tilde would create a dir named "~"). The agent runs
        # inside WSL, so $HOME resolves here; Windows keeps the ~-form for the shell.
        cell_arg = self._cell_arg(cid)
        cell_local = None if self._windows else cell_arg
        receipt_path = self.receipts_dir / f"sealed-{cid}.json"

        spec = {"orders": orders, "poll_seconds": poll_seconds, "poll_interval": poll_interval}

        # Stage the cell dir (fresh) with the in-cell script + orders.json.
        try:
            self._stage_cell(cell_local, cell_arg, spec)
        except Exception:
            return [], None

        argv = [
            BULLA, "run",
            "--work", cell_arg,
            "--egress-allow", "paper-api.alpaca.markets:443",
            "--nondeterministic",
            "--wall-ms", str(int((poll_seconds + 20) * 1000 * max(1, len(orders)))),
            "--out", self._posix(receipt_path),
            "--ledger", self._posix(self.ledger_path),
            "--key", self._posix(self.key_path),
            "--", "python3", "/work/in_cell_execute.py",
        ]
        try:
            r = self._bulla_with_env(argv)
        except Exception:
            return [], None
        if not receipt_path.exists():
            return [], None

        results = self._read_results(cell_arg, cell_local)
        return results, receipt_path

    # ---- internals ----------------------------------------------------------------

    def _cell_arg(self, cid: str) -> str:
        """The --work path bulla receives. Absolute on WSL/Linux (no shell to expand
        ~); ~-form on Windows where the wsl shell expands it."""
        if self._windows:
            return f"{CELL_ROOT_WSL}/{cid}"
        home = os.environ.get("HOME") or os.path.expanduser("~")
        return f"{home}/.cache/quaestor-cells/{cid}"

    def _stage_cell(self, cell_local: str | None, cell_wsl: str, spec: dict) -> None:
        """Create a fresh cell dir and drop in_cell_execute.py + orders.json."""
        spec_json = json.dumps(spec)
        if cell_local is not None:
            p = Path(cell_local)
            if p.exists():
                shutil.rmtree(p)
            p.mkdir(parents=True)
            shutil.copyfile(self._script_host, p / IN_CELL_SCRIPT)
            (p / "orders.json").write_text(spec_json, encoding="utf-8")
            return
        # Windows: use wsl to mkdir, copy the script from its /mnt/c path, write orders.
        script_wsl = to_wsl_path(self._script_host)
        sh = (
            f"rm -rf {cell_wsl} && mkdir -p {cell_wsl} && "
            f"cp {script_wsl} {cell_wsl}/{IN_CELL_SCRIPT} && "
            f"cat > {cell_wsl}/orders.json"
        )
        cmd = ["wsl", "-d", "Ubuntu", "--", "bash", "-lc", sh]
        subprocess.run(cmd, input=spec_json, text=True, timeout=_SUBPROC_TIMEOUT_S, check=True)

    def _read_results(self, cell_arg: str, cell_local: str | None) -> list[dict[str, Any]]:
        if cell_local is not None:
            try:
                rp = Path(cell_local) / "results.json"
                return json.loads(rp.read_text(encoding="utf-8")).get("results", [])
            except (OSError, ValueError):
                return []
        cmd = ["wsl", "-d", "Ubuntu", "--", "bash", "-lc", f"cat {cell_arg}/results.json"]
        try:
            out = subprocess.run(cmd, capture_output=True, text=True,
                                 timeout=_SUBPROC_TIMEOUT_S).stdout
            return json.loads(out).get("results", [])
        except Exception:
            return []

    def _posix(self, path: Path) -> str:
        return to_wsl_path(path) if self._windows else str(path)

    def _bulla(self, args: list[str]) -> subprocess.CompletedProcess:
        argv = [BULLA, *args]
        cmd = wsl_bridge(argv) if self._windows else (
            [str(Path(BULLA).expanduser()), *args])
        return subprocess.run(cmd, capture_output=True, text=True, timeout=30)

    def _bulla_with_env(self, argv: list[str]) -> subprocess.CompletedProcess:
        """Run bulla so the cell inherits ALPACA_* (via --nondeterministic)."""
        env = dict(os.environ)
        env["ALPACA_API_KEY"] = self.settings.api_key
        env["ALPACA_SECRET_KEY"] = self.settings.api_secret
        if self._windows:
            # The env must reach the WSL child: prefix the shell command with exports.
            head, *rest = argv
            head_s = head if head.startswith("~") else _q(head)
            inner = " ".join([head_s, *(_q(a) for a in rest)])
            sh = (f"export ALPACA_API_KEY={_q(self.settings.api_key)}; "
                  f"export ALPACA_SECRET_KEY={_q(self.settings.api_secret)}; {inner}")
            cmd = ["wsl", "-d", "Ubuntu", "--", "bash", "-lc", sh]
            return subprocess.run(cmd, capture_output=True, text=True, timeout=_SUBPROC_TIMEOUT_S * 3)
        cmd = [str(Path(argv[0]).expanduser()), *argv[1:]]
        return subprocess.run(cmd, capture_output=True, text=True,
                              timeout=_SUBPROC_TIMEOUT_S * 3, env=env)


def _safe(s: str) -> str:
    return "".join(c if c.isalnum() or c in "-_." else "_" for c in s) or "cycle"


def _q(s: str) -> str:
    import shlex
    return shlex.quote(s)
