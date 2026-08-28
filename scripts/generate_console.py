"""Generate a self-contained "mission control" console (single portable HTML file).

Unlike the Streamlit judge dashboard (dashboard/app.py), this needs no server:
it reads the latest run's data and EMBEDS it — inline JSON, rendered visuals — at
generation time, so the resulting dashboard/console.html opens anywhere (double-click,
email it, USB stick) and still shows the agent's whole state.

What it embeds, from the working tree at build time:
  * latest runs/<session>/summary.json      — equity points, realized P&L, cycle/order counts
  * order_log.csv (newest non-empty)         — parsed into rows
  * runs/heartbeat.json                       — agent liveness (state, now_et, session)
  * runs/portfolio_state.json                 — day/week P&L, open equity
  * runs/<session>/positions_snapshot.json    — account equity, options level, positions
  * receipts/*.json                           — cycle id, sealed-vs-decision, body.seal_ok
  * receipts/sealed-ledger.jsonl              — sealed-execution chain head + count
  * receipts/ledger.jsonl                     — decision-run chain head + count
  * runs/anchor.jsonl (if present)            — external witness head + time

It is a STATIC snapshot: the "snapshot generated ..." stamp is read from the embedded
data's own timestamps (heartbeat / summary / orders), never from the wall clock. The
page's JS never calls Date.now()/new Date().

Run (WSL):  ~/hack/venv/bin/python scripts/generate_console.py
"""
from __future__ import annotations

import csv
import html
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
RUNS = REPO / "runs"
RECEIPTS = REPO / "receipts"
OUT = REPO / "dashboard" / "console.html"

