"""quaestor dashboard — the judge-facing Streamlit app (read-only audit view).

What it does
------------
Renders four tabs over the on-disk audit contract written by the agent:
  1. "Agent"    — equity curve, realized P&L, open positions and the order log,
                  read from runs/<latest-session>/{summary.json, positions_snapshot.json,
                  order_log.csv}.
  2. "Receipts" — every bulla receipt under receipts/*.json (cycle id, seal status,
                  exit code, stdout sha256 prefix, signing-key prefix) plus the
                  hash-chained ledger head from receipts/ledger.jsonl, and an
                  on-demand `bulla verify` runner (WSL bridge, failure-tolerant).
  3. "🔨 Tamper Playground" — a judge can edit a signed receipt (or a ZK risk
                  proof) in a textarea and re-verify: the original passes, any
                  forged byte is caught by `bulla verify` / `zkrisk verify`.
  4. "About"    — the pitch, the verifiable-agent story, the hackathon requirement
                  checklist, and the mandatory Alpaca risk disclosure.

Alpaca facts this module encodes
--------------------------------
- This app NEVER calls Alpaca: it is a pure read-only view over the audit trail,
  so no API keys are needed (and none are read) to run it.
- Paper trading only: everything shown here came from https://paper-api.alpaca.markets.
- The order log columns mirror the audit contract for Alpaca options orders:
  TIF is always "day" for options; `limit_price` for mleg orders is the SIGNED net
  per strategy unit (+debit / -credit); `client_order_id` is the idempotency key
  (unique per attempt — the broker looks an order up by it before ever resubmitting).
- Fills shown were produced by the paper engine's marketable-at-NBBO-touch simulator
  (with random ~10% partials), which is why the executor uses marketable limits
  plus a cancel-and-repost loop rather than resting mid-spread limits.

No pandas / numpy: tables are lists of dicts fed to st.dataframe, charts are
st.line_chart on plain lists (or altair on raw dicts when altair is importable).
The app renders sensibly with completely empty runs/ and receipts/ directories.
"""
from __future__ import annotations

import csv
import hashlib
import json
import os
import shlex
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import streamlit as st

try:  # altair ships with streamlit in most installs, but keep it strictly optional
    import altair as alt  # type: ignore[import-not-found]
except Exception:  # pragma: no cover - import guard
    alt = None  # type: ignore[assignment]

# --------------------------------------------------------------------------- paths
REPO_ROOT: Path = Path(__file__).resolve().parents[1]
RUNS_DIR: Path = REPO_ROOT / "runs"
RECEIPTS_DIR: Path = REPO_ROOT / "receipts"
LEDGER_PATH: Path = RECEIPTS_DIR / "ledger.jsonl"

# bulla lives inside WSL (see docs/ARCHITECTURE.md receipts contract)
BULLA_WSL: str = "$HOME/.cache/hack-target/release/bulla"
BULLA_POSIX: str = "~/.cache/hack-target/release/bulla"
VERIFY_TIMEOUT_S: float = 60.0

ACCENT: str = "#22d3ee"          # readable on both light and dark themes
MAX_RECEIPT_EXPANDERS: int = 25  # keep first render snappy with many receipts

DISCLOSURE: str = (
    "Important disclosure: not investment advice. Options trading involves "
    "significant risk. Review Alpaca's disclosures at "
    "https://alpaca.markets/disclosures"
)


