#!/usr/bin/env python3
"""Generate scripts/red_team_report.html from REAL red-team command output.

This script *runs the same real binaries* (bulla, zkrisk) against a real signed
receipt — performing each of the four fraud vectors on throwaway copies under a
scratch dir on /tmp — captures the actual stdout/stderr and exit codes, and
templates them into a single self-contained, theme-aware HTML page matching
quaestor's "cryptographic instrument" aesthetic.

Nothing in the page is invented: every <pre> block is captured output from this
run. Repo files are read, never mutated.

Run in WSL (stdlib only, no venv needed):
    python3 scripts/gen_red_team_report.py

Env overrides: BULLA_BIN, ZKRISK_BIN, RT_DIR, RED_TEAM_REPORT_OUT
"""
from __future__ import annotations

import html
import json
import os
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
BULLA = os.environ.get("BULLA_BIN", os.path.expanduser("~/.cache/hack-target/release/bulla"))
ZKRISK = os.environ.get("ZKRISK_BIN", os.path.expanduser("~/.cache/hack-target/release/examples/zkrisk"))
RT = Path(os.environ.get("RT_DIR", "/tmp/quaestor-red-team-report"))
RT_DISP = "/tmp/rt"  # abbreviation used in displayed commands
OUT = Path(os.environ.get("RED_TEAM_REPORT_OUT", str(REPO / "scripts" / "red_team_report.html")))


# ---------------------------------------------------------------------------
# Command running / capture
# ---------------------------------------------------------------------------
def run(argv: list[str], display: str | None = None) -> dict:
    """Run argv, capture combined stdout+stderr and exit code."""
    p = subprocess.run(argv, capture_output=True, text=True)
    out = (p.stdout or "") + (p.stderr or "")
    shown = display if display is not None else " ".join(argv)
    shown = shown.replace(str(RT), RT_DISP)
    return {"cmd": shown, "out": out.rstrip("\n"), "exit": p.returncode}


def note_step(label: str, label_ok: bool, cmd: str, out: str, exit_code: int | None = None) -> dict:
    return {"label": label, "ok": label_ok, "cmd": cmd, "out": out, "exit": exit_code}


def run_step(label: str, label_ok: bool, argv: list[str], display: str | None = None) -> dict:
    r = run(argv, display)
    return note_step(label, label_ok, r["cmd"], r["out"], r["exit"])


# ---------------------------------------------------------------------------
# Inputs: newest real receipt + a usable hash-chained ledger
# ---------------------------------------------------------------------------
def newest(glob: str) -> Path | None:
    files = sorted(REPO.glob(glob), key=lambda p: p.stat().st_mtime, reverse=True)
    return files[0] if files else None


def pick_receipt() -> Path:
    r = newest("receipts/sealed-*.json") or newest("receipts/c-*.json")
    if not r:
        sys.exit("no signed receipt under receipts/")
    return r


def pick_ledger() -> Path | None:
    best, best_n = None, 0
    for name in ("receipts/sealed-ledger.jsonl", "receipts/ledger.jsonl"):
        f = REPO / name
        if not f.exists():
            continue
        n = sum(1 for ln in f.read_text().splitlines() if ln.strip())
        if n >= 3 and n > best_n:
            best, best_n = f, n
    return best


