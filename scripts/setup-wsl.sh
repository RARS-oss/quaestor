#!/usr/bin/env bash
# setup-wsl.sh — idempotent WSL2 (Ubuntu) bootstrap for quaestor + bulla.
#
# What it does (safe to re-run):
#   1. sysctls so unprivileged user namespaces work (hermit-core / bulla cells);
#   2. verify the python venv at ~/hack/venv;
#   3. verify the pinned Alpaca CLI at ~/hack/bin/alpaca;
#   4. build bulla (and sbx, optional) in release mode if the binaries are missing,
#      using this project's CARGO_TARGET_DIR conventions:
#        bulla -> ~/.cache/hack-target/release/bulla
#        sbx   -> ~/.cache/hack-target-sbx/release/sbx
#   5. bulla smoke test in /tmp: run + verify a hermetic receipt (key/ledger kept
#      OUTSIDE the --work dir — bulla refuses trust material inside the cell);
#   6. print a PASS/FAIL summary; exit non-zero on any FAIL.
#
# Run from inside WSL:  bash /mnt/c/Users/Daniil/Desktop/alpaca-hack/quaestor/scripts/setup-wsl.sh

set -u

PASS=()
FAIL=()
WARN=()

ok()   { PASS+=("$1"); echo "  [ok]   $1"; }
bad()  { FAIL+=("$1"); echo "  [FAIL] $1"; }
warn() { WARN+=("$1"); echo "  [warn] $1"; }

echo "== quaestor WSL setup ($(date -u +%Y-%m-%dT%H:%M:%SZ)) =="

# ------------------------------------------------------------------ 1. sysctls
echo "-- sysctls (unprivileged user namespaces)"
SUDO=""
if [ "$(id -u)" -ne 0 ] && command -v sudo >/dev/null 2>&1; then SUDO="sudo"; fi
# Ubuntu 24.04+: AppArmor gates unprivileged userns; older kernels use the clone knob.
$SUDO sysctl -w kernel.apparmor_restrict_unprivileged_userns=0 >/dev/null 2>&1 || true
$SUDO sysctl -w kernel.unprivileged_userns_clone=1 >/dev/null 2>&1 || true

aa="$(sysctl -n kernel.apparmor_restrict_unprivileged_userns 2>/dev/null || echo absent)"
if [ "$aa" = "0" ] || [ "$aa" = "absent" ]; then
  ok "kernel.apparmor_restrict_unprivileged_userns=$aa"
else
  bad "kernel.apparmor_restrict_unprivileged_userns=$aa (want 0; bulla cells will fail)"
fi
uc="$(sysctl -n kernel.unprivileged_userns_clone 2>/dev/null || echo absent)"
if [ "$uc" = "1" ] || [ "$uc" = "absent" ]; then
  ok "kernel.unprivileged_userns_clone=$uc"
else
  bad "kernel.unprivileged_userns_clone=$uc (want 1)"
fi

# ------------------------------------------------------------------ 2. venv
echo "-- python venv"
VENV="$HOME/hack/venv"
if [ -x "$VENV/bin/python" ]; then
  ok "venv: $("$VENV/bin/python" --version 2>&1) at $VENV"
else
  bad "venv missing at $VENV  (create: python3.12 -m venv ~/hack/venv && ~/hack/venv/bin/pip install alpaca-py httpx pyyaml openai python-dotenv pytest)"
fi

# ------------------------------------------------------------------ 3. alpaca CLI
echo "-- alpaca CLI"
ALPACA="$HOME/hack/bin/alpaca"
if [ -x "$ALPACA" ]; then
  ver="$("$ALPACA" version 2>/dev/null || "$ALPACA" --version 2>/dev/null || echo '?')"
  ok "alpaca CLI: $(echo "$ver" | head -n1) at $ALPACA"
else
  bad "alpaca CLI missing at $ALPACA (pinned v0.0.14 expected)"
fi

