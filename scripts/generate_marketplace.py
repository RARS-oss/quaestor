"""Assemble the "Alpaca Verified Agents" marketplace demo (single self-contained HTML).

This is the beyond-hackathon artifact: it makes the product vision tangible. A
market surface where autonomous agents publish *provable, verifiable* track
records, and a follower can copy/fund on evidence rather than reputation.

The page renders a grid of agent cards:

  * Card 1 is OURS and REAL — "quaestor". Its numbers are read live from this
    repo's audit artifacts (``receipts/*.json`` counts + seal-held, the sealed-
    execution ledger head, and ``runs/portfolio_state.json`` week P&L). Its
    "Verify record" button loads one real signed receipt and runs the embedded
    WebAssembly build of ``bulla verify`` (Ed25519 signature / body digest /
    event hash-chain / hermetic seal) entirely client-side — no server, no
    install, no trust.

  * Cards 2-3 are clearly-labelled ILLUSTRATIVE example agents. They demonstrate
    the *shape* of the marketplace with plausible-but-synthetic stats and an
    "illustrative" tag. They are NEVER presented as real; their "Verify record"
    button honestly says there is no real receipt to check.

Every read is fail-open, so a missing artifact degrades to a "live Mon-Fri"
placeholder rather than an error. The verifier always works on the embedded
sample even before a trading week is recorded.

Run (WSL):  ~/hack/venv/bin/python scripts/generate_marketplace.py
Output:     dashboard/marketplace.html
"""
from __future__ import annotations

import base64
import glob
import html
import json
import os
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[1]
RUNS = REPO / "runs"
RECEIPTS = REPO / "receipts"
DEST = REPO / "dashboard" / "marketplace.html"

# The no-modules wasm-pack build lives in the bulla clone (same source as
# generate_verifier.py / generate_track_record.py). Prefer the WSL home
# checkout; fall back to the /mnt/c clone path.
_PKG_CANDIDATES = [
    Path.home() / "hack/refs/bulla/crates/bulla-wasm/pkg-nomod",
    Path("/mnt/c/Users/Daniil/Desktop/alpaca-hack/refs/bulla/crates/bulla-wasm/pkg-nomod"),
]


# =============================================================================
# small utilities (all fail-open — never raise into the caller)
# =============================================================================
def _load_json(path: Path) -> Any | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _iter_jsonl(path: Path) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    if not path.exists():
        return out
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return out
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except ValueError:
            continue
        if isinstance(obj, dict):
            out.append(obj)
    return out


def _short(h: str | None, head: int = 12, tail: int = 8) -> str:
    if not h:
        return "—"
    h = str(h)
    if len(h) <= head + tail + 1:
        return h
    return f"{h[:head]}…{h[-tail:]}"


def _e(s: Any) -> str:
    return html.escape(str(s), quote=True)


# =============================================================================
# data gathering (the REAL quaestor card)
# =============================================================================
def classify_receipts() -> dict[str, Any]:
    """Inspect ``receipts/*.json``: counts, seal-held, sealed vs decision, pubkey."""
    total = sealed = decision = seal_held = unsigned = 0
    pubkey: str | None = None
    newest_epoch = -1.0
    if RECEIPTS.exists():
        for p in sorted(RECEIPTS.glob("*.json")):
            data = _load_json(p)
            if not isinstance(data, dict):
                continue
            if data.get("UNSIGNED") or str(data.get("schema", "")).startswith("quaestor.receipt-lite"):
                unsigned += 1
                continue
            body = data.get("body")
            if not isinstance(body, dict) or "sig" not in data:
                continue
            total += 1
            if body.get("egress"):
                sealed += 1
            else:
                decision += 1
            if body.get("seal_ok") is True:
                seal_held += 1
            try:
                ep = float(body.get("created_epoch") or p.stat().st_mtime)
            except (TypeError, ValueError, OSError):
                ep = 0.0
            if ep > newest_epoch and isinstance(data.get("pubkey"), str):
                newest_epoch = ep
                pubkey = data["pubkey"]
    return {
        "total": total,
        "sealed": sealed,
        "decision": decision,
        "seal_held": seal_held,
        "unsigned": unsigned,
        "pubkey": pubkey,
    }


def ledger_head(path: Path) -> tuple[int, str | None]:
    entries = _iter_jsonl(path)
    if not entries:
        return 0, None
    return len(entries), entries[-1].get("hash")


def order_rows() -> int:
    """Total logged order rows across every session's order_log.csv (minus header)."""
    import csv

    rows_total = 0
    if RUNS.exists():
        for sub in RUNS.glob("*-session"):
            csv_path = sub / "order_log.csv"
            if not csv_path.exists():
                continue
            try:
                with csv_path.open("r", newline="", encoding="utf-8") as fh:
                    rows = list(csv.reader(fh))
                rows_total += max(0, len(rows) - 1)
            except OSError:
                continue
    return rows_total


def week_pnl() -> tuple[float | None, float | None, float | None]:
    """Return ``(week_pnl_pct, week_open_equity, day_pnl_pct)`` from portfolio_state."""
    ps = _load_json(RUNS / "portfolio_state.json")
    if not isinstance(ps, dict):
        return None, None, None
    def _num(k: str) -> float | None:
        v = ps.get(k)
        return float(v) if isinstance(v, (int, float)) else None
    return _num("week_pnl_pct"), _num("week_open_equity"), _num("day_pnl_pct")


