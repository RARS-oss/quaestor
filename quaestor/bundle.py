"""Proof bundle — package the whole trading week so a judge verifies it offline.

Role
----
``build_bundle`` assembles a single self-contained folder that a judge can open on
any machine, with no network and no install, and independently confirm that every
decision and every order this agent placed is cryptographically accounted for:

- ``receipts/``     — every signed bulla receipt (byte-attestation cycle receipts
                      ``c-*.json`` and sealed-execution receipts ``sealed-*.json``),
                      the hash-chained run ledgers (``ledger.jsonl`` /
                      ``sealed-ledger.jsonl``), and the external ``anchor.jsonl``
                      witness chain when present.
- ``verifier.html`` — the self-contained WASM verifier (bulla ``verify`` compiled
                      to WebAssembly): paste any receipt from ``receipts/`` and it
                      checks the Ed25519 signature, body digest and event chain in
                      the browser tab.
- ``summary.json`` / ``order_log.csv`` — the latest session's equity/P&L aggregate
                      and its flat order log, straight from the ``runs/`` contract.
- ``index.html``    — a GENERATED landing page (no external assets) with headline
                      stats, a per-receipt table, and the verify instructions.
- ``README.txt``    — plain-text "what this is / how to verify".

Why this exists (Alpaca facts encoded)
--------------------------------------
Every order the agent submits carries an idempotent Alpaca ``client_order_id`` and
its exchange conversation is hash-chained into a signed receipt; the run ledgers
chain those receipts into one tamper-evident week. Bundling the receipts + ledgers
+ the offline verifier turns "trust our screenshots" into "verify our signatures":
the paper account's whole P&L is provable end to end, not a demo.

Safety
------
The Ed25519 signing seed (``receipts/signing.seed``) is trust material and is NEVER
copied into a bundle — only ``*.json`` receipts and the ``*.jsonl`` ledgers/anchor
are shipped. Every source is optional: a missing file is skipped and noted on the
generated index rather than raising, so producing a bundle never fails partway.
"""
from __future__ import annotations

import html
import json
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover — avoid a runtime import cycle with config.py
    from quaestor.config import Settings

__all__ = ["build_bundle", "stats_from_receipts"]

# Hash-chained run ledgers copied verbatim from receipts_dir alongside the *.json
# receipts. The signing seed is deliberately absent — trust material never leaves the
# machine. anchor.jsonl (the external witness chain) is handled separately because it
# may live under runs/ instead (see build_bundle).
_LEDGER_NAMES: tuple[str, ...] = ("ledger.jsonl", "sealed-ledger.jsonl")
# Keys an anchor line might carry for its rolling chain head (format is self-chaining).
_ANCHOR_HEAD_KEYS: tuple[str, ...] = ("hash", "head", "chain_head", "ledger_head", "anchor")
_SHORT: int = 12

# The mandatory Alpaca risk disclosure (mirrors README.md / dashboard/app.py wording).
DISCLOSURE: str = (
    "Not investment advice. Options trading involves significant risk and is not "
    "suitable for all investors. This project runs exclusively in Alpaca's paper "
    "trading environment — simulated results do not represent actual trading. See "
    "Alpaca's disclosures at https://alpaca.markets/disclosures and the "
    "Characteristics and Risks of Standardized Options."
)


# --------------------------------------------------------------------- json helpers

def _load_json(path: Path) -> Any | None:
    """Load a JSON file; return None on any failure (missing, malformed, perms)."""
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _jsonl_last(path: Path) -> dict[str, Any] | None:
    """Return the last well-formed JSON object in a JSONL file, or None."""
    if not path.exists():
        return None
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None
    last: dict[str, Any] | None = None
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except ValueError:
            continue
        if isinstance(obj, dict):
            last = obj
    return last


def _short(value: Any) -> str:
    """First _SHORT characters of a hash-like value, or "" when absent."""
    if not value:
        return ""
    return str(value)[:_SHORT]


# ------------------------------------------------------------------ receipt facts

def _is_receipt_lite(data: Any) -> bool:
    """True for an UNSIGNED receipt-lite fallback (no cryptographic seal)."""
    return isinstance(data, dict) and bool(
        data.get("UNSIGNED") or data.get("schema") == "quaestor.receipt-lite.v1"
    )