# ----------------------------------------------------------------- small file utils
def _read_json(path: Path) -> Any:
    """Load a JSON file; return None on any failure (missing, malformed, perms)."""
    try:
        with path.open("r", encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        return None


def _read_jsonl(path: Path) -> list[tuple[str, Any]]:
    """Read a JSONL file as [(raw_line, parsed_or_None), ...]; [] on failure."""
    out: list[tuple[str, Any]] = []
    try:
        with path.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append((line, json.loads(line)))
                except Exception:
                    out.append((line, None))
    except Exception:
        return []
    return out


def _read_order_log(path: Path) -> list[dict[str, str]]:
    """order_log.csv -> list of dicts via the csv module (no pandas)."""
    rows: list[dict[str, str]] = []
    try:
        with path.open("r", encoding="utf-8", newline="") as fh:
            for row in csv.DictReader(fh):
                rows.append({str(k): ("" if v is None else str(v)) for k, v in row.items()})
    except Exception:
        return []
    return rows


def _dig(obj: Any, *paths: str | tuple[str, ...]) -> Any:
    """Return the first non-None value found at any of the candidate key paths."""
    if not isinstance(obj, dict):
        return None
    for p in paths:
        keys: tuple[str, ...] = (p,) if isinstance(p, str) else p
        cur: Any = obj
        found = True
        for k in keys:
            if isinstance(cur, dict) and k in cur:
                cur = cur[k]
            else:
                found = False
                break
        if found and cur is not None:
            return cur
    return None


def _as_float(v: Any) -> float | None:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _fmt_usd(v: Any) -> str:
    f = _as_float(v)
    if f is None:
        return "—"
    return f"-${abs(f):,.2f}" if f < 0 else f"${f:,.2f}"


def _fmt_delta(v: Any) -> str | None:
    f = _as_float(v)
    return None if f is None else f"{f:+,.2f}"


def _fmt_ts(t: Any) -> str:
    """Best-effort timestamp label: epoch seconds/ms -> UTC hh:mm, else str()."""
    f = _as_float(t)
    if f is not None and f > 1e9:  # looks like an epoch
        if f > 1e12:  # milliseconds
            f /= 1000.0
        try:
            return datetime.fromtimestamp(f, tz=timezone.utc).strftime("%m-%d %H:%M:%S")
        except (OverflowError, OSError, ValueError):
            return str(t)
    return str(t)


# --------------------------------------------------------------- runs/ (Agent tab)
def _session_dirs(runs_dir: Path) -> list[Path]:
    """Session directories under runs/, newest first (names sort lexicographically:
    <YYYYMMDD-HHMMSS>-session per the audit contract)."""
    try:
        dirs = [p for p in runs_dir.iterdir() if p.is_dir()]
    except Exception:
        return []
    return sorted(dirs, key=lambda p: p.name, reverse=True)


def _normalize_equity_curve(raw: Any) -> list[dict[str, Any]]:
    """Accept several point shapes: dicts, [ts, equity] pairs, or bare numbers."""
    if not isinstance(raw, list):
        return []
    points: list[dict[str, Any]] = []
    for i, p in enumerate(raw):
        val: float | None = None
        label: str = str(i)
        if isinstance(p, dict):
            val = _as_float(_dig(p, "equity", "value", "y", "close"))
            t = _dig(p, "ts", "t", "timestamp", "time", "x")
            if t is not None:
                label = _fmt_ts(t)
        elif isinstance(p, (list, tuple)) and len(p) >= 2:
            label = _fmt_ts(p[0])
            val = _as_float(p[1])
        else:
            val = _as_float(p)
        if val is None:
            continue
        points.append({"i": i, "t": label, "equity": val})
    return points


def _render_equity_chart(points: list[dict[str, Any]]) -> None:
    if alt is not None:
        try:
            chart = (
                alt.Chart(alt.Data(values=points))
                .mark_line(color=ACCENT, strokeWidth=2, interpolate="monotone")
                .encode(
                    x=alt.X("i:Q", title="cycle", axis=alt.Axis(tickMinStep=1)),
                    y=alt.Y("equity:Q", title="equity ($)", scale=alt.Scale(zero=False)),
                    tooltip=[alt.Tooltip("t:N", title="time"),
                             alt.Tooltip("equity:Q", title="equity", format=",.2f")],
                )
                .properties(height=280)
            )
            st.altair_chart(chart, use_container_width=True)
            return
        except Exception:
            pass  # fall through to the simple chart
    st.line_chart({"equity": [p["equity"] for p in points]})


def _position_rows(snapshot: Any) -> list[dict[str, Any]]:
    """positions_snapshot.json may be an AccountSnapshot dict or a bare list."""
    if isinstance(snapshot, list):
        positions: Any = snapshot
    else:
        positions = _dig(snapshot, "positions") or []
    if not isinstance(positions, list):
        return []
    preferred = (
        "symbol", "asset_class", "side", "qty", "avg_entry_price",
        "current_price", "market_value", "cost_basis",
        "unrealized_pl", "unrealized_plpc",
    )
    rows: list[dict[str, Any]] = []
    for p in positions:
        if not isinstance(p, dict):
            continue
        row = {k: p[k] for k in preferred if k in p}
        rows.append(row if row else dict(p))
    return rows


def _count_of(value: Any) -> int | None:
    if isinstance(value, list):
        return len(value)
    f = _as_float(value)
    return None if f is None else int(f)


def _render_agent_tab() -> None:
    sessions = _session_dirs(RUNS_DIR)
    if not sessions:
        st.info(
            "No runs yet. Start the agent (`python -m quaestor once` or `loop`) and "
            "this tab will show the live equity curve, realized P&L, open positions "
            "and the full order log."
        )
        return

    names = [d.name for d in sessions]
    selected = st.selectbox("Session", names, index=0,
                            help="Newest session first; earlier sessions stay browsable.")
    session = RUNS_DIR / str(selected)

    summary = _read_json(session / "summary.json")
    snapshot = _read_json(session / "positions_snapshot.json")
    order_rows = _read_order_log(session / "order_log.csv")

    points = _normalize_equity_curve(
        _dig(summary, "equity_curve", "equity_points", "curve", "equity")
    )
    realized = _dig(summary, "realized_pnl", "realized_pnl_today", "realized",
                    ("pnl", "realized"), "realized_pnl_usd")
    cycles = _count_of(_dig(summary, "cycles", "n_cycles", ("counts", "cycles"),
                            "cycle_count"))
    orders = _count_of(_dig(summary, "orders", "n_orders", ("counts", "orders"),
                            "order_count"))
    if orders is None and order_rows:
        orders = len(order_rows)
    pos_rows = _position_rows(snapshot)

    equity_now: float | None = points[-1]["equity"] if points else _as_float(
        _dig(summary, "equity", "last_equity"))
    equity_delta: float | None = None
    if points and len(points) > 1:
        equity_delta = points[-1]["equity"] - points[0]["equity"]

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Equity", _fmt_usd(equity_now), delta=_fmt_delta(equity_delta))
    c2.metric("Realized P&L", _fmt_usd(realized), delta=_fmt_delta(realized))
    c3.metric("Cycles", "—" if cycles is None else str(cycles))
    c4.metric("Orders", "—" if orders is None else str(orders))

    st.subheader("Equity curve")
    if points:
        _render_equity_chart(points)
    else:
        st.caption("No equity points recorded yet — the curve appears after the "
                   "first completed decision cycle.")

    st.subheader("Open positions")
    if pos_rows:
        st.dataframe(pos_rows, use_container_width=True, hide_index=True)
        ts = _dig(snapshot, "ts")
        if ts is not None:
            st.caption(f"Snapshot taken {_fmt_ts(ts)} UTC")
    else:
        st.caption("No open positions in the latest snapshot (or none recorded yet).")

    st.subheader("Order log")
    if order_rows:
        st.dataframe(list(reversed(order_rows)), use_container_width=True,
                     hide_index=True)
        st.caption(
            "Options orders are TIF=day; mleg limit_price is the SIGNED net per "
            "strategy unit (+debit / −credit); client_order_id is the idempotency "
            "key — one per attempt, looked up before any resubmit."
        )
    else:
        st.caption("No orders logged yet in this session.")

    if isinstance(summary, dict):
        with st.expander("Raw summary.json"):
            st.json(summary, expanded=False)


# ---------------------------------------------------------- receipts/ (Receipts tab)
def _receipt_files() -> list[Path]:
    try:
        files = [p for p in RECEIPTS_DIR.glob("*.json") if p.is_file()]
    except Exception:
        return []
    return sorted(files, key=lambda p: p.stat().st_mtime, reverse=True)


def _seal_label(receipt: Any) -> str:
    """HELD / BROKEN / UNSIGNED / unknown, from whatever field bulla (or the
    degraded receipt-lite writer) used."""
    if not isinstance(receipt, dict):
        return "unknown"
    v = _dig(receipt, "seal_ok", "sealed")
    if isinstance(v, bool):
        return "HELD" if v else "BROKEN"
    s = _dig(receipt, "seal", "seal_status", "status", "mode", "marker")
    if isinstance(s, str):
        u = s.upper()
        if "UNSIGNED" in u:
            return "UNSIGNED"
        if "HELD" in u:
            return "HELD"
        if "BROKEN" in u:
            return "BROKEN"
    if _dig(receipt, "unsigned") is True:
        return "UNSIGNED"
    try:  # last resort: the receipt-lite writer stamps a loud UNSIGNED marker
        if "UNSIGNED" in json.dumps(receipt):
            return "UNSIGNED"
    except Exception:
        pass
    return "unknown"


def _receipt_row(path: Path, receipt: Any) -> dict[str, str]:
    cycle = _dig(receipt, "cycle_id", "cycle", "id", "name")
    exit_code = _dig(receipt, "exit", "exit_code", "exit_status", "returncode",
                     ("result", "exit"))
    stdout_sha = _dig(receipt, "stdout_sha256", ("hashes", "stdout"), "stdout_hash",
                      ("stdout", "sha256"), ("outputs", "stdout_sha256"))
    key = _dig(receipt, "public_key", "pubkey", "key", "key_id", "signer",
               "verify_key", ("signature", "public_key"), ("signature", "key"))
    return {
        "receipt": path.name,
        "cycle_id": str(cycle) if cycle is not None else path.stem,
        "seal": _seal_label(receipt),
        "exit": "—" if exit_code is None else str(exit_code),
        "stdout_sha256": (str(stdout_sha)[:12] + "…") if stdout_sha else "—",
        "key": (str(key)[:12] + "…") if key else "—",
    }


def _to_wsl_path(p: Path) -> str:
    """C:\\Users\\... -> /mnt/c/Users/... for the WSL bridge."""
    s = str(p.resolve())
    if len(s) >= 2 and s[1] == ":":
        return f"/mnt/{s[0].lower()}{s[2:].replace(os.sep, '/')}"
    return s.replace(os.sep, "/")


def _bulla_verify(receipt_path: Path) -> tuple[bool, str]:
    """Run `bulla verify <receipt>` and return (ok, text output).

    Same bridge pattern as receipts.py: on Windows we shell through WSL where the
    pinned bulla binary lives; on Linux/WSL we call it directly. Every failure
    mode (no wsl, no binary, timeout) degrades to a readable message — the
    dashboard must never crash because the verifier is unavailable.
    """
    if os.name == "nt":
        inner = f'"{BULLA_WSL}" verify {shlex.quote(_to_wsl_path(receipt_path))}'
        cmd: list[str] = ["wsl", "bash", "-lc", inner]
    else:
        bulla = Path(BULLA_POSIX).expanduser()
        if not bulla.exists():
            return False, f"bulla binary not found at {bulla} — cannot verify here."
        cmd = [str(bulla), "verify", str(receipt_path)]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True,
                              timeout=VERIFY_TIMEOUT_S)
    except FileNotFoundError:
        return False, ("WSL bridge unavailable (`wsl` not on PATH). Run "
                       f"`bulla verify {receipt_path.name}` inside WSL instead.")
    except subprocess.TimeoutExpired:
        return False, f"bulla verify timed out after {VERIFY_TIMEOUT_S:.0f}s."
    except OSError as exc:
        return False, f"could not launch verifier: {exc}"
    text = (proc.stdout or "") + (("\n" + proc.stderr) if proc.stderr else "")
    text = text.strip() or "(no output)"
    return proc.returncode == 0, f"exit code {proc.returncode}\n{text}"


