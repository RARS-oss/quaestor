#!/usr/bin/env bash
# run-agent.sh — WSL entrypoint for the quaestor agent (python -m quaestor <cmd>).
#
# Usage (from inside WSL, or via `wsl -d Ubuntu -- bash -lc '...'` from Windows):
#   scripts/run-agent.sh status
#   scripts/run-agent.sh once
#   scripts/run-agent.sh loop
#   scripts/run-agent.sh verify
#   scripts/run-agent.sh flatten
#
# Env: sources the repo's .env (ALPACA_API_KEY / ALPACA_SECRET_KEY / FEATHERLESS_API_KEY)
# if present, exports PYTHONPATH to the repo, and execs the pinned venv python.
set -euo pipefail

REPO="${QUAESTOR_REPO:-/mnt/c/Users/Daniil/Desktop/alpaca-hack/quaestor}"

if [ ! -d "$REPO" ]; then
  echo "run-agent: repo not found at $REPO (set QUAESTOR_REPO to override)" >&2
  exit 1
fi

if [ -f "$REPO/.env" ]; then
  set -a
  # shellcheck disable=SC1091
  . "$REPO/.env"
  set +a
fi

export PYTHONPATH="$REPO"

PY="$HOME/hack/venv/bin/python"
if [ ! -x "$PY" ]; then
  echo "run-agent: venv python not found at $PY — run scripts/setup-wsl.sh first" >&2
  exit 1
fi

cd "$REPO"
exec "$PY" -m quaestor "$@"
