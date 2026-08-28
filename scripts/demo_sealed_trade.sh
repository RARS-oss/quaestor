#!/bin/bash
# Demo (and video shot): a real Alpaca call made from inside a hermetic bulla
# cell over the sealed egress tunnel. Produces a signed receipt in receipts/
# that the dashboard's Tamper Playground can show and verify.
#
# Usage (WSL):  bash scripts/demo_sealed_trade.sh
set -e
HERE="$(cd "$(dirname "$0")/.." && pwd)"
source "$HERE/.env"
export ALPACA_API_KEY ALPACA_SECRET_KEY
BULLA="${BULLA_BIN:-$HOME/.cache/hack-target/release/bulla}"
# The egress broker creates a Unix socket under --work; drvfs (/mnt/c) does NOT
# support Unix sockets, so the cell work dir MUST be on a native Linux fs (ext4).
CELL="$HOME/.cache/quaestor-cells/sealed-demo"
OUT="$HERE/receipts/sealed-demo.json"

rm -rf "$CELL" && mkdir -p "$CELL"
cp "$HERE/scripts/in_cell_sealed_order.py" "$CELL/in_cell_sealed_order.py"

echo "== sealed live order: place + cancel a real SPY option from inside a SEAL-HELD cell =="
"$BULLA" run --work "$CELL" \
  --egress-allow paper-api.alpaca.markets:443 \
  --egress-allow data.alpaca.markets:443 \
  --nondeterministic \
  --wall-ms 45000 \
  --out "$OUT" \
  --ledger "$HERE/receipts/ledger.jsonl" \
  --key "$HERE/receipts/signing.seed" \
  -- python3 /work/in_cell_sealed_order.py

echo
echo "cell said: $(cat "$CELL/sealed_order_result.txt" 2>/dev/null || echo '(no output)')"
echo
echo "== verify offline =="
"$BULLA" verify "$OUT"
echo
echo "receipt: $OUT"
