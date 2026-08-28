"""Assemble a self-contained bulla receipt verifier (single HTML file).

Inlines the wasm-bindgen no-modules glue + the base64-encoded .wasm + a sample
receipt, so the page verifies bulla receipts entirely client-side — no backend,
no network, no install. Open it anywhere; paste a receipt; verify; tamper and
watch it fail. Output: dashboard/verifier.html.

Run (WSL):  ~/hack/venv/bin/python scripts/generate_verifier.py
"""
from __future__ import annotations

import base64
import json
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
# The no-modules wasm-pack build lives in the bulla clone.
PKG = Path.home() / "hack/refs/bulla/crates/bulla-wasm/pkg-nomod"
# Fallback to the Windows clone path when run outside WSL home.
if not PKG.exists():
    PKG = Path("/mnt/c/Users/Daniil/Desktop/alpaca-hack/refs/bulla/crates/bulla-wasm/pkg-nomod")

GLUE = (PKG / "bulla_wasm.js").read_text(encoding="utf-8")
WASM_B64 = base64.b64encode((PKG / "bulla_wasm_bg.wasm").read_bytes()).decode("ascii")

sample_path = REPO / "receipts" / "sealed-demo.json"
SAMPLE = sample_path.read_text(encoding="utf-8") if sample_path.exists() else "{}"

