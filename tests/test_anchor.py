"""Offline unit tests for quaestor.anchor — no network, no git, no bulla.

The anchor is a self-chaining, tamper-evident external witness over a bulla/sealed
run ledger. These tests assert that:
- chaining is deterministic (recomputable from the four hashed fields),
- an interior edit is caught by verify_anchor_chain (break_at is set),
- anchor_from_ledger witnesses the LAST ledger line and fails open when the
  ledger is missing/empty,
- push_external is a no-op returning False when unconfigured.
"""
from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from quaestor.anchor import (  # noqa: E402
    GENESIS_ANCHOR_HASH,
    Anchor,
    compute_anchor_hash,
)


def make_anchor(tmp_path: Path) -> Anchor:
    """Anchor over a runs dir under tmp_path (settings stub carries only runs_dir)."""
    runs = tmp_path / "runs"
    return Anchor(SimpleNamespace(runs_dir=runs))


def read_lines(path: Path) -> list[dict]:
    return [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines() if x.strip()]


# -- deterministic chaining ----------------------------------------------------------


def test_anchor_chain_is_deterministic_and_self_linking(tmp_path: Path) -> None:
    anchor = make_anchor(tmp_path)

    e0 = anchor.anchor(0, "head0", "digest0", "/ledger.jsonl")
    e1 = anchor.anchor(1, "head1", "digest1", "/ledger.jsonl")
    e2 = anchor.anchor(2, "head2", "digest2", "/ledger.jsonl")

    # genesis link, then each prev == the previous anchor_hash
    assert e0["prev_anchor_hash"] == GENESIS_ANCHOR_HASH
    assert e1["prev_anchor_hash"] == e0["anchor_hash"]
    assert e2["prev_anchor_hash"] == e1["anchor_hash"]

    # anchor_hash matches the exact byte recipe, independently recomputed
    expected0 = hashlib.sha256(f"{GENESIS_ANCHOR_HASH}0head0digest0".encode()).hexdigest()
    assert e0["anchor_hash"] == expected0 == compute_anchor_hash(GENESIS_ANCHOR_HASH, 0, "head0", "digest0")

    # required fields present on every line
    for e in (e0, e1, e2):
        assert set(e) >= {
            "seq", "ts", "iso_utc", "ledger_head",
            "receipt_digest", "prev_anchor_hash", "anchor_hash",
        }

    # the file holds exactly the three appended lines, in order
    lines = read_lines(anchor.anchor_path)
    assert [x["seq"] for x in lines] == [0, 1, 2]

    result = anchor.verify_anchor_chain()
    assert result == {"ok": True, "entries": 3, "break_at": None}


def test_new_anchor_instance_continues_the_existing_chain(tmp_path: Path) -> None:
    a1 = make_anchor(tmp_path)
    e0 = a1.anchor(0, "h0", "d0", "/l")
    # A fresh instance over the same runs dir must chain onto the persisted tail.
    a2 = Anchor(SimpleNamespace(runs_dir=tmp_path / "runs"))
    e1 = a2.anchor(1, "h1", "d1", "/l")
    assert e1["prev_anchor_hash"] == e0["anchor_hash"]
    assert a2.verify_anchor_chain()["ok"] is True


# -- tamper evidence -----------------------------------------------------------------


def test_interior_edit_is_detected(tmp_path: Path) -> None:
    anchor = make_anchor(tmp_path)
    for i in range(4):
        anchor.anchor(i, f"head{i}", f"digest{i}", "/l")
    assert anchor.verify_anchor_chain()["ok"] is True

    # Flip a hashed field on the middle entry WITHOUT recomputing its anchor_hash.
    lines = read_lines(anchor.anchor_path)
    lines[2]["ledger_head"] = "TAMPERED"
    anchor.anchor_path.write_text(
        "\n".join(json.dumps(x, sort_keys=True) for x in lines) + "\n", encoding="utf-8"
    )

    result = anchor.verify_anchor_chain()
    assert result["ok"] is False
    assert result["break_at"] == 2
    assert result["entries"] == 4


