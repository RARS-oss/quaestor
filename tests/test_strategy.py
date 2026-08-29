"""Fixture-driven unit tests for quaestor.strategy.decide() — no network.

Builds synthetic Contexts with hand-made chains (occ -> {bid, ask, mid, delta, ...}),
Alpaca-shaped position dicts (string numbers, side long/short) and calendar
due_events, then checks each playbook: flat -> no intents, bullish -> debit
vertical with correct legs/sides/limit sign, NFP_OPEN_PLAY -> 0DTE straddle,
ALL_CASH -> CLOSE-everything, plus stop/target/0DTE-flatten exits and the
max-loss sizing math.
"""
from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from quaestor.models import PositionIntent, Side, Structure, AccountSnapshot
from quaestor.signals import Signal
from quaestor.strategy import Context, decide

ET = ZoneInfo("America/New_York")

POLICY: dict = {
    "account": {
        "daily_loss_halt_pct": 15,
        "max_concurrent_positions": 3,
    },
    "per_trade": {
        "max_loss_pct_default": 10,
        "max_loss_pct_catalyst": 20,
        "min_open_interest": 100,
        "max_spread_quality": 0.03,
        "min_leg_price": 0.05,
    },
    "timing": {
        "no_new_0dte_after_et": "15:10",
        "flat_0dte_by_et": "15:25",
        "no_new_positions_after_et": "15:45",
        "final_day": "2026-09-04",
        "final_day_all_cash_by_et": "10:30",
    },
}


# --- tiny local OCC builder (tests only; strategy never constructs symbols) --

def _occ(root: str, yymmdd: str, cp: str, strike: float) -> str:
    return f"{root}{yymmdd}{cp}{int(round(strike * 1000)):08d}"


def _q(bid: float, ask: float, delta: float | None = None) -> dict:
    mid = round((bid + ask) / 2.0, 4)
    return {
        "bid": bid, "ask": ask, "mid": mid, "last": mid,
        "iv": 0.15, "delta": delta, "gamma": None, "theta": None, "vega": None,
        "t": 1_756_800_000,
    }


def _account(equity: float = 100_000.0, positions: list[dict] | None = None) -> AccountSnapshot:
    return AccountSnapshot(
        equity=equity, cash=equity, buying_power=equity,
        options_buying_power=equity, options_approved_level=3,
        options_trading_level=3, positions=positions or [],
    )


def _flat_signals() -> dict[str, Signal]:
    return {
        "SPY": Signal("SPY", 0, 0.0, {"ret_5m": 0.0, "ret_30m": 0.0, "vwap_dev": 0.0,
                                      "range_pos": 0.5, "rv_30m": 0.1, "score": 0.0}),
        "QQQ": Signal("QQQ", 0, 0.0, {"ret_5m": 0.0, "ret_30m": 0.0, "vwap_dev": 0.0,
                                      "range_pos": 0.5, "rv_30m": 0.1, "score": 0.0}),
    }


def _bull_spy_signal(strength: float = 0.65) -> Signal:
    return Signal("SPY", 1, strength, {"ret_5m": 0.002, "ret_30m": 0.004, "vwap_dev": 0.0015,
                                       "range_pos": 0.9, "rv_30m": 0.12, "score": 0.85})


# SPY chain, single expiry 2026-09-03 (1 DTE from the test "now")
SPY_EXP = "260903"
SPY_C640 = _occ("SPY", SPY_EXP, "C", 640)
SPY_C650 = _occ("SPY", SPY_EXP, "C", 650)
SPY_C655 = _occ("SPY", SPY_EXP, "C", 655)
SPY_C660 = _occ("SPY", SPY_EXP, "C", 660)
SPY_P650 = _occ("SPY", SPY_EXP, "P", 650)
SPY_P645 = _occ("SPY", SPY_EXP, "P", 645)
SPY_P640 = _occ("SPY", SPY_EXP, "P", 640)


