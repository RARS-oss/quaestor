# quaestor — run inside WSL2 Ubuntu. `make verify` needs NO Alpaca credentials.
PY ?= $(HOME)/hack/venv/bin/python

.PHONY: verify test once loop status dash flatten

verify:            ## offline: every bulla receipt signature + ledger chain
	$(PY) -m quaestor verify

test:              ## full unit test suite (no network)
	$(PY) -m pytest tests/ -q

status:            ## account + positions + today P&L
	$(PY) -m quaestor status

once:              ## one decision cycle (mints a signed receipt)
	$(PY) -m quaestor once

loop:              ## autonomous mode (market-hours aware)
	$(PY) -m quaestor loop

flatten:           ## emergency: close everything through the risk/audit path
	$(PY) -m quaestor flatten

dash:              ## judge dashboard
	$(PY) -m streamlit run dashboard/app.py