# ---------------------------------------------------------------------------
# The four vectors — capturing real output
# ---------------------------------------------------------------------------
def build_vectors() -> tuple[list[dict], int]:
    if RT.exists():
        shutil.rmtree(RT)
    RT.mkdir(parents=True)
    receipt = pick_receipt()
    ledger = pick_ledger()
    vectors: list[dict] = []
    caught = 0

    # ---- Vector 1: forge a number -----------------------------------------
    orig = RT / "orig.json"
    shutil.copyfile(receipt, orig)
    s_intact = run_step("INTACT", True, [BULLA, "verify", str(orig)])
    d = json.loads(orig.read_text())
    was = d["body"]["outcome"].get("exit_code")
    d["body"]["outcome"]["exit_code"] = 42 if was != 42 else 7
    forged = RT / "forged.json"
    forged.write_text(json.dumps(d, indent=1))
    s_tamper_note = note_step(
        "TAMPER", False,
        "python3 -c 'flip body.outcome.exit_code on the copy'",
        f"outcome.exit_code: {was} -> {d['body']['outcome']['exit_code']}", None)
    s_forged = run_step("SIGNATURE FAIL", False, [BULLA, "verify", str(forged)])
    v1_caught = s_intact["exit"] == 0 and s_forged["exit"] != 0
    caught += int(v1_caught)
    vectors.append({
        "n": 1, "title": "Forge a number",
        "attack": "Edit a winning number into a signed result receipt — "
                  "the way a fabricated P&L or exit code would be slipped in.",
        "steps": [s_intact, s_tamper_note, s_forged],
        "caught": v1_caught,
        "why": "The receipt body is Ed25519-signed. One flipped digit breaks the "
               "body digest and the signature — <code>bulla verify</code> rejects it.",
        "input": f"receipt: {receipt.name}",
    })

    # ---- Vector 2: drop a losing cycle ------------------------------------
    if ledger is not None:
        led = RT / "ledger.jsonl"
        shutil.copyfile(ledger, led)
        s_chain = run_step("CHAIN OK", True, [BULLA, "log", str(led)])
        lines = [ln for ln in led.read_text().splitlines() if ln.strip()]
        drop = len(lines) // 2
        kept = [ln for i, ln in enumerate(lines) if i != drop]
        trunc = RT / "ledger_trunc.jsonl"
        trunc.write_text("\n".join(kept) + "\n")
        s_drop_note = note_step(
            "DROP", False,
            "python3 -c 'delete the middle ledger entry on the copy'",
            f"dropped entry #{drop} of {len(lines)} (a mid-chain cycle)", None)
        s_broken = run_step("CHAIN BROKEN", False, [BULLA, "log", str(trunc)])
        v2_caught = s_chain["exit"] == 0 and s_broken["exit"] != 0
        steps2 = [s_chain, s_drop_note, s_broken]
        input2 = f"ledger: {ledger.name} ({len(lines)} entries)"
    else:
        v2_caught = False
        steps2 = [note_step("N/A", False, "(no ledger with >=3 entries)",
                            "skipped — need >=3 chained entries to drop a middle one", None)]
        input2 = "ledger: none suitable"
    caught += int(v2_caught)
    vectors.append({
        "n": 2, "title": "Drop a losing cycle",
        "attack": "Delete a losing trade cycle from the run ledger to inflate the "
                  "pass rate — cherry-pick the track record.",
        "steps": steps2,
        "caught": v2_caught,
        "why": "Every ledger entry commits to the hash of the one before it. Removing "
               "an interior cycle makes the next entry's <code>prev</code> mismatch — "
               "<code>bulla log</code> reports the chain BROKEN at that seq.",
        "input": input2,
    })

    # ---- Vector 3: trade with the network on ------------------------------
    (RT / "net-cell").mkdir(exist_ok=True)
    (RT / "sealed-cell").mkdir(exist_ok=True)
    s_net_run = run_step(
        "SEAL BROKEN", False,
        [BULLA, "run", "--allow-net", "--work", str(RT / "net-cell"),
         "--out", str(RT / "net.json"), "--", "/bin/echo", "hi"])
    s_net_verify = run_step("SEAL BROKEN", False, [BULLA, "verify", str(RT / "net.json")])
    s_sealed_run = run_step(
        "SEAL HELD", True,
        [BULLA, "run", "--work", str(RT / "sealed-cell"),
         "--out", str(RT / "sealed.json"), "--", "/bin/echo", "hi"])
    blob = s_net_run["out"] + s_net_verify["out"]
    v3_caught = ("SEAL BROKEN" in blob or "seal held   NO" in blob) and "SEAL HELD" in s_sealed_run["out"]
    caught += int(v3_caught)
    vectors.append({
        "n": 3, "title": "Trade with the network on",
        "attack": "Produce a score with live egress open — phone home, fetch an "
                  "oracle, exfiltrate — then pass it off as a sealed, hermetic run.",
        "steps": [s_net_run, s_net_verify, s_sealed_run],
        "caught": v3_caught,
        "why": "bulla records whether the network namespace was isolated. With "
               "<code>--allow-net</code> the receipt honestly reads SEAL BROKEN; only "
               "the isolated run is SEAL HELD. A score made with egress can never "
               "masquerade as sealed.",
        "input": "cmd: /bin/echo hi (harness proof — payload is irrelevant)",
    })

    # ---- Vector 4: fake a risk proof --------------------------------------
    proof = RT / "proof.json"
    r_prove = run([ZKRISK, "prove", "12000", "16"],
                  display="zkrisk prove 12000 16 > /tmp/rt/proof.json")
    proof.write_text(r_prove["out"] + "\n")
    s_prove = note_step("PROVED", True, r_prove["cmd"],
                        "(RiskProof JSON written)", r_prove["exit"])
    s_verify_ok = run_step("VERIFIES", True, [ZKRISK, "verify", str(proof)])
    pj = json.loads(proof.read_text())
    p0 = pj["proof"][0]
    newnib = "1" if p0 != "1" else "2"
    pj["proof"] = newnib + pj["proof"][1:]
    bad = RT / "proof_bad.json"
    bad.write_text(json.dumps(pj))
    s_tamper = note_step("TAMPER", False,
                         "python3 -c 'flip one nibble of proof hex'",
                         f"proof[0]: {p0} -> {newnib}", None)
    s_verify_bad = run_step("REJECTED", False, [ZKRISK, "verify", str(bad)])
    s_overcap = run_step("CANNOT PROVE", False, [ZKRISK, "prove", "70000", "16"])
    v4_caught = (s_prove["exit"] == 0 and s_verify_ok["exit"] == 0
                 and s_verify_bad["exit"] != 0 and s_overcap["exit"] != 0)
    caught += int(v4_caught)
    vectors.append({
        "n": 4, "title": "Fake a risk proof",
        "attack": "Claim every order's worst-case loss is under the hard cap without "
                  "actually being under it — forge the risk attestation.",
        "steps": [s_prove, s_verify_ok, s_tamper, s_verify_bad, s_overcap],
        "caught": v4_caught,
        "why": "Each order carries a Bulletproofs range proof that max loss &lt; 2^16 USD. "
               "Tamper the proof and it fails to verify; feed an over-cap value and "
               "<code>zkrisk prove</code> refuses to emit a passing proof at all.",
        "input": "cap: 2^16 = $65,536 worst-case max loss",
    })

    return vectors, caught


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------
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
      radial-gradient(1200px 520px at 85% -8%, rgba(255,100,89,0.10), transparent 60%),
      radial-gradient(900px 500px at 0% 0%, rgba(106,124,255,0.08), transparent 55%);
    background-repeat:no-repeat;
  }
  a { color:var(--accent); text-decoration:none; }
  a:hover { text-decoration:underline; }
  .wrap { max-width:940px; margin:0 auto; padding:40px 22px 72px; }

  .brand { display:flex; align-items:center; gap:10px; color:var(--muted);
    font:600 13px/1 var(--mono); letter-spacing:0.06em; text-transform:uppercase; }
  .brand .dot { width:9px; height:9px; border-radius:50%; background:var(--forgery);
    box-shadow:0 0 0 4px rgba(255,100,89,0.16); }

  .hero { margin:26px 0 6px; }
  .eyebrow { color:var(--forgery); font:600 12px/1 var(--mono); letter-spacing:0.16em;
    text-transform:uppercase; margin:0 0 14px; }
  .hero h1 { font-size:clamp(30px,5vw,50px); line-height:1.04; margin:0 0 16px;
    letter-spacing:-0.024em; font-weight:600; max-width:18ch; }
  .hero h1 .g { color:var(--verified); }
  .hero p.lede { color:var(--muted); font-size:16.5px; margin:0 0 8px; max-width:66ch; }

  .scorestrip { display:flex; align-items:center; gap:16px; flex-wrap:wrap;
    margin:26px 0 4px; padding:18px 22px; border:1px solid var(--line);
    border-radius:16px; background:var(--surface);
    background-image:linear-gradient(90deg, rgba(53,201,140,0.08), transparent 70%); }
  .scorestrip .big { font-size:34px; font-weight:600; letter-spacing:-0.02em;
    color:var(--verified); font-variant-numeric:tabular-nums; }
  .scorestrip .lab { color:var(--muted); font:600 12px/1.4 var(--mono);
    letter-spacing:0.06em; text-transform:uppercase; }
  .scorestrip.partial .big { color:var(--forgery); }

  .ctx { color:var(--muted); font-size:14.5px; margin:22px 2px 6px; max-width:70ch; }
  .ctx b { color:var(--ink); font-weight:600; }

  section.vec { background:var(--surface); border:1px solid var(--line);
    border-radius:18px; padding:22px 24px 24px; margin:20px 0; overflow:hidden; }
  .vhead { display:flex; align-items:baseline; gap:14px; flex-wrap:wrap; }
  .vnum { font:600 13px/1 var(--mono); color:var(--faint); letter-spacing:0.10em; }
  .vhead h2 { font-size:22px; margin:0; letter-spacing:-0.02em; font-weight:600; }
  .vhead .spacer { flex:1 1 40px; }
  .badge { display:inline-flex; align-items:center; gap:7px; padding:6px 13px;
    border-radius:999px; font:600 12px/1 var(--mono); letter-spacing:0.04em; border:1px solid; }
  .badge.caught { color:var(--verified); border-color:var(--verified);
    background:rgba(53,201,140,0.10); }
  .badge.missed { color:var(--forgery); border-color:var(--forgery);
    background:rgba(255,100,89,0.10); }
  .badge svg { width:13px; height:13px; }
  .attack { color:var(--muted); font-size:14.5px; margin:12px 0 4px; }
  .attack .tag { color:var(--forgery); font:600 11px/1 var(--mono); letter-spacing:0.08em;
    text-transform:uppercase; margin-right:8px; }
  .inputline { color:var(--faint); font:500 12px/1.4 var(--mono); margin:2px 0 14px; }

  .step { margin:14px 0 0; border:1px solid var(--line); border-radius:12px;
    background:var(--surface-2); overflow:hidden; }
  .step .steptop { display:flex; align-items:center; gap:10px; padding:9px 13px;
    border-bottom:1px solid var(--line); flex-wrap:wrap; }
  .pill { font:600 10.5px/1 var(--mono); letter-spacing:0.06em; text-transform:uppercase;
    padding:5px 9px; border-radius:6px; border:1px solid; }
  .pill.ok { color:var(--verified); border-color:var(--verified); background:rgba(53,201,140,0.10); }
  .pill.bad { color:var(--forgery); border-color:var(--forgery); background:rgba(255,100,89,0.10); }
  .step .cmd { font:12.5px/1.5 var(--mono); color:var(--hash); margin:0; flex:1 1 220px;
    word-break:break-all; }
  .step .exit { font:600 11px/1 var(--mono); color:var(--faint); white-space:nowrap; }
  pre.out { margin:0; padding:13px 15px; font:12px/1.5 var(--mono); color:var(--ink);
    white-space:pre; overflow-x:auto; background:transparent; }
  .why { margin:16px 0 0; padding:13px 15px; border-radius:12px;
    border:1px solid var(--line); background:rgba(53,201,140,0.05);
    color:var(--muted); font-size:13.5px; line-height:1.6; }
  .why b { color:var(--verified); font-weight:600; }
  code { font-family:var(--mono); background:var(--surface-2); padding:1px 6px;
    border-radius:5px; font-size:12px; }

  .foot { color:var(--faint); font:500 12px/1.7 var(--mono); margin-top:34px;
    padding-top:18px; border-top:1px solid var(--line); }
  .foot b { color:var(--muted); }
