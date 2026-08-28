"""Zero-knowledge risk-cap proofs (bulla-zk Bulletproofs) per approved order.

The proved statement: "this order's worst-case loss, in whole dollars, is under
2^16 = $65,536" — a cryptographic catastrophic-order guard. It does NOT reveal
the loss amount (Pedersen commitment, information-theoretically hiding). The
fine-grained limits (10%/20% caps etc.) are enforced by the deterministic risk
gates whose policy digest is signed into every bulla receipt; the ZK proof adds
a bound anyone can verify WITHOUT seeing our sizing.

Binding: the commitment's first 16 hex chars ride inside the Alpaca
client_order_id (models.new_client_order_id), and the full RiskProof JSON is
sealed into the cycle's bulla receipt (decision payload -> stdout sha256).

Mechanics: shells out to the `zkrisk` example binary built from the user's own
bulla repo (bulla-zk crate): `zkrisk prove <value_u64> <cap_bits>` -> RiskProof
JSON; `zkrisk verify [file]` -> {"ok": bool} + exit code. Fail-open: any failure
returns None — a missing proof never blocks trading, it just shows up honestly
as an unproven order in the audit trail.
"""
from __future__ import annotations

import json
import math
import os
import subprocess
from pathlib import Path
from typing import Any, Callable, Optional

from quaestor.receipts import BullaResult, to_wsl_path, wsl_bridge

ZKRISK: str = "~/.cache/hack-target/release/examples/zkrisk"
CAP_BITS: int = 16          # 2^16 dollars = $65,536 hard ceiling per order
_TIMEOUT_S: float = 30.0

ZkInvoker = Callable[[list[str]], BullaResult]


class ZkProver:
    """Produces and verifies per-order ZK risk-cap proofs. Never raises to callers."""

    def __init__(self, *, invoker: ZkInvoker | None = None, windows: bool | None = None) -> None:
        self._windows = (os.name == "nt") if windows is None else windows
        self._invoker = invoker if invoker is not None else self._run

    def prove_max_loss(self, max_loss_usd: float) -> Optional[dict[str, Any]]:
        """RiskProof dict for ceil(max_loss_usd) dollars under 2^16, or None.

        None when: value out of provable range, binary missing, or any failure.
        Callers treat None as "order carries no ZK proof" (noted in the audit).
        """
        try:
            value = int(math.ceil(max(0.0, float(max_loss_usd))))
            if value >= (1 << CAP_BITS):
                return None  # not provable under the cap — risk gates reject these anyway
            result = self._invoker([ZKRISK, "prove", str(value), str(CAP_BITS)])
            if result.returncode != 0:
                return None
            proof = json.loads(result.stdout.strip())
            if not isinstance(proof, dict) or "commitment" not in proof:
                return None
            proof["value_usd_ceiling"] = value
            return proof
        except Exception:
            return None

    def verify(self, proof: dict[str, Any]) -> bool:
        """True iff `zkrisk verify` accepts this proof. Fail-closed on any error."""
        try:
            payload = {k: proof[k] for k in ("cap_bits", "commitment", "proof")}
            result = self._invoker_stdin([ZKRISK, "verify"], json.dumps(payload))
            return result.returncode == 0
        except Exception:
            return False

    @staticmethod
    def commitment_prefix(proof: Optional[dict[str, Any]]) -> str:
        """First 16 hex chars of the Pedersen commitment for client_order_id binding."""
        if not proof:
            return ""
        return str(proof.get("commitment", ""))[:16]

    # -- internals -------------------------------------------------------------------

    def _cmd(self, argv: list[str]) -> list[str]:
        if self._windows:
            return wsl_bridge(argv)
        head = str(Path(argv[0]).expanduser())
        return [head, *argv[1:]]

    def _run(self, argv: list[str]) -> BullaResult:
        proc = subprocess.run(
            self._cmd(argv), capture_output=True, text=True, timeout=_TIMEOUT_S
        )
        return BullaResult(proc.returncode, proc.stdout, proc.stderr)

    def _invoker_stdin(self, argv: list[str], stdin_data: str) -> BullaResult:
        proc = subprocess.run(
            self._cmd(argv), capture_output=True, text=True,
            timeout=_TIMEOUT_S, input=stdin_data,
        )
        return BullaResult(proc.returncode, proc.stdout, proc.stderr)
