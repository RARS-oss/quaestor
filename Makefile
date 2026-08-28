# quaestor — run inside WSL2 Ubuntu. `make verify` needs NO Alpaca credentials.
PY ?= $(HOME)/hack/venv/bin/python

.PHONY: verify test once loop status dash flatten rehearse bundle anchor red-team track-record sealed-demo

verify:            ## offline: every bulla receipt signature + ledger chain (no credentials)
	$(PY) -m quaestor verify

test:              ## full unit test suite (no network)
	$(PY) -m pytest tests/ -q

status:            ## account + positions + today P&L
	$(PY) -m quaestor status

rehearse:          ## dress rehearsal on live data — no trading
	$(PY) -m quaestor rehearse

once:              ## one decision cycle (mints a signed receipt)
	$(PY) -m quaestor once

loop:              ## autonomous mode (market-hours aware; QUAESTOR_SEALED=1 to seal every trade)
	$(PY) -m quaestor loop

sealed-demo:       ## place + cancel a real order from inside a sealed cell
	bash scripts/demo_sealed_trade.sh

bundle:            ## export the offline proof bundle (receipts + verifier + index)
	$(PY) -m quaestor bundle

anchor:            ## witness the ledger head into the external anchor chain
	$(PY) -m quaestor anchor

red-team:          ## demonstrate the four fraud vectors, each caught
	bash scripts/red_team.sh

track-record:      ## (re)generate the in-browser-verifiable track-record page
	$(PY) scripts/generate_track_record.py

flatten:           ## emergency: close everything through the risk/audit path
	$(PY) -m quaestor flatten

dash:              ## judge dashboard
	$(PY) -m streamlit run dashboard/app.py