def _ledger_head_and_count() -> tuple[str | None, int]:
    """Head of the hash chain + entry count from receipts/ledger.jsonl."""
    entries = _read_jsonl(LEDGER_PATH)
    if not entries:
        return None, 0
    raw_last, parsed_last = entries[-1]
    head = _dig(parsed_last, "head", "hash", "entry_hash", "receipt_sha256",
                "sha256", "digest", "id")
    if head is None:
        # deterministic fallback: hash of the last raw ledger line
        head = "sha256:" + hashlib.sha256(raw_last.encode("utf-8")).hexdigest()
    return str(head), len(entries)


def _render_receipts_tab() -> None:
    st.subheader("Ledger")
    head, count = _ledger_head_and_count()
    lc1, lc2 = st.columns([1, 3])
    lc1.metric("Ledger entries", str(count))
    if head:
        lc2.markdown("**Head**")
        lc2.code(head, language=None)
    else:
        lc2.caption("No ledger yet — receipts/ledger.jsonl appears with the first "
                    "attested cycle.")

    st.subheader("Receipts")
    files = _receipt_files()
    if not files:
        st.info(
            "No receipts yet. Every decision cycle runs inside a bulla cell and "
            "drops a signed receipt here — cycle inputs, decision output, stdout "
            "hash and signature, chained into ledger.jsonl."
        )
        return

    parsed: list[tuple[Path, Any]] = [(p, _read_json(p)) for p in files]
    st.dataframe([_receipt_row(p, r) for p, r in parsed],
                 use_container_width=True, hide_index=True)
    st.caption(
        "seal HELD = fully sandboxed run; seal BROKEN (honest mode) = network was "
        "allowed for broker calls but the receipt is still signed and verifiable; "
        "UNSIGNED = degraded receipt-lite (bulla unavailable)."
    )

    st.subheader("Verify")
    shown = parsed[:MAX_RECEIPT_EXPANDERS]
    if len(parsed) > len(shown):
        st.caption(f"Showing the {len(shown)} most recent of {len(parsed)} receipts.")
    for path, receipt in shown:
        row = _receipt_row(path, receipt)
        with st.expander(f"verify {path.name}  ·  seal {row['seal']}  ·  exit {row['exit']}"):
            f1, f2, f3, f4 = st.columns(4)
            f1.markdown(f"**cycle**\n\n`{row['cycle_id']}`")
            f2.markdown(f"**seal**\n\n`{row['seal']}`")
            f3.markdown(f"**stdout sha256**\n\n`{row['stdout_sha256']}`")
            f4.markdown(f"**key**\n\n`{row['key']}`")
            if receipt is None:
                st.error("Receipt file is not valid JSON — showing nothing further.")
            elif st.checkbox("Show raw receipt", key=f"raw-{path.name}"):
                st.json(receipt, expanded=False)
            if st.button("Run bulla verify", key=f"verify-{path.name}"):
                ok, text = _bulla_verify(path)
                if ok:
                    st.success("bulla verify passed")
                else:
                    st.error("bulla verify failed or unavailable")
                st.code(text, language=None)