def _receipt_facts(path: Path) -> dict[str, Any]:
    """Extract the judge-facing facts for one receipt file (guarding bad JSON).

    Returns a dict: ``{cycle, kind, seal_ok, seal_display, exit, digest_short,
    readable}`` where ``kind`` is "sealed" | "decision" | "unsigned" | "unreadable".
    """
    cycle = path.stem
    data = _load_json(path)
    if data is None:
        return {
            "cycle": cycle, "kind": "unreadable", "seal_ok": None,
            "seal_display": "?", "exit": "—", "digest_short": "", "readable": False,
        }
    if _is_receipt_lite(data):
        return {
            "cycle": cycle, "kind": "unsigned", "seal_ok": False,
            "seal_display": "UNSIGNED", "exit": "—", "digest_short": "", "readable": True,
        }

    body = data.get("body") if isinstance(data.get("body"), dict) else {}
    seal_ok = body.get("seal_ok")
    egress = body.get("egress")
    command = body.get("command") if isinstance(body.get("command"), list) else []
    sealed = (
        egress is not None
        or path.name.startswith("sealed-")
        or (bool(command) and str(command[0]).endswith("python3"))
    )
    outcome = body.get("outcome") if isinstance(body.get("outcome"), dict) else {}
    exit_kind = outcome.get("exit_kind")
    exit_code = outcome.get("exit_code")
    exit_str = (
        f"{exit_kind}:{exit_code}" if exit_kind is not None or exit_code is not None else "—"
    )
    seal_display = "held" if seal_ok is True else ("broken" if seal_ok is False else "?")
    return {
        "cycle": cycle,
        "kind": "sealed" if sealed else "decision",
        "seal_ok": seal_ok if isinstance(seal_ok, bool) else None,
        "seal_display": seal_display,
        "exit": exit_str,
        "digest_short": _short(data.get("body_digest")),
        "readable": True,
    }


def stats_from_receipts(receipts_dir: Path) -> dict[str, Any]:
    """Summarise a receipts directory for the bundle index.

    Reads every ``*.json`` receipt plus ``sealed-ledger.jsonl`` / ``ledger.jsonl``
    (for the run-chain head) and ``anchor.jsonl`` (for the external witness head),
    guarding malformed JSON throughout.

    Returns ``{total, sealed, decision, seal_held, chain_head_short,
    anchor_head_short}``:
    - ``total``            number of receipt JSON files present;
    - ``sealed``           receipts carrying a mediated-egress block (sealed exec);
    - ``decision``         signed byte-attestation cycle receipts (no egress);
    - ``seal_held``        receipts whose body reports ``seal_ok == True``;
    - ``chain_head_short`` short hash of the run ledger's last entry;
    - ``anchor_head_short`` short hash of the anchor chain's last entry.
    """
    receipts_dir = Path(receipts_dir)
    total = sealed = decision = seal_held = 0
    if receipts_dir.exists():
        for path in sorted(receipts_dir.glob("*.json")):
            total += 1
            facts = _receipt_facts(path)
            if facts["kind"] == "sealed":
                sealed += 1
            elif facts["kind"] == "decision":
                decision += 1
            if facts["seal_ok"] is True:
                seal_held += 1

    # Run-ledger head: prefer the sealed week chain, fall back to the decision chain.
    chain_head = ""
    for name in ("sealed-ledger.jsonl", "ledger.jsonl"):
        last = _jsonl_last(receipts_dir / name)
        if last is not None:
            chain_head = _short(last.get("hash"))
            if chain_head:
                break

    anchor_head = ""
    anchor_last = _jsonl_last(receipts_dir / "anchor.jsonl")
    if anchor_last is not None:
        for key in _ANCHOR_HEAD_KEYS:
            anchor_head = _short(anchor_last.get(key))
            if anchor_head:
                break

    return {
        "total": total,
        "sealed": sealed,
        "decision": decision,
        "seal_held": seal_held,
        "chain_head_short": chain_head,
        "anchor_head_short": anchor_head,
    }


# ----------------------------------------------------------------- session lookup

def _latest_session_file(runs_dir: Path, filename: str) -> Path | None:
    """The newest ``runs/<session>/<filename>`` by mtime, or None if none exist."""
    runs_dir = Path(runs_dir)
    if not runs_dir.exists():
        return None
    candidates: list[Path] = []
    for session in runs_dir.glob("*-session*"):
        if not session.is_dir():
            continue
        candidate = session / filename
        if candidate.is_file():
            candidates.append(candidate)
    if not candidates:
        return None
    return max(candidates, key=lambda p: p.stat().st_mtime)