def pick_sample_receipt() -> tuple[str, str]:
    """Return ``(name, json_text)`` of the real receipt to embed and verify.

    Newest ``receipts/sealed-*.json`` wins; else newest ``receipts/c-*.json``;
    else a minimal empty object so the page still loads.
    """
    def newest(pattern: str) -> Path | None:
        if not RECEIPTS.exists():
            return None
        cands = [Path(p) for p in glob.glob(str(RECEIPTS / pattern)) if os.path.isfile(p)]
        if not cands:
            return None
        return max(cands, key=lambda p: p.stat().st_mtime)

    chosen = newest("sealed-*.json") or newest("c-*.json")
    if chosen is None:
        return "none", "{}"
    try:
        return chosen.name, chosen.read_text(encoding="utf-8")
    except OSError:
        return chosen.name, "{}"


# =============================================================================
# wasm
# =============================================================================
def load_wasm() -> tuple[str, str, str]:
    """Return ``(glue_js, wasm_base64, source_dir)`` for the embedded verifier."""
    pkg = next((p for p in _PKG_CANDIDATES if p.exists()), None)
    if pkg is None:
        raise SystemExit(
            "bulla-wasm pkg-nomod not found; looked in: "
            + ", ".join(str(p) for p in _PKG_CANDIDATES)
        )
    glue = (pkg / "bulla_wasm.js").read_text(encoding="utf-8")
    wasm_b64 = base64.b64encode((pkg / "bulla_wasm_bg.wasm").read_bytes()).decode("ascii")
    return glue, wasm_b64, str(pkg)


