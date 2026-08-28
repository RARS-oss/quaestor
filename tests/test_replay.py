"""Tests for deterministic replay — verdicts re-derive from sealed inputs."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from quaestor import risk as risk_mod
from quaestor.config import load_policy
from quaestor.models import (
    AccountSnapshot, Leg, PositionIntent, Side, Structure, TradeIntent,
)
from quaestor.replay import reconstruct_account, reconstruct_intent, replay_cycle

CALL_650 = "SPY260904C00650000"
CALL_655 = "SPY260904C00655000"


def _intent() -> TradeIntent:
    return TradeIntent(
        underlying="SPY", structure=Structure.VERTICAL_DEBIT,
        legs=[Leg(CALL_650, Side.BUY, 1, PositionIntent.BUY_TO_OPEN),
              Leg(CALL_655, Side.SELL, 1, PositionIntent.SELL_TO_OPEN)],
        qty=1, limit_price=1.05, thesis="t", max_loss_usd=105.0, intent_id="rep-1",
    )


def _account() -> AccountSnapshot:
    return AccountSnapshot(equity=100_000.0, cash=100_000.0, buying_power=200_000.0,
                           options_buying_power=100_000.0, options_approved_level=3,
                           options_trading_level=3, positions=[])


def _chain() -> dict:
    return {
        CALL_650: {"bid": 2.00, "ask": 2.10, "mid": 2.05, "delta": 0.45, "iv": 0.2},
        CALL_655: {"bid": 1.00, "ask": 1.08, "mid": 1.04, "delta": 0.25, "iv": 0.2},
    }


def _state() -> dict:
    return {"halted_today": False, "halted_week": False, "day_pnl_pct": 0.0,
            "week_pnl_pct": 0.0, "open_position_count": 0, "underlying_exposure": {}}


def test_reconstruct_round_trip():
    i = _intent()
    r = reconstruct_intent(i.to_dict())
    assert r.underlying == i.underlying and r.structure is Structure.VERTICAL_DEBIT
    assert [l.symbol for l in r.legs] == [CALL_650, CALL_655]
    assert r.legs[1].side is Side.SELL
    a = _account()
    ra = reconstruct_account({"equity": 100000.0, "options_trading_level": 3})
    assert ra.equity == 100000.0 and ra.options_trading_level == 3


def _write_decision(tmp: Path, cid: str, sealed_verdict: dict, policy_digest: str) -> None:
    cell = tmp / "cells" / cid
    cell.mkdir(parents=True)
    payload = {
        "cycle_id": cid,
        "replay": {
            "now": "2026-09-02T10:00:00-04:00",
            "policy_digest": policy_digest,
            "account": {"equity": 100000.0, "cash": 100000.0, "buying_power": 200000.0,
                        "options_buying_power": 100000.0, "options_approved_level": 3,
                        "options_trading_level": 3, "positions": []},
            "portfolio_state": _state(),
            "chains": {"SPY": _chain()},
            "intents": [_intent().to_dict()],
            "verdicts": [sealed_verdict],
        },
    }
    (cell / "decision.json").write_text(json.dumps(payload), encoding="utf-8")


def test_replay_reproduces_true_verdict(tmp_path):
    policy = load_policy()
    # The ground-truth verdict risk.judge actually produces:
    truth = risk_mod.judge(_intent(), policy=policy, account=_account(),
                           portfolio_state=_state(), chain=_chain(),
                           now=__import__("datetime").datetime.fromisoformat("2026-09-02T10:00:00-04:00"))
    _write_decision(tmp_path, "cycle-true", truth.to_dict(), policy["digest"])
    rep = replay_cycle(tmp_path, policy, "cycle-true")
    assert rep["found"] and rep["policy_digest_match"]
    assert rep["all_match"] is True
    assert rep["results"][0]["match"] is True
    assert rep["results"][0]["rederived_approved"] == truth.approved


def test_replay_detects_forged_verdict(tmp_path):
    policy = load_policy()
    truth = risk_mod.judge(_intent(), policy=policy, account=_account(),
                           portfolio_state=_state(), chain=_chain(),
                           now=__import__("datetime").datetime.fromisoformat("2026-09-02T10:00:00-04:00"))
    forged = truth.to_dict()
    forged["approved"] = not truth.approved  # claim the opposite of what the gates give
    _write_decision(tmp_path, "cycle-forged", forged, policy["digest"])
    rep = replay_cycle(tmp_path, policy, "cycle-forged")
    assert rep["found"]
    assert rep["all_match"] is False
    assert rep["results"][0]["match"] is False


def test_replay_missing_returns_not_found(tmp_path):
    rep = replay_cycle(tmp_path, load_policy(), "nope")
    assert rep["found"] is False and rep["all_match"] is False