def _copy_if_present(src: Path, dst: Path, notes: list[str], label: str) -> bool:
    """Copy ``src`` -> ``dst`` if it exists; record a note when it does not."""
    if src.is_file():
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
        return True
    notes.append(f"{label}: not found ({src.name}) — skipped")
    return False


# --------------------------------------------------------------------- index page

def _fmt_money(value: Any) -> str:
    try:
        return f"${float(value):,.2f}"
    except (TypeError, ValueError):
        return "n/a"


def _stat_cards(stats: dict[str, Any], summary: dict[str, Any] | None) -> list[tuple[str, str, str]]:
    """Build (label, value, tone) triples for the headline stat grid."""
    cards: list[tuple[str, str, str]] = []
    if summary:
        equity_last = summary.get("equity_last")
        equity_open = summary.get("equity_open")
        pnl = summary.get("realized_pnl")
        cards.append(("equity", _fmt_money(equity_last), "plain"))
        if isinstance(pnl, (int, float)):
            tone = "ok" if pnl > 0 else ("bad" if pnl < 0 else "muted")
            sign = "+" if pnl > 0 else ""
            cards.append(("session P&L", f"{sign}{_fmt_money(pnl)}", tone))
        if isinstance(equity_open, (int, float)):
            cards.append(("session open", _fmt_money(equity_open), "muted"))
        cards.append(("cycles", str(summary.get("cycles", 0)), "plain"))
        cards.append(("orders logged", str(summary.get("orders_logged", 0)), "plain"))
    cards.append(("receipts", str(stats["total"]), "accent"))
    cards.append(("sealed / decision", f"{stats['sealed']} / {stats['decision']}", "plain"))
    cards.append(("seal held", str(stats["seal_held"]), "ok"))
    if stats["chain_head_short"]:
        cards.append(("ledger head", stats["chain_head_short"], "mono"))
    if stats["anchor_head_short"]:
        cards.append(("anchor head", stats["anchor_head_short"], "mono"))
    return cards


def _render_index(
    stats: dict[str, Any],
    summary: dict[str, Any] | None,
    receipt_facts: list[dict[str, Any]],
    notes: list[str],
    have_verifier: bool,
) -> str:
    """Assemble the self-contained index.html (dark-first, no external assets)."""
    generated = datetime.now(timezone.utc).isoformat(timespec="seconds")

    cards_html = "\n".join(
        f'      <div class="card {tone}"><div class="k">{html.escape(label)}</div>'
        f'<div class="v">{html.escape(value)}</div></div>'
        for label, value, tone in _stat_cards(stats, summary)
    )

    row_html_parts: list[str] = []
    for facts in receipt_facts:
        kind = facts["kind"]
        seal_ok = facts["seal_ok"]
        seal_cls = "pass" if seal_ok is True else ("fail" if seal_ok is False else "muted")
        kind_cls = {
            "sealed": "b-sealed", "decision": "b-decision",
            "unsigned": "b-unsigned", "unreadable": "b-bad",
        }.get(kind, "b-bad")
        row_html_parts.append(
            "        <tr>"
            f'<td class="mono">{html.escape(facts["cycle"])}</td>'
            f'<td><span class="badge {kind_cls}">{html.escape(kind)}</span></td>'
            f'<td class="{seal_cls}">{html.escape(facts["seal_display"])}</td>'
            f'<td class="mono">{html.escape(facts["exit"])}</td>'
            f'<td class="mono muted">{html.escape(facts["digest_short"])}</td>'
            "</tr>"
        )
    rows_html = "\n".join(row_html_parts) or (
        '        <tr><td colspan="5" class="muted">no receipts present</td></tr>'
    )

    verifier_cta = (
        '<a class="cta" href="verifier.html">Open the offline verifier &rarr;</a>'
        if have_verifier
        else '<span class="cta disabled">verifier.html missing from this bundle</span>'
    )

    notes_html = ""
    if notes:
        items = "\n".join(f"      <li>{html.escape(n)}</li>" for n in notes)
        notes_html = (
            '  <section class="panel notes">\n'
            "    <h2>Assembly notes</h2>\n"
            "    <ul>\n" + items + "\n    </ul>\n  </section>\n"
        )

    template = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>quaestor — verifiable trading week</title>