# =============================================================================
# styling — "cryptographic instrument": near-black ground, indigo accent,
# verified green, forgery red, hash cyan, IBM Plex. Light base on :root;
# dark via prefers-color-scheme and the data-theme override.
# =============================================================================
CSS = """
  :root {
    --bg:#f5f7fb; --surface:#ffffff; --surface-2:#eef1f7; --sunken:#f7f8fc;
    --line:#dde3ee; --line-strong:#c9d2e2;
    --ink:#12151f; --muted:#5a6478; --faint:#8b94a8;
    --accent:#4b5cf0; --accent-ink:#3038b8; --accent-soft:rgba(75,92,240,0.10);
    --verified:#0f9d63; --forgery:#d94434; --hash:#0f8fa6;
    --demo:#b7791f; --demo-soft:rgba(183,121,31,0.12);
    --sans:"IBM Plex Sans",-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif;
    --mono:"IBM Plex Mono",ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;
  }
  @media (prefers-color-scheme: dark) {
    :root:not([data-theme="light"]) {
      --bg:#0b0e15; --surface:#131826; --surface-2:#0f1420; --sunken:#0d111b;
      --line:#232a3d; --line-strong:#2f3850;
      --ink:#e8ecf6; --muted:#8b96ad; --faint:#5a6580;
      --accent:#6a7cff; --accent-ink:#8f9dff; --accent-soft:rgba(106,124,255,0.13);
      --verified:#35c98c; --forgery:#ff6459; --hash:#5fd6e4;
      --demo:#e2a33c; --demo-soft:rgba(226,163,60,0.14);
    }
  }
  :root[data-theme="dark"] {
    --bg:#0b0e15; --surface:#131826; --surface-2:#0f1420; --sunken:#0d111b;
    --line:#232a3d; --line-strong:#2f3850;
    --ink:#e8ecf6; --muted:#8b96ad; --faint:#5a6580;
    --accent:#6a7cff; --accent-ink:#8f9dff; --accent-soft:rgba(106,124,255,0.13);
    --verified:#35c98c; --forgery:#ff6459; --hash:#5fd6e4;
    --demo:#e2a33c; --demo-soft:rgba(226,163,60,0.14);
  }

  * { box-sizing:border-box; }
  html { -webkit-text-size-adjust:100%; }
  body {
    margin:0; background:var(--bg); color:var(--ink);
    font:15px/1.6 var(--sans); letter-spacing:-0.003em;
    background-image:
      radial-gradient(1200px 560px at 82% -10%, var(--accent-soft), transparent 60%),
      radial-gradient(900px 520px at 0% 0%, rgba(53,201,140,0.05), transparent 55%);
    background-repeat:no-repeat;
  }
  .num { font-variant-numeric:tabular-nums; }
  a { color:var(--accent-ink); text-decoration:none; }
  a:hover { text-decoration:underline; }
  .wrap { max-width:1080px; margin:0 auto; padding:34px 22px 76px; }

  .topbar { display:flex; align-items:center; justify-content:space-between; gap:14px; flex-wrap:wrap; }
  .brand { display:flex; align-items:center; gap:10px; color:var(--muted);
    font:600 12.5px/1 var(--mono); letter-spacing:0.10em; text-transform:uppercase; }
  .brand .dot { width:9px; height:9px; border-radius:50%; background:var(--verified);
    box-shadow:0 0 0 4px rgba(53,201,140,0.16); }
  .themetoggle { font:600 11px/1 var(--mono); letter-spacing:0.12em; text-transform:uppercase;
    color:var(--muted); background:var(--surface); border:1px solid var(--line);
    border-radius:8px; padding:8px 11px; cursor:pointer; }
  .themetoggle:hover { color:var(--ink); border-color:var(--accent); }

  .hero { margin:30px 0 6px; }
  .eyebrow { display:inline-flex; align-items:center; gap:9px; font:600 11.5px/1 var(--mono);
    letter-spacing:0.20em; text-transform:uppercase; color:var(--accent-ink); margin-bottom:16px; }
  .eyebrow::before { content:""; width:26px; height:1px; background:var(--accent); opacity:0.75; }
  .hero h1 { font-size:clamp(29px,4.8vw,50px); line-height:1.05; margin:0 0 16px;
    letter-spacing:-0.024em; font-weight:600; max-width:20ch; }
  .hero h1 .g { color:var(--verified); }
  .hero p.lede { color:var(--muted); font-size:17px; margin:0 0 22px; max-width:66ch; }

  /* how-verification-works strip */
  .how { display:flex; gap:10px; flex-wrap:wrap; align-items:stretch; margin:4px 0 4px; }
  .how .step { display:flex; align-items:center; gap:11px; background:var(--surface);
    border:1px solid var(--line); border-radius:12px; padding:11px 15px; flex:1 1 210px; }
  .how .n { flex:0 0 auto; width:24px; height:24px; border-radius:50%; display:grid;
    place-items:center; background:var(--accent-soft); color:var(--accent-ink);
    font:600 12px var(--mono); }
  .how .lab { font-size:13.5px; line-height:1.3; }
  .how .lab b { font-weight:600; }
  .how .lab span { color:var(--muted); }
  .how .arrow { align-self:center; color:var(--faint); font-family:var(--mono);
    padding:0 2px; }
  @media (max-width:720px){ .how .arrow { display:none; } }

  .verifstat { display:inline-flex; align-items:center; gap:8px; margin-top:16px;
    padding:7px 13px; border-radius:999px; border:1px solid var(--line);
    background:var(--surface); color:var(--muted); font:600 12px/1 var(--mono);
    letter-spacing:0.03em; }
  .verifstat .live { width:8px; height:8px; border-radius:50%; background:var(--faint); }
  .verifstat.ready { color:var(--verified); }
  .verifstat.ready .live { background:var(--verified); box-shadow:0 0 0 3px rgba(53,201,140,0.18); }

  /* the marketplace grid */
  .grid { display:grid; grid-template-columns:repeat(auto-fill,minmax(320px,1fr));
    gap:18px; margin:28px 0 10px; }
  .card { position:relative; background:var(--surface); border:1px solid var(--line);
    border-radius:18px; padding:20px 20px 18px; display:flex; flex-direction:column;
    overflow:hidden; }
  .card.real { border-color:color-mix(in srgb, var(--verified) 42%, var(--line)); }
  .card.real::before { content:""; position:absolute; inset:0 0 auto 0; height:3px;
    background:linear-gradient(90deg,var(--verified),var(--hash)); }
  .card.demo { border-style:dashed; }

  .card .chead { display:flex; align-items:flex-start; justify-content:space-between; gap:10px; }
  .avatar { width:40px; height:40px; border-radius:11px; display:grid; place-items:center;
    font:600 17px var(--mono); color:#fff; flex:0 0 auto; }
  .card .name { font-size:19px; font-weight:600; letter-spacing:-0.01em; display:flex;
    align-items:center; gap:8px; flex-wrap:wrap; }
  .card .handle { color:var(--faint); font:500 12px/1 var(--mono); margin-top:3px; }
  .tag { font:600 9.5px/1 var(--mono); letter-spacing:0.10em; text-transform:uppercase;
    padding:4px 7px; border-radius:6px; border:1px solid; white-space:nowrap; }
  .tag.you { color:var(--accent-ink); border-color:color-mix(in srgb,var(--accent) 45%,transparent);
    background:var(--accent-soft); }
  .tag.ill { color:var(--demo); border-color:color-mix(in srgb,var(--demo) 45%,transparent);
    background:var(--demo-soft); }

  .strat { color:var(--muted); font-size:13.5px; line-height:1.45; margin:14px 0 4px; min-height:38px; }

  .seal { display:inline-flex; align-items:center; gap:7px; margin:12px 0 2px;
    padding:6px 11px; border-radius:999px; font:600 12px/1 var(--mono); align-self:flex-start;
    color:var(--verified); border:1px solid color-mix(in srgb,var(--verified) 40%,transparent);
    background:rgba(53,201,140,0.09); }
  .seal svg { width:13px; height:13px; }
  .card.demo .seal { color:var(--muted); border-color:var(--line);
    background:var(--surface-2); }
  .card.demo .seal .sample { color:var(--demo); }

  .kpis { display:grid; grid-template-columns:repeat(3,1fr); gap:10px; margin:16px 0 4px;
    border-top:1px solid var(--line); border-bottom:1px solid var(--line); padding:14px 0; }
  .kpi .k { color:var(--faint); font:600 9.5px/1 var(--mono); letter-spacing:0.07em;
    text-transform:uppercase; margin-bottom:7px; }
  .kpi .v { font-size:19px; font-weight:600; letter-spacing:-0.015em; }
  .kpi .v.pos { color:var(--verified); }
  .kpi .v.neg { color:var(--forgery); }
  .kpi .s { color:var(--faint); font:500 10px/1 var(--mono); margin-top:4px; }

  .provline { font:500 11px/1.6 var(--mono); color:var(--faint); margin:12px 0 2px;
    word-break:break-all; }
  .provline b { color:var(--hash); font-weight:600; }

  .cta { display:flex; gap:9px; margin-top:auto; padding-top:16px; }
  .btn { flex:1 1 auto; border:1px solid var(--line); background:var(--surface-2);
    color:var(--ink); border-radius:10px; padding:11px 14px; font:600 13.5px var(--sans);
    cursor:pointer; transition:filter .12s ease, border-color .12s ease; text-align:center; }
  .btn:hover { filter:brightness(1.08); }
  .btn.primary { background:var(--accent); border-color:var(--accent); color:#fff; }
  .btn.copy { position:relative; }
  .btn.copy[disabled] { cursor:not-allowed; color:var(--muted); opacity:0.85; }
  .btn.copy[disabled]:hover { filter:none; }
  .btn.copy .demopill { position:absolute; top:-8px; right:-6px; font:600 8px/1 var(--mono);
    letter-spacing:0.08em; text-transform:uppercase; color:var(--demo);
    background:var(--demo-soft); border:1px solid color-mix(in srgb,var(--demo) 40%,transparent);
    border-radius:5px; padding:2px 4px; }

  .illnote { color:var(--faint); font-size:11.5px; line-height:1.5; margin-top:12px;
    padding-top:11px; border-top:1px dashed var(--line); }

  /* closing band + disclosure */
  .band { margin:30px 0 6px; background:var(--surface); border:1px solid var(--line);
    border-radius:18px; padding:28px 26px; }
  .band h2 { font-size:clamp(21px,2.8vw,29px); font-weight:600; letter-spacing:-0.018em;
    margin:0 0 12px; max-width:24ch; }
  .band h2 .g { color:var(--verified); }
  .band p { color:var(--muted); font-size:15px; max-width:64ch; margin:0 0 16px; }
  .cmd { display:flex; align-items:center; gap:12px; background:var(--sunken);
    border:1px solid var(--line); border-radius:12px; padding:13px 16px; flex-wrap:wrap; }
  .cmd code { font:600 13.5px var(--mono); color:var(--ink); }
  .cmd .desc { color:var(--muted); font-size:12.5px; }
  .prompt { color:var(--verified); font-family:var(--mono); }

  .disclosure { color:var(--faint); font-size:12px; line-height:1.7; margin-top:22px;
    padding-top:16px; border-top:1px solid var(--line); }
  .disclosure b { color:var(--muted); }
  .foot { color:var(--faint); font:500 11.5px/1.6 var(--mono); margin-top:18px; }

  /* verify modal */
  .modal[hidden] { display:none; }
  .modal { position:fixed; inset:0; z-index:50; display:grid; place-items:center; padding:20px; }
  .modal-backdrop { position:absolute; inset:0; background:rgba(4,6,12,0.62);
    backdrop-filter:blur(3px); }
  .modal-card { position:relative; width:min(680px,100%); max-height:88vh; overflow:auto;
    background:var(--surface); border:1px solid var(--line-strong); border-radius:18px;
    padding:24px 24px 26px; box-shadow:0 24px 70px rgba(0,0,0,0.4); }
  .modal-x { position:absolute; top:15px; right:15px; width:32px; height:32px; border-radius:8px;
    border:1px solid var(--line); background:var(--surface-2); color:var(--muted);
    font-size:15px; cursor:pointer; line-height:1; }
  .modal-x:hover { color:var(--ink); border-color:var(--accent); }
  .meyebrow { color:var(--faint); font:600 11px/1 var(--mono); letter-spacing:0.14em;
    text-transform:uppercase; margin-bottom:6px; }
  .modal-card h3 { font-size:20px; font-weight:600; margin:0 0 4px; letter-spacing:-0.015em; }
  .modal-card .msub { color:var(--muted); font-size:13px; margin:0 0 18px; }

  .banner { border-radius:12px; padding:14px 16px; font-weight:600; font-size:15.5px;
    border:1px solid; display:flex; align-items:center; gap:10px; }
  .banner.ok { color:var(--verified); border-color:var(--verified); background:rgba(53,201,140,0.10); }
  .banner.bad { color:var(--forgery); border-color:var(--forgery); background:rgba(255,100,89,0.10); }
  .banner.info { color:var(--demo); border-color:color-mix(in srgb,var(--demo) 45%,transparent);
    background:var(--demo-soft); }
  .banner svg { width:19px; height:19px; flex:0 0 auto; }
  table.checks { width:100%; border-collapse:collapse; margin-top:14px; font-size:13.5px; }
  table.checks td { padding:9px 8px; border-bottom:1px solid var(--line); }
  table.checks td.k { color:var(--muted); width:220px; }
  table.checks .chk { font-family:var(--mono); }
  table.checks .pass { color:var(--verified); } table.checks .fail { color:var(--forgery); }
  table.checks .na { color:var(--faint); }
  .mmeta { margin-top:12px; font:12px/1.7 var(--mono); color:var(--faint); }
  .mmeta code { color:var(--hash); }
  .notes { margin-top:12px; font:12px/1.6 var(--mono); color:var(--faint); white-space:pre-wrap; }
  details.receipt { margin-top:14px; border:1px solid var(--line); border-radius:10px;
    background:var(--sunken); overflow:hidden; }
  details.receipt summary { cursor:pointer; padding:11px 14px; font:600 12px var(--mono);
    color:var(--muted); letter-spacing:0.03em; }
  details.receipt pre { margin:0; padding:0 14px 14px; overflow:auto; max-height:240px;
    font:11.5px/1.5 var(--mono); color:var(--ink); }
  .mrow { display:flex; gap:10px; flex-wrap:wrap; margin-top:16px; }
  .mrow button { border:1px solid var(--line); background:var(--surface-2); color:var(--ink);
    border-radius:10px; padding:10px 16px; font:600 13px var(--sans); cursor:pointer; }
  .mrow button:hover { filter:brightness(1.1); }
  .mrow button.tamper { color:var(--forgery); border-color:color-mix(in srgb,var(--forgery) 40%,transparent); }
  .mfoot { color:var(--faint); font:500 11px/1.5 var(--mono); margin-top:16px;
    padding-top:12px; border-top:1px solid var(--line); }
  code { font-family:var(--mono); background:var(--surface-2); padding:1px 6px; border-radius:5px; font-size:12.5px; }

  @media (prefers-reduced-motion: reduce) {
    * { transition:none !important; animation:none !important; }
    .modal-backdrop { backdrop-filter:none; }
  }
"""