# ------------------------------------------------------------------ 4. build bulla / sbx
echo "-- bulla / sbx binaries"
BULLA_BIN="$HOME/.cache/hack-target/release/bulla"
BULLA_SRC="${BULLA_SRC:-/mnt/c/Users/Daniil/Desktop/alpaca-hack/refs/bulla}"
if [ ! -x "$BULLA_BIN" ]; then
  if [ -d "$BULLA_SRC" ] && command -v cargo >/dev/null 2>&1; then
    echo "  building bulla (release) from $BULLA_SRC ..."
    (cd "$BULLA_SRC" && CARGO_TARGET_DIR="$HOME/.cache/hack-target" cargo build --release) \
      || warn "cargo build for bulla failed"
  else
    warn "cannot build bulla: src=$BULLA_SRC $( [ -d "$BULLA_SRC" ] || echo '(missing)' ), cargo=$(command -v cargo || echo missing)"
  fi
fi
if [ -x "$BULLA_BIN" ]; then
  ok "bulla at $BULLA_BIN"
else
  bad "bulla missing at $BULLA_BIN — receipts will degrade to UNSIGNED receipt-lite"
fi

SBX_BIN="$HOME/.cache/hack-target-sbx/release/sbx"
SBX_SRC="${SBX_SRC:-}"
if [ -z "$SBX_SRC" ]; then
  for cand in "$HOME/hack/sbx" "/mnt/c/Users/Daniil/Desktop/alpaca-hack/refs/sbx"; do
    if [ -d "$cand" ]; then SBX_SRC="$cand"; break; fi
  done
fi
if [ ! -x "$SBX_BIN" ] && [ -n "$SBX_SRC" ] && command -v cargo >/dev/null 2>&1; then
  echo "  building sbx (release) from $SBX_SRC ..."
  (cd "$SBX_SRC" && CARGO_TARGET_DIR="$HOME/.cache/hack-target-sbx" cargo build --release) \
    || warn "cargo build for sbx failed"
fi
if [ -x "$SBX_BIN" ]; then
  ok "sbx at $SBX_BIN"
else
  warn "sbx missing at $SBX_BIN (optional — bulla vendors hermit-core; quaestor only needs bulla)"
fi

# ------------------------------------------------------------------ 5. bulla smoke test
echo "-- bulla smoke test (run + verify)"
if [ -x "$BULLA_BIN" ]; then
  SMOKE="$(mktemp -d /tmp/bulla-smoke.XXXXXX)"
  mkdir -p "$SMOKE/work"
  echo "quaestor-smoke $(date -u +%s)" > "$SMOKE/work/hello.txt"
  # NOTE: --key/--ledger/--out live in $SMOKE, OUTSIDE the --work dir on purpose:
  # bulla refuses trust material inside the cell-writable mount.
  if "$BULLA_BIN" run \
        --work "$SMOKE/work" \
        --out "$SMOKE/receipt.json" \
        --key "$SMOKE/seed" \
        --ledger "$SMOKE/ledger.jsonl" \
        --wall-ms 20000 --json \
        -- /bin/sh -c "sha256sum hello.txt" > "$SMOKE/run.json" 2>"$SMOKE/run.err"; then
    if grep -q '"seal_ok":true' "$SMOKE/run.json"; then
      ok "bulla run: SEAL HELD"
    else
      warn "bulla run succeeded but seal did not hold: $(cat "$SMOKE/run.json")"
    fi
    if "$BULLA_BIN" verify "$SMOKE/receipt.json" >/dev/null 2>&1; then
      ok "bulla verify: receipt intact (exit 0)"
    else
      bad "bulla verify failed on the smoke receipt"
    fi
  else
    bad "bulla run failed: $(head -c 300 "$SMOKE/run.err" 2>/dev/null) (userns sysctls? see step 1)"
  fi
  rm -rf "$SMOKE"
else
  bad "bulla smoke skipped: binary missing"
fi

# ------------------------------------------------------------------ 6. summary
echo
echo "== summary: ${#PASS[@]} pass, ${#WARN[@]} warn, ${#FAIL[@]} fail =="
for p in ${PASS[@]+"${PASS[@]}"}; do echo "  PASS  $p"; done
for w in ${WARN[@]+"${WARN[@]}"}; do echo "  WARN  $w"; done
for f in ${FAIL[@]+"${FAIL[@]}"}; do echo "  FAIL  $f"; done

if [ "${#FAIL[@]}" -gt 0 ]; then
  echo "RESULT: FAIL"
  exit 1
fi
echo "RESULT: PASS"
