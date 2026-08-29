"""Full trading-day simulator — the agent lives a whole day in seconds, offline.

A deterministic, network-free simulation: a scripted SPY/QQQ price path, a toy
option-pricing model, a FakeBroker that fills instantly and tracks positions +
P&L, and a FakeData that serves synthetic bars/chains. The real Agent.run_cycle
drives the day — open -> momentum entry -> the position moves -> take-profit /
stop / 0DTE curfew -> final flat — so the WHOLE strategy lifecycle (entry AND
exit AND P&L) is exercised end to end, which nothing else tests.

Run (WSL):  ~/hack/venv/bin/python scripts/sim_day.py
"""
from __future__ import annotations

import math
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime, time as dtime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from quaestor import agent as agent_mod
from quaestor import clock as clock_mod
from quaestor.agent import build_agent
from quaestor.models import AccountSnapshot, ExecutionReport, Structure, TradeIntent

import sys

ET = ZoneInfo("America/New_York")
# Default: Friday NFP + all-cash final day. Pass YYYY-MM-DD to sim another day
# (e.g. 2026-09-02 exercises take-profit + the 0DTE curfew flat instead).
SIM_DATE = (date.fromisoformat(sys.argv[1]) if len(sys.argv) > 1 else date(2026, 9, 4))
CONTRACT_MULT = 100.0


# --------------------------------------------------------------------- toy options

def _occ(root: str, exp: date, typ: str, strike: float) -> str:
    return f"{root}{exp:%y%m%d}{typ}{int(round(strike * 1000)):08d}"


def _opt_mid(spot: float, strike: float, typ: str, dte: float) -> float:
    """Crude but monotonic option mid: intrinsic + a Gaussian time-value bump."""
    intrinsic = max(0.0, spot - strike) if typ == "C" else max(0.0, strike - spot)
    width = max(1.0, 0.012 * spot)
    tv = (0.011 * spot + 0.0016 * spot * math.sqrt(max(dte, 0.02))) \
        * math.exp(-0.5 * ((strike - spot) / width) ** 2)
    return round(max(0.05, intrinsic + tv), 2)


def _opt_delta(spot: float, strike: float, typ: str) -> float:
    width = max(1.0, 0.02 * spot)
    call_d = 1.0 / (1.0 + math.exp(-(spot - strike) / width))   # logistic ~ N(d1)
    d = call_d if typ == "C" else call_d - 1.0
    return round(d, 3)


def _synth(root: str, spot: float, now: datetime, expiries: list[date]) -> tuple[dict, list[dict]]:
    """Build (chain, contracts) for strikes spanning +/-6% in $1 steps."""
    chain: dict[str, dict] = {}
    contracts: list[dict] = []
    lo, hi = int(spot * 0.94), int(spot * 1.06)
    for exp in expiries:
        dte = max(0.0, (exp - now.date()).days + 0.3)
        for k in range(lo, hi + 1):
            for typ in ("C", "P"):
                sym = _occ(root, exp, typ, k)
                mid = _opt_mid(spot, k, typ, dte)
                spread = max(0.02, round(mid * 0.02, 2))
                chain[sym] = {
                    "bid": round(mid - spread / 2, 2), "ask": round(mid + spread / 2, 2),
                    "mid": mid, "last": mid, "iv": 0.2,
                    "delta": _opt_delta(spot, k, typ), "gamma": 0.01, "theta": -0.05,
                    "vega": 0.1, "oi": 5000, "t": now.isoformat(),
                }
                contracts.append({
                    "symbol": sym, "expiration_date": exp.isoformat(),
                    "strike_price": str(k), "type": "call" if typ == "C" else "put",
                    "tradable": True, "open_interest": 5000,
                    "root_symbol": root, "underlying_symbol": root,
                })
    return chain, contracts


# --------------------------------------------------------------------- sim market

@dataclass
class DayMarket:
    now: datetime
    spots: dict[str, float]
    path: dict[str, list[float]]     # per-underlying spot at each 5-min step
    step_i: int = 0

    def advance(self) -> None:
        self.step_i += 1
        self.now = self.now + timedelta(minutes=5)
        for u, series in self.path.items():
            self.spots[u] = series[min(self.step_i, len(series) - 1)]

    def expiries(self) -> list[date]:
        d = self.now.date()
        nxt = d + timedelta(days=(7 - d.weekday()) % 7 or 7)   # next weekly-ish
        return [d, nxt]

    def chain(self, u: str) -> tuple[dict, list[dict]]:
        return _synth(u, self.spots[u], self.now, self.expiries())


# --------------------------------------------------------------------- fake data

