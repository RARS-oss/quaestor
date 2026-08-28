"""Market clock for quaestor: ET session phases, catalyst timing, policy cutoffs.

Pure functions, no network. All market logic runs in US/Eastern (zoneinfo).

Alpaca/market facts encoded here:
- Regular US equity/options session: 09:30-16:00 ET. Options orders are TIF=day
  with NO extended-hours trading, so the regular session is the only window that
  matters for this agent. power_hour = 15:00-16:00 ET.
- Contest week (2026-08-28 .. 2026-09-04) has NO market holidays: every weekday
  trades. Labor Day (Mon 2026-09-07) falls after the submission deadline, so no
  holiday table is needed — weekend detection suffices.
- Catalyst times in configs/calendar.yaml are ET wall-clock "HH:MM" strings; policy
  cutoffs in configs/policy.yaml (timing:) use the same format.

Naive datetimes passed to these functions are assumed to already be ET wall time;
aware datetimes are converted to ET first.
"""
from __future__ import annotations

from datetime import date, datetime
from typing import Protocol
from zoneinfo import ZoneInfo

__all__ = [
    "ET",
    "TradingCalendarLike",
    "now_et",
    "is_market_open_now",
    "session_phase",
    "minutes_to_close",
    "parse_et_hhmm",
    "due_catalysts",
    "is_final_day",
    "past_cutoff",
]

ET = ZoneInfo("America/New_York")

OPEN_HHMM = (9, 30)      # regular session open, ET
POWER_HHMM = (15, 0)     # power hour start, ET
CLOSE_HHMM = (16, 0)     # regular session close, ET


class TradingCalendarLike(Protocol):
    """Anything that can answer 'is the market open at dt?' (e.g. a wrapper over
    Alpaca's GET /v2/clock). clock.py itself never does network I/O."""

    def is_open(self, dt: datetime) -> bool: ...


def now_et() -> datetime:
    """Current wall-clock time in US/Eastern (tz-aware)."""
    return datetime.now(tz=ET)


def _as_et(dt: datetime) -> datetime:
    """Normalize to ET: naive datetimes are taken as ET wall time; aware ones are
    converted."""
    if dt.tzinfo is None:
        return dt.replace(tzinfo=ET)
    return dt.astimezone(ET)


def _as_date(value: object) -> date:
    """Coerce a calendar/policy date value (str 'YYYY-MM-DD', date, or datetime —
    yaml.safe_load yields date objects for unquoted dates) to a date."""
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        return date.fromisoformat(value.strip())
    raise TypeError(f"cannot interpret {value!r} as a date")


def session_phase(dt: datetime) -> str:
    """Classify dt into one of: "pre" | "open" | "power_hour" | "close" | "closed".

    - "closed":     weekend (contest week has no holidays).
    - "pre":        trading weekday before 09:30 ET.
    - "open":       09:30 (inclusive) to 15:00 ET.
    - "power_hour": 15:00 (inclusive) to 16:00 ET.
    - "close":      trading weekday at/after 16:00 ET (session over for the day).
    """
    et = _as_et(dt)
    if et.weekday() >= 5:  # Saturday=5, Sunday=6
        return "closed"
    t = (et.hour, et.minute, et.second, et.microsecond)
    open_t = (*OPEN_HHMM, 0, 0)
    power_t = (*POWER_HHMM, 0, 0)
    close_t = (*CLOSE_HHMM, 0, 0)
    if t < open_t:
        return "pre"
    if t < power_t:
        return "open"
    if t < close_t:
        return "power_hour"
    return "close"


def is_market_open_now(cal: TradingCalendarLike | None = None) -> bool:
    """Is the regular session open right now?

    With a TradingCalendarLike, defer to it (authoritative, e.g. Alpaca /v2/clock).
    Otherwise fall back to the simple ET schedule: trading weekday, 09:30-16:00 ET.
    """
    now = now_et()
    if cal is not None:
        return bool(cal.is_open(now))
    return session_phase(now) in ("open", "power_hour")


def minutes_to_close(dt: datetime) -> float:
    """Minutes from dt until 16:00 ET on dt's (ET) date. Negative once past the
    close; only meaningful on trading days."""
    et = _as_et(dt)
    close = et.replace(
        hour=CLOSE_HHMM[0], minute=CLOSE_HHMM[1], second=0, microsecond=0
    )
    return (close - et).total_seconds() / 60.0


def parse_et_hhmm(s: str, on_date: date) -> datetime:
    """Parse an ET wall-clock "HH:MM" string onto on_date -> tz-aware ET datetime."""
    hh, mm = s.strip().split(":")
    return datetime(
        on_date.year, on_date.month, on_date.day, int(hh), int(mm), tzinfo=ET
    )


def due_catalysts(calendar: dict, dt: datetime, fired: set[str]) -> list[dict]:
    """Calendar events on dt's ET date whose scheduled time has passed (inclusive)
    and whose tag is not already in the fired set.

    Never re-fires: callers add returned tags to their persisted fired set. Events
    from earlier dates are deliberately NOT returned — a catalyst missed while the
    agent was down is stale, not actionable.

    Each returned dict is a copy of the calendar event with a "date" key
    ("YYYY-MM-DD") added.
    """
    et = _as_et(dt)
    today = et.date()
    due: list[dict] = []
    for day in calendar.get("week", []) or []:
        if _as_date(day.get("date")) != today:
            continue
        for event in day.get("events", []) or []:
            tag = str(event.get("tag", ""))
            if not tag or tag in fired:
                continue
            event_dt = parse_et_hhmm(str(event["time"]), today)
            if event_dt <= et:
                out = dict(event)
                out["date"] = today.isoformat()
                due.append(out)
    return due


def is_final_day(dt: datetime, policy: dict) -> bool:
    """True when dt's ET date is the policy's timing.final_day (all-cash day)."""
    final = _as_date(policy["timing"]["final_day"])
    return _as_et(dt).date() == final


def past_cutoff(dt: datetime, policy: dict, key: str) -> bool:
    """True when dt is at/after the ET "HH:MM" cutoff policy["timing"][key] on dt's
    own ET date (inclusive: the cutoff minute itself counts as past).

    e.g. key="no_new_0dte_after_et" | "flat_0dte_by_et" | "no_new_positions_after_et"
    | "final_day_all_cash_by_et". Missing keys raise KeyError (loud config error).
    """
    et = _as_et(dt)
    cutoff = parse_et_hhmm(str(policy["timing"][key]), et.date())
    return et >= cutoff
