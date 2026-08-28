"""Offline unit tests for quaestor.receipts — no network, no bulla binary, no WSL.

The bulla CLI is simulated by injecting a fake ``bulla_invoker`` into ReceiptPress;
tests assert the exact composed CLI args, the receipt-lite fallback path, and the
Windows -> WSL path translation (pure functions).
"""
from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from quaestor.receipts import (  # noqa: E402
    BULLA,
    CELL_WALL_MS,
    RECEIPT_LITE_SCHEMA,
    BullaResult,
    BullaUnavailableError,
    ReceiptPress,
    to_wsl_path,
    wsl_bridge,
)


class FakeBulla:
    """Records every invocation; optionally writes the receipt file bulla would write."""

    def __init__(
        self,
        returncode: int = 0,
        stdout: str = '{"seal_ok": true}',
        write_receipt: bool = True,
        raise_exc: Exception | None = None,
    ) -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.write_receipt = write_receipt
        self.raise_exc = raise_exc
        self.calls: list[list[str]] = []

    def __call__(self, argv: list[str]) -> BullaResult:
        self.calls.append(list(argv))
        if self.raise_exc is not None:
            raise self.raise_exc
        if len(argv) > 1 and argv[1] == "run" and self.write_receipt and "--out" in argv:
            out = Path(argv[argv.index("--out") + 1])
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text(
                json.dumps({"body": {"seal_ok": True}, "sig": "ed25519:fake"}),
                encoding="utf-8",
            )
        return BullaResult(self.returncode, self.stdout, "")


def make_press(tmp_path: Path, fake: FakeBulla, **kw) -> ReceiptPress:
    kw.setdefault("windows", False)
    return ReceiptPress(
        settings=None,
        receipts_dir=tmp_path / "receipts",
        bulla_invoker=fake,
        **kw,
    )


# -- path translation (Windows -> WSL) ----------------------------------------------


def test_to_wsl_path_translates_drive_paths() -> None:
    assert (
        to_wsl_path(r"C:\Users\Daniil\Desktop\alpaca-hack\quaestor")
        == "/mnt/c/Users/Daniil/Desktop/alpaca-hack/quaestor"
    )
    assert to_wsl_path("D:\\data\\receipts\\c1.json") == "/mnt/d/data/receipts/c1.json"
    assert to_wsl_path("C:/mixed/slashes") == "/mnt/c/mixed/slashes"


def test_to_wsl_path_passes_posix_through() -> None:
    assert to_wsl_path("/tmp/receipts/c1.json") == "/tmp/receipts/c1.json"
    assert to_wsl_path("relative\\dir\\f.json") == "relative/dir/f.json"


def test_wsl_bridge_shape_and_quoting() -> None:
    cmd = wsl_bridge([BULLA, "verify", "/mnt/c/receipts/c1.json"])
    assert cmd[:6] == ["wsl", "-d", "Ubuntu", "--", "bash", "-lc"]
    shell = cmd[6]
    # the binary stays unquoted so bash -lc expands ~ to the WSL home
    assert shell.startswith("~/.cache/hack-target/release/bulla ")
    assert "verify" in shell and "/mnt/c/receipts/c1.json" in shell

    cmd2 = wsl_bridge(
        [BULLA, "run", "--", "/bin/sh", "-c", "sha256sum input.json decision.json"]
    )
    # multi-word arg must survive as ONE shell token
    assert "'sha256sum input.json decision.json'" in cmd2[6]


# -- composed CLI args for attested_cycle -------------------------------------------