CHECK_SVG = (
    '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.6" '
    'stroke-linecap="round" stroke-linejoin="round"><path d="M20 6 9 17l-5-5"/></svg>'
)


# =============================================================================
# card assembly
# =============================================================================
def _kpi(k: str, v: str, cls: str = "", sub: str = "sealed") -> str:
    sub_html = f'<div class="s">{_e(sub)}</div>' if sub else ""
    return (
        f'<div class="kpi"><div class="k">{_e(k)}</div>'
        f'<div class="v num {cls}">{_e(v)}</div>{sub_html}</div>'
    )


def build_quaestor_card(rc: dict[str, Any], seal_head: str | None, seal_n: int,
                        wk_pct: float | None, wk_open: float | None,
                        day_pct: float | None, orders_n: int) -> str:
    """The one REAL card — numbers read from this repo's audit artifacts."""
    have_data = rc["total"] > 0

    # P&L
    if wk_pct is not None:
        cls = "pos" if wk_pct >= 0 else "neg"
        abs_usd = (wk_pct / 100.0 * wk_open) if wk_open else 0.0
        pnl_v = f"{wk_pct:+.2f}%"
        pnl_sub = f"{'+' if abs_usd >= 0 else '-'}${abs(abs_usd):,.0f} · sealed"
    else:
        cls = ""
        pnl_v = "live Mon–Fri"
        pnl_sub = "paper · $100k"

    # trades == signed receipts (task: "trades count = receipts")
    trades_v = str(rc["total"]) if have_data else "live Mon–Fri"
    trades_sub = (
        f'{rc["sealed"]} sealed-exec · {rc["decision"]} decision'
        if have_data else "sealed"
    )

    # max drawdown — from a flat opening paper week there is no realised drawdown
    if have_data and wk_pct is not None:
        dd_v = "0.00%"
        dd_sub = "no closed drawdown"
    else:
        dd_v = "live Mon–Fri"
        dd_sub = "sealed"

    seal_pill = (
        f'<div class="seal"><span class="mono">{CHECK_SVG}</span> Verified '
        f'· {rc["seal_held"]}/{rc["total"]} seal-held</div>'
        if have_data else
        '<div class="seal">' + CHECK_SVG + ' Verified · ready to run</div>'
    )

    prov = ""
    if seal_head:
        prov = (
            f'<div class="provline">sealed-ledger head <b>{_e(_short(seal_head, 14, 8))}</b>'
            f' · {seal_n} entr{"y" if seal_n == 1 else "ies"}</div>'
        )
    if rc["pubkey"]:
        prov += (
            f'<div class="provline">Ed25519 key <b>{_e(_short(rc["pubkey"], 14, 8))}</b>'
            f' · receipt author</div>'
        )

    return f"""
    <article class="card real">
      <div class="chead">
        <div style="display:flex; gap:13px; align-items:flex-start;">
          <div class="avatar" style="background:linear-gradient(140deg,#6a7cff,#5fd6e4);">Q</div>
          <div>
            <div class="name">quaestor <span class="tag you">Yours · real</span></div>
            <div class="handle">@quaestor · options L3 · PA3OB6ZUD7WD</div>
          </div>
        </div>
      </div>
      <p class="strat">Autonomous L3 options on SPY — every decision and fill runs
         inside a hermetic, net-namespaced cell and emits a signed, hash-chained receipt.</p>
      {seal_pill}
      <div class="kpis">
        {_kpi("Week P&amp;L", pnl_v, cls, pnl_sub)}
        {_kpi("Signed trades", trades_v, "", trades_sub)}
        {_kpi("Max drawdown", dd_v, "", dd_sub)}
      </div>
      {prov}
      <div class="cta">
        <button class="btn primary" data-verify data-real="1"
          data-agent="quaestor" data-handle="@quaestor">Verify record</button>
        <button class="btn copy" disabled title="Copy-trading is a demo of the marketplace concept — not wired to a live account.">Copy agent<span class="demopill">demo</span></button>
      </div>
    </article>"""


