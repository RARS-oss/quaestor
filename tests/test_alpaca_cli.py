"""Tests for the per-cycle Alpaca CLI probe sealed into decision receipts.

The probe exists to make the contest's "must use Alpaca's MCP server or CLI"
requirement provable from a signed receipt. Its one hard rule is that it must
never be able to stop a trading cycle, so most of these tests are about failing
open with a recorded reason.
"""
from __future__ import annotations

import json
import subprocess

import pytest

from quaestor import alpaca_cli


class _S:
    api_key = "PKTEST"
    api_secret = "secret"


CLOCK_JSON = json.dumps({
    "is_open": True,
    "next_close": "2026-09-01T16:00:00-04:00",
    "next_open": "2026-09-02T09:30:00-04:00",
    "timestamp": "2026-09-01T11:00:00-04:00",
})


def _fake_run(stdout="", stderr="", code=0):
    def run(argv, **kw):
        return subprocess.CompletedProcess(argv, code, stdout=stdout, stderr=stderr)
    return run


def test_probe_reads_alpacas_clock(monkeypatch):
    monkeypatch.setattr(alpaca_cli, "resolve_cli", lambda: "/usr/bin/alpaca")
    monkeypatch.setattr(subprocess, "run", _fake_run(stdout=CLOCK_JSON))
    rec = alpaca_cli.probe_clock(_S())
    assert rec["ok"] is True
    assert rec["is_open"] is True
    assert rec["next_close"].startswith("2026-09-01")
    # the raw bytes are fingerprinted so the sealed copy is checkable
    assert len(rec["stdout_sha256"]) == 64


def test_missing_cli_is_recorded_not_raised(monkeypatch):
    monkeypatch.setattr(alpaca_cli, "resolve_cli", lambda: None)
    rec = alpaca_cli.probe_clock(_S())
    assert rec["ok"] is False and rec["is_open"] is None
    assert "not found" in rec["error"]


def test_timeout_is_recorded_not_raised(monkeypatch):
    monkeypatch.setattr(alpaca_cli, "resolve_cli", lambda: "/usr/bin/alpaca")

    def boom(argv, **kw):
        raise subprocess.TimeoutExpired(argv, 15)
    monkeypatch.setattr(subprocess, "run", boom)
    rec = alpaca_cli.probe_clock(_S(), timeout_s=15)
    assert rec["ok"] is False and "timed out" in rec["error"]


def test_nonzero_exit_keeps_the_reason_but_not_the_secret(monkeypatch):
    monkeypatch.setattr(alpaca_cli, "resolve_cli", lambda: "/usr/bin/alpaca")
    monkeypatch.setattr(subprocess, "run",
                        _fake_run(stdout="", stderr="authentication required", code=1))
    rec = alpaca_cli.probe_clock(_S())
    assert rec["ok"] is False
    assert "authentication required" in rec["error"]
    assert _S.api_secret not in json.dumps(rec)


def test_non_json_output_does_not_crash(monkeypatch):
    monkeypatch.setattr(alpaca_cli, "resolve_cli", lambda: "/usr/bin/alpaca")
    monkeypatch.setattr(subprocess, "run", _fake_run(stdout="not json at all"))
    rec = alpaca_cli.probe_clock(_S())
    assert rec["ok"] is False and rec["error"] == "CLI output was not JSON"


def test_reconcile_flags_a_clock_disagreement():
    agree = {"ok": True, "is_open": True}
    assert alpaca_cli.reconcile(agree, market_open=True) is None

    disagree = {"ok": True, "is_open": False}
    note = alpaca_cli.reconcile(disagree, market_open=True)
    assert note is not None and "disagreement" in note

    failed = {"ok": False, "error": "boom"}
    assert "probe failed" in alpaca_cli.reconcile(failed, market_open=True)
