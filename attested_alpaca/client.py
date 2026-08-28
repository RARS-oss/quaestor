"""attested-alpaca — verifiable execution for any Alpaca agent.

Wrap your Alpaca credentials and place orders that come back with a
cryptographically signed, hermetically-sealed receipt of the exact exchange
conversation — then verify any receipt offline, prove each order respected its
risk cap in zero knowledge, anchor your track record to an external witness, and
export the whole run as a self-contained proof bundle.

The trust primitive an agent marketplace / copy-trading layer needs: an
autonomous agent's track record you can *verify*, not just trust.

    from attested_alpaca import AttestedAlpaca

    aa = AttestedAlpaca(api_key, secret_key)              # paper by default
    order = aa.submit_sealed({                            # placed inside a sealed cell
        "symbol": "SPY260904C00650000", "qty": "1", "side": "buy",
        "type": "limit", "limit_price": "0.50", "time_in_force": "day",
        "position_intent": "buy_to_open", "client_order_id": "demo-1",
    })
    print(order.status, order.receipt_path)               # e.g. canceled, receipts/sealed-*.json
    print(aa.verify(order.receipt_path))                  # -> True (signature + seal + chain)

This is a thin, agent-agnostic facade over the quaestor primitives (sealed
execution, signed receipts, ZK risk proofs, external anchoring, proof bundles).
quaestor itself is just the reference agent that uses it.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

PAPER_TRADING = "https://paper-api.alpaca.markets"
LIVE_TRADING = "https://api.alpaca.markets"


@dataclass
class _Settings:
    """Minimal settings shim so the facade needs no quaestor.config env loading."""
    api_key: str
    api_secret: str
    paper: bool = True
    trading_base: str = PAPER_TRADING
    data_base: str = "https://data.alpaca.markets"
    repo_root: Path = field(default_factory=lambda: Path.cwd())
    runs_dir: Path = field(default_factory=lambda: Path.cwd() / "runs")
    receipts_dir: Path = field(default_factory=lambda: Path.cwd() / "receipts")


@dataclass
class AttestedOrder:
    """Result of a sealed order placement: the fill + the signed receipt."""
    client_order_id: str
    order_id: str
    status: str
    filled_qty: float
    filled_avg_price: float
    request_ids: list[str]
    error: str
    receipt_path: Optional[str]
    zk_proof: Optional[dict[str, Any]] = None

    @property
    def sealed(self) -> bool:
        return bool(self.receipt_path)


class AttestedAlpaca:
    """Verifiable-execution wrapper around Alpaca order placement.

    Every order is placed from inside a hermetic no-root cell over a mediated
    egress tunnel (TLS terminates in the cell — your keys never leave it), and
    the exact request/response transcript is hash-chained into an Ed25519-signed
    receipt. `verify()` re-checks any receipt offline with no trust in the runner.
    """

    def __init__(
        self,
        api_key: str,
        secret_key: str,
        *,
        paper: bool = True,
        receipts_dir: str | Path = "receipts",
        runs_dir: str | Path = "runs",
    ) -> None:
        if not paper:
            # This reference implementation is paper-only by construction: sealed
            # live trading against real money is deliberately out of scope here.
            raise ValueError("AttestedAlpaca reference impl is paper-only (paper=True)")
        rd = Path(receipts_dir)
        rd.mkdir(parents=True, exist_ok=True)
        Path(runs_dir).mkdir(parents=True, exist_ok=True)
        self.settings = _Settings(
            api_key=api_key, api_secret=secret_key, paper=True,
            receipts_dir=rd, runs_dir=Path(runs_dir),
        )
        self.receipts_dir = rd
        # Lazily wire the primitives (each fail-open where the toolchain is absent).
        from quaestor.anchor import Anchor
        from quaestor.sealed_exec import SealedExecutor
        from quaestor.zk import ZkProver
        self._executor = SealedExecutor(self.settings, rd)
        self._zk = ZkProver()
        self._anchor = Anchor(self.settings)

    # -- capability probe -----------------------------------------------------------

    def available(self) -> bool:
        """True when the sealed-execution toolchain (bulla) is reachable."""
        return self._executor.available()

    # -- sealed execution -----------------------------------------------------------

    def submit_sealed(
        self, order_payload: dict[str, Any], *, label: str = "order",
        poll_seconds: float = 8.0, prove_risk_usd: float | None = None,
    ) -> AttestedOrder:
        """Place ONE order inside a sealed cell. Returns an AttestedOrder with the
        fill result and the signed receipt path. Optionally attach a ZK proof that
        the order's worst-case loss is under the cap (bound into client_order_id)."""
        orders = self.submit_batch_sealed(
            [order_payload], label=label, poll_seconds=poll_seconds,
            prove_risk_usd=[prove_risk_usd] if prove_risk_usd is not None else None)
        return orders[0] if orders else AttestedOrder(
            client_order_id=str(order_payload.get("client_order_id", "")), order_id="",
            status="error", filled_qty=0.0, filled_avg_price=0.0, request_ids=[],
            error="no result from sealed cell", receipt_path=None)

    def submit_batch_sealed(
        self, payloads: list[dict[str, Any]], *, label: str = "cycle",
        poll_seconds: float = 8.0, prove_risk_usd: list[float] | None = None,
    ) -> list[AttestedOrder]:
        """Place several orders in ONE sealed cell (one receipt for the batch)."""
        proofs: list[Optional[dict]] = []
        if prove_risk_usd is not None:
            proofs = [self._zk.prove_max_loss(v) for v in prove_risk_usd]
        results, receipt = self._executor.execute_cycle(
            label, payloads, poll_seconds=poll_seconds)
        by_cid = {str(r.get("client_order_id", "")): r for r in results}
        out: list[AttestedOrder] = []
        for i, payload in enumerate(payloads):
            cid = str(payload.get("client_order_id", ""))
            r = by_cid.get(cid, {})
            out.append(AttestedOrder(
                client_order_id=cid,
                order_id=str(r.get("order_id", "")),
                status=str(r.get("status", "error")),
                filled_qty=float(r.get("filled_qty", 0) or 0),
                filled_avg_price=float(r.get("filled_avg_price", 0) or 0),
                request_ids=list(r.get("request_ids", []) or []),
                error=str(r.get("error", "")),
                receipt_path=str(receipt) if receipt else None,
                zk_proof=proofs[i] if i < len(proofs) else None,
            ))
        return out

    # -- verification & proofs ------------------------------------------------------

    def verify(self, receipt_path: str | Path) -> bool:
        """True iff the receipt is intact (Ed25519 signature + body digest + event
        chain all check). Runs offline; no trust in the runner beyond its key."""
        from quaestor.receipts import ReceiptPress
        press = ReceiptPress(self.settings, self.receipts_dir)
        return press.verify(Path(receipt_path))

    def prove_risk_cap(self, max_loss_usd: float) -> Optional[dict[str, Any]]:
        """Zero-knowledge Bulletproofs proof that max_loss_usd < 2^16, size hidden."""
        return self._zk.prove_max_loss(max_loss_usd)

    def verify_risk_proof(self, proof: dict[str, Any]) -> bool:
        return self._zk.verify(proof)

    # -- track record ---------------------------------------------------------------

    def anchor_head(self) -> dict[str, Any]:
        """Witness the sealed ledger head into the external anchor chain."""
        ledger = self.receipts_dir / "sealed-ledger.jsonl"
        if not ledger.exists():
            ledger = self.receipts_dir / "ledger.jsonl"
        entry = self._anchor.anchor_from_ledger(str(ledger))
        if entry:
            self._anchor.push_external(entry)
        return entry

    def track_record(self) -> dict[str, Any]:
        """Summary of the signed, sealed record so far (counts + chain heads)."""
        from quaestor.bundle import stats_from_receipts
        return stats_from_receipts(self.receipts_dir)

    def export_bundle(self, out_dir: str | Path | None = None) -> Path:
        """Package the whole run (receipts + ledgers + anchor + WASM verifier +
        index.html) into a self-contained folder a third party can verify offline."""
        from quaestor.bundle import build_bundle
        return build_bundle(self.settings, Path(out_dir) if out_dir else None)


def from_env(**kwargs: Any) -> AttestedAlpaca:
    """Construct from ALPACA_API_KEY / ALPACA_SECRET_KEY in the environment."""
    key = os.environ.get("ALPACA_API_KEY", "")
    secret = os.environ.get("ALPACA_SECRET_KEY", "")
    if not key or not secret:
        raise RuntimeError("set ALPACA_API_KEY and ALPACA_SECRET_KEY in the environment")
    return AttestedAlpaca(key, secret, **kwargs)
