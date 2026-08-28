"""Assemble the verifiable agent track-record page (single self-contained HTML).

This is the PRODUCT surface of quaestor: it turns the audit tooling into
"provable agent performance". It reads the week's audit artifacts (the latest
``runs/<session>/summary.json`` equity/P&L/order counts, ``receipts/*.json``, the
bulla run ledger, the sealed-execution ledger, and the external anchor) and
renders ``dashboard/track-record.html`` — a polished, theme-aware page showing
the agent's signed, anchored P&L that ANYONE can verify in-browser.

The page embeds the SAME WebAssembly build of ``bulla verify`` used by
``dashboard/verifier.html`` (glue + base64 .wasm inlined) plus one real sealed
receipt as the pre-loaded sample, so "Verify this record" runs the Ed25519
signature / body-digest / event-chain check entirely client-side — no server,
no install, no trust.

Nothing here touches the network or the trading loop; every read is fail-open,
so a missing artifact degrades to an empty-state placeholder rather than an
error. The verifier always works on the embedded sample even with no trading
week recorded yet.

Run (WSL):  ~/hack/venv/bin/python scripts/generate_track_record.py
"""
from __future__ import annotations

import base64
import csv
import html
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[1]
RUNS = REPO / "runs"
RECEIPTS = REPO / "receipts"
DEST = REPO / "dashboard" / "track-record.html"

# The no-modules wasm-pack build lives in the bulla clone (same source as
# generate_verifier.py). Prefer the WSL home checkout; fall back to /mnt/c.
_PKG_CANDIDATES = [
    Path.home() / "hack/refs/bulla/crates/bulla-wasm/pkg-nomod",
    Path("/mnt/c/Users/Daniil/Desktop/alpaca-hack/refs/bulla/crates/bulla-wasm/pkg-nomod"),
]

# ------------------------------------------------------------------------- palette
BG = "#0b0e15"
SURFACE = "#131826"
ACCENT = "#6a7cff"       # indigo
VERIFIED = "#35c98c"     # green
FORGERY = "#ff6459"      # red
HASHCYAN = "#5fd6e4"     # hash cyan


# =============================================================================
# small utilities (all fail-open — never raise into the caller)
# =============================================================================
def _load_json(path: Path) -> Any | None:
    """Parse a JSON file, returning None on any error."""
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _iter_jsonl(path: Path) -> list[dict[str, Any]]:
    """Parse a .jsonl file into a list of dicts, skipping unreadable lines."""
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


def _epoch(value: Any) -> float | None:
    """Coerce a cycle timestamp (float epoch, numeric string, or ISO 8601) to epoch."""
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        s = value.strip()
        if not s:
            return None
        try:
            return float(s)
        except ValueError:
            pass
        try:
            return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()
        except ValueError:
            return None
    return None


def _money(x: float) -> str:
    return f"${x:,.2f}"


def _pct(x: float) -> str:
    return f"{x:+.2f}%"


def _short(h: str | None, head: int = 12, tail: int = 8) -> str:
    """Middle-elided hash for compact display; '—' when absent."""
    if not h:
        return "—"
    h = str(h)
    if len(h) <= head + tail + 1:
        return h
    return f"{h[:head]}…{h[-tail:]}"


def _et_label(ep: float) -> str:
    """Format an epoch as 'Aug 28, 17:27 ET' (best-effort ET; UTC fallback)."""
    try:
        from zoneinfo import ZoneInfo

        dt = datetime.fromtimestamp(ep, tz=ZoneInfo("America/New_York"))
        return dt.strftime("%b %-d, %H:%M ET")
    except Exception:
        try:
            dt = datetime.fromtimestamp(ep, tz=timezone.utc)
            return dt.strftime("%b %d, %H:%M UTC")
        except (OverflowError, OSError, ValueError):
            return "—"


def _e(s: Any) -> str:
    """HTML-escape a value for text/attribute context."""
    return html.escape(str(s), quote=True)


