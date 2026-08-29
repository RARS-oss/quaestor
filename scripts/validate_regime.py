#!/usr/bin/env python3
"""Validate the day-regime classifier on REAL market bars (not just unit tests).

Mine #2: the regime classifier has only ever seen synthetic bars. Before we let it
gate live capital on Monday we run it against the real tape of the most recent
trading day(s), walk-forward, exactly as production feeds it: an expanding window
of the session's 5-minute bars. For every 5-minute step it prints the label the
classifier WOULD have produced live, plus the features behind that label
(net_move / vwap_dev / rvol), so we can eyeball whether the reads are sane —
does a quiet chop read RANGE, does a one-way trend read TREND, does nothing absurd
flash STORM. This is the honest weekend check: real data, real logic, no fills.

Note: historical option IV is not replayed here, so the STORM-by-IV path is not
exercised (the live cycle has the chain). STORM-by-realized-vol IS exercised, and
the trend/range reads — the ones that actually gate the income sleeve — are fully
real. Usage:

    python -m scripts.validate_regime                # last trading day, SPY+QQQ
    python -m scripts.validate_regime 2026-08-28     # a specific date
    python -m scripts.validate_regime --days 3       # last 3 trading days
    python -m scripts.validate_regime --symbols SPY,QQQ,IWM
"""
from __future__ import annotations

import sys
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

sys.path.insert(0, ".")

from alpaca.data.historical.stock import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame
from alpaca.data.enums import DataFeed

from quaestor import regime as R
from quaestor.config import load_settings
from quaestor.data import _normalize_bar

ET = ZoneInfo("America/New_York")
RTH_OPEN = time(9, 30)
RTH_CLOSE = time(16, 0)


def _last_trading_days(n: int) -> list[date]:
    """The n most recent weekdays on/before yesterday (holiday-naive; good enough
    for a sanity replay — a holiday just yields an empty bar set we skip)."""
    out: list[date] = []
    # start from yesterday ET so we only ask for completed sessions
    d = datetime.now(ET).date() - timedelta(days=1)
    while len(out) < n:
        if d.weekday() < 5:            # Mon..Fri
            out.append(d)
        d -= timedelta(days=1)
    return list(reversed(out))


def _fetch(client: StockHistoricalDataClient, symbol: str, day: date) -> list[dict]:
    """Real 5-minute RTH bars for one symbol on one day, IEX feed, normalized like prod."""
    start = datetime.combine(day, RTH_OPEN, tzinfo=ET).astimezone(timezone.utc)
    end = datetime.combine(day, RTH_CLOSE, tzinfo=ET).astimezone(timezone.utc)
    req = StockBarsRequest(
        symbol_or_symbols=[symbol],
        timeframe=TimeFrame(5, TimeFrame.Minute.unit_value if hasattr(TimeFrame.Minute, "unit_value") else 1),
        start=start,
        end=end,
        feed=DataFeed.IEX,
    )
    # TimeFrame(5, Minute) construction differs across alpaca-py versions; fall back safely.
    try:
        resp = client.get_stock_bars(req)
    except Exception:
        from alpaca.data.timeframe import TimeFrameUnit
        req = StockBarsRequest(symbol_or_symbols=[symbol],
                               timeframe=TimeFrame(5, TimeFrameUnit.Minute),
                               start=start, end=end, feed=DataFeed.IEX)
        resp = client.get_stock_bars(req)
    data = getattr(resp, "data", None)
    if not isinstance(data, dict):
        data = resp if isinstance(resp, dict) else {}
    return [_normalize_bar(b) for b in (data.get(symbol) or [])]


def _spot(bars: list[dict]) -> float:
    for b in reversed(bars):
        if b.get("c") is not None:
            return float(b["c"])
    return 0.0