# ------------------------------------------- 🔨 Tamper Playground tab (WSL bridge)
# POSIX-side binary paths (inside WSL on Windows; local home on Linux/WSL) — the
# same bridge contract as quaestor/receipts.py: `~`-headed argv[0] is left
# unquoted so `bash -lc` expands it to the WSL home.
BULLA_BIN: str = "~/.cache/hack-target/release/bulla"
ZKRISK_BIN: str = "~/.cache/hack-target/release/examples/zkrisk"
WSL_DISTRO: str = "Ubuntu"
RECEIPT_LITE_SCHEMA: str = "quaestor.receipt-lite.v1"
_ZK_DEMO_KEY: str = "tamper_zk_demo_proof"
_ZK_KEYS: tuple[str, ...] = ("cap_bits", "commitment", "proof")
_TOOLCHAIN_HINT: str = (
    "The tamper playground needs the WSL toolchain: the `bulla` and `zkrisk` "
    "binaries built at ~/.cache/hack-target/release inside WSL (Ubuntu). "
    "They are unreachable from here, so live verification is disabled — the "
    "receipts themselves are still on disk and verifiable from any machine "
    "with the binaries."
)


def _invoke(argv: list[str], stdin: str | None = None) -> tuple[int | None, str, str]:
    """Run a POSIX argv (argv[0] = binary path inside WSL/home) and return
    ``(returncode, stdout, stderr)``.

    Same direct-vs-bridge logic as quaestor/receipts.py: on Windows the argv is
    wrapped as ``["wsl", "-d", "Ubuntu", "--", "bash", "-lc", <cmd>]`` (every
    token shell-quoted except a ``~``-headed argv[0]); on Linux/WSL the binary
    is called directly. ``returncode is None`` means the toolchain itself was
    unreachable (stderr then carries a human explanation) — callers must render
    that as st.info, never a traceback.
    """
    if os.name == "nt":
        head, *rest = argv
        head_s = head if head.startswith("~") else shlex.quote(head)
        shell_cmd = " ".join([head_s, *(shlex.quote(a) for a in rest)])
        cmd: list[str] = ["wsl", "-d", WSL_DISTRO, "--", "bash", "-lc", shell_cmd]
    else:
        binary = Path(argv[0]).expanduser()
        if not binary.exists():
            return None, "", f"binary not found at {binary}"
        cmd = [str(binary), *argv[1:]]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True,
                              timeout=VERIFY_TIMEOUT_S, input=stdin)
    except FileNotFoundError:
        return None, "", "WSL bridge unavailable (`wsl` not on PATH)"
    except subprocess.TimeoutExpired:
        return None, "", f"verifier timed out after {VERIFY_TIMEOUT_S:.0f}s"
    except OSError as exc:
        return None, "", f"could not launch verifier: {exc}"
    if proc.returncode == 127:  # bash -lc: command not found inside WSL
        return None, "", ((proc.stderr or proc.stdout).strip()
                          or "binary not found inside WSL (exit 127)")
    return proc.returncode, proc.stdout or "", proc.stderr or ""