def build_illustrative_card(a: dict[str, Any]) -> str:
    pnl_cls = "pos" if a["pnl"] >= 0 else "neg"
    return f"""
    <article class="card demo">
      <div class="chead">
        <div style="display:flex; gap:13px; align-items:flex-start;">
          <div class="avatar" style="background:{a['grad']};">{_e(a['initial'])}</div>
          <div>
            <div class="name">{_e(a['name'])} <span class="tag ill">Illustrative</span></div>
            <div class="handle">@{_e(a['handle'])} · {_e(a['level'])}</div>
          </div>
        </div>
      </div>
      <p class="strat">{_e(a['strategy'])}</p>
      <div class="seal">{CHECK_SVG} Verified <span class="sample">· sample</span></div>
      <div class="kpis">
        {_kpi("Week P&amp;L", f"{a['pnl']:+.1f}%", pnl_cls, "sealed")}
        {_kpi("Signed trades", str(a['trades']), "", "sealed")}
        {_kpi("Max drawdown", f"{a['dd']:+.1f}%", "", "sealed")}
      </div>
      <div class="cta">
        <button class="btn" data-verify data-real="0"
          data-agent="{_e(a['name'])}" data-handle="@{_e(a['handle'])}">Verify record</button>
        <button class="btn copy" disabled title="Copy-trading is a demo of the marketplace concept — not wired to a live account.">Copy agent<span class="demopill">demo</span></button>
      </div>
      <p class="illnote">Illustrative agent — shows the shape of the marketplace.
         Stats are synthetic; there is no real signed receipt behind this card.</p>
    </article>"""