def _spy_chain() -> dict[str, dict]:
    return {
        SPY_C640: _q(5.90, 6.10, 0.60),
        SPY_C650: _q(3.95, 4.05, 0.45),   # mid 4.00 -> long leg
        SPY_C655: _q(1.95, 2.05, 0.25),   # mid 2.00 -> short leg
        SPY_C660: _q(0.75, 0.85, 0.10),
        SPY_P650: _q(3.40, 3.60, -0.55),
        SPY_P645: _q(2.20, 2.40, -0.45),
        SPY_P640: _q(1.40, 1.60, -0.25),
    }


def _spy_contracts(expiry: str = "2026-09-03") -> list[dict]:
    return [
        {"symbol": SPY_C650, "expiration_date": expiry, "strike_price": "650", "type": "call"},
        {"symbol": SPY_C655, "expiration_date": expiry, "strike_price": "655", "type": "call"},
        {"symbol": SPY_P645, "expiration_date": expiry, "strike_price": "645", "type": "put"},
    ]


def make_ctx(**over) -> Context:
    kw: dict = {
        "settings": None,
        "policy": POLICY,
        "calendar": {},
        "account": _account(),
        "portfolio": None,
        "signals": _flat_signals(),
        "sentiment": {},
        "chains": {"SPY": _spy_chain()},
        "contracts": {"SPY": _spy_contracts()},
        "now": datetime(2026, 9, 2, 10, 30, tzinfo=ET),
        "due_events": [],
        # Default to TREND so the long-playbook tests exercise their path; the
        # income/storm tests override regimes explicitly.
        "regimes": {"SPY": "trend", "QQQ": "trend"},
        # A ~1.2% typical daily move so the catalyst IM/RM gate lets a normally-priced
        # straddle through; the quiet-day test overrides this to a small value.
        "realized_moves": {"SPY": 0.012, "QQQ": 0.012},
    }
    kw.update(over)
    return Context(**kw)


# --- playbook 1: core momentum debit vertical --------------------------------

def test_no_intents_when_flat() -> None:
    ctx = make_ctx()
    assert decide(ctx) == []


def test_bullish_debit_vertical_legs_and_sizing() -> None:
    ctx = make_ctx(signals={**_flat_signals(), "SPY": _bull_spy_signal()})
    intents = decide(ctx)
    assert len(intents) == 1
    it = intents[0]
    assert it.structure == Structure.VERTICAL_DEBIT
    assert it.underlying == "SPY"
    assert len(it.legs) == 2
    buy, sell = it.legs
    assert buy.symbol == SPY_C650                      # ~0.45 delta call
    assert buy.side == Side.BUY
    assert buy.position_intent == PositionIntent.BUY_TO_OPEN
    assert buy.ratio_qty == 1
    assert sell.symbol == SPY_C655                     # ~0.25 delta call
    assert sell.side == Side.SELL
    assert sell.position_intent == PositionIntent.SELL_TO_OPEN
    assert sell.ratio_qty == 1
    # net debit -> positive limit (mleg sign convention), 4.00 - 2.00 = 2.00
    assert it.limit_price == pytest.approx(2.00)
    assert it.limit_price > 0
    assert not it.is_credit
    # sizing: cap = 10% of 100k = 10_000; per unit = 2.00*100 = 200 -> 50 units
    assert it.qty == 50
    assert it.max_loss_usd == pytest.approx(it.limit_price * 100 * it.qty)
    assert it.max_loss_usd == pytest.approx(10_000.0)
    assert it.expiry == "2026-09-03"
    assert it.is_0dte is False
    assert it.catalyst_tag == ""
    assert it.thesis
    for key in ("ret_5m", "ret_30m", "vwap_dev", "range_pos", "rv_30m"):
        assert key in it.signal_snapshot


def test_bearish_signal_builds_put_vertical() -> None:
    bear = Signal("SPY", -1, 0.7, {"ret_5m": -0.002, "ret_30m": -0.004, "vwap_dev": -0.002,
                                   "range_pos": 0.1, "rv_30m": 0.15, "score": -0.7})
    ctx = make_ctx(signals={**_flat_signals(), "SPY": bear})
    intents = decide(ctx)
    assert len(intents) == 1
    it = intents[0]
    assert it.structure == Structure.VERTICAL_DEBIT
    buy, sell = it.legs
    assert buy.symbol == SPY_P645                      # ~0.45 |delta| put
    assert sell.symbol == SPY_P640                     # ~0.25 |delta| put
    assert buy.side == Side.BUY and sell.side == Side.SELL
    # buy 645P mid 2.30, sell 640P mid 1.50 -> debit 0.80
    assert it.limit_price == pytest.approx(0.80)
    assert it.max_loss_usd == pytest.approx(it.limit_price * 100 * it.qty)


