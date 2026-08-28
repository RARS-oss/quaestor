#!/usr/bin/env bash
# red_team.sh — the RED-TEAM demo for quaestor's audit layer.
#
# "Everyone fixes tasks. We closed the harness."
#
# Demonstrates FOUR ways someone fakes autonomous-trading results — and shows
# each one CAUGHT by the REAL binaries (bulla, zkrisk) against a REAL signed
# receipt. Deterministic and safe: every attack is performed on a COPY under a
# scratch dir on native tmpfs (/tmp). Repo files are read, never mutated.
#
# Runs in WSL:
#   wsl -d Ubuntu -- bash -lc "cd /mnt/c/Users/Daniil/Desktop/alpaca-hack/quaestor && bash scripts/red_team.sh"
#
# Env overrides: BULLA_BIN, ZKRISK_BIN, RT_DIR, NO_COLOR, RT_NO_REPORT=1
set -u
set -o pipefail

# ---------------------------------------------------------------------------
# Resolve paths + binaries
# ---------------------------------------------------------------------------
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BULLA="${BULLA_BIN:-$HOME/.cache/hack-target/release/bulla}"
ZKRISK="${ZKRISK_BIN:-$HOME/.cache/hack-target/release/examples/zkrisk}"
RT="${RT_DIR:-/tmp/quaestor-red-team}"
PY="$(command -v python3 || command -v python)"

# ---------------------------------------------------------------------------
# Presentation helpers
# ---------------------------------------------------------------------------
if [ -t 1 ] && [ -z "${NO_COLOR:-}" ]; then
  BOLD=$'\033[1m'; DIM=$'\033[2m'; RST=$'\033[0m'
  GRN=$'\033[38;5;42m'; RED=$'\033[38;5;203m'; CYA=$'\033[38;5;80m'; IND=$'\033[38;5;105m'
else
  BOLD=""; DIM=""; RST=""; GRN=""; RED=""; CYA=""; IND=""
fi

CAUGHT=0
BAR="============================================================================"

hdr() { # $1 = number, $2 = title
  printf '\n%s%s%s\n' "$IND" "$BAR" "$RST"
  printf '%s  FRAUD VECTOR %s — %s%s\n' "${IND}${BOLD}" "$1" "$2" "$RST"
  printf '%s%s%s\n' "$IND" "$BAR" "$RST"
}
step() { printf '\n%s» %s%s\n' "$CYA" "$*" "$RST"; }          # narration
cmd()  { printf '%s$ %s%s\n' "$DIM" "$*" "$RST"; }             # echo a command
caught() { CAUGHT=$((CAUGHT+1)); printf '\n  %s%s CAUGHT%s  %s\n' "$GRN" "$BOLD" "$RST" "$*"; }
missed() { printf '\n  %s%s NOT CAUGHT%s  %s\n' "$RED" "$BOLD" "$RST" "$*"; }

# ---------------------------------------------------------------------------
# Preflight
# ---------------------------------------------------------------------------
[ -x "$BULLA" ]  || { echo "FATAL: bulla not found/executable at $BULLA"  >&2; exit 3; }
[ -x "$ZKRISK" ] || { echo "FATAL: zkrisk not found/executable at $ZKRISK" >&2; exit 3; }
[ -n "$PY" ]     || { echo "FATAL: python3 not found" >&2; exit 3; }

rm -rf "$RT"; mkdir -p "$RT"

# Newest REAL signed receipt: prefer a sealed-*.json (live-trading, egress
# block), else a c-*.json decision receipt.
RECEIPT="$(ls -1t "$HERE"/receipts/sealed-*.json 2>/dev/null | head -n1)"
[ -n "$RECEIPT" ] || RECEIPT="$(ls -1t "$HERE"/receipts/c-*.json 2>/dev/null | head -n1)"
[ -n "$RECEIPT" ] || { echo "FATAL: no signed receipt in $HERE/receipts" >&2; exit 3; }

# A hash-chained ledger with enough entries to drop a *middle* one. Prefer the
# sealed run ledger; fall back to the honest-mode ledger if it is longer.
pick_ledger() {
  local best="" best_n=0 f n
  for f in "$HERE/receipts/sealed-ledger.jsonl" "$HERE/receipts/ledger.jsonl"; do
    [ -f "$f" ] || continue
    n=$(grep -c . "$f")
    if [ "$n" -ge 3 ] && [ "$n" -gt "$best_n" ]; then best="$f"; best_n="$n"; fi
  done
  echo "$best"
}
LEDGER="$(pick_ledger)"