def test_attested_cycle_composes_hermetic_run_args(tmp_path: Path) -> None:
    fake = FakeBulla()
    press = make_press(tmp_path, fake)

    decision_in = {
        "inputs": {"equity": 100000.0, "signals": {"SPY": 0.7}},
        "intents": [{"underlying": "SPY", "structure": "vertical_debit"}],
        "verdicts": [{"approved": True}],
    }
    decision, receipt_path = press.attested_cycle("20260829-100000", lambda: dict(decision_in))

    assert decision == decision_in
    assert receipt_path == press.receipts_dir / "20260829-100000.json"
    assert receipt_path.exists()  # the fake wrote it, as real bulla would

    # first use: keygen, then run
    assert [c[1] for c in fake.calls] == ["keygen", "run"]
    keygen = fake.calls[0]
    assert keygen[:2] == [BULLA, "keygen"]
    assert keygen[keygen.index("--key") + 1] == str(press.key_path)

    run = fake.calls[1]
    assert run[0] == BULLA and run[1] == "run"
    cell_dir = press.cells_dir / "20260829-100000"
    assert run[run.index("--work") + 1] == str(cell_dir)
    assert run[run.index("--out") + 1] == str(receipt_path)
    assert run[run.index("--key") + 1] == str(press.key_path)
    assert run[run.index("--ledger") + 1] == str(press.ledger_path)
    assert run[run.index("--wall-ms") + 1] == str(CELL_WALL_MS)
    assert "--json" in run
    # v1 is HERMETIC: seal must hold, so neither escape hatch is passed
    assert "--allow-net" not in run
    assert "--nondeterministic" not in run
    # command after the -- separator
    sep = run.index("--")
    assert run[sep + 1 :] == ["/bin/sh", "-c", "sha256sum input.json decision.json"]

    # trust material + receipt live OUTSIDE the cell work dir (bulla refuses otherwise)
    for p in (press.key_path, press.ledger_path, receipt_path):
        assert cell_dir not in p.parents

    # cell contents: exact bytes bulla attested
    input_data = json.loads((cell_dir / "input.json").read_text(encoding="utf-8"))
    assert input_data["cycle_id"] == "20260829-100000"
    assert input_data["inputs"] == decision_in["inputs"]
    decision_data = json.loads((cell_dir / "decision.json").read_text(encoding="utf-8"))
    assert decision_data == decision_in


def test_keygen_skipped_when_seed_exists(tmp_path: Path) -> None:
    fake = FakeBulla()
    press = make_press(tmp_path, fake)
    press.key_path.write_text("aa" * 32, encoding="utf-8")

    press.attested_cycle("c1", lambda: {"intents": []})
    assert [c[1] for c in fake.calls] == ["run"]


def test_cycle_id_is_sanitized_against_traversal(tmp_path: Path) -> None:
    fake = FakeBulla()
    press = make_press(tmp_path, fake)

    _, receipt_path = press.attested_cycle("../evil id/x", lambda: {"ok": True})
    # everything stays under receipts/; no separators survive
    assert receipt_path.parent == press.receipts_dir
    assert "/" not in receipt_path.name and "\\" not in receipt_path.name
    run = fake.calls[-1]
    work = Path(run[run.index("--work") + 1])
    assert work.parent == press.cells_dir


def test_non_dict_decision_is_wrapped(tmp_path: Path) -> None:
    press = make_press(tmp_path, FakeBulla())
    decision, _ = press.attested_cycle("c1", lambda: ["not", "a", "dict"])
    assert decision == {"decision": ["not", "a", "dict"]}


def test_windows_mode_translates_argv_paths(tmp_path: Path) -> None:
    fake = FakeBulla(write_receipt=False)  # windows-mode paths are for the WSL side
    press = make_press(tmp_path, fake, windows=True)
    press.key_path.write_text("aa" * 32, encoding="utf-8")

    press.attested_cycle("c1", lambda: {"ok": True})
    run = fake.calls[0]
    # every path arg went through to_wsl_path (posix separators, /mnt/<drive> for C:\)
    expected = {
        "--work": to_wsl_path(press.cells_dir / "c1"),
        "--out": to_wsl_path(press.receipts_dir / "c1.json"),
        "--key": to_wsl_path(press.key_path),
        "--ledger": to_wsl_path(press.ledger_path),
    }
    for flag, want in expected.items():
        value = run[run.index(flag) + 1]
        assert value == want
        assert "\\" not in value


# -- receipt-lite fallback -----------------------------------------------------------


@pytest.mark.parametrize(
    "fake",
    [
        FakeBulla(raise_exc=BullaUnavailableError("bulla binary not found")),
        FakeBulla(raise_exc=FileNotFoundError("wsl.exe not found")),
        FakeBulla(returncode=1, write_receipt=False),
        FakeBulla(returncode=0, write_receipt=False),  # "success" but no receipt file
    ],
    ids=["unavailable", "no-wsl", "nonzero-exit", "missing-receipt"],
)
def test_receipt_lite_fallback(tmp_path: Path, fake: FakeBulla) -> None:
    press = make_press(tmp_path, fake)
    decision, receipt_path = press.attested_cycle("c2", lambda: {"intents": [1, 2]})

    assert decision == {"intents": [1, 2]}
    assert receipt_path == press.receipts_dir / "c2.json"
    lite = json.loads(receipt_path.read_text(encoding="utf-8"))
    assert lite["UNSIGNED"] is True
    assert lite["schema"] == RECEIPT_LITE_SCHEMA
    assert lite["reason"]
    # self-reported hashes match the actual cell bytes
    cell = press.cells_dir / "c2"
    assert lite["input_sha256"] == hashlib.sha256((cell / "input.json").read_bytes()).hexdigest()
    assert (
        lite["decision_sha256"]
        == hashlib.sha256((cell / "decision.json").read_bytes()).hexdigest()
    )


