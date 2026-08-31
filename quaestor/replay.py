"""Deterministic replay: re-derive every risk verdict from the SIGNED inputs.

A signed receipt proves *what the agent decided*. Replay proves the decision was
not arbitrary: it re-runs the deterministic risk gates over the exact inputs
sealed in the receipt (intents, account, portfolio state, the chain quotes each
leg was judged against, the clock) and confirms the re-derived approve/reject
verdict matches — byte for byte, from data anyone can verify was not tampered.

"Not only did we sign what we did — you can re-derive that our agent *would*
make the same call from the same inputs."

Each decision cycle's `attested_cycle` seals a `replay` block into
receipts/cells/<cycle_id>/decision.json (whose sha256 is bound into the signed
receipt). `replay_cycle` reads it, reconstructs the typed inputs, re-runs
risk.judge, and reports per-intent whether the verdict reproduces.
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

from quaestor import risk as risk_mod
from quaestor.models import (
    AccountSnapshot, Leg, PositionIntent, RiskVerdict, Side, Structure, TradeIntent,
)


def reconstruct_intent(d: dict[str, Any]) -> TradeIntent:
    """Rebuild a TradeIntent from its to_dict() form (as sealed in the receipt)."""
    legs = [
        Leg(
            symbol=leg["symbol"],
            side=Side(leg["side"]),
            ratio_qty=int(leg["ratio_qty"]),
            position_intent=PositionIntent(leg["position_intent"]),
        )
        for leg in d.get("legs", [])
    ]
    return TradeIntent(
        underlying=d["underlying"],
        structure=Structure(d["structure"]),
        legs=legs,
        qty=int(d["qty"]),
        limit_price=float(d["limit_price"]),
        thesis=d.get("thesis", ""),
        max_loss_usd=float(d.get("max_loss_usd", 0.0)),
        catalyst_tag=d.get("catalyst_tag", ""),
        is_0dte=bool(d.get("is_0dte", False)),
        expiry=d.get("expiry", ""),
        signal_snapshot=d.get("signal_snapshot", {}) or {},
        intent_id=d.get("intent_id", ""),
        created_at=float(d.get("created_at", 0.0)),
    )


def reconstruct_account(d: dict[str, Any]) -> AccountSnapshot:
    return AccountSnapshot(
        equity=float(d.get("equity", 0.0)),
        cash=float(d.get("cash", 0.0)),
        buying_power=float(d.get("buying_power", 0.0)),
        options_buying_power=float(d.get("options_buying_power", 0.0)),
        options_approved_level=int(d.get("options_approved_level", 0)),
        options_trading_level=int(d.get("options_trading_level", 0)),
        positions=d.get("positions", []) or [],
        ts=float(d.get("ts", 0.0)),
    )


def _decision_json_for(receipts_dir: Path, ref: str) -> Path | None:
    """Resolve a cycle id or receipt path to its sealed decision.json."""
    p = Path(ref)
    if p.name == "decision.json" and p.exists():
        return p
    # A receipt path -> its cell decision.json.
    cid = p.stem if p.suffix == ".json" else ref
    cid = cid.replace("sealed-", "")
    cell = Path(receipts_dir) / "cells" / cid / "decision.json"
    if cell.exists():
        return cell
    # Bare cycle id.
    cell = Path(receipts_dir) / "cells" / ref / "decision.json"
    return cell if cell.exists() else None


def replay_cycle(receipts_dir: Path, policy: dict, ref: str) -> dict[str, Any]:
    """Re-derive the risk verdicts sealed for one cycle and compare.

    Returns {cycle_id, found, policy_digest_match, results:[...], all_match, note}.
    Each result: {intent_id, sealed_approved, rederived_approved, match,
    mismatched_checks:[...]}.
    """
    dj = _decision_json_for(Path(receipts_dir), ref)
    if dj is None:
        return {"found": False, "note": f"no sealed decision.json for {ref!r}",
                "results": [], "all_match": False}
    try:
        payload = json.loads(dj.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return {"found": False, "note": f"unreadable decision.json: {exc}",
                "results": [], "all_match": False}

    block = payload.get("replay") or {}
    if not block:
        return {"found": False, "note": "this receipt predates the replay block",
                "results": [], "all_match": False}

    sealed_digest = str(block.get("policy_digest", ""))
    current_digest = str(policy.get("digest", ""))
    digest_match = sealed_digest == current_digest

    # Re-derive under the rules that actually judged this cycle when the receipt
    # sealed them. Without the sealed body we can only re-run the CURRENT rules,
    # which proves nothing about determinism once policy.yaml has moved on.
    sealed_policy = block.get("policy")
    if isinstance(sealed_policy, dict) and sealed_policy:
        judge_policy = sealed_policy
        judged_under = "sealed"
    else:
        judge_policy = policy
        judged_under = "current"
    decidable = judged_under == "sealed" or digest_match

    now = _parse_dt(block.get("now", ""))
    account = reconstruct_account(block.get("account") or {})
    portfolio_state = block.get("portfolio_state") or {}
    chains = block.get("chains") or {}
    sealed_verdicts = {v.get("intent_id"): v for v in block.get("verdicts", [])}

    results: list[dict[str, Any]] = []
    for idict in block.get("intents", []):
        try:
            intent = reconstruct_intent(idict)
        except Exception as exc:
            results.append({"intent_id": idict.get("intent_id", "?"), "match": False,
                            "note": f"could not reconstruct intent: {exc!r}"})
            continue
        chain = chains.get(intent.underlying, {})
        rederived = risk_mod.judge(
            intent, policy=judge_policy, account=account,
            portfolio_state=portfolio_state, chain=chain, now=now)
        sealed = sealed_verdicts.get(intent.intent_id, {})
        sealed_ok = bool(sealed.get("approved"))
        sealed_checks = {c["name"]: c["ok"] for c in sealed.get("checks", [])
                         if isinstance(c, dict)}
        rederived_checks = {c.name: c.ok for c in rederived.checks}
        mismatched = sorted(
            name for name in set(sealed_checks) | set(rederived_checks)
            if sealed_checks.get(name) != rederived_checks.get(name))
        match = (rederived.approved == sealed_ok) and not mismatched
        results.append({
            "intent_id": intent.intent_id,
            "sealed_approved": sealed_ok,
            "rederived_approved": rederived.approved,
            "match": match,
            "mismatched_checks": mismatched,
        })

    all_match = bool(results) and all(r.get("match") for r in results)
    if not results:
        all_match = True  # a quiet cycle (no intents) trivially reproduces
    return {
        "cycle_id": payload.get("cycle_id", ref),
        "found": True,
        "policy_digest_match": digest_match,
        "sealed_policy_digest": sealed_digest[:16],
        "current_policy_digest": current_digest[:16],
        "judged_under": judged_under,
        "decidable": decidable,
        "results": results,
        "all_match": all_match,
        "note": "" if digest_match else (
            "policy.yaml changed since this cycle — re-derived under the policy "
            "sealed in the receipt" if judged_under == "sealed" else
            "policy.yaml changed and this receipt did not seal the policy body — "
            "determinism cannot be decided for this cycle"),
    }


def _parse_dt(s: str) -> datetime:
    try:
        return datetime.fromisoformat(s)
    except (TypeError, ValueError):
        from datetime import timezone
        return datetime.now(timezone.utc)