def test_weak_signal_produces_nothing() -> None:
    ctx = make_ctx(signals={**_flat_signals(), "SPY": _bull_spy_signal(strength=0.5)})
    assert decide(ctx) == []


def test_vertical_skipped_when_cap_too_small() -> None:
    # cap = 10% of 1500 = 150 USD < one unit's 200 USD max loss -> no trade
    ctx = make_ctx(account=_account(equity=1_500.0),
                   signals={**_flat_signals(), "SPY": _bull_spy_signal()})
    assert decide(ctx) == []


def test_vertical_skipped_when_same_direction_position_open() -> None:
    pos = {
        "symbol": SPY_C650, "asset_class": "us_option", "qty": "1", "side": "long",
        "avg_entry_price": "4.00", "cost_basis": "400", "unrealized_pl": "40",
        "unrealized_plpc": "0.10", "current_price": "4.40",
    }
    ctx = make_ctx(account=_account(positions=[pos]),
                   signals={**_flat_signals(), "SPY": _bull_spy_signal()})
    assert decide(ctx) == []


# --- playbook 2: catalyst straddle -------------------------------------------

NFP_EXP = "260904"
NFP_C648 = _occ("SPY", NFP_EXP, "C", 648)
NFP_P648 = _occ("SPY", NFP_EXP, "P", 648)
NFP_C650 = _occ("SPY", NFP_EXP, "C", 650)
NFP_P650 = _occ("SPY", NFP_EXP, "P", 650)
NFP_C652 = _occ("SPY", NFP_EXP, "C", 652)
NFP_P652 = _occ("SPY", NFP_EXP, "P", 652)


def _nfp_chain() -> dict[str, dict]:
    return {
        NFP_C648: _q(2.60, 2.80, 0.62),
        NFP_P648: _q(0.55, 0.65, -0.38),
        NFP_C650: _q(1.45, 1.55, 0.50),   # mid 1.50 — ATM call
        NFP_P650: _q(1.35, 1.45, -0.50),  # mid 1.40 — ATM put
        NFP_C652: _q(0.60, 0.70, 0.38),
        NFP_P652: _q(2.50, 2.70, -0.62),
    }


def test_straddle_on_nfp_open_play() -> None:
    now = datetime(2026, 9, 4, 9, 31, tzinfo=ET)
    ctx = make_ctx(
        now=now,
        chains={"SPY": _nfp_chain()},
        contracts={"SPY": [
            {"symbol": NFP_C650, "expiration_date": "2026-09-04", "strike_price": "650", "type": "call"},
            {"symbol": NFP_P650, "expiration_date": "2026-09-04", "strike_price": "650", "type": "put"},
        ]},
        due_events=[{"time": "09:30", "tag": "NFP_OPEN_PLAY",
                     "desc": "Post-NFP open playbook window", "status": "confirmed"}],
    )
    intents = decide(ctx)
    assert len(intents) == 1
    it = intents[0]
    assert it.structure == Structure.STRADDLE
    assert it.underlying == "SPY"
    assert it.catalyst_tag == "NFP_OPEN_PLAY"
    assert it.is_0dte is True
    assert it.expiry == "2026-09-04"
    assert len(it.legs) == 2
    assert {leg.symbol for leg in it.legs} == {NFP_C650, NFP_P650}
    for leg in it.legs:
        assert leg.side == Side.BUY
        assert leg.position_intent == PositionIntent.BUY_TO_OPEN
        assert leg.ratio_qty == 1
    # net debit = 1.50 + 1.40 = 2.90, positive limit
    assert it.limit_price == pytest.approx(2.90)
    # sizing: catalyst cap = 20% of 100k = 20_000; 20_000 // 290 = 68 units
    assert it.qty == 68
    assert it.max_loss_usd == pytest.approx(it.limit_price * 100 * it.qty)
    assert it.max_loss_usd <= 20_000.0 + 1e-6
    assert it.thesis
    assert it.signal_snapshot.get("catalyst") == "NFP_OPEN_PLAY"
    # IM/RM was recorded for the audit trail (straddle price 2.90 / spot 650 = 0.446%)
    assert it.signal_snapshot.get("implied_move") == pytest.approx(2.90 / 650, abs=1e-4)