def _invoke_text(rc: int | None, stdout: str, stderr: str) -> str:
    """Human-readable transcript of one _invoke outcome for st.code."""
    text = (stdout + (("\n" + stderr) if stderr.strip() else "")).strip()
    text = text or "(no output)"
    return text if rc is None else f"exit code {rc}\n{text}"


def _posix_path(p: Path) -> str:
    """The path as the (possibly WSL-side) binary must see it."""
    return _to_wsl_path(p) if os.name == "nt" else str(p)


def _signed_receipt_files() -> list[Path]:
    """receipts/*.json that are real signed bulla receipts (skip receipt-lite/UNSIGNED)."""
    out: list[Path] = []
    for p in _receipt_files():
        r = _read_json(p)
        if not isinstance(r, dict):
            continue
        if r.get("UNSIGNED") or r.get("schema") == RECEIPT_LITE_SCHEMA:
            continue
        if "sig" not in r or "body" not in r:  # bulla-receipt/v0 shape
            continue
        out.append(p)
    return out


def _zk_proof_sources() -> list[tuple[str, dict[str, Any]]]:
    """(label, RiskProof) pairs from session demo, cell decisions and receipts/zk/."""
    sources: list[tuple[str, dict[str, Any]]] = []
    demo = st.session_state.get(_ZK_DEMO_KEY)
    if isinstance(demo, dict) and all(k in demo for k in _ZK_KEYS):
        sources.append(("demo proof (generated this session)", demo))
    try:
        decision_files = sorted((RECEIPTS_DIR / "cells").glob("*/decision.json"))
    except Exception:
        decision_files = []
    for f in decision_files:
        d = _read_json(f)
        zk = d.get("zk_proof") if isinstance(d, dict) else None
        if isinstance(zk, dict) and all(k in zk for k in _ZK_KEYS):
            sources.append((f"cells/{f.parent.name}/decision.json", zk))
    try:
        zk_files = sorted((RECEIPTS_DIR / "zk").glob("*.json"))
    except Exception:
        zk_files = []
    for f in zk_files:
        d = _read_json(f)
        if isinstance(d, dict) and all(k in d for k in _ZK_KEYS):
            sources.append((f"zk/{f.name}", d))
    return sources