# =============================================================================
# data gathering
# =============================================================================
def gather_sessions() -> tuple[dict[str, Any] | None, list[tuple[float, float]], int, int]:
    """Read every ``runs/<session>/summary.json``.

    Returns ``(latest_summary, equity_points, session_days, trade_rows)`` where
    equity_points is the week's ``(epoch, equity)`` series across all sessions
    (chronological), session_days is the count of distinct trading dates, and
    trade_rows is the total logged order rows across all sessions.
    """
    summaries: list[tuple[str, dict[str, Any]]] = []
    if RUNS.exists():
        for sub in sorted(RUNS.glob("*-session")):
            data = _load_json(sub / "summary.json")
            if isinstance(data, dict):
                summaries.append((sub.name, data))

    latest = summaries[-1][1] if summaries else None

    points: list[tuple[float, float]] = []
    for _name, data in summaries:
        for pt in data.get("equity_points") or []:
            if not isinstance(pt, dict):
                continue
            ep = _epoch(pt.get("t"))
            eq = pt.get("equity")
            if ep is None or eq is None:
                continue
            try:
                points.append((ep, float(eq)))
            except (TypeError, ValueError):
                continue
    points.sort(key=lambda p: p[0])

    days: set[str] = set()
    if RUNS.exists():
        for sub in RUNS.glob("*-session"):
            if (sub / "summary.json").exists() or (sub / "cycles.jsonl").exists():
                days.add(sub.name[:8])  # YYYYMMDD prefix

    trade_rows = 0
    if RUNS.exists():
        for sub in RUNS.glob("*-session"):
            csv_path = sub / "order_log.csv"
            if not csv_path.exists():
                continue
            try:
                with csv_path.open("r", newline="", encoding="utf-8") as fh:
                    rows = list(csv.reader(fh))
                trade_rows += max(0, len(rows) - 1)  # minus header
            except OSError:
                continue

    return latest, points, len(days), trade_rows


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
            if data.get("UNSIGNED") or data.get("schema", "").startswith("quaestor.receipt-lite"):
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
                ep = p.stat().st_mtime if p.exists() else 0.0
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
    """Return ``(entry_count, chain_head_hash)`` for a bulla run/exec ledger."""
    entries = _iter_jsonl(path)
    if not entries:
        return 0, None
    return len(entries), entries[-1].get("hash")


def anchor_head() -> tuple[int, str | None, str | None]:
    """Return ``(entry_count, head_hash, time_label)`` for runs/anchor.jsonl.

    The anchor file is written by quaestor.anchor (self-chaining ledger heads);
    its exact field names may vary, so head/time are read defensively.
    """
    entries = _iter_jsonl(RUNS / "anchor.jsonl")
    if not entries:
        return 0, None, None
    last = entries[-1]
    head = None
    for key in ("hash", "anchor_hash", "head", "ledger_head", "chain_head"):
        if last.get(key):
            head = str(last[key])
            break
    time_label: str | None = None
    for key in ("ts", "created_epoch", "epoch", "time", "timestamp", "created_utc", "witnessed_at"):
        if last.get(key) is not None:
            ep = _epoch(last[key])
            if ep is not None:
                time_label = _et_label(ep)
            else:
                time_label = str(last[key])
            break
    return len(entries), head, time_label


def policy_digest() -> str | None:
    """Live quaestor trading-policy digest (configs/policy.yaml sha256), if loadable."""
    try:
        from quaestor.config import load_policy

        return str(load_policy().get("digest") or "") or None
    except Exception:
        return None