class FakeData:
    def __init__(self, mkt: DayMarket) -> None:
        self.mkt = mkt

    def stock_snapshot(self, symbols: list[str]) -> dict[str, dict]:
        return {u: {"price": self.mkt.spots.get(u, 0.0)} for u in symbols}

    def stock_bars(self, symbols, timeframe: str = "5Min", lookback_minutes: int = 390):
        # Emit the last ~30 steps of the path as rising/f alling bars so signals fire.
        out: dict[str, list[dict]] = {}
        for u in symbols:
            series = self.mkt.path.get(u, [self.mkt.spots.get(u, 0.0)])
            hist = series[max(0, self.mkt.step_i - 30): self.mkt.step_i + 1] or series[:1]
            bars = []
            for i, px in enumerate(hist):
                bars.append({"o": px, "h": px * 1.001, "l": px * 0.999, "c": px,
                             "v": 1_000_000, "vw": px, "n": 100,
                             "t": (self.mkt.now - timedelta(minutes=5 * (len(hist) - i))).isoformat()})
            out[u] = bars
        return out

    def option_chain(self, underlying: str, *, expiry_gte: str, expiry_lte: str,
                     strike_gte=None, strike_lte=None, type_=None) -> dict[str, dict]:
        chain, _ = self.mkt.chain(underlying)
        return {s: q for s, q in chain.items() if expiry_gte <= _exp_of(s) <= expiry_lte}

    def option_snapshots(self, occ_symbols: list[str]) -> dict[str, dict]:
        merged: dict[str, dict] = {}
        for u in self.mkt.spots:
            merged.update(self.mkt.chain(u)[0])
        return {s: merged[s] for s in occ_symbols if s in merged}

    def news(self, symbols, limit: int = 20) -> list[dict]:
        return []


def _exp_of(sym: str) -> str:
    m = sym[-15:-9]
    return f"20{m[0:2]}-{m[2:4]}-{m[4:6]}"


# --------------------------------------------------------------------- fake broker

@dataclass
class FakeBroker:
    mkt: DayMarket
    sealed: bool = False
    request_ids: list[str] = field(default_factory=list)
    last_request_id: str = ""
    positions_: dict[str, dict] = field(default_factory=dict)   # occ -> {qty, entry}
    realized: float = 0.0
    trade_log: list[str] = field(default_factory=list)

    # ---- price helpers
    def _mid(self, sym: str) -> float:
        root = _root_of(sym)
        chain, _ = self.mkt.chain(root)
        q = chain.get(sym)
        return float(q["mid"]) if q else 0.05

    # ---- account / positions
    def account_snapshot(self) -> AccountSnapshot:
        pos = self.positions()
        unreal = sum(float(p["unrealized_pl"]) for p in pos)
        equity = 100_000.0 + self.realized + unreal
        return AccountSnapshot(equity=equity, cash=100_000.0 + self.realized,
                               buying_power=(100_000.0 + self.realized) * 2,
                               options_buying_power=100_000.0 + self.realized,
                               options_approved_level=3, options_trading_level=3,
                               positions=pos)

    def positions(self) -> list[dict]:
        out = []
        for sym, p in self.positions_.items():
            qty, entry = p["qty"], p["entry"]
            cur = self._mid(sym)
            mv = cur * qty * CONTRACT_MULT
            cost = entry * qty * CONTRACT_MULT
            upl = mv - cost
            plpc = (cur - entry) / entry if (entry and qty > 0) else \
                   ((entry - cur) / entry if entry else 0.0)
            out.append({
                "symbol": sym, "asset_class": "us_option",
                "qty": str(int(qty)), "side": "long" if qty > 0 else "short",
                "avg_entry_price": f"{entry:.2f}", "current_price": f"{cur:.2f}",
                "market_value": f"{mv:.2f}", "unrealized_pl": f"{upl:.2f}",
                "unrealized_plpc": f"{plpc:.4f}",
            })
        return out

    def open_orders(self) -> list[dict]:
        return []

    def cancel_opposing(self, symbols) -> list[str]:
        return []

    def close_position(self, symbol_or_id: str) -> dict:
        self.positions_.pop(symbol_or_id, None)
        return {"status": "closed"}

    # ---- execution: fill instantly at the current chain price
    def execute(self, intent: TradeIntent, chain: dict, policy: dict,
                zk_prefix: str = "", chain_refresh=None) -> ExecutionReport:
        cid = f"sim-{intent.intent_id}-{uuid.uuid4().hex[:4]}"
        fills = []
        for leg in intent.legs:
            mid = self._mid(leg.symbol)
            signed = leg.ratio_qty * intent.qty * (1 if leg.side.value == "buy" else -1)
            fills.append((leg.symbol, signed, mid))
        if intent.structure is Structure.CLOSE:
            for sym, signed, mid in fills:
                held = self.positions_.get(sym)
                if held:
                    # realize P&L on the closed quantity
                    self.realized += (mid - held["entry"]) * held["qty"] * CONTRACT_MULT
                    self.positions_.pop(sym, None)
            self.trade_log.append(f"{self.mkt.now:%H:%M} CLOSE {intent.underlying} "
                                  f"({intent.structure.value}) realized=${self.realized:,.0f}")
        else:
            for sym, signed, mid in fills:
                cur = self.positions_.get(sym)
                if cur:
                    cur["qty"] += signed
                else:
                    self.positions_[sym] = {"qty": signed, "entry": mid}
            self.trade_log.append(f"{self.mkt.now:%H:%M} OPEN  {intent.underlying} "
                                  f"{intent.structure.value} x{intent.qty} @ {intent.limit_price:+.2f}")
        return ExecutionReport(intent_id=intent.intent_id, client_order_id=cid,
                               order_id=cid, status="filled",
                               filled_qty=float(intent.qty),
                               filled_avg_price=abs(intent.limit_price), request_ids=[cid],
                               raw={"sim": True})

    def close(self) -> None:
        pass


