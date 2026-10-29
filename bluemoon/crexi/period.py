"""Crexi credit periods.

Crexi's owner-contact credits reset on one day of the month (config.yaml `crexi: reset_day: 29`). A credit
period starts on that day and ends the day before the next one: with reset_day 29, Oct 29 to Nov 28 is one
period, and a new one starts on Nov 29. A month that has no such day (Feb 29 or 30, April 31...) starts its
period on its last day instead, so there is always exactly one period per calendar month. The period is named
by its start date as text, 'YYYY-MM-DD' (for example '2026-10-29'); that text is stored on every batch.
"""
from __future__ import annotations

import calendar
from datetime import date, datetime, timedelta

from ..config import Settings, get_settings

DEFAULT_RESET_DAY = 29


def reset_day(settings: Settings | None = None) -> int:
    """crexi.reset_day from config.yaml (1 to 31); 29 when missing or not a day number."""
    v = ((settings or get_settings()).section("crexi") or {}).get("reset_day", DEFAULT_RESET_DAY)
    try:
        n = int(v)
    except (TypeError, ValueError):
        return DEFAULT_RESET_DAY
    return n if 1 <= n <= 31 else DEFAULT_RESET_DAY


def _start_in(year: int, month: int, day: int) -> date:
    return date(year, month, min(day, calendar.monthrange(year, month)[1]))


def _shift(year: int, month: int, by: int) -> tuple[int, int]:
    i = year * 12 + (month - 1) + by
    return i // 12, i % 12 + 1


def period_start(today: date | None = None, day: int = DEFAULT_RESET_DAY) -> date:
    """First day of the credit period that contains `today`."""
    d = today or datetime.now().date()
    here = _start_in(d.year, d.month, day)
    if d >= here:
        return here
    y, m = _shift(d.year, d.month, -1)
    return _start_in(y, m, day)


def next_period_start(start: date, day: int = DEFAULT_RESET_DAY) -> date:
    """The day the period after the one starting on `start` begins (Crexi's next reset)."""
    y, m = _shift(start.year, start.month, 1)
    return _start_in(y, m, day)


def period_end(start: date, day: int = DEFAULT_RESET_DAY) -> date:
    """Last day of the period that starts on `start` (the day before the next reset)."""
    return next_period_start(start, day) - timedelta(days=1)


def period_key(today: date | None = None, settings: Settings | None = None) -> str:
    """The period that contains `today`, as 'YYYY-MM-DD' (its first day)."""
    return period_start(today, reset_day(settings)).isoformat()


def days_left(today: date | None = None, settings: Settings | None = None) -> int:
    """Days left in the period, counting today (the last day gives 1)."""
    d = today or datetime.now().date()
    day = reset_day(settings)
    return (period_end(period_start(d, day), day) - d).days + 1


def describe(today: date | None = None, settings: Settings | None = None) -> dict:
    """{key, start, end, next_reset, days_left, reset_day} (dates as ISO text)."""
    d = today or datetime.now().date()
    day = reset_day(settings)
    start = period_start(d, day)
    nxt = next_period_start(start, day)
    return {"key": start.isoformat(), "start": start.isoformat(), "end": (nxt - timedelta(days=1)).isoformat(),
            "next_reset": nxt.isoformat(), "days_left": (nxt - d).days, "reset_day": day}


def label(key: str, settings: Settings | None = None) -> str:
    """'Oct 29 to Nov 28, 2026' for a period key."""
    start = date.fromisoformat(key)
    end = period_end(start, reset_day(settings))
    return f"{start.strftime('%b')} {start.day} to {end.strftime('%b')} {end.day}, {end.year}"
