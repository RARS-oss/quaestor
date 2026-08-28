"""Deterministic unit tests for quaestor.clock — no network, no wall clock.

All datetimes are constructed explicitly in ET (America/New_York). Contest-week
facts under test: regular session 09:30-16:00 ET, power_hour 15:00-16:00 ET,
weekdays 2026-08-28 .. 2026-09-04 all trade (no holidays), weekends closed.
"""
from __future__ import annotations

from datetime import date, datetime, timezone

from quaestor.clock import (
    ET,
    due_catalysts,
    is_final_day,
    is_market_open_now,
    minutes_to_close,
    parse_et_hhmm,
    past_cutoff,
    session_phase,
)

# 2026-09-01 is a Tuesday inside the contest week.
TUESDAY = date(2026, 9, 1)

POLICY: dict = {
    "timing": {
        "no_new_0dte_after_et": "15:10",
        "flat_0dte_by_et": "15:25",
        "no_new_positions_after_et": "15:45",
        "final_day": "2026-09-04",
        "final_day_all_cash_by_et": "10:30",
    }
}

CALENDAR: dict = {
    "week": [
        {
            # yaml.safe_load yields date objects for unquoted dates — mirror that.
            "date": date(2026, 8, 31),
            "events": [
                {"time": "09:30", "tag": "MONTH_END", "desc": "Month-end flows"},
            ],
        },
        {
            # String dates must work too.
            "date": "2026-09-01",
            "events": [
                {"time": "10:00", "tag": "ISM_MFG", "desc": "ISM Manufacturing"},
                {"time": "10:00", "tag": "JOLTS", "desc": "JOLTS"},
                {"time": "14:00", "tag": "BEIGE_ISH", "desc": "Afternoon event"},
            ],
        },
    ]
}


def et(y: int, m: int, d: int, hh: int, mm: int, ss: int = 0) -> datetime:
    return datetime(y, m, d, hh, mm, ss, tzinfo=ET)


# ---------------------------------------------------------------- session_phase

def test_session_phase_pre_before_open() -> None:
    assert session_phase(et(2026, 9, 1, 9, 29, 59)) == "pre"
    assert session_phase(et(2026, 9, 1, 4, 0)) == "pre"


def test_session_phase_open_boundary_inclusive() -> None:
    assert session_phase(et(2026, 9, 1, 9, 30, 0)) == "open"
    assert session_phase(et(2026, 9, 1, 12, 0)) == "open"
    assert session_phase(et(2026, 9, 1, 14, 59, 59)) == "open"


def test_session_phase_power_hour_boundaries() -> None:
    assert session_phase(et(2026, 9, 1, 15, 0, 0)) == "power_hour"
    assert session_phase(et(2026, 9, 1, 15, 59, 59)) == "power_hour"


def test_session_phase_close_at_and_after_1600() -> None:
    assert session_phase(et(2026, 9, 1, 16, 0, 0)) == "close"
    assert session_phase(et(2026, 9, 1, 20, 0)) == "close"


def test_session_phase_weekend_closed() -> None:
    assert session_phase(et(2026, 8, 29, 12, 0)) == "closed"  # Saturday
    assert session_phase(et(2026, 8, 30, 12, 0)) == "closed"  # Sunday


def test_session_phase_contest_weekdays_all_trade() -> None:
    # Contest week has no holidays: Fri 8/28, Mon 8/31 .. Fri 9/4 all trade.
    for d in (date(2026, 8, 28), date(2026, 8, 31), date(2026, 9, 2),
              date(2026, 9, 3), date(2026, 9, 4)):
        assert session_phase(et(d.year, d.month, d.day, 12, 0)) == "open"


def test_session_phase_accepts_naive_as_et_and_converts_aware() -> None:
    # Naive datetimes are ET wall time.
    assert session_phase(datetime(2026, 9, 1, 12, 0)) == "open"
    # 16:00 UTC on 2026-09-01 (EDT, UTC-4) is 12:00 ET -> open.
    assert session_phase(datetime(2026, 9, 1, 16, 0, tzinfo=timezone.utc)) == "open"


# ------------------------------------------------------------- minutes_to_close

def test_minutes_to_close_values() -> None:
    assert minutes_to_close(et(2026, 9, 1, 15, 0)) == 60.0
    assert minutes_to_close(et(2026, 9, 1, 9, 30)) == 390.0
    assert minutes_to_close(et(2026, 9, 1, 16, 0)) == 0.0
    assert minutes_to_close(et(2026, 9, 1, 16, 30)) == -30.0


# --------------------------------------------------------------- parse_et_hhmm

def test_parse_et_hhmm_builds_aware_et_datetime() -> None:
    dt = parse_et_hhmm("15:10", TUESDAY)
    assert dt == et(2026, 9, 1, 15, 10)
    assert dt.tzinfo is ET
    assert (dt.hour, dt.minute) == (15, 10)