def pick_sample_receipt() -> tuple[str, str]:
    """Return ``(name, json_text)`` of the sample receipt to embed.

    Newest ``receipts/sealed-*.json`` wins; else newest ``receipts/c-*.json``;
    else a minimal empty object so the page still loads.
    """
    def newest(glob: str) -> Path | None:
        if not RECEIPTS.exists():
            return None
        cands = [p for p in RECEIPTS.glob(glob) if p.is_file()]
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
# equity curve (inline SVG area chart)
# =============================================================================
def build_equity_svg(points: list[tuple[float, float]]) -> str:
    """A faint-grid area chart with a green fill and an emphasized endpoint.

    Handles 0 points (caller renders a placeholder instead), 1 point (flat
    baseline), and N points. Uses a fixed viewBox and scales responsively.
    """
    w, h = 920.0, 300.0
    ml, mr, mt, mb = 62.0, 22.0, 24.0, 34.0
    pw, ph = w - ml - mr, h - mt - mb

    series = list(points)
    if len(series) == 1:  # a single reading -> draw it as a flat baseline
        t0, eq = series[0]
        series = [(t0 - 1.0, eq), (t0, eq)]

    ts = [p[0] for p in series]
    eqs = [p[1] for p in series]
    tmin, tmax = min(ts), max(ts)
    emin, emax = min(eqs), max(eqs)

    flat = (emax - emin) < 1e-9
    if flat:
        pad = max(1.0, abs(emin) * 0.001)
        ylo, yhi = emin - pad, emax + pad
    else:
        span = emax - emin
        ylo, yhi = emin - span * 0.18, emax + span * 0.18

    def sx(t: float) -> float:
        if tmax - tmin < 1e-9:
            return ml + pw
        return ml + (t - tmin) / (tmax - tmin) * pw

    def sy(v: float) -> float:
        if yhi - ylo < 1e-9:
            return mt + ph / 2.0
        return mt + (yhi - v) / (yhi - ylo) * ph

    pts = [(sx(t), sy(v)) for t, v in series]
    baseline = mt + ph

    line_d = "M " + " L ".join(f"{x:.2f},{y:.2f}" for x, y in pts)
    area_d = (
        f"M {pts[0][0]:.2f},{baseline:.2f} "
        + "L " + " L ".join(f"{x:.2f},{y:.2f}" for x, y in pts)
        + f" L {pts[-1][0]:.2f},{baseline:.2f} Z"
    )

    # gridlines + y labels
    grid_parts: list[str] = []
    if flat:
        rows = [(mt + ph / 2.0, series[0][1], True),
                (mt + ph * 0.18, None, False),
                (mt + ph * 0.82, None, False)]
    else:
        rows = []
        for i in range(4):
            frac = i / 3.0
            yv = yhi - frac * (yhi - ylo)
            rows.append((mt + frac * ph, yv, True))
    for gy, val, labelled in rows:
        grid_parts.append(
            f'<line x1="{ml:.1f}" y1="{gy:.2f}" x2="{ml + pw:.1f}" y2="{gy:.2f}" '
            f'class="grid"/>'
        )
        if labelled and val is not None:
            grid_parts.append(
                f'<text x="{ml - 10:.1f}" y="{gy + 3.5:.2f}" class="ylab" '
                f'text-anchor="end">{_e(f"${val:,.0f}")}</text>'
            )

    ex, ey = pts[-1]
    endpoint = (
        f'<circle cx="{ex:.2f}" cy="{ey:.2f}" r="9" class="end-halo"/>'
        f'<circle cx="{ex:.2f}" cy="{ey:.2f}" r="4.5" class="end-dot"/>'
    )

    return f"""<svg viewBox="0 0 {w:.0f} {h:.0f}" class="equity" role="img"
     aria-label="agent equity curve" preserveAspectRatio="none">
  <defs>
    <linearGradient id="equityFill" x1="0" y1="0" x2="0" y2="1">
      <stop offset="0%" stop-color="{VERIFIED}" stop-opacity="0.34"/>
      <stop offset="100%" stop-color="{VERIFIED}" stop-opacity="0.02"/>
    </linearGradient>
  </defs>
  {''.join(grid_parts)}
  <path d="{area_d}" fill="url(#equityFill)" stroke="none"/>
  <path d="{line_d}" class="equity-line" fill="none"/>
  {endpoint}
</svg>"""


# =============================================================================
# HTML assembly
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