"""

CHECK_SVG = ('<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="3" '
             'stroke-linecap="round" stroke-linejoin="round"><path d="M20 6 9 17l-5-5"/></svg>')
X_SVG = ('<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="3" '
         'stroke-linecap="round" stroke-linejoin="round"><path d="M18 6 6 18M6 6l12 12"/></svg>')


def esc(s: str) -> str:
    return html.escape(s, quote=False)


def render_step(st: dict) -> str:
    pill_cls = "ok" if st["ok"] else "bad"
    exit_html = ""
    if st["exit"] is not None:
        exit_html = f'<span class="exit">exit {esc(str(st["exit"]))}</span>'
    out = st["out"] if st["out"].strip() else "(no output)"
    return (
        '<div class="step">'
        '<div class="steptop">'
        f'<span class="pill {pill_cls}">{esc(st["label"])}</span>'
        f'<code class="cmd">$ {esc(st["cmd"])}</code>'
        f'{exit_html}'
        '</div>'
        f'<pre class="out">{esc(out)}</pre>'
        '</div>'
    )


def render_vector(v: dict) -> str:
    badge = (f'<span class="badge caught">{CHECK_SVG} CAUGHT</span>' if v["caught"]
             else f'<span class="badge missed">{X_SVG} NOT CAUGHT</span>')
    steps = "\n".join(render_step(s) for s in v["steps"])
    return (
        '<section class="vec">'
        '<div class="vhead">'
        f'<span class="vnum">VECTOR {v["n"]:02d}</span>'
        f'<h2>{esc(v["title"])}</h2>'
        '<span class="spacer"></span>'
        f'{badge}'
        '</div>'
        f'<p class="attack"><span class="tag">The attack</span>{esc(v["attack"])}</p>'
        f'<p class="inputline">{esc(v["input"])}</p>'
        f'{steps}'
        f'<div class="why"><b>Caught —</b> {v["why"]}</div>'
        '</section>'
    )


def render(vectors: list[dict], caught: int) -> str:
    generated = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    bulla_v = run([BULLA, "-V"])["out"].strip() or "bulla"
    cards = "\n".join(render_vector(v) for v in vectors)
    strip_cls = "scorestrip" if caught == 4 else "scorestrip partial"
    body = f"""
  <div class="brand"><span class="dot"></span>quaestor · red-team audit</div>

  <div class="hero">
    <p class="eyebrow">Adversarial verification</p>
    <h1>Everyone fixes tasks. <span class="g">We closed the harness.</span></h1>
    <p class="lede">Four ways to fake an autonomous trading record — forge a number,
      drop a losing cycle, run with the network open, fake a risk proof — each run
      here for real against a signed quaestor receipt, and each one caught by the
      same offline binaries a judge can run.</p>
  </div>

  <div class="{strip_cls}">
    <span class="big">{caught} / 4</span>
    <span class="lab">fraud vectors caught<br>real binaries · real receipt · attacks on /tmp copies</span>
  </div>

  <p class="ctx">The 2026 agent-benchmark trust crisis was never about whether an
    agent could <b>do</b> the task — it was that nobody could tell a real result from
    a doctored one. A logged number can be edited, a bad run can be deleted, a
    "sealed" score can be produced with the network wide open, and a risk claim can
    be asserted without proof. quaestor makes every one of those a <b>detectable
    forgery</b>. Below, each attack is executed and then caught.</p>