# --------------------------------------------------------------- due_catalysts

def test_due_catalysts_returns_only_past_same_day_events() -> None:
    due = due_catalysts(CALENDAR, et(2026, 9, 1, 10, 5), fired=set())
    tags = {e["tag"] for e in due}
    assert tags == {"ISM_MFG", "JOLTS"}          # 14:00 event not yet due
    assert all(e["date"] == "2026-09-01" for e in due)
    # MONTH_END (previous day, never fired) must NOT leak in: stale, not actionable.
    assert "MONTH_END" not in tags


def test_due_catalysts_inclusive_at_exact_event_time() -> None:
    due = due_catalysts(CALENDAR, et(2026, 9, 1, 10, 0, 0), fired=set())
    assert {e["tag"] for e in due} == {"ISM_MFG", "JOLTS"}


def test_due_catalysts_skips_already_fired() -> None:
    due = due_catalysts(CALENDAR, et(2026, 9, 1, 10, 5), fired={"JOLTS"})
    assert [e["tag"] for e in due] == ["ISM_MFG"]


def test_due_catalysts_fires_each_event_once() -> None:
    fired: set[str] = set()
    dt = et(2026, 9, 1, 14, 30)
    first = due_catalysts(CALENDAR, dt, fired)
    assert {e["tag"] for e in first} == {"ISM_MFG", "JOLTS", "BEIGE_ISH"}
    fired.update(e["tag"] for e in first)         # caller persists fired tags
    assert due_catalysts(CALENDAR, dt, fired) == []
    # Later the same day, still nothing new.
    assert due_catalysts(CALENDAR, et(2026, 9, 1, 15, 55), fired) == []


def test_due_catalysts_date_object_days_match() -> None:
    due = due_catalysts(CALENDAR, et(2026, 8, 31, 9, 45), fired=set())
    assert [e["tag"] for e in due] == ["MONTH_END"]


def test_due_catalysts_nothing_before_first_event() -> None:
    assert due_catalysts(CALENDAR, et(2026, 9, 1, 9, 59, 59), fired=set()) == []


# ----------------------------------------------------------------- past_cutoff

def test_past_cutoff_0dte_boundary() -> None:
    key = "no_new_0dte_after_et"
    assert past_cutoff(et(2026, 9, 1, 15, 9, 59), POLICY, key) is False
    assert past_cutoff(et(2026, 9, 1, 15, 10, 0), POLICY, key) is True   # inclusive
    assert past_cutoff(et(2026, 9, 1, 15, 11), POLICY, key) is True


def test_past_cutoff_other_keys() -> None:
    assert past_cutoff(et(2026, 9, 1, 15, 44), POLICY, "no_new_positions_after_et") is False
    assert past_cutoff(et(2026, 9, 1, 15, 45), POLICY, "no_new_positions_after_et") is True
    assert past_cutoff(et(2026, 9, 1, 15, 25), POLICY, "flat_0dte_by_et") is True


# ------------------------------------------------------------- final-day logic

def test_is_final_day() -> None:
    assert is_final_day(et(2026, 9, 4, 8, 0), POLICY) is True
    assert is_final_day(et(2026, 9, 4, 23, 59), POLICY) is True
    assert is_final_day(et(2026, 9, 3, 12, 0), POLICY) is False


def test_is_final_day_accepts_date_object_in_policy() -> None:
    policy = {"timing": {"final_day": date(2026, 9, 4)}}
    assert is_final_day(et(2026, 9, 4, 12, 0), policy) is True


def test_final_day_all_cash_cutoff() -> None:
    dt_before = et(2026, 9, 4, 10, 29)
    dt_at = et(2026, 9, 4, 10, 30)
    assert is_final_day(dt_before, POLICY) is True
    assert past_cutoff(dt_before, POLICY, "final_day_all_cash_by_et") is False
    assert past_cutoff(dt_at, POLICY, "final_day_all_cash_by_et") is True


# --------------------------------------------------------- is_market_open_now

class _StubCal:
    """TradingCalendarLike stub: records the call, returns a fixed answer."""

    def __init__(self, open_: bool) -> None:
        self.open_ = open_
        self.calls = 0

    def is_open(self, dt: datetime) -> bool:
        self.calls += 1
        assert dt.tzinfo is not None
        return self.open_

def test_is_market_open_now_defers_to_calendar_object() -> None:
    cal_open = _StubCal(True)
    cal_closed = _StubCal(False)
    assert is_market_open_now(cal_open) is True
    assert is_market_open_now(cal_closed) is False
    assert cal_open.calls == 1
    assert cal_closed.calls == 1