CSS = """
  :root {
    --bg:#0b0e15; --surface:#131826; --surface-2:#0f1420; --line:#232a3d;
    --ink:#e8ecf6; --muted:#8b96ad; --faint:#5a6580;
    --accent:#6a7cff; --verified:#35c98c; --forgery:#ff6459; --hash:#5fd6e4;
    --sans:"IBM Plex Sans",-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif;
    --mono:"IBM Plex Mono",ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;
  }
  @media (prefers-color-scheme: light) {
    :root {
      --bg:#f5f7fb; --surface:#ffffff; --surface-2:#eef1f7; --line:#dde3ee;
      --ink:#12151f; --muted:#5a6478; --faint:#8b94a8;
      --accent:#4b5cf0; --verified:#0f9d63; --forgery:#d94434; --hash:#0f8fa6;
    }
  }
  * { box-sizing:border-box; }
  html { -webkit-text-size-adjust:100%; }
  body {
    margin:0; background:var(--bg); color:var(--ink);
    font:15px/1.6 var(--sans); letter-spacing:-0.003em;
    background-image:
      radial-gradient(1200px 520px at 80% -8%, rgba(106,124,255,0.10), transparent 60%),
      radial-gradient(900px 500px at 0% 0%, rgba(53,201,140,0.06), transparent 55%);
    background-repeat:no-repeat;
  }
  a { color:var(--accent); text-decoration:none; }
  a:hover { text-decoration:underline; }
  .wrap { max-width:960px; margin:0 auto; padding:40px 22px 72px; }

  .brand { display:flex; align-items:center; gap:10px; color:var(--muted);
    font:600 13px/1 var(--mono); letter-spacing:0.06em; text-transform:uppercase; }
  .brand .dot { width:9px; height:9px; border-radius:50%; background:var(--verified);
    box-shadow:0 0 0 4px rgba(53,201,140,0.16); }

  .hero { margin:26px 0 8px; }
  .hero h1 { font-size:clamp(28px,4.6vw,46px); line-height:1.06; margin:0 0 14px;
    letter-spacing:-0.022em; font-weight:600; max-width:16ch; }
  .hero h1 .g { color:var(--verified); }
  .hero p.lede { color:var(--muted); font-size:16.5px; margin:0 0 26px; max-width:62ch; }

  .stats { display:grid; grid-template-columns:repeat(auto-fit,minmax(140px,1fr));
    gap:14px; margin:22px 0 6px; }
  .stat { background:var(--surface); border:1px solid var(--line); border-radius:14px;
    padding:16px 16px 15px; position:relative; overflow:hidden; }
  .stat .k { color:var(--muted); font:600 11px/1 var(--mono); letter-spacing:0.08em;
    text-transform:uppercase; margin-bottom:9px; }
  .stat .v { font-size:26px; font-weight:600; letter-spacing:-0.02em; }
  .stat .v.sub { font-size:14px; color:var(--muted); font-weight:500; margin-top:3px;
    letter-spacing:0; }
  .stat.pl-pos .v { color:var(--verified); }
  .stat.pl-neg .v { color:var(--forgery); }

  .sealbar { display:inline-flex; align-items:center; gap:8px; margin-top:20px;
    padding:7px 13px; border-radius:999px; border:1px solid var(--line);
    background:var(--surface); color:var(--verified); font:600 12.5px/1 var(--mono);
    letter-spacing:0.03em; }
  .sealbar svg { width:14px; height:14px; }

  section.card { background:var(--surface); border:1px solid var(--line);
    border-radius:18px; padding:24px 24px 26px; margin:22px 0; }
  section.card > h2 { font-size:13px; margin:0 0 4px; color:var(--muted);
    font-family:var(--mono); font-weight:600; letter-spacing:0.10em; text-transform:uppercase; }
  section.card > .h2sub { color:var(--faint); font-size:13.5px; margin:0 0 18px; }

  /* equity chart */
  .chartwrap { margin-top:6px; }
  svg.equity { width:100%; height:auto; display:block; overflow:visible; }
  svg.equity .grid { stroke:var(--line); stroke-width:1; stroke-dasharray:2 5; opacity:0.7; }
  svg.equity .ylab { fill:var(--faint); font:500 11px var(--mono); }
  svg.equity .equity-line { stroke:var(--verified); stroke-width:2.4;
    stroke-linejoin:round; stroke-linecap:round; }
  svg.equity .end-dot { fill:var(--verified); stroke:var(--bg); stroke-width:2; }
  svg.equity .end-halo { fill:var(--verified); opacity:0.18; }
  .chartcap { display:flex; justify-content:space-between; align-items:baseline;
    color:var(--faint); font:500 12px/1.4 var(--mono); margin-top:12px; flex-wrap:wrap; gap:6px; }
  .empty { color:var(--muted); font-size:15px; padding:34px 0 30px; text-align:center;
    border:1px dashed var(--line); border-radius:12px; background:var(--surface-2); }

  /* provenance */
  .prov { display:grid; grid-template-columns:1fr 1fr; gap:0; border:1px solid var(--line);
    border-radius:12px; overflow:hidden; }
  .prov .row { display:flex; flex-direction:column; gap:6px; padding:15px 16px;
    border-bottom:1px solid var(--line); background:var(--surface); }
  .prov .row:nth-child(odd) { border-right:1px solid var(--line); }
  .prov .row .k { color:var(--muted); font:600 11px/1 var(--mono); letter-spacing:0.06em;
    text-transform:uppercase; }
  .prov .row .v { font-size:15px; font-weight:600; }
  .prov .row .hash { font-family:var(--mono); font-size:12.5px; font-weight:500;
    color:var(--hash); word-break:break-all; }
  .prov .row .muted { color:var(--muted); font-weight:500; font-size:12.5px; }
  @media (max-width:620px){ .prov { grid-template-columns:1fr; }
    .prov .row:nth-child(odd){ border-right:none; } }

  /* verifier */
  textarea { width:100%; min-height:240px; resize:vertical; border:1px solid var(--line);
    border-radius:12px; background:var(--surface-2); color:var(--ink);
    font:12.5px/1.5 var(--mono); padding:14px; }
  textarea:focus { outline:none; border-color:var(--accent); }
  .row { display:flex; gap:10px; flex-wrap:wrap; margin-top:14px; }
  button { border:1px solid var(--line); background:var(--surface-2); color:var(--ink);
    border-radius:10px; padding:11px 18px; font:600 14px var(--sans); cursor:pointer;
    transition:filter .12s ease, border-color .12s ease; }
  button:hover { filter:brightness(1.12); }
  button.primary { background:var(--accent); border-color:var(--accent); color:#fff; }
  button.ghost { color:var(--muted); }
  .verdict { display:none; margin-top:18px; }
  .verdict.show { display:block; }
  .banner { border-radius:12px; padding:15px 17px; font-weight:600; font-size:16px;
    border:1px solid; display:flex; align-items:center; gap:10px; }
  .banner.ok { color:var(--verified); border-color:var(--verified);
    background:rgba(53,201,140,0.10); }
  .banner.bad { color:var(--forgery); border-color:var(--forgery);
    background:rgba(255,100,89,0.10); }
  table.checks { width:100%; border-collapse:collapse; margin-top:14px; font-size:13.5px; }
  table.checks td { padding:8px 8px; border-bottom:1px solid var(--line); }
  table.checks td.k { color:var(--muted); width:210px; }
  table.checks .chk { font-family:var(--mono); }
  table.checks .pass { color:var(--verified); } table.checks .fail { color:var(--forgery); }
  .notes { margin-top:12px; font-family:var(--mono); font-size:12px; color:var(--faint);
    white-space:pre-wrap; }
  code { font-family:var(--mono); background:var(--surface-2); padding:1px 6px;
    border-radius:5px; font-size:12.5px; }

  .disclosure { color:var(--muted); font-size:13px; line-height:1.7; }
  .disclosure b { color:var(--ink); }
  .foot { color:var(--faint); font:500 12px/1.6 var(--mono); margin-top:30px;
    padding-top:18px; border-top:1px solid var(--line); }
"""