def _root_of(sym: str) -> str:
    i = 0
    while i < len(sym) and sym[i].isalpha():
        i += 1
    return sym[:i]


# --------------------------------------------------------------------- price paths

def _spy_path() -> list[float]:
    """78 five-min steps 09:30->16:00: gap up, trend up into midday, fade late."""
    base = 660.0
    pts = []
    for i in range(79):
        frac = i / 78.0
        trend = 9.0 * math.sin(min(frac, 0.6) / 0.6 * (math.pi / 2))   # +9 by midday
        fade = -3.0 * max(0.0, frac - 0.7) / 0.3                        # give back late
        wiggle = 0.6 * math.sin(i * 0.9)
        pts.append(round(base + trend + fade + wiggle, 2))
    return pts


def _qqq_path() -> list[float]:
    base = 585.0
    return [round(base + 6.0 * math.sin(min(i / 78.0, 0.6) / 0.6 * (math.pi / 2))
                  + 0.5 * math.sin(i * 0.7), 2) for i in range(79)]


# --------------------------------------------------------------------- run

def main() -> int:
    start = datetime.combine(SIM_DATE, dtime(9, 30), tzinfo=ET)
    spy, qqq = _spy_path(), _qqq_path()
    mkt = DayMarket(now=start, spots={"SPY": spy[0], "QQQ": qqq[0]},
                    path={"SPY": spy, "QQQ": qqq})

    agent = build_agent()
    agent.broker = FakeBroker(mkt)
    agent.data = FakeData(mkt)
    agent.sealed_executor = None       # pure offline sim (execution path is the FakeBroker)
    agent.receipts = None
    # Drive the clock + market-open off the sim, and stub the network contract lookup.
    agent._now_et = lambda: mkt.now
    agent._market_open_now = lambda: dtime(9, 30) <= mkt.now.timetz().replace(tzinfo=None) < dtime(16, 0)
    orig_open = clock_mod.is_market_open_now
    clock_mod.is_market_open_now = lambda *a, **k: agent._market_open_now()

    def fake_discover(settings, underlying, *, expiry_gte, expiry_lte, strike_band_pct=0.03, type_=None):
        _, contracts = mkt.chain(underlying)
        return [c for c in contracts if expiry_gte <= c["expiration_date"] <= expiry_lte]
    agent_mod.universe_mod.discover_contracts = fake_discover

    print("=" * 66)
    print(f"  quaestor — full trading-day simulation ({SIM_DATE}, fake broker)")
    print("=" * 66)
    peak_equity = 100_000.0
    trough_equity = 100_000.0
    cycles = 0
    for step in range(79):
        agent.run_cycle()
        cycles += 1
        eq = agent.broker.account_snapshot().equity
        peak_equity = max(peak_equity, eq)
        trough_equity = min(trough_equity, eq)
        mkt.advance()

    clock_mod.is_market_open_now = orig_open

    fb: FakeBroker = agent.broker
    final = fb.account_snapshot()
    print("\n  --- trade log ---")
    for line in fb.trade_log:
        print("   ", line)
    print("\n  --- day result ---")
    print(f"   cycles run           : {cycles}")
    print(f"   trades               : {len(fb.trade_log)}")
    print(f"   realized P&L         : ${fb.realized:,.2f}")
    print(f"   final equity         : ${final.equity:,.2f}  ({(final.equity/100_000-1)*100:+.2f}%)")
    print(f"   intraday peak/trough : ${peak_equity:,.0f} / ${trough_equity:,.0f}")
    print(f"   open positions at EOD: {len(final.positions)}")
    max_dd = (trough_equity / peak_equity - 1) * 100 if peak_equity else 0.0
    print(f"   max drawdown         : {max_dd:.2f}%")

    ok = True
    if not fb.trade_log:
        ok = False; print("   FAIL: the agent never traded")
    if len(final.positions) != 0:
        print(f"   WARN: {len(final.positions)} positions left open at EOD "
              "(0DTE curfew/flatten should have closed them)")
    print("=" * 66)
    print("DAY SIM: " + ("completed ✓" if ok else "FAILED ✗"))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
