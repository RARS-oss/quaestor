"""Shared data contracts for quaestor. Every module speaks these types.

Conventions locked to Alpaca's API (verified 2026-08-28):
- Option symbols are OCC format: ROOT + YYMMDD + C/P + strike*1000 zero-padded to 8 digits.
- Multi-leg (mleg) orders: max 4 legs, ratio_qty coprime across legs (GCD == 1),
  limit_price is NET per strategy unit: positive = debit, negative = credit.
- Options TIF is always "day". qty is whole contracts. No notional, no extended hours.
- Every order carries a unique client_order_id; retries must look up by it, never resubmit blind.
"""
from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field, asdict
from enum import Enum
from typing import Any, Optional


class Structure(str, Enum):
    LONG_CALL = "long_call"
    LONG_PUT = "long_put"
    VERTICAL_DEBIT = "vertical_debit"     # buy near strike, sell far strike, net debit > 0
    VERTICAL_CREDIT = "vertical_credit"   # sell near, buy protection, net credit -> limit_price < 0
    STRADDLE = "straddle"                 # long call + long put, same strike/expiry, net debit
    CLOSE = "close"                       # closing an existing position (single or mleg)


class Side(str, Enum):
    BUY = "buy"
    SELL = "sell"


class PositionIntent(str, Enum):
    BUY_TO_OPEN = "buy_to_open"
    BUY_TO_CLOSE = "buy_to_close"
    SELL_TO_OPEN = "sell_to_open"
    SELL_TO_CLOSE = "sell_to_close"


@dataclass
class Leg:
    """One leg of an options order. ratio_qty values across legs must be coprime."""
    symbol: str                     # OCC symbol, e.g. SPY260904C00650000
    side: Side
    ratio_qty: int
    position_intent: PositionIntent

    def to_alpaca(self) -> dict[str, str]:
        return {
            "symbol": self.symbol,
            "ratio_qty": str(self.ratio_qty),
            "side": self.side.value,
            "position_intent": self.position_intent.value,
        }


@dataclass
class TradeIntent:
    """A fully-specified proposed trade. Produced by strategy, judged by risk, executed by broker."""
    underlying: str
    structure: Structure
    legs: list[Leg]
    qty: int                         # strategy units (each leg fills qty * ratio_qty contracts)
    limit_price: float               # NET per unit: >0 debit, <0 credit (mleg convention)
    thesis: str                      # one-sentence human-readable reason
    max_loss_usd: float              # worst-case loss for the whole order (defined risk only)
    catalyst_tag: str = ""           # e.g. "NFP", "AVGO-earnings", "" for core flow
    is_0dte: bool = False
    expiry: str = ""                 # YYYY-MM-DD of nearest leg expiry
    signal_snapshot: dict[str, Any] = field(default_factory=dict)
    intent_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    created_at: float = field(default_factory=time.time)

    @property
    def is_credit(self) -> bool:
        return self.limit_price < 0

    @property
    def is_multileg(self) -> bool:
        return len(self.legs) > 1

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["structure"] = self.structure.value
        for leg in d["legs"]:
            leg["side"] = leg["side"].value if isinstance(leg["side"], Side) else leg["side"]
            pi = leg["position_intent"]
            leg["position_intent"] = pi.value if isinstance(pi, PositionIntent) else pi
        return d


@dataclass
class RiskCheck:
    name: str
    ok: bool
    detail: str


@dataclass
class RiskVerdict:
    approved: bool
    checks: list[RiskCheck]
    policy_digest: str               # sha256 of the policy file that judged this intent
    intent_id: str

    @property
    def reasons(self) -> list[str]:
        return [c.detail for c in self.checks if not c.ok]

    def to_dict(self) -> dict[str, Any]:
        return {
            "approved": self.approved,
            "intent_id": self.intent_id,
            "policy_digest": self.policy_digest,
            "checks": [asdict(c) for c in self.checks],
        }


@dataclass
class ExecutionReport:
    """Result of trying to execute one approved intent."""
    intent_id: str
    client_order_id: str
    order_id: str = ""
    status: str = ""                 # accepted|filled|partially_filled|canceled|rejected|error
    filled_qty: float = 0.0
    filled_avg_price: float = 0.0
    attempts: int = 0
    request_ids: list[str] = field(default_factory=list)   # X-Request-ID per Alpaca call
    attempts_log: list[dict] = field(default_factory=list)  # per-attempt {attempt, client_order_id, limit_price, qty}
    error: str = ""
    raw: dict[str, Any] = field(default_factory=dict)
    submitted_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class AccountSnapshot:
    equity: float
    cash: float
    buying_power: float
    options_buying_power: float
    options_approved_level: int
    options_trading_level: int
    positions: list[dict[str, Any]] = field(default_factory=dict)  # raw position dicts
    ts: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class CycleRecord:
    """One full decision cycle: what we saw, decided, and did. Archived + receipted."""
    cycle_id: str
    started_at: float
    account: Optional[AccountSnapshot] = None
    intents: list[dict[str, Any]] = field(default_factory=list)       # TradeIntent.to_dict()
    verdicts: list[dict[str, Any]] = field(default_factory=list)      # RiskVerdict.to_dict()
    executions: list[dict[str, Any]] = field(default_factory=list)    # ExecutionReport.to_dict()
    receipt_path: str = ""
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        if self.account:
            d["account"] = self.account.to_dict()
        return d


def new_client_order_id(intent_id: str, attempt: int, zk_commitment_prefix: str = "") -> str:
    """Deterministic idempotency key binding order -> intent -> (optional) ZK risk proof.
    Max 128 chars per Alpaca. Never reuse across logical orders."""
    parts = ["q", intent_id, f"a{attempt}"]
    if zk_commitment_prefix:
        parts.append(zk_commitment_prefix[:16])
    return "-".join(parts)