printf '%s%s  quaestor RED-TEAM — four frauds, four catches%s\n' "$BOLD" "$IND" "$RST"
printf '%s  real binaries · real receipt · attacks on /tmp copies only%s\n' "$DIM" "$RST"
printf '  %sbulla%s   %s\n' "$CYA" "$RST" "$BULLA"
printf '  %szkrisk%s  %s\n' "$CYA" "$RST" "$ZKRISK"
printf '  %sreceipt%s %s\n' "$CYA" "$RST" "$RECEIPT"
printf '  %sledger%s  %s\n' "$CYA" "$RST" "${LEDGER:-<none suitable>}"

# ===========================================================================
# VECTOR 1 — FORGE A NUMBER
# ===========================================================================
hdr 1 "FORGE A NUMBER (edit a signed result)"
step "Copy a real signed receipt and verify it — the honest baseline."
cp "$RECEIPT" "$RT/orig.json"
cmd "bulla verify \$RT/orig.json"
"$BULLA" verify "$RT/orig.json"; RC_ORIG=$?
echo "  [exit=$RC_ORIG]"

step "Now flip a number inside the signed body (outcome.exit_code) and re-verify."
cmd "python3 -c 'flip body.outcome.exit_code'  # tamper on the COPY"
"$PY" - "$RT/orig.json" "$RT/forged.json" <<'PY'
import json, sys
d = json.load(open(sys.argv[1]))
o = d["body"]["outcome"]
was = o.get("exit_code")
o["exit_code"] = 42 if was != 42 else 7          # change the recorded result
json.dump(d, open(sys.argv[2], "w"), indent=1)
print(f"  tampered outcome.exit_code: {was} -> {o['exit_code']}")
PY
cmd "bulla verify \$RT/forged.json"
"$BULLA" verify "$RT/forged.json"; RC_FORGED=$?
echo "  [exit=$RC_FORGED]"

if [ "$RC_ORIG" -eq 0 ] && [ "$RC_FORGED" -ne 0 ]; then
  caught "original verifies (exit 0); one flipped digit ⇒ Ed25519 signature FAIL (exit $RC_FORGED)."
else
  missed "expected intact=0, forged!=0 (got $RC_ORIG / $RC_FORGED)"
fi

# ===========================================================================
# VECTOR 2 — DROP A LOSING CYCLE
# ===========================================================================
hdr 2 "DROP A LOSING CYCLE (delete a ledger entry)"
if [ -z "$LEDGER" ]; then
  missed "no ledger with >=3 entries available to demonstrate a mid-chain drop"
else
  step "Copy the hash-chained run ledger and verify the chain."
  cp "$LEDGER" "$RT/ledger.jsonl"
  cmd "bulla log \$RT/ledger.jsonl"
  "$BULLA" log "$RT/ledger.jsonl"; RC_LOG=$?
  echo "  [exit=$RC_LOG]"

  step "Delete a middle cycle (a losing run) to inflate the pass rate, then re-check."
  cmd "python3 -c 'drop the middle line'  # tamper on the COPY"
  "$PY" - "$RT/ledger.jsonl" "$RT/ledger_trunc.jsonl" <<'PY'
import sys
lines = [l for l in open(sys.argv[1]).read().splitlines() if l.strip()]
drop = len(lines) // 2
print(f"  dropping entry #{drop} of {len(lines)} (a mid-chain cycle)")
kept = [l for i, l in enumerate(lines) if i != drop]
open(sys.argv[2], "w").write("\n".join(kept) + "\n")
PY
  cmd "bulla log \$RT/ledger_trunc.jsonl"
  "$BULLA" log "$RT/ledger_trunc.jsonl"; RC_TRUNC=$?
  echo "  [exit=$RC_TRUNC]"

  if [ "$RC_LOG" -eq 0 ] && [ "$RC_TRUNC" -ne 0 ]; then
    caught "intact chain passes (exit 0); a removed cycle ⇒ chain BROKEN (exit $RC_TRUNC)."
  else
    missed "expected intact=0, truncated!=0 (got $RC_LOG / $RC_TRUNC)"
  fi
fi

# ===========================================================================
# VECTOR 3 — TRADE WITH THE NETWORK ON
# ===========================================================================
hdr 3 "TRADE WITH THE NETWORK ON (score can't masquerade as sealed)"
step "Run WITH host network reachable (--allow-net): the seal cannot hold."
mkdir -p "$RT/net-cell" "$RT/sealed-cell"
cmd "bulla run --allow-net --work \$RT/net-cell --out \$RT/net.json -- /bin/echo hi"
NET_RUN="$("$BULLA" run --allow-net --work "$RT/net-cell" --out "$RT/net.json" -- /bin/echo hi 2>&1)"
echo "$NET_RUN"
step "The signed receipt honestly records it — verify reports SEAL BROKEN."
cmd "bulla verify \$RT/net.json"
NET_VERIFY="$("$BULLA" verify "$RT/net.json" 2>&1)"
echo "$NET_VERIFY"