def _nfp_ctx(**over):
    """The NFP open-play setup shared by the straddle + IM/RM gate tests."""
    kw = dict(
        now=datetime(2026, 9, 4, 9, 31, tzinfo=ET),
        chains={"SPY": _nfp_chain()},
        contracts={"SPY": [
            {"symbol": NFP_C650, "expiration_date": "2026-09-04", "strike_price": "650", "type": "call"},
            {"symbol": NFP_P650, "expiration_date": "2026-09-04", "strike_price": "650", "type": "put"},
        ]},
        due_events=[{"time": "09:30", "tag": "NFP_OPEN_PLAY",
                     "desc": "Post-NFP open playbook window", "status": "confirmed"}],
    )
    kw.update(over)
    return make_ctx(**kw)


def test_catalyst_straddle_skipped_when_vol_not_cheap() -> None:
    """IM/RM gate (the fourth mine): a long straddle whose implied move is NOT cheap vs
    the realized daily move must be skipped — it would just bleed theta on a quiet day.
    IM = 2.90/650 = 0.446%; with RM = 0.30% the ratio is ~1.49 >> 0.85, so no straddle."""
    ctx = _nfp_ctx(realized_moves={"SPY": 0.0030, "QQQ": 0.0030})
    assert decide(ctx) == []


def test_catalyst_straddle_fires_when_vol_cheap() -> None:
    # RM = 1.2% makes IM 0.446% < 0.85*1.2% = 1.02% -> genuinely cheap vol -> buy.
    ctx = _nfp_ctx(realized_moves={"SPY": 0.012, "QQQ": 0.012})
    intents = decide(ctx)
    assert len(intents) == 1 and intents[0].structure == Structure.STRADDLE


def test_avgo_earnings_is_not_traded_via_index_proxy() -> None:
    """Upgrade #4: a single-stock earnings tag must NOT trigger an index straddle.
    Broadcom +-5% barely moves QQQ (+-0.25%), so a QQQ bet can't pay for the AVGO move.
    AVGO_EARNINGS is informational only — no STRADDLE_PLAYS mapping -> no trade."""
    ctx = make_ctx(
        now=datetime(2026, 9, 3, 15, 55, tzinfo=ET),
        due_events=[{"time": "16:05", "tag": "AVGO_EARNINGS",
                     "desc": "Broadcom earnings AMC", "status": "inferred"}],
    )
    # no catalyst straddle/directional on QQQ (or anything) for AVGO earnings
    assert all(i.catalyst_tag != "AVGO_EARNINGS" for i in decide(ctx))
    assert all(i.structure != Structure.STRADDLE for i in decide(ctx))


AVGO_EXP = "260904"
def _avgo(cp: str, k: int) -> str: return _occ("AVGO", AVGO_EXP, cp, k)


def _avgo_earnings_chain() -> dict[str, dict]:
    # spot ~300, earnings implied move ~+-5% (ATM straddle 15 -> IM 0.05). Shorts ~0.18d
    # at +-15 (315/285), wings ~0.08d at +-22 (322/278). Rich credit (43% of width).
    return {
        _avgo("C", 300): _q(7.4, 7.6, 0.50), _avgo("P", 300): _q(7.4, 7.6, -0.50),
        _avgo("C", 315): _q(2.9, 3.1, 0.18), _avgo("P", 285): _q(2.9, 3.1, -0.18),
        _avgo("C", 322): _q(1.4, 1.6, 0.08), _avgo("P", 278): _q(1.4, 1.6, -0.08),
    }