def _zk_payload_json(proof: dict[str, Any]) -> str:
    """Exactly the fields `zkrisk verify` checks — what you see is what is proved."""
    return json.dumps({k: proof[k] for k in _ZK_KEYS if k in proof}, indent=2)


def _show_invoke_result(rc: int | None, stdout: str, stderr: str,
                        ok_msg: str, fail_msg: str) -> None:
    """Uniform green/red/info treatment for one verifier run."""
    if rc is None:
        st.info(_TOOLCHAIN_HINT)
        if stderr.strip():
            st.caption(f"detail: {stderr.strip()}")
        return
    if rc == 0:
        st.success(ok_msg)
    else:
        st.error(fail_msg)
    st.code(_invoke_text(rc, stdout, stderr), language=None)


def _render_forge_receipt_section() -> None:
    st.subheader("Forge a receipt")
    files = _signed_receipt_files()
    if not files:
        st.info(
            "No signed bulla receipts on disk yet (receipt-lite/UNSIGNED files "
            "don't count — there is nothing cryptographic to forge in those). "
            "Run a decision cycle with bulla available and come back."
        )
        return
    names = [p.name for p in files]
    selected = st.selectbox("Signed receipt", names, key="tamper_receipt_sel",
                            help="Every file here carries an Ed25519 signature over "
                                 "the receipt body — edit any byte and it breaks.")
    path = files[names.index(str(selected))]
    receipt = _read_json(path)
    pretty = json.dumps(receipt, indent=2) if receipt is not None else path.read_text(
        encoding="utf-8", errors="replace")
    edited = st.text_area(
        "Receipt JSON — edit anything, then verify your copy",
        value=pretty, height=400, key=f"tamper_receipt_txt_{selected}",
    )
    st.caption("Hint: try changing `exit_code`, a hash, or `seal_ok` — then verify.")
    c1, c2 = st.columns(2)
    if c1.button("Verify original", key="tamper_receipt_orig"):
        rc, out, err = _invoke([BULLA_BIN, "verify", _posix_path(path)])
        _show_invoke_result(
            rc, out, err,
            ok_msg="SEAL intact — signature, digest and event chain verify",
            fail_msg="Original receipt FAILED verification — the on-disk file has "
                     "been altered since it was signed.",
        )
    if c2.button("Verify my edited copy", key="tamper_receipt_edit"):
        fd, tmp_name = tempfile.mkstemp(prefix="quaestor-tamper-", suffix=".json")
        tmp = Path(tmp_name)
        try:
            os.close(fd)
            tmp.write_text(edited, encoding="utf-8")
            rc, out, err = _invoke([BULLA_BIN, "verify", _posix_path(tmp)])
        finally:
            try:
                tmp.unlink()
            except OSError:
                pass
        _show_invoke_result(
            rc, out, err,
            ok_msg="Your copy still verifies — the signed content is unchanged. "
                   "(Whitespace and key order aren't part of the signed body — "
                   "change an actual value and try again.)",
            fail_msg="FORGERY DETECTED — signature does not match the edited body",
        )


