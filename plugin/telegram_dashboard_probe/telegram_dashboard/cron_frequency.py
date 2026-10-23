"""How often a cron job runs, from the schedule the engine stores for it (``cron/jobs.py``,
v0.22.2): ``{"kind": "cron", "expr": "0 9 * * *"}`` (:780), ``{"kind": "interval", "minutes":
30}`` (:783-784), ``{"kind": "once", "run_at": "<iso>"}`` (:829-833). The words come from
``kind`` and ``expr`` or ``minutes``, never from ``display``: the engine's display can disagree
with the expression (a job shown as "every monday 6am" runs ``0 6 * * 1``).

The cron section groups the jobs by the label and orders the groups by the period, the minutes
between two runs: ``Once`` first, ``Other`` (a schedule these rules do not name, a malformed or
missing one) last. Pure: no file, no clock, nothing raises.
"""

from __future__ import annotations

import math
from collections.abc import Mapping

from .schema import Frequency

HOUR = 60
DAY = 24 * HOUR
WEEK = 7 * DAY
# A month for the order only: thirty days.
MONTH = 30 * DAY
ONCE = Frequency("Once", 0.0)
OTHER = Frequency("Other", math.inf)
_WEEKDAYS = frozenset(range(1, 6))


def frequency_of(schedule: object) -> Frequency:
    """The stored ``schedule`` mapping as a frequency; anything else is ``Other``."""
    if not isinstance(schedule, Mapping):
        return OTHER
    kind = schedule.get("kind")
    if kind == "once":
        return ONCE
    if kind == "interval":
        return _interval(schedule.get("minutes"))
    if kind == "cron":
        return _cron(schedule.get("expr"))
    return OTHER


def _interval(minutes: object) -> Frequency:
    """``every {m}m`` as the engine stores it: whole positive minutes."""
    if not isinstance(minutes, int) or isinstance(minutes, bool) or minutes <= 0:
        return OTHER
    return _every(minutes)


def _every(minutes: int) -> Frequency:
    """A run every ``minutes``, in the words of the largest unit that divides it."""
    if minutes < HOUR:
        label = f"Every {minutes} min"
    elif minutes == HOUR:
        label = "Every hour"
    elif minutes % HOUR == 0 and minutes < DAY:
        label = f"Every {minutes // HOUR} h"
    elif minutes == DAY:
        label = "Daily"
    elif minutes % DAY == 0:
        label = f"Every {minutes // DAY} days"
    else:
        label = f"Every {minutes} min"
    return Frequency(label, float(minutes))


def _cron(expr: object) -> Frequency:
    """Five fields, ``minute hour day-of-month month day-of-week``: the shapes a person writes
    for a steady rhythm are named, every other expression is ``Other``."""
    fields = expr.split() if isinstance(expr, str) else []
    if len(fields) != 5:
        return OTHER
    minute, hour, day, month, weekday = fields
    if month != "*":
        return OTHER
    step = _step(minute, 59)
    if step is not None:
        every_field_open = hour == day == weekday == "*"
        return _every(step) if every_field_open else OTHER
    if _number(minute, 0, 59) is None:
        return OTHER
    return _at_a_minute(hour, day, weekday)


def _at_a_minute(hour: str, day: str, weekday: str) -> Frequency:
    """A fixed minute: every hour, every N hours, or at the listed hours of the days given."""
    hours = _numbers(hour, 0, 23)
    if day == weekday == "*":
        if hour == "*":
            return _every(HOUR)
        step = _step(hour, 23)
        if step is not None:
            return _every(step * HOUR)
        if hours:
            return _times_a_day(len(hours))
        return OTHER
    if len(hours) != 1:
        return OTHER
    if day == "*":
        return _days_of_the_week(_weekdays(weekday))
    if weekday == "*" and _number(day, 1, 31) is not None:
        return Frequency("Monthly", float(MONTH))
    return OTHER


def _times_a_day(times: int) -> Frequency:
    if times == 1:
        return Frequency("Daily", float(DAY))
    label = "Twice a day" if times == 2 else f"{times} times a day"
    return Frequency(label, DAY / times)


def _days_of_the_week(days: frozenset[int]) -> Frequency:
    if len(days) == 1:
        return Frequency("Weekly", float(WEEK))
    if len(days) == 2:
        return Frequency("Twice a week", WEEK / 2)
    if days == _WEEKDAYS:
        return Frequency("Weekdays", WEEK / 5)
    return OTHER


def _number(text: str, low: int, high: int) -> int | None:
    """One field value in ``[low, high]``; ``None`` for anything else."""
    if not text.isdecimal() or not text.isascii():
        return None
    value = int(text)
    return value if low <= value <= high else None


def _numbers(text: str, low: int, high: int) -> frozenset[int]:
    """A list of single values (``5``, ``5,6``, ``9,13,18``); empty when any item is not one."""
    values = [_number(item, low, high) for item in text.split(",")]
    numbers = [value for value in values if value is not None]
    return frozenset(numbers) if len(numbers) == len(values) else frozenset()


def _step(text: str, high: int) -> int | None:
    """``*/N`` with ``N`` in ``[1, high]``; ``None`` for anything else (``*/0`` among them)."""
    return _number(text[2:], 1, high) if text.startswith("*/") else None


def _weekdays(text: str) -> frozenset[int]:
    """The days of a day-of-week field given as a list of days and ranges (``1``, ``0,3``,
    ``1-5``), Sunday as 0 or 7; empty when any item is neither."""
    days: set[int] = set()
    for item in text.split(","):
        low, dash, high = item.partition("-")
        first = _number(low, 0, 7)
        last = _number(high, 0, 7) if dash else first
        if first is None or last is None or first > last:
            return frozenset()
        days.update(day % 7 for day in range(first, last + 1))
    return frozenset(days)