<style>
  :root {
    --bg:#0b0d12; --panel:#141821; --panel2:#1b2130; --ink:#e6edf3; --muted:#8b95a7;
    --line:#242c3a; --accent:#818cf8; --accent-ink:#c7cbff; --ok:#22c55e; --bad:#ef6a5f;
    --mono:"IBM Plex Mono",ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;
    --sans:"IBM Plex Sans",ui-sans-serif,system-ui,-apple-system,"Segoe UI",Roboto,Helvetica,Arial,sans-serif;
  }
  @media (prefers-color-scheme: light) {
    :root:not([data-theme="dark"]) {
      --bg:#f6f7f9; --panel:#ffffff; --panel2:#f0f2f6; --ink:#161a20; --muted:#5b6572;
      --line:#e3e7ec; --accent:#4f46e5; --accent-ink:#4338ca; --ok:#0f8a5f; --bad:#c8372d;
    }
  }
  :root[data-theme="light"] {
    --bg:#f6f7f9; --panel:#ffffff; --panel2:#f0f2f6; --ink:#161a20; --muted:#5b6572;
    --line:#e3e7ec; --accent:#4f46e5; --accent-ink:#4338ca; --ok:#0f8a5f; --bad:#c8372d;
  }
  * { box-sizing:border-box; }
  body { margin:0; background:var(--bg); color:var(--ink); font:15px/1.55 var(--sans);
    -webkit-font-smoothing:antialiased; }
  .wrap { max-width:960px; margin:0 auto; padding:40px 20px 72px; }
  header { border-bottom:1px solid var(--line); padding-bottom:22px; margin-bottom:26px; }
  .kicker { font:600 12px/1 var(--mono); letter-spacing:0.14em; text-transform:uppercase;
    color:var(--accent-ink); margin:0 0 10px; }
  h1 { font-size:30px; line-height:1.15; letter-spacing:-0.02em; margin:0 0 12px; }
  .lede { color:var(--muted); margin:0; max-width:70ch; }
  h2 { font-size:15px; letter-spacing:0.02em; margin:0 0 14px; color:var(--ink); }
  .grid { display:grid; grid-template-columns:repeat(auto-fit,minmax(150px,1fr)); gap:12px;
    margin-bottom:26px; }
  .card { background:var(--panel); border:1px solid var(--line); border-radius:12px; padding:14px 16px; }
  .card .k { font-size:12px; color:var(--muted); text-transform:uppercase; letter-spacing:0.06em; }
  .card .v { font-size:22px; font-weight:600; margin-top:6px; letter-spacing:-0.01em; }
  .card.ok .v { color:var(--ok); } .card.bad .v { color:var(--bad); }
  .card.accent .v { color:var(--accent); } .card.muted .v { color:var(--muted); }
  .card.mono .v { font-family:var(--mono); font-size:16px; }
  .panel { background:var(--panel); border:1px solid var(--line); border-radius:14px;
    padding:22px; margin-bottom:22px; }
  .verify { display:flex; flex-wrap:wrap; align-items:center; gap:18px 24px; }
  .verify ol { margin:0; padding-left:20px; color:var(--muted); flex:1 1 320px; }
  .verify ol b { color:var(--ink); }
  .cta { display:inline-block; background:var(--accent); color:#0b0d12; font-weight:600;
    text-decoration:none; padding:12px 20px; border-radius:10px; white-space:nowrap; }
  .cta:hover { filter:brightness(1.08); }
  .cta.disabled { background:transparent; color:var(--bad); border:1px solid var(--bad);
    font-weight:500; }
  table { width:100%; border-collapse:collapse; font-size:13.5px; }
  thead th { text-align:left; color:var(--muted); font-weight:600; font-size:12px;
    text-transform:uppercase; letter-spacing:0.05em; padding:0 10px 10px; border-bottom:1px solid var(--line); }
  tbody td { padding:9px 10px; border-bottom:1px solid var(--line); vertical-align:middle; }
  tbody tr:last-child td { border-bottom:none; }
  .mono { font-family:var(--mono); font-size:12.5px; }
  .muted { color:var(--muted); }
  .pass { color:var(--ok); font-weight:600; } .fail { color:var(--bad); font-weight:600; }
  .badge { display:inline-block; font:600 11px/1 var(--mono); letter-spacing:0.04em;
    padding:4px 8px; border-radius:999px; border:1px solid var(--line); text-transform:uppercase; }
  .b-sealed { color:var(--accent); border-color:var(--accent); }
  .b-decision { color:var(--muted); }
  .b-unsigned { color:var(--bad); border-color:var(--bad); }
  .b-bad { color:var(--bad); border-color:var(--bad); }
  .table-scroll { overflow-x:auto; }
  .notes ul { margin:0; padding-left:20px; color:var(--muted); font-size:13px; }
  .foot { color:var(--muted); font-size:12px; line-height:1.6; border-top:1px solid var(--line);
    margin-top:30px; padding-top:18px; }
  .foot .gen { font-family:var(--mono); }
  a { color:var(--accent-ink); }
