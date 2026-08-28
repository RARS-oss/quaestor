"""External anchor — the public witness that closes the ledger's last hole.

Role
----
The sealed run ledger (``receipts/sealed-ledger.jsonl``, and the honest-mode
``receipts/ledger.jsonl``) is a bulla hash-chain: every entry commits to the one
before it, so an *interior* edit is detected the moment the chain is recomputed.
The one attack a self-contained hash-chain cannot detect on its own is
**truncation of the newest entries** — lopping off the tail leaves a shorter but
still perfectly self-consistent chain. Nothing inside the file remembers that a
later entry ever existed.

``Anchor`` closes that hole by emitting an *external, timestamped, append-only
witness*. Each anchor line records the ledger head (the newest entry's chain
``hash``) together with its ``receipt_digest`` and ``seq``, and self-chains into
the previous anchor. Publishing those lines to a third-party append-only store
(a public git repository — see ``push_external``) makes the tail durable: once
GitHub has recorded ``anchor seq N`` at commit time T, a later attempt to pretend
seq N never happened contradicts an immutable, independently-timestamped history.

Local witness file
------------------
``runs/anchor.jsonl`` — one JSON object per line, append-only::

    {seq, ts, iso_utc, ledger_head, receipt_digest, prev_anchor_hash, anchor_hash}

``anchor_hash = sha256(prev_anchor_hash + str(seq) + ledger_head + receipt_digest)``
and ``prev_anchor_hash`` is the previous line's ``anchor_hash`` (genesis is 64
zeros). Recomputing the chain (``verify_anchor_chain``) therefore detects any
interior edit; the external copy detects truncation.

Privacy / why this is a *real* witness
--------------------------------------
An anchor line carries only opaque digests — the ledger head hash, the receipt
digest, a sequence number and timestamps. It contains **no strategy, no order
payloads, no account values, no code**: a witness repository of these lines
leaks nothing about how the agent trades. Yet because a public git host records
an immutable commit history with its own server-side timestamps, the mere
existence of ``anchor seq N`` there — committed before the contest deadline — is
independent, tamper-evident proof that the agent's ledger reached at least seq N
with that exact head, which no later local rewrite can retract.

Fail-open
---------
Every public method swallows its own errors (logging to stderr) and returns an
empty/false result rather than raising: the anchor layer, like the receipt layer,
must never crash the trading loop.
"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover — avoid a runtime import cycle with config.py
    from quaestor.config import Settings

__all__ = ["Anchor", "GENESIS_ANCHOR_HASH", "compute_anchor_hash"]

# Genesis link: matches bulla's all-zero ``prev`` for the first ledger entry.
GENESIS_ANCHOR_HASH: str = "0" * 64
ANCHOR_FILENAME: str = "anchor.jsonl"
WITNESS_FILENAME: str = "anchors.jsonl"
# Env opt-in for the external (public) witness.
ENV_WITNESS_REPO: str = "QUAESTOR_WITNESS_REPO"
ENV_WITNESS_DIR: str = "QUAESTOR_WITNESS_DIR"
_GIT_TIMEOUT_S: float = 20.0


def compute_anchor_hash(
    prev_anchor_hash: str, seq: int, ledger_head: str, receipt_digest: str
) -> str:
    """The self-chaining anchor digest.

    ``sha256(prev_anchor_hash + str(seq) + ledger_head + receipt_digest)`` — the
    exact byte recipe used both when writing a line and when re-verifying it, so
    the two can never disagree for an untampered entry.
    """
    material = f"{prev_anchor_hash}{seq}{ledger_head}{receipt_digest}".encode("utf-8")
    return hashlib.sha256(material).hexdigest()


class Anchor:
    """Append-only external witness over a bulla/sealed run ledger."""

    def __init__(self, settings: "Settings | Any") -> None:
        self.settings = settings
        self.runs_dir: Path = Path(settings.runs_dir)
        self.anchor_path: Path = self.runs_dir / ANCHOR_FILENAME
        try:
            self.runs_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:  # never fatal — anchor() re-checks and fails open
            print(f"quaestor.anchor: could not create {self.runs_dir}: {exc!r}", file=sys.stderr)

    # -- public API ------------------------------------------------------------------

    def anchor(
        self, seq: int, ledger_head: str, receipt_digest: str, ledger_path: str
    ) -> dict[str, Any]:
        """Append ONE self-chaining witness line and return it.

        Reads the current tail of ``runs/anchor.jsonl`` for ``prev_anchor_hash``
        (genesis when empty), computes ``anchor_hash`` and appends the entry.
        Fail-open: on ANY error, logs to stderr and returns ``{}`` — never raises
        into the caller.
        """
        try:
            seq_int = int(seq)
            head = str(ledger_head)
            digest = str(receipt_digest)
            prev = self._last_anchor_hash()
            now = datetime.now(timezone.utc)
            entry: dict[str, Any] = {
                "seq": seq_int,
                "ts": now.timestamp(),
                "iso_utc": now.isoformat(timespec="seconds"),
                "ledger_head": head,
                "receipt_digest": digest,
                "ledger_path": str(ledger_path),
                "prev_anchor_hash": prev,
                "anchor_hash": compute_anchor_hash(prev, seq_int, head, digest),
            }
            line = json.dumps(entry, sort_keys=True) + "\n"
            with self.anchor_path.open("a", encoding="utf-8") as fh:
                fh.write(line)
            return entry
        except Exception as exc:  # the anchor layer must never kill the trading loop
            print(f"quaestor.anchor: anchor() failed: {exc!r}", file=sys.stderr)
            return {}

    def anchor_from_ledger(self, ledger_path: str) -> dict[str, Any]:
        """Witness the head of a bulla/sealed ledger jsonl.

        Reads the LAST entry of ``ledger_path`` (a bulla ``LedgerEntry`` with
        fields ``seq``, ``hash``, ``receipt_digest``, ``seal_ok``, ``prev``) and
        anchors ``hash`` as the ledger head. Returns ``{}`` when the ledger is
        missing or empty (fail-open).
        """
        try:
            path = Path(ledger_path)
            if not path.exists():
                return {}
            last: dict[str, Any] | None = None
            for raw in path.read_text(encoding="utf-8").splitlines():
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    obj = json.loads(raw)
                except ValueError:
                    continue
                if isinstance(obj, dict):
                    last = obj
            if last is None:
                return {}
            seq = last.get("seq")
            ledger_head = last.get("hash") or ""
            receipt_digest = last.get("receipt_digest") or ""
            return self.anchor(
                int(seq) if seq is not None else -1,
                str(ledger_head),
                str(receipt_digest),
                str(path),
            )
        except Exception as exc:
            print(f"quaestor.anchor: anchor_from_ledger failed: {exc!r}", file=sys.stderr)
            return {}

    def push_external(self, entry: dict[str, Any]) -> bool:
        """Best-effort append of ``entry`` to the external public witness.

        Opt-in via the environment: ``QUAESTOR_WITNESS_DIR`` names a local working
        tree (created if absent) and ``QUAESTOR_WITNESS_REPO`` names a git url or
        local path (its presence signals intent to publish). The entry is appended
        to ``<witness_dir>/anchors.jsonl``; if that directory is a git repository
        the change is staged, committed (``"anchor seq N"``) and pushed. All git
        calls run through subprocess with a 20s timeout and never raise.

        Returns ``True`` once the entry is durably appended to the witness file
        (the minimum witness), ``False`` when unconfigured or on failure. Because
        the file holds only opaque hashes it leaks no strategy or code; the git
        host's immutable, server-timestamped history is what makes it a genuine
        external witness against tail-truncation.
        """
        try:
            if not entry:
                return False
            witness_dir = os.environ.get(ENV_WITNESS_DIR, "").strip()
            witness_repo = os.environ.get(ENV_WITNESS_REPO, "").strip()
            if not witness_dir and not witness_repo:
                return False  # not opted in
            if not witness_dir:
                # Only the repo was named: keep a working tree under runs/.
                witness_dir = str(self.runs_dir / "witness")
            wdir = Path(witness_dir)
            wdir.mkdir(parents=True, exist_ok=True)
            line = json.dumps(entry, sort_keys=True) + "\n"
            with (wdir / WITNESS_FILENAME).open("a", encoding="utf-8") as fh:
                fh.write(line)
            if (wdir / ".git").exists():
                self._git(wdir, ["add", WITNESS_FILENAME])
                self._git(wdir, ["commit", "-m", f"anchor seq {entry.get('seq')}"])
                self._git(wdir, ["push"])
            return True
        except Exception as exc:
            print(f"quaestor.anchor: push_external failed: {exc!r}", file=sys.stderr)
            return False

    def verify_anchor_chain(self, path: str | Path | None = None) -> dict[str, Any]:
        """Recompute the anchor chain and report the first break.

        Returns ``{"ok": bool, "entries": int, "break_at": int | None}`` where
        ``break_at`` is the 0-based index of the first entry whose stored
        ``prev_anchor_hash`` does not equal the running chain value, or whose
        ``anchor_hash`` does not equal ``compute_anchor_hash`` of its fields — i.e.
        any interior edit. An empty/missing witness is trivially consistent
        (``ok=True, entries=0``).
        """
        target = Path(path) if path is not None else self.anchor_path
        entries = self._read_entries(target)
        prev = GENESIS_ANCHOR_HASH
        break_at: int | None = None
        for i, entry in enumerate(entries):
            try:
                seq = int(entry["seq"])
                head = str(entry.get("ledger_head", ""))
                digest = str(entry.get("receipt_digest", ""))
                recomputed = compute_anchor_hash(prev, seq, head, digest)
            except (KeyError, TypeError, ValueError):
                break_at = i
                break
            if entry.get("prev_anchor_hash") != prev or entry.get("anchor_hash") != recomputed:
                break_at = i
                break
            prev = str(entry.get("anchor_hash"))
        return {"ok": break_at is None, "entries": len(entries), "break_at": break_at}

    # -- internals -------------------------------------------------------------------

    def _read_entries(self, path: Path) -> list[dict[str, Any]]:
        """Parse every valid JSON-object line of ``path`` (missing file -> [])."""
        entries: list[dict[str, Any]] = []
        try:
            if not path.exists():
                return entries
            for raw in path.read_text(encoding="utf-8").splitlines():
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    obj = json.loads(raw)
                except ValueError:
                    continue
                if isinstance(obj, dict):
                    entries.append(obj)
        except OSError as exc:
            print(f"quaestor.anchor: could not read {path}: {exc!r}", file=sys.stderr)
        return entries

    def _last_anchor_hash(self) -> str:
        """The tail entry's ``anchor_hash``, or the genesis link when empty."""
        entries = self._read_entries(self.anchor_path)
        for entry in reversed(entries):
            value = entry.get("anchor_hash")
            if isinstance(value, str) and value:
                return value
        return GENESIS_ANCHOR_HASH

    def _git(
        self, cwd: Path, args: list[str], timeout: float = _GIT_TIMEOUT_S
    ) -> subprocess.CompletedProcess[str] | None:
        """Run ``git -C <cwd> <args>`` with a hard timeout; never raise."""
        try:
            return subprocess.run(
                ["git", "-C", str(cwd), *args],
                capture_output=True,
                text=True,
                timeout=timeout,
            )
        except Exception as exc:
            print(
                f"quaestor.anchor: git {' '.join(args)} failed: {exc!r}", file=sys.stderr
            )
            return None