HTML = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>bulla receipt verifier</title>
<style>
  :root {
    --bg:#f6f7f9; --panel:#ffffff; --ink:#111418; --muted:#5b6572; --line:#e3e7ec;
    --ok:#0f8a5f; --bad:#c8372d; --accent:#2f6df6; --mono:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;
  }
  @media (prefers-color-scheme: dark) {
    :root:not([data-theme="light"]) {
      --bg:#0e1116; --panel:#161b22; --ink:#e6edf3; --muted:#9aa4b2; --line:#2a323d;
      --ok:#3fb984; --bad:#ef6a5f; --accent:#5b8cff;
    }
  }
  * { box-sizing:border-box; }
  body { margin:0; background:var(--bg); color:var(--ink);
    font:15px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif; }
  .wrap { max-width:920px; margin:0 auto; padding:32px 20px 64px; }
  h1 { font-size:22px; margin:0 0 4px; letter-spacing:-0.01em; }
  .sub { color:var(--muted); margin:0 0 24px; }
  .card { background:var(--panel); border:1px solid var(--line); border-radius:12px; padding:18px; margin-bottom:16px; }
  textarea { width:100%; min-height:280px; resize:vertical; border:1px solid var(--line); border-radius:8px;
    background:var(--bg); color:var(--ink); font:12.5px/1.45 var(--mono); padding:12px; }
  .row { display:flex; gap:10px; flex-wrap:wrap; margin-top:12px; }
  button { border:1px solid var(--line); background:var(--panel); color:var(--ink); border-radius:8px;
    padding:9px 16px; font-size:14px; cursor:pointer; }
  button.primary { background:var(--accent); border-color:var(--accent); color:#fff; font-weight:600; }
  button:hover { filter:brightness(1.05); }
  .hint { color:var(--muted); font-size:13px; margin-top:10px; }
  .verdict { display:none; margin-top:16px; }
  .verdict.show { display:block; }
  .banner { border-radius:10px; padding:14px 16px; font-weight:600; font-size:16px; border:1px solid; }
  .banner.ok { color:var(--ok); border-color:var(--ok); background:color-mix(in srgb, var(--ok) 10%, transparent); }
  .banner.bad { color:var(--bad); border-color:var(--bad); background:color-mix(in srgb, var(--bad) 10%, transparent); }
  table { width:100%; border-collapse:collapse; margin-top:12px; font-size:13.5px; }
  td { padding:7px 8px; border-bottom:1px solid var(--line); }
  td.k { color:var(--muted); width:190px; }
  .chk { font-family:var(--mono); }
  .pass { color:var(--ok); } .fail { color:var(--bad); }
  .notes { margin-top:10px; font-family:var(--mono); font-size:12.5px; color:var(--muted); white-space:pre-wrap; }
  code { font-family:var(--mono); background:var(--bg); padding:1px 5px; border-radius:4px; }
  .foot { color:var(--muted); font-size:12px; margin-top:28px; }
</style>
</head>
<body>
<div class="wrap">
  <h1>bulla receipt verifier</h1>
  <p class="sub">Verify a signed trading-decision receipt <b>entirely in your browser</b> — no server, no install.
     Load the sample, hit Verify. Then change any character and watch the signature reject it.</p>

  <div class="card">
    <textarea id="input" spellcheck="false" placeholder="Paste a bulla receipt JSON here..."></textarea>
    <div class="row">
      <button class="primary" id="verify">Verify</button>
      <button id="sample">Load sample receipt</button>
      <button id="tamper">Tamper: flip exit code</button>
    </div>
    <div class="hint">Runs <code>bulla verify</code> compiled to WebAssembly — the same code that checks
      the Ed25519 signature, the body digest, and the event hash-chain offline.</div>
    <div class="verdict" id="verdict"></div>
  </div>

  <p class="foot" id="foot"></p>
</div>

<script>__GLUE__</script>
<script>
  const WASM_B64 = "__WASM_B64__";
  const SAMPLE = __SAMPLE_JSON__;
  const bytes = Uint8Array.from(atob(WASM_B64), c => c.charCodeAt(0));
  let ready = false;

  wasm_bindgen(bytes).then(() => {
    ready = true;
    document.getElementById("foot").textContent = "bulla-wasm v" + wasm_bindgen.version() + " · verification runs locally in this tab";
  }).catch(e => {
    document.getElementById("foot").textContent = "failed to load wasm: " + e;
  });

  const $ = id => document.getElementById(id);
  $("sample").onclick = () => { $("input").value = JSON.stringify(SAMPLE, null, 2); };
  $("tamper").onclick = () => {
    try {
      const o = JSON.parse($("input").value || JSON.stringify(SAMPLE));
      if (o.body && o.body.outcome) o.body.outcome.exit_code = (o.body.outcome.exit_code || 0) + 99;
      $("input").value = JSON.stringify(o, null, 2);
    } catch (e) { alert("Load a valid receipt first."); }
  };

  $("verify").onclick = () => {
    if (!ready) return;
    const raw = $("input").value.trim();
    if (!raw) { $("input").value = JSON.stringify(SAMPLE, null, 2); return; }
    const rep = JSON.parse(wasm_bindgen.verify_receipt(raw));
    render(rep);
  };

  function render(r) {
    const v = $("verdict"); v.className = "verdict show";
    if (!r.ok_json) {
      v.innerHTML = '<div class="banner bad">NOT A RECEIPT</div><div class="notes">' + esc(r.error) + '</div>';
      return;
    }
    const intact = r.intact;
    const banner = intact
      ? '<div class="banner ok">✓ INTACT — signature, digest and event chain all verify</div>'
      : '<div class="banner bad">✗ FORGERY DETECTED — this receipt does not verify</div>';
    const rows = [
      ["signature (Ed25519)", r.sig_ok],
      ["body digest (sha256)", r.digest_ok],
      ["event hash-chain", r.chain_ok],
      ["seal held (hermetic)", r.seal_ok],
    ].map(([k, ok]) => '<tr><td class="k">' + k + '</td><td class="chk ' + (ok?'pass':'fail') + '">'
        + (ok ? 'ok' : 'FAIL') + '</td></tr>').join("");
    const meta = '<tr><td class="k">command</td><td><code>' + esc(r.command || '') + '</code></td></tr>'
      + '<tr><td class="k">exit</td><td>' + esc(String(r.exit_kind)) + ':' + esc(String(r.exit_code)) + '</td></tr>'
      + '<tr><td class="k">signing key</td><td><code>' + esc((r.pubkey||'').slice(0,24)) + '…</code></td></tr>';
    const notes = (r.notes && r.notes.length)
      ? '<div class="notes">' + r.notes.map(esc).join("\\n") + '</div>' : '';
    v.innerHTML = banner + '<table>' + rows + meta + '</table>' + notes;
  }
  function esc(s){ return String(s).replace(/[&<>]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;'}[c])); }
</script>
</body>
</html>
"""

out = (
    HTML.replace("__GLUE__", GLUE)
    .replace("__WASM_B64__", WASM_B64)
    .replace("__SAMPLE_JSON__", json.dumps(json.loads(SAMPLE)))
)
dest = REPO / "dashboard" / "verifier.html"
dest.write_text(out, encoding="utf-8")
print(f"wrote {dest} ({len(out)//1024} KB, wasm {len(WASM_B64)//1024} KB base64)")