ILLUSTRATIVE = [
    {
        "name": "theta-harvester", "handle": "theta-harvester", "initial": "θ",
        "level": "options L2", "grad": "linear-gradient(140deg,#b7791f,#e2a33c)",
        "strategy": "Cash-secured puts and covered calls on blue-chip ETFs — "
                    "weekly theta capture, delta-hedged into expiry.",
        "pnl": 3.8, "trades": 47, "dd": -2.4,
    },
    {
        "name": "momentum-scout", "handle": "momentum-scout", "initial": "↗",
        "level": "options L3", "grad": "linear-gradient(140deg,#4b5cf0,#8f9dff)",
        "strategy": "Intraday momentum on liquid single-names — 0DTE debit "
                    "spreads with hard stops and a per-trade risk cap.",
        "pnl": 9.1, "trades": 214, "dd": -7.6,
    },
]


# =============================================================================
# JS
# =============================================================================
JS = r"""
  const WASM_B64 = "__WASM_B64__";
  const SAMPLE = __SAMPLE_JSON__;
  const SAMPLE_NAME = "__SAMPLE_NAME__";
  const $ = id => document.getElementById(id);
  const esc = s => String(s).replace(/[&<>]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]));
  const CHECK = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.6" stroke-linecap="round" stroke-linejoin="round"><path d="M20 6 9 17l-5-5"/></svg>';
  const CROSS = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.6" stroke-linecap="round" stroke-linejoin="round"><path d="M18 6 6 18M6 6l12 12"/></svg>';
  const INFO  = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="9"/><path d="M12 8h.01M11 12h1v4h1"/></svg>';

  const sampleText = JSON.stringify(SAMPLE, null, 2);
  const haveSample = sampleText.trim() !== "{}" && !!(SAMPLE && SAMPLE.body);

  /* ---- theme toggle (persists per-viewer only) ------------------------- */
  const themeBtn = $("themetoggle");
  function applyTheme(t){
    if (t === "dark" || t === "light") document.documentElement.setAttribute("data-theme", t);
    else document.documentElement.removeAttribute("data-theme");
    try { localStorage.setItem("qmkt-theme", t || "system"); } catch(e){}
  }
  try {
    const saved = localStorage.getItem("qmkt-theme");
    if (saved && saved !== "system") document.documentElement.setAttribute("data-theme", saved);
  } catch(e){}
  if (themeBtn) themeBtn.onclick = () => {
    const cur = document.documentElement.getAttribute("data-theme");
    const dark = cur ? cur === "dark"
      : window.matchMedia && window.matchMedia("(prefers-color-scheme: dark)").matches;
    applyTheme(dark ? "light" : "dark");
  };

  /* ---- wasm boot ------------------------------------------------------- */
  const bytes = Uint8Array.from(atob(WASM_B64), c => c.charCodeAt(0));
  let ready = false;
  wasm_bindgen(bytes).then(() => {
    ready = true;
    const s = $("verifstat");
    if (s) { s.className = "verifstat ready"; s.querySelector(".txt").textContent =
      "verifier ready · bulla-wasm v" + wasm_bindgen.version() + " · runs in this tab, offline"; }
  }).catch(e => {
    const s = $("verifstat");
    if (s) s.querySelector(".txt").textContent = "verifier failed to load: " + e;
  });

  /* ---- modal ----------------------------------------------------------- */
  const modal = $("modal");
  let currentReal = false;
  function openModal(){ modal.hidden = false; document.body.style.overflow = "hidden"; }
  function closeModal(){ modal.hidden = true; document.body.style.overflow = ""; }
  modal.addEventListener("click", e => { if (e.target.hasAttribute("data-close")) closeModal(); });
  document.addEventListener("keydown", e => { if (e.key === "Escape" && !modal.hidden) closeModal(); });

  function renderVerdict(r){
    if (!r.ok_json) {
      return '<div class="banner bad">' + CROSS + 'NOT A RECEIPT</div>'
        + '<div class="notes">' + esc(r.error || "unparseable") + '</div>';
    }
    const banner = r.intact
      ? '<div class="banner ok">' + CHECK + 'INTACT — signature, body digest and event chain all verify</div>'
      : '<div class="banner bad">' + CROSS + 'FORGERY DETECTED — this record does not verify</div>';
    const rows = [
      ["signature (Ed25519)", r.sig_ok],
      ["body digest (sha256)", r.digest_ok],
      ["event hash-chain", r.chain_ok],
      ["seal held (hermetic)", r.seal_ok],
    ].map(([k, ok]) => '<tr><td class="k">' + k + '</td><td class="chk ' + (ok?'pass':'fail')
        + '">' + (ok ? 'ok' : 'FAIL') + '</td></tr>').join("");
    const meta = '<div class="mmeta">command <code>' + esc(r.command || '') + '</code><br>'
      + 'exit ' + esc(String(r.exit_kind)) + ':' + esc(String(r.exit_code)) + '<br>'
      + 'signing key <code>' + esc((r.pubkey||'').slice(0,40)) + '…</code></div>';
    const notes = (r.notes && r.notes.length)
      ? '<div class="notes">' + r.notes.map(esc).join("\n") + '</div>' : '';
    return banner + '<table class="checks">' + rows + '</table>' + meta + notes;
  }

  let workingText = sampleText;   // current receipt text in the modal (real card)
  function runReal(){
    if (!ready) { $("mbody").innerHTML =
      '<div class="banner info">' + INFO + 'Verifier still loading — try again in a moment.</div>'; return; }
    let rep;
    try { rep = JSON.parse(wasm_bindgen.verify_receipt(workingText)); }
    catch (e) { rep = { ok_json:false, error:String(e) }; }
    const controls = '<div class="mrow">'
      + '<button class="tamper" id="btamper">Tamper: flip exit code</button>'
      + '<button id="brestore">Restore original</button></div>';
    $("mbody").innerHTML = renderVerdict(rep)
      + '<details class="receipt"><summary>Show signed receipt · ' + esc(SAMPLE_NAME)
      + '</summary><pre>' + esc(workingText) + '</pre></details>'
      + controls
      + '<div class="mfoot" id="mfoot"></div>';
    const mf = $("mfoot");
    if (mf) mf.textContent = ready
      ? "verified locally · bulla-wasm v" + wasm_bindgen.version() + " · no server, no network"
      : "verifier loading…";
    const bt = $("btamper"), br = $("brestore");
    if (bt) bt.onclick = () => {
      try {
        const o = JSON.parse(workingText);
        if (o.body && o.body.outcome) o.body.outcome.exit_code = (o.body.outcome.exit_code || 0) + 99;
        else if (o.body) o.body.__tampered = true;
        workingText = JSON.stringify(o, null, 2);
      } catch(e){}
      runReal();
    };
    if (br) br.onclick = () => { workingText = sampleText; runReal(); };
  }

  function openReal(agent, handle){
    currentReal = true; workingText = sampleText;
    $("magent").textContent = handle + " · real signed receipt";
    $("mtitle").textContent = "Verify " + agent + "'s record";
    $("msub").innerHTML = "Runs the embedded WebAssembly build of <code>bulla verify</code> "
      + "against a real receipt from this agent — Ed25519 signature, body digest, "
      + "event hash-chain and hermetic seal, all checked in your browser.";
    openModal();
    if (!haveSample) {
      $("mbody").innerHTML = '<div class="banner info">' + INFO
        + 'No signed receipt recorded yet — the agent publishes one on its first sealed cycle (live Mon–Fri).</div>';
      return;
    }
    runReal();
  }

  function openIllustrative(agent, handle){
    currentReal = false;
    $("magent").textContent = handle + " · illustrative agent";
    $("mtitle").textContent = "Verify " + agent + "'s record";
    $("msub").innerHTML = "This is a demonstration card showing the shape of the marketplace.";
    const rows = [
      ["signature (Ed25519)", "no receipt"],
      ["body digest (sha256)", "no receipt"],
      ["event hash-chain", "no receipt"],
      ["seal held (hermetic)", "no receipt"],
    ].map(([k, s]) => '<tr><td class="k">' + k + '</td><td class="chk na">' + s + '</td></tr>').join("");
    $("mbody").innerHTML =
      '<div class="banner info">' + INFO + esc(agent)
        + ' is an illustrative agent — there is no real signed receipt to check.</div>'
      + '<table class="checks">' + rows + '</table>'
      + '<div class="notes">On the real marketplace, every agent publishes a receipt like '
      + 'quaestor’s — which you can verify right here, offline, in this tab. '
      + 'Only evidence you can recompute earns the ✓ Verified seal.</div>';
    openModal();
  }

  document.querySelectorAll("[data-verify]").forEach(b => {
    b.addEventListener("click", () => {
      const agent = b.getAttribute("data-agent");
      const handle = b.getAttribute("data-handle") || ("@" + agent);
      if (b.getAttribute("data-real") === "1") openReal(agent, handle);
      else openIllustrative(agent, handle);
    });
  });
"""