def _avgo_earnings_contracts() -> list[dict]:
    out = []
    for k, cp in [(300, "call"), (300, "put"), (315, "call"), (285, "put"),
                  (322, "call"), (278, "put")]:
        sym = _avgo("C" if cp == "call" else "P", k)
        out.append({"symbol": sym, "expiration_date": "2026-09-04",
                    "strike_price": str(k), "type": cp})
    return out


def _avgo_ctx(**over):
    kw = dict(
        now=datetime(2026, 9, 3, 15, 35, tzinfo=ET),        # pre-close, before 15:45 cutoff
        calendar={"week": [{"date": "2026-09-03", "events": [
            {"time": "16:05", "tag": "AVGO_EARNINGS", "desc": "Broadcom AMC", "status": "inferred"}]}]},
        chains={"AVGO": _avgo_earnings_chain()},
        contracts={"AVGO": _avgo_earnings_contracts()},
        realized_moves={"AVGO": 0.025, "SPY": 0.012, "QQQ": 0.012},
        due_events=[],
    )
    kw.update(over)
    return make_ctx(**kw)


def test_avgo_earnings_iv_crush_harvest_fires_when_vol_rich() -> None:
    """The opt-in sell-side: before AVGO's AMC report, with implied move (5%) well above
    realized (2.5%) -> ratio 2.0 > 1.3 -> SELL a defined-risk iron condor to harvest the
    IV crush. Next-day expiry (survives the report), net credit, sized to the income cap."""
    from quaestor.strategy import earnings_underlyings_today
    from datetime import date as _date
    assert earnings_underlyings_today(
        {"week": [{"date": "2026-09-03", "events": [{"tag": "AVGO_EARNINGS"}]}]},
        _date(2026, 9, 3)) == {"AVGO"}

    intents = decide(_avgo_ctx())
    harvest = [i for i in intents if i.underlying == "AVGO"]
    assert len(harvest) == 1
    it = harvest[0]
    assert it.structure == Structure.VERTICAL_CREDIT
    assert it.catalyst_tag == "AVGO_EARNINGS"
    assert it.limit_price < 0                            # net credit
    assert it.expiry == "2026-09-04"                     # holds PAST the AMC report
    assert it.is_0dte is False
    assert len(it.legs) == 4
    assert it.max_loss_usd <= 2_500.0 + 1e-6             # income-cap sized, gap bounded
    assert it.signal_snapshot.get("harvest") is True
    assert it.signal_snapshot.get("im_rm_ratio") == pytest.approx(2.0, abs=0.05)


def test_avgo_harvest_silent_before_entry_window() -> None:
    # same rich vol, but mid-morning -> too early to sell peak IV -> no harvest
    intents = decide(_avgo_ctx(now=datetime(2026, 9, 3, 10, 30, tzinfo=ET)))
    assert [i for i in intents if i.underlying == "AVGO"] == []


def test_avgo_harvest_silent_when_vol_not_rich() -> None:
    # realized move 5% == implied 5% -> ratio 1.0 <= 1.3 -> vol not rich -> don't sell
    intents = decide(_avgo_ctx(realized_moves={"AVGO": 0.05, "SPY": 0.012, "QQQ": 0.012}))
    assert [i for i in intents if i.underlying == "AVGO"] == []


def test_catalyst_directional_lean_unaffected_by_im_rm_gate() -> None:
    """The gate guards ONLY the pure straddle. With a confirmed lean, the catalyst
    still fires as a directional long even when event vol is not cheap (small RM)."""
    ctx = _nfp_ctx(
        signals={**_flat_signals(), "SPY": _bull_spy_signal(strength=0.65)},
        realized_moves={"SPY": 0.0030, "QQQ": 0.0030},
    )
    intents = decide(ctx)
    assert len(intents) == 1
    assert intents[0].structure == Structure.LONG_CALL      # directional lean, not a straddle
    assert intents[0].catalyst_tag == "NFP_OPEN_PLAY"