def test_decision_fn_errors_propagate(tmp_path: Path) -> None:
    press = make_press(tmp_path, FakeBulla())

    def boom() -> dict:
        raise RuntimeError("strategy blew up")

    with pytest.raises(RuntimeError, match="strategy blew up"):
        press.attested_cycle("c3", boom)


# -- verify --------------------------------------------------------------------------


def test_verify_signed_receipt_uses_bulla_exit_code(tmp_path: Path) -> None:
    fake = FakeBulla(returncode=0)
    press = make_press(tmp_path, fake)
    receipt = press.receipts_dir / "r.json"
    receipt.write_text(json.dumps({"body": {"seal_ok": True}, "sig": "x"}), encoding="utf-8")

    assert press.verify(receipt) is True
    assert fake.calls[-1] == [BULLA, "verify", str(receipt)]

    fake.returncode = 2  # bulla verify exits 2 when not intact
    assert press.verify(receipt) is False


def test_verify_rejects_receipt_lite_without_invoking_bulla(tmp_path: Path) -> None:
    fake = FakeBulla(raise_exc=BullaUnavailableError("down"))
    press = make_press(tmp_path, fake)
    _, lite_path = press.attested_cycle("c4", lambda: {"a": 1})
    calls_before = len(fake.calls)

    assert press.verify(lite_path) is False
    assert len(fake.calls) == calls_before  # UNSIGNED short-circuits: no bulla call


def test_verify_missing_or_bad_file_is_false(tmp_path: Path) -> None:
    press = make_press(tmp_path, FakeBulla())
    assert press.verify(press.receipts_dir / "nope.json") is False
    bad = press.receipts_dir / "bad.json"
    bad.write_text("{not json", encoding="utf-8")
    assert press.verify(bad) is False


# -- ledger_summary ------------------------------------------------------------------


def test_ledger_summary_counts(tmp_path: Path) -> None:
    press = make_press(tmp_path, FakeBulla())
    entries = [
        {"seq": 0, "hash": "h0", "receipt_digest": "d0", "seal_ok": True, "solve_exit": 0},
        {"seq": 1, "hash": "h1", "receipt_digest": "d1", "seal_ok": True, "solve_exit": 0},
        {"seq": 2, "hash": "h2", "receipt_digest": "d2", "seal_ok": False, "solve_exit": 1},
    ]
    press.ledger_path.write_text(
        "\n".join(json.dumps(e) for e in entries) + "\n", encoding="utf-8"
    )
    # one unsigned receipt-lite alongside a signed one
    (press.receipts_dir / "s1.json").write_text(
        json.dumps({"body": {"seal_ok": True}}), encoding="utf-8"
    )
    (press.receipts_dir / "u1.json").write_text(
        json.dumps({"schema": RECEIPT_LITE_SCHEMA, "UNSIGNED": True}), encoding="utf-8"
    )

    s = press.ledger_summary()
    assert s["attempts"] == 3
    assert s["seal_held"] == 2
    assert s["seal_broken"] == 1
    assert s["last_seq"] == 2
    assert s["last_hash"] == "h2"
    assert s["unsigned_receipts"] == 1
    assert s["unsigned_files"] == ["u1.json"]


def test_ledger_summary_empty(tmp_path: Path) -> None:
    press = make_press(tmp_path, FakeBulla())
    s = press.ledger_summary()
    assert s["attempts"] == 0
    assert s["seal_held"] == 0 and s["seal_broken"] == 0
    assert s["last_seq"] is None and s["last_hash"] is None
    assert s["unsigned_receipts"] == 0


# -- constructor guards --------------------------------------------------------------


def test_key_inside_cells_dir_is_rejected(tmp_path: Path) -> None:
    receipts_dir = tmp_path / "receipts"
    with pytest.raises(ValueError, match="cells dir"):
        ReceiptPress(
            settings=None,
            receipts_dir=receipts_dir,
            key_path=receipts_dir / "cells" / "x" / "seed",
            bulla_invoker=FakeBulla(),
            windows=False,
        )