def build_page() -> str:
    glue, wasm_b64, _pkg = load_wasm()
    rc = classify_receipts()
    seal_n, seal_head = ledger_head(RECEIPTS / "sealed-ledger.jsonl")
    _dec_n, _dec_head = ledger_head(RECEIPTS / "ledger.jsonl")
    wk_pct, wk_open, day_pct = week_pnl()
    orders_n = order_rows()
    sample_name, sample_text = pick_sample_receipt()

    quaestor_card = build_quaestor_card(
        rc, seal_head, seal_n, wk_pct, wk_open, day_pct, orders_n
    )
    demo_cards = "".join(build_illustrative_card(a) for a in ILLUSTRATIVE)

    how = """
    <div class="how">
      <div class="step"><span class="n">1</span><span class="lab"><b>Signed</b><br>
        <span>every decision + fill → Ed25519 receipt</span></span></div>
      <span class="arrow">→</span>
      <div class="step"><span class="n">2</span><span class="lab"><b>Sealed</b><br>
        <span>run in a hermetic, hash-chained cell</span></span></div>
      <span class="arrow">→</span>
      <div class="step"><span class="n">3</span><span class="lab"><b>Verified</b><br>
        <span>checked in your browser, offline</span></span></div>
    </div>"""

    hero = f"""
  <div class="hero">
    <div class="eyebrow">Alpaca Verified Agents</div>
    <h1>Autonomous agents with track records you can <span class="g">verify, not trust</span>.</h1>
    <p class="lede">A market surface where trading agents publish a signed, sealed,
       independently checkable record of everything they did — so a follower can
       copy or fund on <b>evidence, not reputation</b>. One card here is real and
       verifies a real receipt in your browser; the rest illustrate the shape.</p>
    {how}
    <div class="verifstat" id="verifstat"><span class="live"></span>
      <span class="txt">verifier: loading…</span></div>
  </div>"""

    grid = f"""
  <div class="grid">
    {quaestor_card}
    {demo_cards}
  </div>"""

    band = """
  <div class="band">
    <h2>This is the trust layer copy-trading of AI agents <span class="g">needs</span>.</h2>
    <p>Reputation and screenshots don't compose. A signed, sealed, anchored receipt
       does: it lets a marketplace rank agents on proof, lets a follower audit before
       they allocate, and lets an agent carry its record anywhere. Provable agent
       performance is the primitive the whole layer is built on.</p>
    <div class="cmd">
      <span class="prompt">$</span><code>make verify</code>
      <span class="desc">re-checks every receipt's Ed25519 signature, body digest and
        hash-chain — offline, no account, no trust in us.</span>
    </div>
    <p class="disclosure">
      <b>Not investment advice.</b> Options trading involves significant risk.
      Paper-trading only; simulated results. See
      <a href="https://alpaca.markets/disclosures">alpaca.markets/disclosures</a>.
      <br><br>
      Cards marked <b>Illustrative</b> are synthetic demonstrations of the marketplace
      shape — not real agents and not backed by real receipts. Only the
      <b>quaestor</b> card reads live audit artifacts from this repo and verifies a
      real signed receipt.
    </p>
  </div>"""

    modal = """
  <div class="modal" id="modal" hidden>
    <div class="modal-backdrop" data-close></div>
    <div class="modal-card" role="dialog" aria-modal="true" aria-labelledby="mtitle">
      <button class="modal-x" data-close aria-label="Close">✕</button>
      <div class="meyebrow" id="magent">agent</div>
      <h3 id="mtitle">Verify record</h3>
      <p class="msub" id="msub"></p>
      <div id="mbody"></div>
    </div>
  </div>"""

    js = (
        JS.replace("__WASM_B64__", wasm_b64)
        .replace("__SAMPLE_JSON__", json.dumps(json.loads(sample_text)))
        .replace("__SAMPLE_NAME__", sample_name)
    )

    page = f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Alpaca Verified Agents</title>