CHECK_SVG = (
    '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" '
    'stroke-linecap="round" stroke-linejoin="round"><path d="M20 6 9 17l-5-5"/></svg>'
)

JS = r"""
  const WASM_B64 = "__WASM_B64__";
  const SAMPLE = __SAMPLE_JSON__;
  const CHECK = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.6" stroke-linecap="round" stroke-linejoin="round" style="width:18px;height:18px"><path d="M20 6 9 17l-5-5"/></svg>';
  const CROSS = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.6" stroke-linecap="round" stroke-linejoin="round" style="width:18px;height:18px"><path d="M18 6 6 18M6 6l12 12"/></svg>';
  const $ = id => document.getElementById(id);
  const esc = s => String(s).replace(/[&<>]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]));

  const sampleText = JSON.stringify(SAMPLE, null, 2);
  $("input").value = sampleText;

  const bytes = Uint8Array.from(atob(WASM_B64), c => c.charCodeAt(0));
  let ready = false;
  wasm_bindgen(bytes).then(() => {
    ready = true;
    $("wasmfoot").textContent = "bulla-wasm v" + wasm_bindgen.version() + " · verification runs locally in this tab, offline";
  }).catch(e => {
    $("wasmfoot").textContent = "failed to load verifier wasm: " + e;
  });

  $("reset").onclick = () => { $("input").value = sampleText; $("verdict").className = "verdict"; };
  $("tamper").onclick = () => {
    try {
      const o = JSON.parse($("input").value || sampleText);
      if (o.body && o.body.outcome) {
        o.body.outcome.exit_code = (o.body.outcome.exit_code || 0) + 99;
      } else if (o.body) {
        o.body.__tampered = true;
      }
      $("input").value = JSON.stringify(o, null, 2);
    } catch (e) { alert("Load a valid receipt first."); }
  };
  $("verify").onclick = () => {
    if (!ready) return;
    const raw = ($("input").value || "").trim();
    if (!raw) { $("input").value = sampleText; return; }
    let rep;
    try { rep = JSON.parse(wasm_bindgen.verify_receipt(raw)); }
    catch (e) { rep = { ok_json:false, error:String(e) }; }
    render(rep);
  };

  function render(r) {
    const v = $("verdict"); v.className = "verdict show";
    if (!r.ok_json) {
      v.innerHTML = '<div class="banner bad">' + CROSS + 'NOT A RECEIPT</div>'
        + '<div class="notes">' + esc(r.error || "unparseable") + '</div>';
      return;
    }
    const intact = r.intact;
    const banner = intact
      ? '<div class="banner ok">' + CHECK + 'INTACT — signature, body digest and event chain all verify</div>'
      : '<div class="banner bad">' + CROSS + 'FORGERY DETECTED — this record does not verify</div>';
    const rows = [
      ["signature (Ed25519)", r.sig_ok],
      ["body digest (sha256)", r.digest_ok],
      ["event hash-chain", r.chain_ok],
      ["seal held (hermetic)", r.seal_ok],
    ].map(([k, ok]) => '<tr><td class="k">' + k + '</td><td class="chk ' + (ok?'pass':'fail')
        + '">' + (ok ? 'ok' : 'FAIL') + '</td></tr>').join("");
    const meta = '<tr><td class="k">command</td><td><code>' + esc(r.command || '') + '</code></td></tr>'
      + '<tr><td class="k">exit</td><td>' + esc(String(r.exit_kind)) + ':' + esc(String(r.exit_code)) + '</td></tr>'
      + '<tr><td class="k">signing key</td><td><code>' + esc((r.pubkey||'').slice(0,32)) + '…</code></td></tr>';
    const notes = (r.notes && r.notes.length)
      ? '<div class="notes">' + r.notes.map(esc).join("\n") + '</div>' : '';
    v.innerHTML = banner + '<table class="checks">' + rows + meta + '</table>' + notes;
  }
"""