def test_reseated_hash_breaks_the_next_link(tmp_path: Path) -> None:
    """Re-hashing one entry consistently still breaks the following prev-link."""
    anchor = make_anchor(tmp_path)
    for i in range(3):
        anchor.anchor(i, f"head{i}", f"digest{i}", "/l")

    lines = read_lines(anchor.anchor_path)
    # Forge entry 1: change a field AND recompute its own anchor_hash so it is
    # internally consistent — the chain still catches it at entry 2 (stale prev).
    forged_head = "FORGED"
    lines[1]["ledger_head"] = forged_head
    lines[1]["anchor_hash"] = compute_anchor_hash(
        lines[1]["prev_anchor_hash"], int(lines[1]["seq"]), forged_head, lines[1]["receipt_digest"]
    )
    anchor.anchor_path.write_text(
        "\n".join(json.dumps(x, sort_keys=True) for x in lines) + "\n", encoding="utf-8"
    )

    result = anchor.verify_anchor_chain()
    assert result["ok"] is False
    assert result["break_at"] == 2


# -- anchor_from_ledger --------------------------------------------------------------


def test_anchor_from_ledger_witnesses_the_last_entry(tmp_path: Path) -> None:
    anchor = make_anchor(tmp_path)
    ledger = tmp_path / "sealed-ledger.jsonl"
    entries = [
        {"seq": 0, "receipt_digest": "d0", "seal_ok": True, "prev": "0" * 64, "hash": "H0"},
        {"seq": 1, "receipt_digest": "d1", "seal_ok": True, "prev": "H0", "hash": "H1"},
        {"seq": 2, "receipt_digest": "d2", "seal_ok": True, "prev": "H1", "hash": "H2"},
    ]
    ledger.write_text("\n".join(json.dumps(e) for e in entries) + "\n", encoding="utf-8")

    entry = anchor.anchor_from_ledger(str(ledger))
    assert entry["seq"] == 2
    assert entry["ledger_head"] == "H2"          # bulla LedgerEntry.hash == chain head
    assert entry["receipt_digest"] == "d2"
    assert entry["prev_anchor_hash"] == GENESIS_ANCHOR_HASH
    assert entry["ledger_path"] == str(ledger)
    # it is a real appended line and verifies
    assert read_lines(anchor.anchor_path)[-1]["anchor_hash"] == entry["anchor_hash"]
    assert anchor.verify_anchor_chain()["ok"] is True


def test_anchor_from_ledger_missing_is_fail_open(tmp_path: Path) -> None:
    anchor = make_anchor(tmp_path)
    assert anchor.anchor_from_ledger(str(tmp_path / "does-not-exist.jsonl")) == {}
    # nothing was written
    assert not anchor.anchor_path.exists() or read_lines(anchor.anchor_path) == []


def test_anchor_from_ledger_empty_is_fail_open(tmp_path: Path) -> None:
    anchor = make_anchor(tmp_path)
    empty = tmp_path / "empty.jsonl"
    empty.write_text("\n   \n", encoding="utf-8")
    assert anchor.anchor_from_ledger(str(empty)) == {}


# -- verify on empty / missing witness ----------------------------------------------


def test_verify_empty_witness_is_trivially_ok(tmp_path: Path) -> None:
    anchor = make_anchor(tmp_path)
    assert anchor.verify_anchor_chain() == {"ok": True, "entries": 0, "break_at": None}


# -- external witness gating ---------------------------------------------------------


def test_push_external_unconfigured_returns_false(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv("QUAESTOR_WITNESS_DIR", raising=False)
    monkeypatch.delenv("QUAESTOR_WITNESS_REPO", raising=False)
    anchor = make_anchor(tmp_path)
    entry = anchor.anchor(0, "h0", "d0", "/l")
    assert anchor.push_external(entry) is False


def test_push_external_appends_to_configured_dir(tmp_path: Path, monkeypatch) -> None:
    witness = tmp_path / "witness"  # non-git dir: append only, no commit attempted
    monkeypatch.setenv("QUAESTOR_WITNESS_DIR", str(witness))
    monkeypatch.delenv("QUAESTOR_WITNESS_REPO", raising=False)
    anchor = make_anchor(tmp_path)
    entry = anchor.anchor(0, "h0", "d0", "/l")

    assert anchor.push_external(entry) is True
    published = read_lines(witness / "anchors.jsonl")
    assert len(published) == 1 and published[0]["anchor_hash"] == entry["anchor_hash"]


def test_push_external_empty_entry_returns_false(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("QUAESTOR_WITNESS_DIR", str(tmp_path / "witness"))
    anchor = make_anchor(tmp_path)
    assert anchor.push_external({}) is False


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))