</style>
</head>
<body>
<div class="wrap">
  <header>
    <p class="kicker">quaestor · proof bundle</p>
    <h1>A verifiable trading week</h1>
    <p class="lede">Every decision and every order this autonomous options agent placed on
      Alpaca&rsquo;s paper API is sealed into a signed <b>bulla</b> receipt and hash-chained into a
      tamper-evident ledger. This folder is self-contained: no server, no install, no network.
      Verify the signatures yourself, offline, right here.</p>
  </header>

  <section>
    <h2>Headline</h2>
    <div class="grid">
__CARDS__
    </div>
  </section>

  <section class="panel verify">
    <ol>
      <li>Open <b>verifier.html</b> (double-click it — it runs entirely in your browser tab).</li>
      <li>Open any receipt in <b>receipts/</b>, copy its JSON, and <b>paste it</b> into the verifier.</li>
      <li>Hit <b>Verify</b> — it checks the Ed25519 signature, body digest and event hash-chain
          locally, then tamper one character and watch it reject.</li>
    </ol>
    __VERIFIER_CTA__
  </section>

  <section class="panel">
    <h2>Receipts (__TOTAL__)</h2>
    <div class="table-scroll">
    <table>
      <thead><tr>
        <th>cycle id</th><th>type</th><th>seal</th><th>exit</th><th>body digest</th>
      </tr></thead>
      <tbody>
__ROWS__
      </tbody>
    </table>
    </div>
  </section>

__NOTES__
  <p class="foot">
    <span class="gen">generated __GENERATED__ · bundle produced by quaestor.bundle</span><br>
    __DISCLOSURE__
  </p>