def _render_forge_zk_section() -> None:
    st.subheader("Forge a ZK risk proof")
    st.caption(
        "Each approved order can carry a Bulletproof range proof: \"worst-case "
        "loss < 2^cap_bits dollars\" — without revealing the amount (Pedersen "
        "commitment). The JSON below is exactly what `zkrisk verify` checks."
    )
    sources = _zk_proof_sources()
    if not sources:
        st.info("No RiskProof found on disk (receipts/cells/*/decision.json or "
                "receipts/zk/). Generate a live one instead:")
        if st.button("Generate a demo proof (max loss $9,500 < $65,536)",
                     key="tamper_zk_gen"):
            rc, out, err = _invoke([ZKRISK_BIN, "prove", "9500", "16"])
            if rc is None:
                st.info(_TOOLCHAIN_HINT)
                if err.strip():
                    st.caption(f"detail: {err.strip()}")
                return
            proof: Any = None
            if rc == 0:
                try:
                    proof = json.loads(out.strip())
                except ValueError:
                    proof = None
            if isinstance(proof, dict) and all(k in proof for k in _ZK_KEYS):
                st.session_state[_ZK_DEMO_KEY] = proof
                st.rerun()
            else:
                st.error("zkrisk prove failed — no proof produced.")
                st.code(_invoke_text(rc, out, err), language=None)
        return

    labels = [lbl for lbl, _ in sources]
    if len(sources) > 1:
        chosen = st.selectbox("RiskProof", labels, key="tamper_zk_sel")
        label, proof_dict = sources[labels.index(str(chosen))]
    else:
        label, proof_dict = sources[0]
        st.caption(f"Source: {label}")
    pristine = _zk_payload_json(proof_dict)
    edited = st.text_area(
        "RiskProof JSON — edit anything, then verify your copy",
        value=pristine, height=260, key=f"tamper_zk_txt_{label}",
    )
    st.caption("Hint: flip one hex character in `commitment` or `proof`, or bump "
               "`cap_bits` — then verify.")
    c1, c2 = st.columns(2)
    if c1.button("Verify original", key="tamper_zk_orig"):
        rc, out, err = _invoke([ZKRISK_BIN, "verify"], stdin=pristine)
        _show_invoke_result(
            rc, out, err,
            ok_msg="proof verifies — the order's worst-case loss is under the cap",
            fail_msg="Original proof FAILED verification — the stored proof is "
                     "not valid for its commitment.",
        )
    if c2.button("Verify my edited copy", key="tamper_zk_edit"):
        rc, out, err = _invoke([ZKRISK_BIN, "verify"], stdin=edited)
        _show_invoke_result(
            rc, out, err,
            ok_msg="Your copy still verifies — the proof is unchanged. (Edit a "
                   "hex digit or `cap_bits` and try again.)",
            fail_msg="INVALID PROOF — the math caught you",
        )