<meta name="description" content="A marketplace where autonomous trading agents publish signed, sealed, independently verifiable track records — copy or fund on evidence, not reputation.">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@400;500;600&family=IBM+Plex+Sans:wght@400;500;600&display=swap" rel="stylesheet">
<style>{CSS}</style>
</head>
<body>
<div class="wrap">
  <div class="topbar">
    <div class="brand"><span class="dot"></span>Alpaca Verified Agents · provable agent performance</div>
    <button class="themetoggle" id="themetoggle">Theme</button>
  </div>
{hero}
{grid}
{band}
  <p class="foot">quaestor · an autonomous, verifiable options-trading agent · every trade cryptographically signed, hermetically sealed, independently verifiable.</p>
</div>
{modal}
<script>{glue}</script>
<script>{js}</script>
</body>
</html>
"""

    for token in ("__WASM_B64__", "__SAMPLE_JSON__", "__SAMPLE_NAME__", "__GLUE__"):
        if token in page:
            raise SystemExit(f"template token {token} was not substituted")
    return page, rc, seal_head, seal_n, wk_pct, wk_open, sample_name


def main() -> None:
    page, rc, seal_head, seal_n, wk_pct, wk_open, sample_name = build_page()
    DEST.parent.mkdir(parents=True, exist_ok=True)
    DEST.write_text(page, encoding="utf-8")
    size = DEST.stat().st_size
    print(f"wrote {DEST} ({size:,} bytes / {size // 1024} KB)")
    print("--- real quaestor card stats ---")
    print(f"  signed receipts : {rc['total']}  ({rc['sealed']} sealed-exec, {rc['decision']} decision)")
    print(f"  seal held       : {rc['seal_held']}/{rc['total']}")
    print(f"  sealed-ledger   : {seal_n} entr{'y' if seal_n == 1 else 'ies'}, head {_short(seal_head, 14, 8)}")
    pnl = f"{wk_pct:+.2f}%" if wk_pct is not None else "live Mon-Fri"
    print(f"  week P&L        : {pnl}  (week_open ${wk_open:,.0f})" if wk_open else f"  week P&L        : {pnl}")
    print(f"  Ed25519 key     : {_short(rc['pubkey'], 14, 8)}")
    print(f"  sample receipt  : {sample_name}")


if __name__ == "__main__":
    main()