</div>
</body>
</html>
"""
    return (
        template
        .replace("__CARDS__", cards_html)
        .replace("__VERIFIER_CTA__", verifier_cta)
        .replace("__TOTAL__", str(stats["total"]))
        .replace("__ROWS__", rows_html)
        .replace("__NOTES__", notes_html)
        .replace("__GENERATED__", html.escape(generated))
        .replace("__DISCLOSURE__", html.escape(DISCLOSURE))
    )


def _render_readme(bundle_dir: Path, stats: dict[str, Any], notes: list[str]) -> str:
    """Plain-text README.txt: what the bundle is and how to verify it."""
    lines = [
        "quaestor — proof bundle",
        "=======================",
        "",
        "This folder is a self-contained, offline-verifiable record of an autonomous",
        "options-trading agent's paper-trading week on Alpaca. Every decision cycle and",
        "every order was sealed into a signed bulla receipt and hash-chained into a",
        "tamper-evident ledger. Nothing here needs a server, an install, or a network",
        "connection to check.",
        "",
        "Contents",
        "--------",
        "  index.html          Start here. Headline stats + a table of every receipt.",
        "  verifier.html       The bulla verifier compiled to WebAssembly (runs in-browser).",
        "  receipts/           Every signed receipt (c-*.json cycle attestations and",
        "                      sealed-*.json order receipts) plus the hash-chained ledgers",
        "                      (ledger.jsonl / sealed-ledger.jsonl) and anchor.jsonl if present.",
        "  summary.json        Latest session equity curve, P&L and counts.",
        "  order_log.csv       Latest session flat order log (one row per Alpaca exchange).",
        "",
        "How to verify",
        "-------------",
        "  1. Open index.html in any browser and read the receipt table.",
        "  2. Open verifier.html (double-click). Copy the JSON of any file in receipts/,",
        "     paste it in, and click Verify. It checks the Ed25519 signature, the body",
        "     digest and the event hash-chain locally. Change one character to see it fail.",
        "  3. If you have the bulla CLI installed, you can verify from a terminal instead:",
        "         bulla verify receipts/<file>.json",
        "     (exit code 0 == intact and signed; 2 == tampered or unsigned).",
        "",
        "Snapshot",
        "--------",
        f"  receipts total     {stats['total']}",
        f"  sealed / decision  {stats['sealed']} / {stats['decision']}",
        f"  seal held          {stats['seal_held']}",
        f"  ledger head        {stats['chain_head_short'] or '(none)'}",
        f"  anchor head        {stats['anchor_head_short'] or '(none)'}",
        "",
    ]
    if notes:
        lines.append("Assembly notes (missing sources were skipped, not fatal)")
        lines.append("-------------------------------------------------------")
        lines.extend(f"  - {n}" for n in notes)
        lines.append("")
    lines.append("Disclosure")
    lines.append("----------")
    lines.append(DISCLOSURE)
    lines.append("")
    return "\n".join(lines)


# ------------------------------------------------------------------------ build

def build_bundle(settings: "Settings", out_dir: Path | None = None) -> Path:
    """Assemble the offline proof bundle and return its folder path.

    Copies the signed receipts + ledgers + WASM verifier + latest session summary
    and order log into ``out_dir`` (default ``settings.runs_dir/bundle``), then
    generates ``index.html`` and ``README.txt``. Fail-open: any missing source is
    skipped and recorded in the index's assembly notes rather than raising, so a
    bundle is always produced.
    """
    receipts_dir = Path(settings.receipts_dir)
    runs_dir = Path(settings.runs_dir)
    repo_root = Path(settings.repo_root)

    bundle_dir = Path(out_dir) if out_dir is not None else runs_dir / "bundle"
    bundle_receipts = bundle_dir / "receipts"
    bundle_receipts.mkdir(parents=True, exist_ok=True)

    notes: list[str] = []

    # 1. Receipts: every *.json (signed + sealed + any lite fallbacks). The signing
    #    seed is NOT *.json, so trust material never enters the bundle.
    copied_receipts = 0
    if receipts_dir.exists():
        for path in sorted(receipts_dir.glob("*.json")):
            if not path.is_file():
                continue
            shutil.copy2(path, bundle_receipts / path.name)
            copied_receipts += 1
    if copied_receipts == 0:
        notes.append("receipts: none found — bundle has no signed receipts to verify")

    # 2. Hash-chained run ledgers (each optional).
    for name in _LEDGER_NAMES:
        src = receipts_dir / name
        if src.is_file():
            shutil.copy2(src, bundle_receipts / name)
        else:
            notes.append(f"ledger: {name} not found — skipped")

    # 3. External anchor witness chain: prefer receipts/anchor.jsonl, fall back to
    #    runs/anchor.jsonl (where `quaestor anchor` writes it). Landed into the bundle's
    #    receipts/ so the offline reader finds it beside the ledgers. Expected-absent
    #    until anchoring has run, so its absence is not noted as a problem.
    anchor_src = receipts_dir / "anchor.jsonl"
    if not anchor_src.is_file():
        anchor_src = runs_dir / "anchor.jsonl"
    if anchor_src.is_file():
        shutil.copy2(anchor_src, bundle_receipts / "anchor.jsonl")

    # 4. The offline WASM verifier.
    have_verifier = _copy_if_present(
        repo_root / "dashboard" / "verifier.html",
        bundle_dir / "verifier.html",
        notes,
        "verifier.html",
    )

    # 5. Latest session summary.json + order_log.csv (from the runs/ contract).
    summary_src = _latest_session_file(runs_dir, "summary.json")
    summary: dict[str, Any] | None = None
    if summary_src is not None:
        shutil.copy2(summary_src, bundle_dir / "summary.json")
        loaded = _load_json(summary_src)
        summary = loaded if isinstance(loaded, dict) else None
        if summary is None:
            notes.append("summary.json: present but not valid JSON")
    else:
        notes.append("summary.json: no session summary found — headline stats limited")

    order_log_src = _latest_session_file(runs_dir, "order_log.csv")
    if order_log_src is not None:
        shutil.copy2(order_log_src, bundle_dir / "order_log.csv")
    else:
        notes.append("order_log.csv: no session order log found — skipped")

    # 6. Stats over exactly what was shipped, then the generated pages.
    stats = stats_from_receipts(bundle_receipts)
    receipt_facts = [
        _receipt_facts(p) for p in sorted(bundle_receipts.glob("*.json"))
    ]

    index_html = _render_index(stats, summary, receipt_facts, notes, have_verifier)
    (bundle_dir / "index.html").write_text(index_html, encoding="utf-8")
    (bundle_dir / "README.txt").write_text(
        _render_readme(bundle_dir, stats, notes), encoding="utf-8"
    )

    return bundle_dir