def test_income_condor_on_range_day() -> None:
    from quaestor import regime as R
    exp = "2026-09-02"
    def s(root, e, t, k): return _occ(root, "260902", t, k)
    lp, sp, sc, lc = s("SPY", exp, "P", 650), s("SPY", exp, "P", 655), \
                     s("SPY", exp, "C", 665), s("SPY", exp, "C", 670)
    # A realistic, sellable condor: credit 1.40 on $5 wings (28% of width) clears the
    # width-relative floor (mine #3). Shorts ~0.90 mid, wings ~0.20 mid.
    chain = {
        lp: _q(0.15, 0.25, -0.08), sp: _q(0.85, 0.95, -0.18),
        sc: _q(0.85, 0.95, 0.18),  lc: _q(0.15, 0.25, 0.08),
    }
    contracts = [
        {"symbol": lp, "expiration_date": exp, "strike_price": "650", "type": "put"},
        {"symbol": sp, "expiration_date": exp, "strike_price": "655", "type": "put"},
        {"symbol": sc, "expiration_date": exp, "strike_price": "665", "type": "call"},
        {"symbol": lc, "expiration_date": exp, "strike_price": "670", "type": "call"},
    ]
    ctx = make_ctx(
        now=datetime(2026, 9, 2, 10, 30, tzinfo=ET),
        chains={"SPY": chain}, contracts={"SPY": contracts},
        signals=_flat_signals(), regimes={"SPY": R.RANGE},
    )
    intents = decide(ctx)
    condors = [i for i in intents if i.structure == Structure.VERTICAL_CREDIT]
    assert len(condors) == 1
    it = condors[0]
    assert len(it.legs) == 4
    assert it.limit_price < 0                       # net credit
    assert it.max_loss_usd > 0
    # mine #1: premium selling is sized SMALL — worst-case loss stays within the
    # 2.5% income cap ($2,500 on $100k), NOT the 12% directional cap. A 12%-sized
    # condor here would risk ~$12k; the income cap holds it to a few contracts.
    assert it.max_loss_usd <= 2_500.0 + 1e-6
    assert it.qty <= 7                              # small and frequent, not a turnover bet
    # shorts are the near strikes (655 put, 665 call), covered by the far wings
    shorts = {l.symbol for l in it.legs if l.side == Side.SELL}
    assert shorts == {sp, sc}
    # payload builds (covered-short + coprime + <=4 legs)
    from quaestor.orders import build_order_payload
    payload = build_order_payload(it, attempt=0)
    assert payload["order_class"] == "mleg" and len(payload["legs"]) == 4
    assert float(payload["limit_price"]) < 0


def test_range_suppresses_long_debits() -> None:
    from quaestor import regime as R
    # a strong bull signal on a RANGE day must NOT produce a long debit/convex bet
    ctx = make_ctx(signals={**_flat_signals(), "SPY": _bull_spy_signal(strength=0.85)},
                   regimes={"SPY": R.RANGE})
    intents = decide(ctx)
    assert all(i.structure != Structure.LONG_CALL for i in intents)
    assert all(i.structure != Structure.VERTICAL_DEBIT for i in intents)


def test_storm_stands_down() -> None:
    from quaestor import regime as R
    ctx = make_ctx(signals={**_flat_signals(), "SPY": _bull_spy_signal(strength=0.85)},
                   regimes={"SPY": R.STORM})
    assert decide(ctx) == []       # no new entries in a storm


def test_penny_premium_condor_rejected() -> None:
    """mine #3: a $5-wide condor collecting only $0.50 (10% of width) is junk R/R —
    the width-relative min-credit floor must reject it rather than sell mush."""
    from quaestor import regime as R
    exp = "2026-09-02"
    def s(t, k): return _occ("SPY", "260902", t, k)
    lp, sp, sc, lc = s("P", 650), s("P", 655), s("C", 665), s("C", 670)
    # shorts ~0.40, wings ~0.15 -> credit 0.50 on $5 wings = 10% of width (< 20% floor)
    chain = {
        lp: _q(0.10, 0.20, -0.08), sp: _q(0.35, 0.45, -0.18),
        sc: _q(0.35, 0.45, 0.18),  lc: _q(0.10, 0.20, 0.08),
    }
    contracts = [
        {"symbol": lp, "expiration_date": exp, "strike_price": "650", "type": "put"},
        {"symbol": sp, "expiration_date": exp, "strike_price": "655", "type": "put"},
        {"symbol": sc, "expiration_date": exp, "strike_price": "665", "type": "call"},
        {"symbol": lc, "expiration_date": exp, "strike_price": "670", "type": "call"},
    ]
    ctx = make_ctx(
        now=datetime(2026, 9, 2, 10, 30, tzinfo=ET),
        chains={"SPY": chain}, contracts={"SPY": contracts},
        signals=_flat_signals(), regimes={"SPY": R.RANGE},
    )
    condors = [i for i in decide(ctx) if i.structure == Structure.VERTICAL_CREDIT]
    assert condors == []            # too thin to sell


