"""Unit tests for the attested-alpaca facade (no live API, no bulla — fakes injected)."""
from __future__ import annotations

from pathlib import Path

import pytest

from attested_alpaca import AttestedAlpaca, AttestedOrder


class _FakeExecutor:
    """Stands in for SealedExecutor: records calls, returns canned results."""

    def __init__(self, receipt: Path | None) -> None:
        self.receipt = receipt
        self.calls: list[tuple[str, list[dict]]] = []

    def available(self) -> bool:
        return True

    def execute_cycle(self, label, orders, *, poll_seconds=8.0, poll_interval=1.5):
        self.calls.append((label, orders))
        results = [{
            "client_order_id": o.get("client_order_id", ""),
            "order_id": "oid-" + o.get("client_order_id", ""),
            "status": "canceled", "filled_qty": 0.0, "filled_avg_price": 0.0,
            "request_ids": ["rid1", "rid2"], "error": "",
        } for o in orders]
        return results, self.receipt


def _mk(tmp_path: Path, receipt: Path | None = None) -> AttestedAlpaca:
    aa = AttestedAlpaca("PKTEST", "secret", receipts_dir=tmp_path / "receipts",
                        runs_dir=tmp_path / "runs")
    aa._executor = _FakeExecutor(receipt)
    return aa


def test_paper_only_enforced(tmp_path):
    with pytest.raises(ValueError, match="paper-only"):
        AttestedAlpaca("k", "s", paper=False, receipts_dir=tmp_path / "r")


def test_submit_sealed_maps_result_and_receipt(tmp_path):
    receipt = tmp_path / "receipts" / "sealed-x.json"
    aa = _mk(tmp_path, receipt)
    payload = {"symbol": "SPY260904C00650000", "qty": "1", "side": "buy",
               "type": "limit", "limit_price": "0.50", "time_in_force": "day",
               "client_order_id": "demo-1"}
    order = aa.submit_sealed(payload, label="unit")
    assert isinstance(order, AttestedOrder)
    assert order.client_order_id == "demo-1"
    assert order.order_id == "oid-demo-1"
    assert order.status == "canceled"
    assert order.request_ids == ["rid1", "rid2"]
    assert order.receipt_path == str(receipt)
    assert order.sealed is True
    assert aa._executor.calls[0][0] == "unit"


def test_submit_batch_maps_each_by_client_id(tmp_path):
    aa = _mk(tmp_path, tmp_path / "receipts" / "sealed-batch.json")
    payloads = [
        {"client_order_id": "a", "symbol": "S1"},
        {"client_order_id": "b", "symbol": "S2"},
    ]
    orders = aa.submit_batch_sealed(payloads, label="batch")
    assert [o.client_order_id for o in orders] == ["a", "b"]
    assert all(o.sealed for o in orders)


def test_no_receipt_marks_unsealed(tmp_path):
    aa = _mk(tmp_path, None)  # cell produced no receipt
    order = aa.submit_sealed({"client_order_id": "c"}, label="u")
    assert order.receipt_path is None
    assert order.sealed is False


def test_zk_proof_attached_when_requested(tmp_path):
    aa = _mk(tmp_path, tmp_path / "receipts" / "sealed-zk.json")

    class _FakeZk:
        def prove_max_loss(self, v):
            return {"cap_bits": 16, "commitment": "abc123", "proof": "deadbeef"} if v < 65536 else None
    aa._zk = _FakeZk()
    order = aa.submit_sealed({"client_order_id": "z"}, prove_risk_usd=100.0)
    assert order.zk_proof is not None
    assert order.zk_proof["commitment"] == "abc123"