step "Contrast: a normal isolated run keeps the seal HELD."
cmd "bulla run --work \$RT/sealed-cell --out \$RT/sealed.json -- /bin/echo hi"
SEALED_RUN="$("$BULLA" run --work "$RT/sealed-cell" --out "$RT/sealed.json" -- /bin/echo hi 2>&1)"
echo "$SEALED_RUN"

if echo "$NET_RUN$NET_VERIFY" | grep -qiE 'SEAL BROKEN|seal held +NO|not (hermetically )?sealed' \
   && echo "$SEALED_RUN" | grep -qi 'SEAL HELD'; then
  caught "egress present ⇒ receipt says SEAL BROKEN; only the isolated run is SEAL HELD."
else
  missed "expected SEAL BROKEN on --allow-net and SEAL HELD on the isolated run"
fi

# ===========================================================================
# VECTOR 4 — FAKE A RISK PROOF
# ===========================================================================
hdr 4 "FAKE A RISK PROOF (zero-knowledge max-loss cap)"
step "Prove a within-cap max loss (\$12,000 < 2^16) and verify — the honest proof."
cmd "zkrisk prove 12000 16 > \$RT/proof.json ; zkrisk verify \$RT/proof.json"
"$ZKRISK" prove 12000 16 > "$RT/proof.json"; RC_PROVE=$?
PROOF_VERIFY="$("$ZKRISK" verify "$RT/proof.json" 2>&1)"; RC_PVOK=$?
echo "  prove:  [exit=$RC_PROVE]"
echo "  verify: $PROOF_VERIFY  [exit=$RC_PVOK]"

step "(a) Tamper one nibble of the proof hex — the Bulletproof no longer verifies."
cmd "python3 -c 'flip proof[0]'  ;  zkrisk verify \$RT/proof_bad.json"
"$PY" - "$RT/proof.json" "$RT/proof_bad.json" <<'PY'
import json, sys
d = json.load(open(sys.argv[1]))
p = d["proof"]; new = "1" if p[0] != "1" else "2"
d["proof"] = new + p[1:]
json.dump(d, open(sys.argv[2], "w"))
print(f"  proof[0]: {p[0]} -> {new}")
PY
TAMPER_VERIFY="$("$ZKRISK" verify "$RT/proof_bad.json" 2>&1)"; RC_TAMPER=$?
echo "  verify: $TAMPER_VERIFY  [exit=$RC_TAMPER]"

step "(b) An OVER-cap loss (\$70,000 >= 2^16) cannot even produce a passing proof."
cmd "zkrisk prove 70000 16"
OVERCAP="$("$ZKRISK" prove 70000 16 2>&1)"; RC_OVER=$?
echo "  $OVERCAP  [exit=$RC_OVER]"

if [ "$RC_PROVE" -eq 0 ] && [ "$RC_PVOK" -eq 0 ] && [ "$RC_TAMPER" -ne 0 ] && [ "$RC_OVER" -ne 0 ]; then
  caught "honest proof verifies (exit 0); tampered proof rejected (exit $RC_TAMPER); over-cap value can't prove (exit $RC_OVER)."
else
  missed "expected prove=0 verify=0 tampered!=0 overcap!=0 (got $RC_PROVE/$RC_PVOK/$RC_TAMPER/$RC_OVER)"
fi

# ===========================================================================
# Optional: regenerate the self-contained HTML report from real output
# ===========================================================================
if [ -z "${RT_NO_REPORT:-}" ] && [ -f "$HERE/scripts/gen_red_team_report.py" ]; then
  printf '\n%s» generating scripts/red_team_report.html (real captured output)…%s\n' "$CYA" "$RST"
  if "$PY" "$HERE/scripts/gen_red_team_report.py" >/dev/null 2>&1; then
    SZ=$(wc -c < "$HERE/scripts/red_team_report.html" 2>/dev/null | tr -d ' ')
    printf '  report: %s (%s bytes)\n' "$HERE/scripts/red_team_report.html" "${SZ:-?}"
  else
    printf '  %s(report generation skipped — gen_red_team_report.py returned non-zero)%s\n' "$DIM" "$RST"
  fi
fi

# ===========================================================================
# Summary
# ===========================================================================
printf '\n%s%s%s\n' "$IND" "$BAR" "$RST"
if [ "$CAUGHT" -eq 4 ]; then
  printf '%s%s  %d/4 fraud vectors caught%s\n' "$GRN" "$BOLD" "$CAUGHT" "$RST"
  printf '%s  Everyone fixes tasks. We closed the harness.%s\n' "$DIM" "$RST"
  printf '%s%s%s\n' "$IND" "$BAR" "$RST"
  exit 0
else
  printf '%s%s  %d/4 fraud vectors caught%s\n' "$RED" "$BOLD" "$CAUGHT" "$RST"
  printf '%s%s%s\n' "$IND" "$BAR" "$RST"
  exit 1
fi