def test_conviction_directional_long_on_strong_signal() -> None:
    # strength >= 0.78 -> a convex directional LONG (single leg), not a vertical
    ctx = make_ctx(signals={**_flat_signals(), "SPY": _bull_spy_signal(strength=0.85)})
    intents = decide(ctx)
    assert len(intents) == 1
    it = intents[0]
    assert it.structure == Structure.LONG_CALL
    assert it.underlying == "SPY"
    assert len(it.legs) == 1
    assert it.legs[0].side == Side.BUY
    assert it.legs[0].position_intent == PositionIntent.BUY_TO_OPEN
    assert it.catalyst_tag == "CONVICTION"     # gets the bigger conviction cap
    assert it.limit_price > 0                  # a debit (long premium)
    assert it.max_loss_usd == pytest.approx(it.limit_price * 100 * it.qty)


def test_catalyst_goes_directional_when_lean_confirmed() -> None:
    now = datetime(2026, 9, 4, 9, 31, tzinfo=ET)
    ctx = make_ctx(
        now=now,
        chains={"SPY": _nfp_chain()},
        contracts={"SPY": [
            {"symbol": NFP_C650, "expiration_date": "2026-09-04", "strike_price": "650", "type": "call"},
            {"symbol": NFP_P650, "expiration_date": "2026-09-04", "strike_price": "650", "type": "put"},
        ]},
        signals={**_flat_signals(), "SPY": _bull_spy_signal(strength=0.6)},  # a confirmed lean
        due_events=[{"time": "09:30", "tag": "NFP_OPEN_PLAY", "desc": "post-NFP", "status": "confirmed"}],
    )
    intents = decide(ctx)
    assert len(intents) == 1
    it = intents[0]
    assert it.structure == Structure.LONG_CALL   # directional, not a straddle
    assert it.catalyst_tag == "NFP_OPEN_PLAY"
    assert it.is_0dte is True
    assert len(it.legs) == 1


def test_no_straddle_without_due_event() -> None:
    ctx = make_ctx(
        now=datetime(2026, 9, 4, 9, 31, tzinfo=ET),
        chains={"SPY": _nfp_chain()},
        contracts={"SPY": [
            {"symbol": NFP_C650, "expiration_date": "2026-09-04", "strike_price": "650", "type": "call"},
        ]},
        due_events=[],
    )
    assert decide(ctx) == []


# --- playbook 3: exit management ---------------------------------------------