_MONTHS = ["", "Jan", "Feb", "Mar", "Apr", "May", "Jun",
           "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


# ---------------------------------------------------------------- IO helpers
def read_json(p: Path):
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None


def read_jsonl(p: Path):
    rows = []
    try:
        for line in p.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except Exception:
                continue
    except Exception:
        return []
    return rows


def load_orders(p: Path):
    try:
        text = p.read_text(encoding="utf-8")
    except Exception:
        return []
    rows = list(csv.DictReader(text.splitlines()))
    return [r for r in rows if any((v or "").strip() for v in r.values())]


# ---------------------------------------------------------------- format helpers
def iso_to_epoch(s):
    if not s:
        return None
    try:
        return datetime.fromisoformat(str(s).replace("Z", "+00:00")).timestamp()
    except Exception:
        return None


def et(epoch):
    """UTC epoch -> 'Aug 28, 18:51 ET' (EDT = UTC-4; August is always EDT)."""
    if epoch is None:
        return "—"
    dt = datetime.fromtimestamp(float(epoch), tz=timezone.utc) - timedelta(hours=4)
    return f"{_MONTHS[dt.month]} {dt.day}, {dt:%H:%M} ET"


def et_full(epoch):
    if epoch is None:
        return "—"
    dt = datetime.fromtimestamp(float(epoch), tz=timezone.utc) - timedelta(hours=4)
    return f"{_MONTHS[dt.month]} {dt.day}, {dt.year} · {dt:%H:%M} ET"


def et_clock(epoch):
    if epoch is None:
        return "—"
    dt = datetime.fromtimestamp(float(epoch), tz=timezone.utc) - timedelta(hours=4)
    return f"{_MONTHS[dt.month]} {dt.day}, {dt:%H:%M:%S} ET"


def money(x):
    try:
        return f"${float(x):,.2f}"
    except Exception:
        return "—"


def pct(x):
    try:
        return f"{float(x):+.2f}%"
    except Exception:
        return "—"


def short_hash(h):
    if not h:
        return None
    h = str(h)
    if len(h) <= 26:
        return h
    return h[:14] + "…" + h[-10:]


def e(s):
    return html.escape("" if s is None else str(s), quote=True)


# ---------------------------------------------------------------- gather data
sessions = sorted(d for d in RUNS.iterdir() if d.is_dir() and d.name.endswith("-session")) if RUNS.exists() else []

primary = None
for d in reversed(sessions):
    if (d / "summary.json").exists():
        primary = d
        break

summary = read_json(primary / "summary.json") if primary else None
positions = read_json(primary / "positions_snapshot.json") if primary else None

# orders: newest session with a non-empty order_log.csv
orders, orders_src = [], None
for d in reversed(sessions):
    rows = load_orders(d / "order_log.csv")
    if rows:
        orders, orders_src = rows, d
        break

heartbeat = read_json(RUNS / "heartbeat.json")
portfolio = read_json(RUNS / "portfolio_state.json")

# receipts
receipt_files = sorted(RECEIPTS.glob("*.json")) if RECEIPTS.exists() else []
receipts = []
pubkey = None
policy_digest = None
egress_allow = []
for p in receipt_files:
    d = read_json(p)
    if not isinstance(d, dict) or "body" not in d:
        continue
    body = d.get("body") or {}
    egr = body.get("egress") or {}
    calls = egr.get("calls") or []
    kind = "sealed" if calls else "decision"
    seal_ok = bool(body.get("seal_ok"))
    if pubkey is None and d.get("pubkey"):
        pubkey = d.get("pubkey")
    if policy_digest is None and body.get("policy_digest"):
        policy_digest = body.get("policy_digest")
    if kind == "sealed" and not egress_allow:
        egress_allow = egr.get("allowlist") or []
    receipts.append({
        "id": p.stem,
        "kind": kind,
        "seal_ok": seal_ok,
        "chain_head": body.get("chain_head"),
        "created_epoch": body.get("created_epoch"),
        "exit_code": (body.get("outcome") or {}).get("exit_code"),
        "calls": len(calls),
    })

n_receipts = len(receipts)
n_sealed = sum(1 for r in receipts if r["kind"] == "sealed")
n_decision = n_receipts - n_sealed
n_held = sum(1 for r in receipts if r["seal_ok"])

sealed_ledger = read_jsonl(RECEIPTS / "sealed-ledger.jsonl")
decision_ledger = read_jsonl(RECEIPTS / "ledger.jsonl")
anchor = read_jsonl(RUNS / "anchor.jsonl")

sealed_head = sealed_ledger[-1].get("hash") if sealed_ledger else None
decision_head = decision_ledger[-1].get("hash") if decision_ledger else None
anchor_head = None
anchor_epoch = None
if anchor:
    last = anchor[-1]
    anchor_head = last.get("hash") or last.get("anchor") or last.get("head")
    anchor_epoch = last.get("ts") or iso_to_epoch(last.get("time") or last.get("at"))

# ---------------------------------------------------------------- status values
acct = (positions or {}).get("account") or {}
equity = None
for cand in (summary.get("equity_last") if summary else None,
             acct.get("equity"),
             (portfolio or {}).get("day_open_equity")):
    if isinstance(cand, (int, float)):
        equity = float(cand)
        break

day_pnl_pct = (portfolio or {}).get("day_pnl_pct")
week_pnl_pct = (portfolio or {}).get("week_pnl_pct")
realized_today = (portfolio or {}).get("realized_pnl_today")
realized_week = None  # not tracked separately; show week %/open only
opt_level = acct.get("options_trading_level")
opt_level_lbl = f"L{opt_level}" if isinstance(opt_level, int) else "—"
open_positions = acct.get("positions")
n_positions = len(open_positions) if isinstance(open_positions, list) else 0
cycles = (summary or {}).get("cycles")

# ---------------------------------------------------------------- snapshot stamp (from data, never wall clock)
epochs = []
if heartbeat and isinstance(heartbeat.get("ts"), (int, float)):
    epochs.append(heartbeat["ts"])
if summary:
    epochs.append(iso_to_epoch(summary.get("generated_at")))
    for pt in summary.get("equity_points") or []:
        if isinstance(pt.get("t"), (int, float)):
            epochs.append(pt["t"])
if portfolio and isinstance(portfolio.get("saved_at"), (int, float)):
    epochs.append(portfolio["saved_at"])
for r in orders:
    epochs.append(iso_to_epoch(r.get("timestamp")))
for r in receipts:
    if isinstance(r.get("created_epoch"), (int, float)):
        epochs.append(r["created_epoch"])
epochs = [x for x in epochs if isinstance(x, (int, float))]
snapshot_epoch = max(epochs) if epochs else None
hb_epoch = heartbeat.get("ts") if heartbeat else None


# ---------------------------------------------------------------- equity SVG
def build_equity_svg(points):
    W, H = 920, 300
    padL, padR, padT, padB = 66, 26, 26, 34
    x0, x1 = padL, W - padR
    y0, y1 = padT, H - padB
    plotH = y1 - y0
    pts = [(p.get("t"), p.get("equity")) for p in (points or [])
           if isinstance(p.get("equity"), (int, float))]
    if not pts:
        return None
    ys = [v for _, v in pts]
    ymin, ymax = min(ys), max(ys)
    flat = ymax == ymin
    if flat:
        pad = max(abs(ymax) * 0.0008, 1.0)
        lo, hi = ymin - pad, ymax + pad
    else:
        span = ymax - ymin
        lo, hi = ymin - span * 0.18, ymax + span * 0.18

    def sy(v):
        return y1 - (v - lo) / (hi - lo) * plotH

    ts = [t for t, _ in pts if isinstance(t, (int, float))]
    tmin, tmax = (min(ts), max(ts)) if ts else (0, 0)

    def sx(t):
        if not ts or tmax == tmin:
            return x1
        return x0 + (t - tmin) / (tmax - tmin) * (x1 - x0)

    if len(pts) == 1:
        y = sy(pts[0][1])
        coords = [(x0, y), (x1, y)]
    else:
        coords = [(sx(t), sy(v)) for t, v in pts]

    line = "M " + " L ".join(f"{x:.2f},{y:.2f}" for x, y in coords)
    area = (f"M {coords[0][0]:.2f},{y1:.2f} L "
            + " L ".join(f"{x:.2f},{y:.2f}" for x, y in coords)
            + f" L {coords[-1][0]:.2f},{y1:.2f} Z")

    parts = [f'<svg viewBox="0 0 {W} {H}" class="equity" role="img" '
             'aria-label="agent equity curve" preserveAspectRatio="none">',
             '<defs><linearGradient id="eqFill" x1="0" y1="0" x2="0" y2="1">'
             '<stop offset="0%" stop-color="#35c98c" stop-opacity="0.30"/>'
             '<stop offset="100%" stop-color="#35c98c" stop-opacity="0.02"/>'
             '</linearGradient></defs>']
    mid = (hi + lo) / 2.0
    grid_rows = [(y0, hi if not flat else None),
                 (y0 + plotH / 2.0, pts[-1][1] if flat else mid),
                 (y1, lo if not flat else None)]
    for yy, lab in grid_rows:
        parts.append(f'<line x1="{x0}" y1="{yy:.2f}" x2="{x1}" y2="{yy:.2f}" class="grid"/>')
        if lab is not None:
            parts.append(f'<text x="{x0 - 10}" y="{yy + 3.5:.2f}" class="ylab" '
                         f'text-anchor="end">{e(money(lab))}</text>')
    parts.append(f'<path d="{area}" fill="url(#eqFill)" stroke="none"/>')
    parts.append(f'<path d="{line}" class="equity-line" fill="none"/>')
    ex, ey = coords[-1]
    parts.append(f'<circle cx="{ex:.2f}" cy="{ey:.2f}" r="9" class="end-halo"/>')
    parts.append(f'<circle cx="{ex:.2f}" cy="{ey:.2f}" r="4.5" class="end-dot"/>')
    parts.append("</svg>")
    return {
        "svg": "".join(parts),
        "tmin": tmin, "tmax": tmax,
        "n": len(pts), "last": pts[-1][1],
    }


eq = build_equity_svg((summary or {}).get("equity_points"))


# ---------------------------------------------------------------- status pill
def pill_info():
    if not heartbeat:
        return ("idle", "IDLE", "no heartbeat recorded")
    s = str(heartbeat.get("state") or "").lower()
    sub = f"last heartbeat {et_clock(hb_epoch)}" if hb_epoch else "heartbeat recorded"
    if s in {"open", "running", "awake", "live", "trading", "loop", "once", "active"}:
        return ("live", (s.upper() or "LIVE"), sub)
    if s in {"closed", "close"}:
        return ("closed", "MARKET CLOSED", sub)
    if s in {"sleeping", "waiting", "pre", "post", "premarket", "afterhours", "paused"}:
        return ("wait", s.upper(), sub)
    return ("idle", (s.upper() if s else "IDLE"), sub)


pill_cls, pill_label, pill_sub = pill_info()


# ---------------------------------------------------------------- orders rows
def status_class(s):
    s = (s or "").lower()
    if s in {"filled", "partially_filled", "partial_fill"}:
        return "ok"
    if s in {"rejected", "expired", "suspended", "stopped"}:
        return "bad"
    if s in {"new", "accepted", "pending_new", "pending", "held",
             "accepted_for_bidding", "calculated", "pending_cancel"}:
        return "live"
    return "muted"  # canceled, done_for_day, replaced, etc.


def order_rows_html():
    if not orders:
        return ('<div class="empty">No orders logged in this session yet — '
                'the agent minted decision cycles without placing a trade. '
                'Order rows appear here once it fires.</div>')
    body = []
    for r in orders:
        ts = iso_to_epoch(r.get("timestamp"))
        sym = r.get("symbol") or "—"
        side = (r.get("side") or "").strip() or "—"
        typ = (r.get("type") or "").strip() or (r.get("action") or "").strip() or "—"
        lim = r.get("limit_price")
        lim_s = money(lim) if (lim not in (None, "", "0", "0.0")) else (money(lim) if lim not in (None, "") else "—")
        status = (r.get("status") or "—").strip()
        try:
            fq = float(r.get("filled_qty") or 0)
        except Exception:
            fq = 0.0
        try:
            fp = float(r.get("filled_avg_price") or 0)
        except Exception:
            fp = 0.0
        qty = (r.get("qty") or "").strip()
        if fq > 0:
            fill = f"{fq:g} @ {money(fp)}"
        else:
            fill = f"0 / {qty}" if qty else "0"
        body.append(
            "<tr>"
            f'<td class="mono t">{e(et_clock(ts))}</td>'
            f'<td class="mono sym">{e(sym)}</td>'
            f'<td class="side {e(side.lower())}">{e(side)}</td>'
            f'<td class="mono ty">{e(typ)}</td>'
            f'<td class="mono num">{e(lim_s)}</td>'
            f'<td><span class="st {status_class(status)}">{e(status)}</span></td>'
            f'<td class="mono num">{e(fill)}</td>'
            "</tr>"
        )
    return (
        '<div class="tablewrap"><table class="orders"><thead><tr>'
        '<th>Time</th><th>Symbol</th><th>Side</th><th>Type</th>'
        '<th class="num">Limit</th><th>Status</th><th class="num">Fill</th>'
        "</tr></thead><tbody>" + "".join(body) + "</tbody></table></div>"
    )


# ---------------------------------------------------------------- provenance rows
def prov_row(k, v_html, sub=""):
    sub_html = f'<div class="muted">{sub}</div>' if sub else ""
    return f'<div class="row"><div class="k">{e(k)}</div>{v_html}{sub_html}</div>'


def hash_val(h):
    sh = short_hash(h)
    if not sh:
        return '<div class="muted">—</div>'
    return f'<div class="hashv" title="{e(h)}">{e(sh)}</div>'


prov_rows = [
    prov_row("Signed receipts", f'<div class="v">{n_receipts}</div>',
             f"{n_sealed} sealed-execution · {n_decision} decision"),
    prov_row("Seal held (hermetic)", f'<div class="v">{n_held} / {n_receipts}</div>',
             "net-namespaced, seccomp-filtered cells"),
    prov_row("Sealed-execution ledger",
             hash_val(sealed_head) if sealed_head else '<div class="muted">no sealed chain yet</div>',
             f"{len(sealed_ledger)} " + ("entry" if len(sealed_ledger) == 1 else "entries")
             if sealed_ledger else "run make once with QUAESTOR_SEALED=1"),
    prov_row("Decision-run ledger",
             hash_val(decision_head) if decision_head else '<div class="muted">no decision chain yet</div>',
             f"{len(decision_ledger)} " + ("entry" if len(decision_ledger) == 1 else "entries")
             if decision_ledger else ""),
    prov_row("External anchor",
             hash_val(anchor_head) if anchor_head else '<div class="muted">not yet anchored</div>',
             (f"{len(anchor)} anchored · {et(anchor_epoch)}" if anchor_head
              else "witness the head with make anchor")),
    prov_row("Signing key (Ed25519)", hash_val(pubkey), "receipt author"),
    prov_row("Egress allowlist",
             f'<div class="hashv">{e(egress_allow[0])}</div>' if egress_allow
             else '<div class="muted">deny-by-default</div>',
             "sealed CONNECT · deny-by-default"),
    prov_row("Trading policy digest", hash_val(policy_digest), "fail-closed paper gate"),
]

# receipts mini-list
receipt_list_rows = []
for r in sorted(receipts, key=lambda x: x.get("created_epoch") or 0, reverse=True):
    badge = ("sealed" if r["kind"] == "sealed" else "decision")
    seal = ('<span class="st ok">seal held</span>' if r["seal_ok"]
            else '<span class="st bad">seal broken</span>')
    calls = f'<span class="muted">{r["calls"]} egress</span>' if r["kind"] == "sealed" else '<span class="muted">—</span>'
    receipt_list_rows.append(
        "<tr>"
        f'<td class="mono sym">{e(r["id"])}</td>'
        f'<td><span class="badge {badge}">{badge}</span></td>'
        f"<td>{seal}</td>"
        f'<td class="mono">{e(short_hash(r["chain_head"]) or "—")}</td>'
        f'<td class="num">{calls}</td>'
        "</tr>"
    )
receipt_list_html = (
    '<div class="tablewrap"><table class="orders receipts"><thead><tr>'
    '<th>Receipt</th><th>Kind</th><th>Seal</th><th>Chain head</th><th class="num">Egress</th>'
    "</tr></thead><tbody>" + "".join(receipt_list_rows) + "</tbody></table></div>"
) if receipt_list_rows else ('<div class="empty">No signed receipts on disk yet — '
                             'run make once to mint the first.</div>')

# ---------------------------------------------------------------- equity section
if eq:
    span = f"{et(eq['tmin'])} → {et(eq['tmax'])}" if eq["tmin"] != eq["tmax"] else et(eq["tmax"])
    equity_section = (
        '<div class="chartwrap">' + eq["svg"] + "</div>"
        f'<div class="chartcap"><span>{e(span)}</span>'
        f'<span>{eq["n"]} equity mark' + ("" if eq["n"] == 1 else "s")
        + f' · endpoint {e(money(eq["last"]))}</span></div>'
    )
else:
    equity_section = ('<div class="empty">No equity marks recorded yet. Each decision '
                      'cycle marks the paper account to market; the curve renders once a '
                      'session has run.</div>')

# status tiles
tiles = [
    ("Equity", money(equity) if equity is not None else "—", "", ""),
    ("Today P&amp;L", pct(day_pnl_pct), money(realized_today) if realized_today is not None else "",
     "pl-pos" if (day_pnl_pct or 0) >= 0 else "pl-neg"),
    ("Week P&amp;L", pct(week_pnl_pct), "realized",
     "pl-pos" if (week_pnl_pct or 0) >= 0 else "pl-neg"),
    ("Options level", opt_level_lbl, "approved" if opt_level_lbl != "—" else "", ""),
    ("Open positions", str(n_positions), "flat" if n_positions == 0 else "held", ""),
]
tiles_html = ""
for k, v, sub, cls in tiles:
    sub_html = f'<div class="v sub">{e(sub)}</div>' if sub else ""
    tiles_html += (f'<div class="stat {cls}"><div class="k">{k}</div>'
                   f'<div class="v">{e(v)}</div>{sub_html}</div>')

# snapshot / provenance line for the embedded JSON
snapshot = {
    "generated_from": "static build-time embed (no server, no wall clock)",
    "snapshot_epoch": snapshot_epoch,
    "snapshot_et": et_full(snapshot_epoch),
    "account": "PA3OB6ZUD7WD",
    "primary_session": primary.name if primary else None,
    "orders_session": orders_src.name if orders_src else None,
    "heartbeat": heartbeat,
    "portfolio_state": portfolio,
    "summary": summary,
    "positions_account": acct,
    "orders": orders,
    "receipts": receipts,
    "sealed_ledger_head": sealed_head,
    "sealed_ledger_count": len(sealed_ledger),
    "decision_ledger_head": decision_head,
    "decision_ledger_count": len(decision_ledger),
    "anchor_head": anchor_head,
    "anchor_present": bool(anchor),
}
snapshot_json = json.dumps(snapshot, indent=2).replace("</", "<\\/")

primary_lbl = e(primary.name) if primary else "no session yet"
orders_lbl = e(orders_src.name) if orders_src else "—"

# ---------------------------------------------------------------- CSS (plain string; no f-string braces)
CSS = """
  :root{
    --bg:#f5f7fb; --surface:#ffffff; --surface-2:#eef1f7; --line:#dde3ee;
    --ink:#12151f; --muted:#5a6478; --faint:#8b94a8;
    --accent:#4b5cf0; --verified:#0f9d63; --forgery:#d94434; --hash:#0f8fa6;
    --sans:"IBM Plex Sans",-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif;
    --mono:"IBM Plex Mono",ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;
  }
  @media (prefers-color-scheme: dark){
    :root:not([data-theme="light"]){
      --bg:#0b0e15; --surface:#131826; --surface-2:#0f1420; --line:#232a3d;
      --ink:#e8ecf6; --muted:#8b96ad; --faint:#5a6580;
      --accent:#6a7cff; --verified:#35c98c; --forgery:#ff6459; --hash:#5fd6e4;
    }
  }
  :root[data-theme="dark"]{
    --bg:#0b0e15; --surface:#131826; --surface-2:#0f1420; --line:#232a3d;
    --ink:#e8ecf6; --muted:#8b96ad; --faint:#5a6580;
    --accent:#6a7cff; --verified:#35c98c; --forgery:#ff6459; --hash:#5fd6e4;
  }
  :root[data-theme="light"]{
    --bg:#f5f7fb; --surface:#ffffff; --surface-2:#eef1f7; --line:#dde3ee;
    --ink:#12151f; --muted:#5a6478; --faint:#8b94a8;
    --accent:#4b5cf0; --verified:#0f9d63; --forgery:#d94434; --hash:#0f8fa6;
  }
  * { box-sizing:border-box; }
  html { -webkit-text-size-adjust:100%; }
  body {
    margin:0; background:var(--bg); color:var(--ink);
    font:15px/1.6 var(--sans); letter-spacing:-0.003em;
    background-image:
      radial-gradient(1200px 520px at 82% -8%, rgba(106,124,255,0.10), transparent 60%),
      radial-gradient(900px 500px at -5% 0%, rgba(53,201,140,0.06), transparent 55%);
    background-repeat:no-repeat; background-attachment:fixed;
  }
  a { color:var(--accent); text-decoration:none; }
  a:hover { text-decoration:underline; }
  .wrap { max-width:1040px; margin:0 auto; padding:34px 22px 72px; }
  .num, .mono { font-variant-numeric:tabular-nums; }

  .top { display:flex; align-items:flex-start; justify-content:space-between; gap:16px;
    flex-wrap:wrap; }
  .brand { display:flex; align-items:center; gap:10px; color:var(--muted);
    font:600 13px/1 var(--mono); letter-spacing:0.06em; text-transform:uppercase; }
  .brand .dot { width:9px; height:9px; border-radius:50%; background:var(--verified);
    box-shadow:0 0 0 4px rgba(53,201,140,0.16); }
  .toolbar { display:flex; align-items:center; gap:10px; }
  .theme-btn { border:1px solid var(--line); background:var(--surface); color:var(--muted);
    border-radius:9px; padding:8px 12px; font:600 12px var(--mono); cursor:pointer;
    letter-spacing:0.04em; }
  .theme-btn:hover { color:var(--ink); border-color:var(--accent); }

  h1.title { font-size:clamp(24px,3.5vw,34px); line-height:1.08; margin:18px 0 6px;
    letter-spacing:-0.022em; font-weight:600; }
  h1.title .g { color:var(--verified); }
  p.lede { color:var(--muted); font-size:15.5px; margin:0 0 8px; max-width:70ch; }
  .stamp { color:var(--faint); font:500 12px/1.5 var(--mono); margin:6px 0 0;
    display:flex; gap:14px; flex-wrap:wrap; }
  .stamp b { color:var(--muted); font-weight:600; }

  /* status strip */
  .strip { display:flex; align-items:stretch; gap:14px; flex-wrap:wrap; margin:22px 0 6px; }
  .pill { display:flex; align-items:center; gap:12px; padding:14px 18px; border-radius:14px;
    border:1px solid var(--line); background:var(--surface); min-width:230px; }
  .pill .pdot { width:11px; height:11px; border-radius:50%; background:var(--faint);
    flex:none; position:relative; }
  .pill.live .pdot { background:var(--verified); box-shadow:0 0 0 4px rgba(53,201,140,0.18); }
  .pill.closed .pdot { background:var(--hash); box-shadow:0 0 0 4px rgba(95,214,228,0.16); }
  .pill.wait .pdot { background:var(--accent); box-shadow:0 0 0 4px rgba(106,124,255,0.16); }
  .pill.idle .pdot { background:var(--faint); }
  .pill.live .pdot::after { content:""; position:absolute; inset:-4px; border-radius:50%;
    border:2px solid var(--verified); opacity:0.55; animation:ping 1.8s ease-out infinite; }
  @keyframes ping { 0%{ transform:scale(0.6); opacity:0.6; } 100%{ transform:scale(2.1); opacity:0; } }
  .pill .plabel { font:600 14px/1 var(--mono); letter-spacing:0.04em; }
  .pill .psub { color:var(--faint); font:500 11px/1.3 var(--mono); margin-top:5px; }

  .stats { display:grid; grid-template-columns:repeat(auto-fit,minmax(130px,1fr));
    gap:14px; flex:1 1 480px; }
  .stat { background:var(--surface); border:1px solid var(--line); border-radius:14px;
    padding:14px 16px 13px; }
  .stat .k { color:var(--muted); font:600 11px/1 var(--mono); letter-spacing:0.08em;
    text-transform:uppercase; margin-bottom:8px; }
  .stat .v { font-size:23px; font-weight:600; letter-spacing:-0.02em; font-variant-numeric:tabular-nums; }
  .stat .v.sub { font-size:13px; color:var(--muted); font-weight:500; margin-top:3px; letter-spacing:0; }
  .stat.pl-pos .v { color:var(--verified); }
  .stat.pl-neg .v { color:var(--forgery); }

  section.card { background:var(--surface); border:1px solid var(--line);
    border-radius:18px; padding:22px 22px 24px; margin:20px 0; }
  section.card > h2 { font-size:13px; margin:0 0 4px; color:var(--muted);
    font-family:var(--mono); font-weight:600; letter-spacing:0.10em; text-transform:uppercase; }
  section.card > .h2sub { color:var(--faint); font-size:13.5px; margin:0 0 16px; }

  /* equity chart */
  svg.equity { width:100%; height:auto; display:block; overflow:visible; }
  svg.equity .grid { stroke:var(--line); stroke-width:1; stroke-dasharray:2 5; opacity:0.7; }
  svg.equity .ylab { fill:var(--faint); font:500 11px var(--mono); }
  svg.equity .equity-line { stroke:var(--verified); stroke-width:2.4;
    stroke-linejoin:round; stroke-linecap:round; }
  svg.equity .end-dot { fill:var(--verified); stroke:var(--bg); stroke-width:2; }
  svg.equity .end-halo { fill:var(--verified); opacity:0.18; }
  .chartcap { display:flex; justify-content:space-between; align-items:baseline;
    color:var(--faint); font:500 12px/1.4 var(--mono); margin-top:12px; flex-wrap:wrap; gap:6px; }

  .empty { color:var(--muted); font-size:14.5px; padding:30px 22px; text-align:center;
    border:1px dashed var(--line); border-radius:12px; background:var(--surface-2);
    max-width:70ch; margin:0 auto; }

  /* tables */
  .tablewrap { overflow-x:auto; border:1px solid var(--line); border-radius:12px; }
  table.orders { width:100%; border-collapse:collapse; font-size:13px; min-width:640px; }
  table.orders th { text-align:left; color:var(--muted); font:600 11px var(--mono);
    letter-spacing:0.06em; text-transform:uppercase; padding:11px 14px;
    border-bottom:1px solid var(--line); background:var(--surface-2); white-space:nowrap; }
  table.orders th.num, table.orders td.num { text-align:right; }
  table.orders td { padding:11px 14px; border-bottom:1px solid var(--line); white-space:nowrap; }
  table.orders tbody tr:last-child td { border-bottom:none; }
  table.orders .mono { font-family:var(--mono); font-variant-numeric:tabular-nums; }
  table.orders .sym { color:var(--hash); font-weight:500; }
  table.orders .side.buy { color:var(--verified); font-weight:600; }
  table.orders .side.sell { color:var(--forgery); font-weight:600; }
  .st { font:600 11.5px/1 var(--mono); letter-spacing:0.04em; text-transform:uppercase;
    padding:4px 9px; border-radius:999px; border:1px solid transparent; display:inline-block; }
  .st.ok { color:var(--verified); border-color:var(--verified); background:rgba(53,201,140,0.10); }
  .st.bad { color:var(--forgery); border-color:var(--forgery); background:rgba(255,100,89,0.10); }
  .st.live { color:var(--accent); border-color:var(--accent); background:rgba(106,124,255,0.10); }
  .st.muted { color:var(--faint); border-color:var(--line); background:var(--surface-2); }
  .badge { font:600 11px/1 var(--mono); letter-spacing:0.05em; text-transform:uppercase;
    padding:4px 9px; border-radius:6px; display:inline-block; }
  .badge.sealed { color:var(--verified); background:rgba(53,201,140,0.12); }
  .badge.decision { color:var(--accent); background:rgba(106,124,255,0.12); }

  /* provenance */
  .prov { display:grid; grid-template-columns:1fr 1fr; gap:0; border:1px solid var(--line);
    border-radius:12px; overflow:hidden; }
  .prov .row { display:flex; flex-direction:column; gap:6px; padding:14px 16px;
    border-bottom:1px solid var(--line); background:var(--surface); }
  .prov .row:nth-child(odd) { border-right:1px solid var(--line); }
  .prov .row .k { color:var(--muted); font:600 11px/1 var(--mono); letter-spacing:0.06em;
    text-transform:uppercase; }
  .prov .row .v { font-size:16px; font-weight:600; font-variant-numeric:tabular-nums; }
  .prov .row .hashv { font-family:var(--mono); font-size:12.5px; font-weight:500;
    color:var(--hash); word-break:break-all; }
  .prov .row .muted { color:var(--muted); font-weight:500; font-size:12.5px; }
  @media (max-width:640px){ .prov { grid-template-columns:1fr; }
    .prov .row:nth-child(odd){ border-right:none; } }

  .recmini { margin-top:16px; }

  code { font-family:var(--mono); background:var(--surface-2); padding:2px 6px;
    border-radius:5px; font-size:12.5px; }
  .cmds { display:flex; flex-direction:column; gap:8px; margin:4px 0 0; }
  .cmds .cmd { font:500 13px/1.5 var(--mono); color:var(--ink); background:var(--surface-2);
    border:1px solid var(--line); border-radius:9px; padding:10px 13px; overflow-x:auto; }
  .cmds .cmd .c { color:var(--faint); }
  .disclosure { color:var(--muted); font-size:12.5px; line-height:1.7; margin-top:6px; }
  .disclosure b { color:var(--ink); }
  .foot { color:var(--faint); font:500 12px/1.6 var(--mono); margin-top:28px;
    padding-top:16px; border-top:1px solid var(--line); }

  @media (prefers-reduced-motion: reduce){
    * { animation:none !important; transition:none !important; }
    .pill.live .pdot::after { display:none; }
  }
"""

# ---------------------------------------------------------------- HTML assembly
head = (
    '<!doctype html>\n<html lang="en">\n<head>\n'
    '<meta charset="utf-8">\n'
    '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
    "<title>quaestor mission control</title>\n"
    '<meta name="description" content="A static, self-contained console snapshot of an '
    'autonomous verifiable options-trading agent — every trade cryptographically signed.">\n'
    '<link rel="preconnect" href="https://fonts.googleapis.com">\n'
    '<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>\n'
    '<link href="https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@400;500;600&'
    'family=IBM+Plex+Sans:wght@400;500;600&display=swap" rel="stylesheet">\n'
    "<style>" + CSS + "</style>\n</head>\n<body>\n"
)

body = []
body.append('<div class="wrap">')

# top bar
body.append(
    '<div class="top">'
    '<div class="brand"><span class="dot"></span>quaestor · mission control</div>'
    '<div class="toolbar">'
    '<button class="theme-btn" id="themeBtn" type="button" aria-label="toggle theme">theme</button>'
    '</div></div>'
)

body.append(
    '<h1 class="title">The agent\'s whole state, in one <span class="g">verifiable</span> page.</h1>'
    '<p class="lede">A static snapshot of quaestor — an autonomous options-trading agent whose '
    'every decision and fill is sealed in a signed, hash-chained receipt. No server runs behind '
    'this file; the data below was embedded at build time and travels with the page.</p>'
)
body.append(
    '<div class="stamp">'
    f'<span><b>snapshot generated</b> {e(et_full(snapshot_epoch))}</span>'
    f'<span><b>account</b> PA3OB6ZUD7WD · paper</span>'
    f'<span><b>session</b> {primary_lbl}</span>'
    + (f'<span><b>orders from</b> {orders_lbl}</span>' if orders_src and orders_src is not primary else "")
    + '</div>'
)

# status strip
body.append('<div class="strip">')
body.append(
    f'<div class="pill {pill_cls}"><span class="pdot"></span>'
    f'<div><div class="plabel">{e(pill_label)}</div>'
    f'<div class="psub">{e(pill_sub)}</div></div></div>'
)
body.append('<div class="stats">' + tiles_html + "</div>")
body.append("</div>")

# equity
body.append('<section class="card"><h2>Equity curve</h2>'
            '<p class="h2sub">Paper account equity, marked to market at each decision cycle.</p>'
            + equity_section + "</section>")

# orders
body.append('<section class="card"><h2>Orders</h2>'
            '<p class="h2sub">Every order the agent logged this session — timestamp, contract, '
            'intent, limit and fill. Sealed orders carry a receipt of the exact request.</p>'
            + order_rows_html() + "</section>")

# receipts & seal
body.append('<section class="card"><h2>Receipts &amp; seal</h2>'
            '<p class="h2sub">The cryptographic backbone: each hash below is recomputable from '
            'the receipts on disk, and every seal is checkable offline.</p>'
            '<div class="prov">' + "".join(prov_rows) + "</div>"
            '<div class="recmini">' + receipt_list_html + "</div>"
            "</section>")

# footer: verify commands + disclosure
body.append(
    '<section class="card"><h2>Verify it yourself</h2>'
    '<p class="h2sub">None of this asks for trust. Re-check every signature and ledger chain '
    'offline, with no Alpaca credentials:</p>'
    '<div class="cmds">'
    '<div class="cmd"><span class="c"># re-check every receipt signature + ledger chain, offline</span><br>make verify</div>'
    '<div class="cmd">python -m quaestor verify</div>'
    '<div class="cmd"><span class="c"># or open the in-browser verifier and tamper with a receipt</span><br>open dashboard/verifier.html</div>'
    '</div>'
    '<p class="disclosure" style="margin-top:16px">'
    '<b>Not investment advice.</b> Options trading involves significant risk. '
    'Paper-trading only; simulated results. A verifying receipt proves the agent executed '
    'exactly the inputs and orders recorded, inside a hermetic cell, signed by the key shown '
    'above — it does not predict future returns. '
    'See <a href="https://alpaca.markets/disclosures">alpaca.markets/disclosures</a>.'
    '</p></section>'
)

body.append(
    '<p class="foot">quaestor · provable agent performance · '
    'python -m quaestor {status,once,loop,verify,anchor,bundle,rehearse} · '
    'this page is a static snapshot — data embedded at build, no live connection</p>'
)

# embedded snapshot JSON (portable / transparent)
body.append('<script type="application/json" id="quaestor-snapshot">'
            + snapshot_json + '</script>')

# minimal JS: theme toggle only (no clock, no Date.now)
body.append(
    "<script>\n"
    "(function(){\n"
    "  var KEY='quaestor-theme';\n"
    "  var root=document.documentElement, btn=document.getElementById('themeBtn');\n"
    "  try{ var s=localStorage.getItem(KEY); if(s){ root.setAttribute('data-theme', s); } }catch(e){}\n"
    "  function current(){\n"
    "    var t=root.getAttribute('data-theme');\n"
    "    if(t) return t;\n"
    "    return (window.matchMedia && window.matchMedia('(prefers-color-scheme: dark)').matches) ? 'dark':'light';\n"
    "  }\n"
    "  if(btn){ btn.addEventListener('click', function(){\n"
    "    var next = current()==='dark' ? 'light':'dark';\n"
    "    root.setAttribute('data-theme', next);\n"
    "    try{ localStorage.setItem(KEY, next); }catch(e){}\n"
    "  }); }\n"
    "})();\n"
    "</script>\n"
)

body.append("</div>\n</body>\n</html>\n")

out = head + "".join(body)
OUT.parent.mkdir(parents=True, exist_ok=True)
OUT.write_text(out, encoding="utf-8")

# ---------------------------------------------------------------- report
size_kb = len(out.encode("utf-8")) / 1024.0
print(f"wrote {OUT} ({size_kb:.1f} KB)")
print(f"  snapshot stamp     : {et_full(snapshot_epoch)}")
print(f"  primary session    : {primary.name if primary else '(none)'}")
print(f"  orders session     : {orders_src.name if orders_src else '(none)'} ({len(orders)} order rows)")
print(f"  equity marks        : {(eq['n'] if eq else 0)}")
print(f"  receipts embedded  : {n_receipts} ({n_sealed} sealed / {n_decision} decision), "
      f"{n_held}/{n_receipts} seal held")
print(f"  sealed ledger      : {len(sealed_ledger)} entries, head {short_hash(sealed_head)}")
print(f"  decision ledger    : {len(decision_ledger)} entries, head {short_hash(decision_head)}")
print(f"  heartbeat present  : {bool(heartbeat)} (state={heartbeat.get('state') if heartbeat else None})")
print(f"  anchor present     : {bool(anchor)}")