def _walk(symbol: str, day: date, bars: list[dict]) -> dict[str, int]:
    """Walk the session forward; classify at each 5-min step on the tape-so-far."""
    tally: dict[str, int] = {}
    if len(bars) < R.MIN_BARS:
        print(f"    {symbol}: only {len(bars)} bars — market holiday or thin data; skipped")
        return tally
    print(f"    {symbol}  ({len(bars)} bars, open={bars[0].get('c')}, close={bars[-1].get('c')})")
    print(f"      {'ET':>5}  {'label':<8} {'net_move':>9} {'vwap_dev':>9} {'rvol':>8}  reason")
    for t in range(R.MIN_BARS, len(bars) + 1):
        window = bars[:t]
        diag = R.diagnostics(window, {}, _spot(window))
        tally[diag["label"]] = tally.get(diag["label"], 0) + 1
        # label the step by the ET clock time of the last bar in the window
        ts = window[-1].get("t") or ""
        hhmm = "?"
        try:
            hhmm = datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone(ET).strftime("%H:%M")
        except Exception:
            pass
        nm = diag["net_move"]
        vd = diag["vwap_dev"]
        rv = diag["rvol"]
        # print every 6th step (~30 min) plus any step whose label changed, to keep it readable
        show = (t == R.MIN_BARS) or (t % 6 == 0) or (t == len(bars))
        if show:
            print(f"      {hhmm:>5}  {diag['label']:<8} "
                  f"{(f'{nm:+.4f}' if nm is not None else '   n/a'):>9} "
                  f"{(f'{vd:+.4f}' if vd is not None else '   n/a'):>9} "
                  f"{(f'{rv:.5f}' if rv is not None else '  n/a'):>8}  {diag.get('reason','')}")
    return tally


def main(argv: list[str]) -> int:
    symbols = ["SPY", "QQQ"]
    days_back = 1
    explicit: date | None = None
    i = 0
    while i < len(argv):
        a = argv[i]
        if a == "--days":
            days_back = int(argv[i + 1]); i += 2; continue
        if a == "--symbols":
            symbols = [s.strip().upper() for s in argv[i + 1].split(",") if s.strip()]; i += 2; continue
        try:
            explicit = date.fromisoformat(a)
        except ValueError:
            print(f"unrecognized arg {a!r}"); return 2
        i += 1

    settings = load_settings()
    client = StockHistoricalDataClient(api_key=settings.api_key, secret_key=settings.api_secret)
    days = [explicit] if explicit else _last_trading_days(days_back)

    print("=" * 78)
    print("REGIME CLASSIFIER — LIVE-TAPE VALIDATION (real IEX 5-min bars, walk-forward)")
    print(f"symbols={symbols}  days={[d.isoformat() for d in days]}")
    print("=" * 78)

    grand: dict[str, int] = {}
    per_day: list[tuple[str, dict[str, int]]] = []
    for day in days:
        print(f"\n{day.isoformat()} ({day.strftime('%A')}):")
        day_tally: dict[str, int] = {}
        for sym in symbols:
            try:
                bars = _fetch(client, sym, day)
            except Exception as exc:
                print(f"    {sym}: fetch failed: {exc!r}")
                continue
            tally = _walk(sym, day, bars)
            for k, v in tally.items():
                grand[k] = grand.get(k, 0) + v
                day_tally[k] = day_tally.get(k, 0) + v
        per_day.append((day.isoformat(), day_tally))

    # Per-day RANGE share — the friend's exact question: does the income sleeve go silent
    # on real quiet days? A day near 0% range would mean the condor never sells that day.
    print("\n" + "=" * 78)
    print("PER-DAY income-sleeve activity (RANGE share = fraction of steps the condor could sell):")
    for iso, dt in per_day:
        tot = sum(dt.values()) or 1
        rng = dt.get(R.RANGE, 0)
        flag = "  <- income quiet" if rng / tot < 0.15 else ""
        print(f"  {iso}: range {100*rng/tot:4.1f}%  trend {100*dt.get(R.TREND,0)/tot:4.1f}%  "
              f"unknown {100*dt.get(R.UNKNOWN,0)/tot:4.1f}%{flag}")

    total = sum(grand.values()) or 1
    print("\n" + "=" * 78)
    print("SUMMARY — label distribution across all walk-forward steps:")
    for label in (R.TREND, R.RANGE, R.STORM, R.UNKNOWN):
        n = grand.get(label, 0)
        bar = "#" * round(40 * n / total)
        print(f"  {label:<8} {n:>4} ({100*n/total:4.1f}%) {bar}")
    # sanity flags
    print("\nSANITY:")
    if grand.get(R.STORM, 0) / total > 0.5:
        print("  ! >50% STORM — thresholds may be too tight for this tape (or it WAS a storm day)")
    elif grand.get(R.UNKNOWN, 0) / total > 0.8:
        print("  ! mostly UNKNOWN — classifier rarely commits; fine (conservative) but low signal")
    else:
        print("  ok — classifier commits to trend/range reads and doesn't flash storm on a calm tape")
    print("  (income sleeve fires ONLY on a RANGE read; long convexity ONLY on TREND; STORM stands down)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