def test_close_everything_on_all_cash() -> None:
    long_call = {
        "symbol": NFP_C650, "asset_class": "us_option", "qty": "2", "side": "long",
        "avg_entry_price": "1.50", "cost_basis": "300", "unrealized_pl": "-30",
        "unrealized_plpc": "-0.10", "current_price": "1.35",
    }
    short_put = {
        "symbol": NFP_P648, "asset_class": "us_option", "qty": "-1", "side": "short",
        "avg_entry_price": "0.80", "cost_basis": "-80", "unrealized_pl": "5",
        "unrealized_plpc": "0.06", "current_price": "0.60",
    }
    ctx = make_ctx(
        now=datetime(2026, 9, 4, 10, 31, tzinfo=ET),
        account=_account(positions=[long_call, short_put]),
        # strong signal + full chain: entries must STILL be suppressed by ALL_CASH
        signals={**_flat_signals(), "SPY": _bull_spy_signal(strength=0.95)},
        chains={"SPY": _nfp_chain()},
        contracts={"SPY": [
            {"symbol": NFP_C650, "expiration_date": "2026-09-04", "strike_price": "650", "type": "call"},
        ]},
        due_events=[{"time": "10:30", "tag": "ALL_CASH", "desc": "Flatten everything"}],
    )
    intents = decide(ctx)
    assert len(intents) == 2
    assert all(it.structure == Structure.CLOSE for it in intents)
    by_sym = {it.legs[0].symbol: it for it in intents}
    assert set(by_sym) == {NFP_C650, NFP_P648}

    closing_long = by_sym[NFP_C650]
    assert len(closing_long.legs) == 1
    leg = closing_long.legs[0]
    assert leg.side == Side.SELL
    assert leg.position_intent == PositionIntent.SELL_TO_CLOSE
    assert leg.ratio_qty == 1
    assert closing_long.qty == 2                              # qty from position
    assert closing_long.limit_price == pytest.approx(-1.45)   # sell at bid touch = credit < 0
    assert closing_long.is_credit
    assert closing_long.max_loss_usd == pytest.approx(1.45 * 100 * 2)

    closing_short = by_sym[NFP_P648]
    leg = closing_short.legs[0]
    assert leg.side == Side.BUY
    assert leg.position_intent == PositionIntent.BUY_TO_CLOSE
    assert closing_short.qty == 1
    assert closing_short.limit_price == pytest.approx(0.65)   # buy back at ask touch = debit > 0
    assert not closing_short.is_credit
    assert closing_short.max_loss_usd == pytest.approx(0.65 * 100 * 1)


def test_stop_and_target_exits_fire_healthy_position_kept() -> None:
    stopped = {
        "symbol": SPY_C650, "asset_class": "us_option", "qty": "1", "side": "long",
        "avg_entry_price": "4.00", "cost_basis": "400", "unrealized_pl": "-248",
        "unrealized_plpc": "-0.62", "current_price": "1.52",
    }
    winner = {
        "symbol": SPY_P645, "asset_class": "us_option", "qty": "1", "side": "long",
        "avg_entry_price": "1.00", "cost_basis": "100", "unrealized_pl": "150",
        "unrealized_plpc": "1.50", "current_price": "2.50",
    }
    healthy = {
        "symbol": SPY_C655, "asset_class": "us_option", "qty": "1", "side": "long",
        "avg_entry_price": "1.90", "cost_basis": "190", "unrealized_pl": "19",
        "unrealized_plpc": "0.10", "current_price": "2.09",
    }
    ctx = make_ctx(account=_account(positions=[stopped, winner, healthy]))
    intents = decide(ctx)
    assert len(intents) == 2
    assert all(it.structure == Structure.CLOSE for it in intents)
    symbols = {it.legs[0].symbol for it in intents}
    assert symbols == {SPY_C650, SPY_P645}
    for it in intents:
        assert it.legs[0].position_intent == PositionIntent.SELL_TO_CLOSE
        assert it.limit_price < 0                              # closing longs = credit
        assert it.max_loss_usd == pytest.approx(abs(it.limit_price) * 100 * it.qty)


def test_0dte_flatten_window() -> None:
    pos_0dte = {
        "symbol": _occ("SPY", "260902", "C", 650), "asset_class": "us_option",
        "qty": "1", "side": "long", "avg_entry_price": "1.00", "cost_basis": "100",
        "unrealized_pl": "5", "unrealized_plpc": "0.05", "current_price": "1.05",
    }
    # 15:20 ET is inside the [flat_0dte_by_et - 10min] window -> close
    ctx = make_ctx(now=datetime(2026, 9, 2, 15, 20, tzinfo=ET),
                   account=_account(positions=[pos_0dte]))
    intents = decide(ctx)
    assert len(intents) == 1
    assert intents[0].structure == Structure.CLOSE
    assert intents[0].legs[0].position_intent == PositionIntent.SELL_TO_CLOSE
    # limit falls back to position current_price when the chain has no quote
    assert intents[0].limit_price == pytest.approx(-1.05)

    # 14:00 ET is well before the window -> keep the position
    ctx_early = make_ctx(now=datetime(2026, 9, 2, 14, 0, tzinfo=ET),
                         account=_account(positions=[pos_0dte]))
    assert decide(ctx_early) == []
