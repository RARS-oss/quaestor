"""Offline unit tests for quaestor.bundle — no network, no bulla binary, no WSL.

Builds a proof bundle from a temp receipts dir holding two fake signed-receipt
JSONs (one byte-attestation "decision" receipt, one egress "sealed" receipt) plus
a fake session summary, then asserts the generated index.html exists and names both
receipt ids, links to verifier.html, embeds the Alpaca disclosure, and that the
receipts were actually copied into the bundle. stats_from_receipts is checked on
the same fixtures.
"""
from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from quaestor.bundle import build_bundle, stats_from_receipts  # noqa: E402


@dataclass
class FakeSettings:
    """Minimal stand-in for config.Settings (only the paths bundle.py reads)."""

    repo_root: Path
    runs_dir: Path
    receipts_dir: Path


def _decision_receipt(cycle_id: str) -> dict:
    """A signed byte-attestation cycle receipt (no egress -> 'decision')."""
    return {
        "body": {
            "schema": "bulla-receipt/v0",
            "command": ["/bin/sh", "-c", "sha256sum input.json decision.json"],
            "outcome": {"exit_kind": "code", "exit_code": 0},
            "egress": None,
            "seal_ok": True,
            "chain_head": "aa11bb22cc33dd44",
        },
        "body_digest": "d1" * 16,
        "pubkey": "pk" * 16,
        "sig": "00" * 32,
    }


def _sealed_receipt(cycle_id: str) -> dict:
    """A signed sealed-execution receipt (egress present -> 'sealed')."""
    return {
        "body": {
            "schema": "bulla-receipt/v0",
            "command": ["python3", "/work/in_cell_execute.py"],
            "outcome": {"exit_kind": "code", "exit_code": 0},
            "egress": {"allowlist": ["paper-api.alpaca.markets:443"], "calls": []},
            "seal_ok": True,
            "chain_head": "ee55ff66aa77bb88",
        },
        "body_digest": "d2" * 16,
        "pubkey": "pk" * 16,
        "sig": "11" * 32,
    }


def _make_fixture(tmp_path: Path) -> tuple[FakeSettings, str, str]:
    """Lay out repo_root/receipts/runs with two receipts, a ledger and a summary."""
    repo_root = tmp_path / "repo"
    receipts_dir = repo_root / "receipts"
    runs_dir = repo_root / "runs"
    dashboard = repo_root / "dashboard"
    for d in (receipts_dir, runs_dir, dashboard):
        d.mkdir(parents=True)

    decision_id = "c-20260828-212859-b52eeb"
    sealed_id = "sealed-20260828-214009-038152"
    (receipts_dir / f"{decision_id}.json").write_text(
        json.dumps(_decision_receipt(decision_id), indent=2), encoding="utf-8"
    )
    (receipts_dir / f"{sealed_id}.json").write_text(
        json.dumps(_sealed_receipt(sealed_id), indent=2), encoding="utf-8"
    )
    # A private signing seed that must NEVER be copied into the bundle.
    (receipts_dir / "signing.seed").write_bytes(b"TOP-SECRET-ED25519-SEED")
    # A hash-chained run ledger (its last hash becomes the chain head).
    (receipts_dir / "ledger.jsonl").write_text(
        json.dumps({"seq": 0, "hash": "headhash000111222333", "seal_ok": True}) + "\n",
        encoding="utf-8",
    )
    # The self-contained verifier the bundle links to.
    (dashboard / "verifier.html").write_text("<!doctype html><title>verifier</title>", encoding="utf-8")

    # A latest-session summary the headline stats read from.
    session = runs_dir / "20260828-185103-session"
    session.mkdir()
    (session / "summary.json").write_text(
        json.dumps(
            {
                "cycles": 2,
                "equity_open": 100000.0,
                "equity_last": 100500.0,
                "realized_pnl": 500.0,
                "orders_logged": 3,
            }
        ),
        encoding="utf-8",
    )
    (session / "order_log.csv").write_text(
        "timestamp,action,order_id\n2026-08-28T21:00:00Z,execute,abc123\n", encoding="utf-8"
    )

    settings = FakeSettings(repo_root=repo_root, runs_dir=runs_dir, receipts_dir=receipts_dir)
    return settings, decision_id, sealed_id


def test_build_bundle_assembles_offline_proof(tmp_path: Path) -> None:
    settings, decision_id, sealed_id = _make_fixture(tmp_path)

    bundle_dir = build_bundle(settings)

    # Default location is runs/bundle.
    assert bundle_dir == settings.runs_dir / "bundle"
    assert bundle_dir.is_dir()

    # index.html exists and is the generated landing page.
    index = bundle_dir / "index.html"
    assert index.is_file()
    html = index.read_text(encoding="utf-8")
    assert "quaestor — verifiable trading week" in html

    # Both receipt ids appear in the index table.
    assert decision_id in html
    assert sealed_id in html

    # The index links to the offline verifier and the disclosure is present.
    assert 'href="verifier.html"' in html
    assert "not investment advice" in html.lower()

    # Receipts were copied into the bundle (both, plus the ledger); the private seed was NOT.
    copied = {p.name for p in (bundle_dir / "receipts").glob("*")}
    assert f"{decision_id}.json" in copied
    assert f"{sealed_id}.json" in copied
    assert "ledger.jsonl" in copied
    assert "signing.seed" not in copied

    # The other sources were copied too.
    assert (bundle_dir / "verifier.html").is_file()
    assert (bundle_dir / "summary.json").is_file()
    assert (bundle_dir / "order_log.csv").is_file()
    assert (bundle_dir / "README.txt").is_file()

    # Headline stats surfaced from the summary.
    assert "$100,500.00" in html
    assert "+$500.00" in html


def test_stats_from_receipts(tmp_path: Path) -> None:
    settings, _, _ = _make_fixture(tmp_path)
    stats = stats_from_receipts(settings.receipts_dir)

    assert stats["total"] == 2
    assert stats["sealed"] == 1
    assert stats["decision"] == 1
    assert stats["seal_held"] == 2
    assert stats["chain_head_short"] == "headhash0001"  # first 12 of the ledger head
    assert stats["anchor_head_short"] == ""  # no anchor.jsonl present


def test_build_bundle_fail_open_on_missing_sources(tmp_path: Path) -> None:
    """A bare receipts dir (no verifier, no summary, no ledger) still yields a bundle."""
    repo_root = tmp_path / "repo"
    receipts_dir = repo_root / "receipts"
    runs_dir = repo_root / "runs"
    receipts_dir.mkdir(parents=True)
    runs_dir.mkdir(parents=True)
    settings = FakeSettings(repo_root=repo_root, runs_dir=runs_dir, receipts_dir=receipts_dir)

    out_dir = tmp_path / "custom-out"
    bundle_dir = build_bundle(settings, out_dir)

    assert bundle_dir == out_dir
    assert (bundle_dir / "index.html").is_file()
    assert (bundle_dir / "README.txt").is_file()
    html = (bundle_dir / "index.html").read_text(encoding="utf-8")
    # Missing sources are noted, not fatal.
    assert "Assembly notes" in html
    assert "verifier.html missing from this bundle" in html