def _render_tamper_tab() -> None:
    st.markdown(
        "**Don't trust our audit trail — attack it.** Below are the actual "
        "cryptographic artifacts this agent produced. Edit them however you "
        "like and re-run the verifiers: the genuine records pass, and any "
        "forged byte is caught by the Ed25519 signature (receipts) or the "
        "Bulletproof range proof (risk caps)."
    )
    _render_forge_receipt_section()
    st.divider()
    _render_forge_zk_section()


# ------------------------------------------------------------------------ About tab
def _render_about_tab() -> None:
    st.markdown(
        """
### What is quaestor?

**quaestor** is an autonomous options-trading agent built for the Alpaca AI Trading
Agents Hackathon. It trades defined-risk options structures (debit verticals,
catalyst straddles, 0–3 DTE) on SPY/QQQ through the **Alpaca paper Trading API** —
and every decision it makes is *provable after the fact*.

### The verifiable-agent story

Most trading bots ask you to trust their logs. quaestor does not:

- **Signed receipts** — each decision cycle (risk judgment + order construction)
  runs inside a *bulla* cell that produces a cryptographically signed receipt:
  inputs, decision output, stdout sha256, exit code, seal status.
- **Hash-chained ledger** — receipts append to `receipts/ledger.jsonl`; tampering
  with any past cycle breaks the chain head shown on the Receipts tab.
- **Deterministic risk gates** — every trade intent passes `risk.judge`: paper
  gate, defined-risk-only, per-trade loss cap, daily halt, concurrency,
  concentration, spread quality, open interest, 0DTE cutoffs, mleg sanity
  (≤4 legs, coprime ratios, signed net limit price). The exact policy that judged
  each trade is pinned by the **sha256 digest of `configs/policy.yaml`** inside
  every verdict and receipt.
- **Idempotent execution** — every order carries a unique `client_order_id`
  binding order → intent → (optional) ZK risk-cap commitment; retries look up by
  id, never double-send. TIF is always `day`; fills come from marketable limits
  with a cancel-and-repost loop tuned to Alpaca's paper fill engine.

### Requirement checklist

| Requirement | Status | How |
|---|---|---|
| Alpaca Trading API | ✓ | account, positions, single-leg + mleg options orders on paper-api |
| CLI + MCP | ✓ | `python -m quaestor {status,once,loop,verify,flatten}`; official Alpaca CLI/MCP conventions (`ALPACA_API_KEY`/`ALPACA_SECRET_KEY`) |
| Options trading | ✓ | defined-risk verticals, straddles, 0–3 DTE, OCC symbology, signed net mleg pricing |
| Risk gates | ✓ | deterministic `risk.judge` with policy sha256 digest in every verdict and receipt |

**Paper trading only.** The agent refuses to start if it resolves to a live
endpoint (`ALPACA_LIVE_TRADE` fail-closed gate).
        """
    )
    st.warning(DISCLOSURE)


# ----------------------------------------------------------------------------- main
def main() -> None:
    st.set_page_config(
        page_title="quaestor — verifiable options agent",
        page_icon="🧾",
        layout="wide",
    )
    st.title("quaestor")
    st.caption(
        "Autonomous options agent on Alpaca paper trading — every decision sealed "
        f"in a signed receipt. Read-only audit view · refreshed "
        f"{datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')} UTC"
    )

    tab_agent, tab_receipts, tab_tamper, tab_about = st.tabs(
        ["Agent", "Receipts", "🔨 Tamper Playground", "About"]
    )
    with tab_agent:
        _render_agent_tab()
    with tab_receipts:
        _render_receipts_tab()
    with tab_tamper:
        _render_tamper_tab()
    with tab_about:
        _render_about_tab()


if __name__ == "__main__":
    main()