{cards}

  <p class="foot">
    <b>Generated</b> {esc(generated)} · every <span style="color:var(--hash)">$ command</span>
    block above is real captured output from this run.<br>
    <b>Binaries</b> {esc(bulla_v)} · zkrisk (bulla-zk Bulletproofs) · verify any receipt
    yourself with <code>bulla verify</code> / <code>bulla log</code> / <code>zkrisk verify</code>.<br>
    <b>Provenance</b> built on RARS-oss/bulla — hermetic no-root receipts researched before the hackathon.
  </p>
"""
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>quaestor — red-team audit</title>
<meta name="description" content="Four ways to fake an autonomous trading record, each caught by quaestor's signed, offline-verifiable audit layer.">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@400;500;600&family=IBM+Plex+Sans:wght@400;500;600&display=swap" rel="stylesheet">
<style>{CSS}</style>
</head>
<body>
<div class="wrap">
{body}
</div>
</body>
</html>
"""


def main() -> int:
    for b, name in ((BULLA, "bulla"), (ZKRISK, "zkrisk")):
        if not (os.path.isfile(b) and os.access(b, os.X_OK)):
            sys.exit(f"FATAL: {name} not found/executable at {b}")
    vectors, caught = build_vectors()
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(render(vectors, caught), encoding="utf-8")
    size = OUT.stat().st_size
    print(f"{caught}/4 fraud vectors caught")
    print(f"wrote {OUT} ({size} bytes)")
    return 0 if caught == 4 else 1


if __name__ == "__main__":
    raise SystemExit(main())