def _stat(k: str, v: str, cls: str = "", sub: str | None = None) -> str:
    sub_html = f'<div class="v sub">{_e(sub)}</div>' if sub else ""
    return (
        f'<div class="stat {cls}"><div class="k">{_e(k)}</div>'
        f'<div class="v">{_e(v)}</div>{sub_html}</div>'
    )


def _prov_row(k: str, value_html: str) -> str:
    return f'<div class="row"><div class="k">{_e(k)}</div>{value_html}</div>'


def build_page() -> str:
    """Assemble the full track-record HTML document."""
    glue, wasm_b64, _pkg = load_wasm()
    latest, points, days, trade_rows = gather_sessions()
    rc = classify_receipts()
    dec_count, dec_head = ledger_head(RECEIPTS / "ledger.jsonl")
    seal_count, seal_hd = ledger_head(RECEIPTS / "sealed-ledger.jsonl")
    anchor_n, anchor_hd, anchor_time = anchor_head()
    pol_digest = policy_digest()
    sample_name, sample_text = pick_sample_receipt()

    have_week = bool(points) or bool(latest)

    # ---- headline numbers -----------------------------------------------------
    if points:
        first_eq, last_eq = points[0][1], points[-1][1]
    elif latest:
        first_eq = latest.get("equity_open") or 0.0
        last_eq = latest.get("equity_last") or first_eq
    else:
        first_eq = last_eq = 0.0

    # Week baseline: portfolio_state week_open_equity is authoritative; fall back
    # to the first observed equity.
    pstate = _load_json(RUNS / "portfolio_state.json")
    week_open = first_eq
    if isinstance(pstate, dict) and isinstance(pstate.get("week_open_equity"), (int, float)):
        week_open = float(pstate["week_open_equity"])
    pnl_abs = last_eq - week_open
    pnl_pct = (pnl_abs / week_open * 100.0) if week_open else 0.0
    pl_cls = "pl-pos" if pnl_abs >= 0 else "pl-neg"

    cycles_total = 0
    if RUNS.exists():
        for sub in RUNS.glob("*-session"):
            s = _load_json(sub / "summary.json")
            if isinstance(s, dict):
                cycles_total += int(s.get("cycles") or 0)

    # ---- hero -----------------------------------------------------------------
    all_sealed = rc["total"] > 0 and rc["seal_held"] == rc["total"]
    sealbar = ""
    if rc["total"] > 0:
        label = (
            f"all {rc['total']} receipts sealed · seal held"
            if all_sealed
            else f"{rc['seal_held']}/{rc['total']} receipts seal-held"
        )
        sealbar = f'<div class="sealbar">{CHECK_SVG}{_e(label)}</div>'

    stats_html = (
        _stat("Equity", _money(last_eq))
        + _stat("Week P&L", _pct(pnl_pct), cls=pl_cls, sub=_money(pnl_abs))
        + _stat("Trades", str(trade_rows))
        + _stat("Cycles", str(cycles_total))
        + _stat("Days", str(days))
        + _stat("Signed receipts", str(rc["total"]))
    )

    hero = f"""
  <div class="hero">
    <h1>An autonomous agent's track record you can <span class="g">verify, not trust</span>.</h1>
    <p class="lede">Every decision and every fill this agent made is wrapped in a
       cryptographically signed, hash-chained receipt and anchored to an external
       witness. The numbers below aren't a claim — they're a proof you can check
       yourself, in this browser, offline.</p>
    <div class="stats">{stats_html}</div>
    {sealbar}
  </div>"""

    # ---- equity chart ---------------------------------------------------------
    if points:
        chart_inner = build_equity_svg(points)
        t0, t1 = points[0][0], points[-1][0]
        cap_left = f"{_et_label(t0)} → {_et_label(t1)}"
        cap_right = f"{len(points)} equity marks · endpoint {_money(last_eq)}"
        chart_body = (
            f'<div class="chartwrap">{chart_inner}</div>'
            f'<div class="chartcap"><span>{_e(cap_left)}</span>'
            f'<span>{_e(cap_right)}</span></div>'
        )
    else:
        chart_body = (
            '<div class="empty">No trading week recorded yet — the equity curve '
            'appears here once the agent runs its first cycle. The verifier below '
            'already works on the embedded sample receipt.</div>'
        )

    chart = f"""
  <section class="card">
    <h2>Equity curve</h2>
    <p class="h2sub">Paper account equity across the recorded week (marked to market each cycle).</p>
    {chart_body}
  </section>"""

    # ---- provenance -----------------------------------------------------------
    def hashrow(k: str, h: str | None, suffix: str = "") -> str:
        if h:
            inner = (
                f'<div class="hash" title="{_e(h)}">{_e(_short(h, 14, 10))}</div>'
                + (f'<div class="muted">{_e(suffix)}</div>' if suffix else "")
            )
        else:
            inner = f'<div class="muted">{_e(suffix or "—")}</div>'
        return _prov_row(k, inner)

    prov_rows = (
        _prov_row(
            "Signed receipts",
            f'<div class="v">{rc["total"]}</div>'
            f'<div class="muted">{rc["sealed"]} sealed-execution · {rc["decision"]} decision'
            + (f' · {rc["unsigned"]} unsigned' if rc["unsigned"] else "")
            + "</div>",
        )
        + _prov_row(
            "Seal held (hermetic)",
            f'<div class="v">{rc["seal_held"]} / {rc["total"]}</div>'
            f'<div class="muted">net-namespaced, seccomp-filtered cells</div>',
        )
        + hashrow(
            "Sealed-execution ledger",
            seal_hd,
            f"{seal_count} entries" if seal_count else "no sealed cycles yet",
        )
        + hashrow(
            "Decision run ledger",
            dec_head,
            f"{dec_count} entries" if dec_count else "no entries yet",
        )
        + hashrow(
            "External anchor",
            anchor_hd,
            (f"{anchor_n} anchored · {anchor_time}" if anchor_hd else "not yet anchored"),
        )
        + hashrow("Signing key (Ed25519)", rc["pubkey"], "receipt author")
        + hashrow("Trading policy digest", pol_digest, "configs/policy.yaml")
        + _prov_row(
            "Verifier",
            '<div class="v">bulla-wasm</div>'
            '<div class="muted">embedded · runs client-side</div>',
        )
    )

    provenance = f"""
  <section class="card">
    <h2>Provenance</h2>
    <p class="h2sub">The cryptographic backbone — every hash below is recomputable from the receipts.</p>
    <div class="prov">{prov_rows}</div>
  </section>"""

    # ---- verify-it-yourself ---------------------------------------------------
    verify = f"""
  <section class="card">
    <h2>Verify it yourself</h2>
    <p class="h2sub">A real sealed receipt from this agent is loaded below. Hit
       <b>Verify this record</b> to check its Ed25519 signature, body digest and event
       hash-chain — then <b>Tamper</b> with any field and watch it reject. Sample:
       <code>{_e(sample_name)}</code>.</p>
    <textarea id="input" spellcheck="false"></textarea>
    <div class="row">
      <button class="primary" id="verify">Verify this record</button>
      <button id="tamper">Tamper: flip exit code</button>
      <button class="ghost" id="reset">Reset to sample</button>
    </div>
    <div class="verdict" id="verdict"></div>
  </section>"""

    # ---- disclosure -----------------------------------------------------------
    disclosure = f"""
  <section class="card">
    <h2>Disclosure</h2>
    <p class="disclosure">
      <b>Paper trading.</b> quaestor runs against the Alpaca paper environment
      (<code>paper-api.alpaca.markets</code>) — a fail-closed gate refuses to start
      against a live host. Fills are simulated at the NBBO touch with zero fees, so
      equity here reflects paper marks, not realized cash.<br><br>
      <b>What the proof does and doesn't say.</b> A verifying receipt proves the agent
      executed <i>exactly</i> the inputs and orders recorded, inside a hermetic cell,
      signed by the key shown above, in an unbroken chain anchored to an external
      witness. It does <i>not</i> predict future returns. Past agent performance is
      evidence of process integrity, not a guarantee of profit.<br><br>
      <b>Verification is local.</b> The check runs entirely in your browser via the
      embedded WebAssembly build of <code>bulla verify</code>. Nothing is sent
      anywhere; you can save this page and verify offline.
    </p>
  </section>"""

    body = hero + chart + provenance + verify + disclosure
    if not have_week:
        note = (
            '<section class="card"><div class="empty">No trading week recorded yet '
            '— headline figures are placeholders. Provenance and the in-browser '
            'verifier are fully live on the embedded sample receipt.</div></section>'
        )
        body = hero + note + chart + provenance + verify + disclosure

    js = JS.replace("__WASM_B64__", wasm_b64).replace(
        "__SAMPLE_JSON__", json.dumps(json.loads(sample_text))
    )

    page = f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>quaestor — verifiable track record</title>
<meta name="description" content="An autonomous options-trading agent's signed, anchored P&amp;L — verifiable in your browser.">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@400;500;600&family=IBM+Plex+Sans:wght@400;500;600&display=swap" rel="stylesheet">
<style>{CSS}</style>
</head>
<body>
<div class="wrap">
  <div class="brand"><span class="dot"></span>quaestor · provable agent performance</div>
{body}
  <p class="foot" id="wasmfoot">loading verifier…</p>
</div>
<script>{glue}</script>
<script>{js}</script>
</body>
</html>
"""
    # Guard: no sentinel tokens survived substitution.
    for token in ("__WASM_B64__", "__SAMPLE_JSON__", "__GLUE__"):
        if token in page:
            raise SystemExit(f"template token {token} was not substituted")
    return page


def main() -> None:
    page = build_page()
    DEST.parent.mkdir(parents=True, exist_ok=True)
    DEST.write_text(page, encoding="utf-8")
    size = DEST.stat().st_size
    print(f"wrote {DEST} ({size:,} bytes / {size // 1024} KB)")


if __name__ == "__main__":
    main()
