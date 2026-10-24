"""``cron_frequency``: how often a job runs, from the schedule the engine stores for it.

The schedules are literals in the engine's own forms (``cron/jobs.py`` at v0.22.2: ``:780``
cron, ``:783-784`` interval, ``:829-833`` once). The owner's real expressions and the showcase's
leftovers are pinned by name; every rule has a row, and so has every malformed form.
"""

import math

import pytest

from telegram_dashboard.cron_frequency import OTHER, frequency_of
from telegram_dashboard.schema import Frequency


def _cron(expr: object, display: str = "") -> dict[str, object]:
    return {"kind": "cron", "expr": expr, "display": display or str(expr)}


def _interval(minutes: object) -> dict[str, object]:
    return {"kind": "interval", "minutes": minutes, "display": f"every {minutes}m"}


ONCE = {
    "kind": "once",
    "run_at": "2026-10-30T10:00:00+03:00",
    "display": "once at 2026-10-30 10:00",
}


@pytest.mark.parametrize(
    ("schedule", "label", "period"),
    [
        # The owner's real jobs (24.10).
        (_cron("0 5,6 * * *"), "Twice a day", 720),
        (_cron("0 6 * * 0,3"), "Twice a week", 5040),
        (_cron("0 9 28 * *"), "Monthly", 43200),
        (_cron("0 6 * * 1"), "Weekly", 10080),
        (_interval(30), "Every 30 min", 30),
        (_interval(1440), "Daily", 1440),
        (_interval(2880), "Every 2 days", 2880),
        (ONCE, "Once", 0),
        # The showcase's leftovers.
        (_interval(45), "Every 45 min", 45),
        (_interval(60), "Every hour", 60),
        (_interval(120), "Every 2 h", 120),
        (_interval(180), "Every 3 h", 180),
        (_interval(240), "Every 4 h", 240),
        (_interval(360), "Every 6 h", 360),
        (_cron("0 */6 * * *"), "Every 6 h", 360),
        (_cron("0 7 * * 1-5"), "Weekdays", 2016),
        (_cron("*/30 9-17 * * 1-5"), "Other", math.inf),
    ],
)
def test_the_owner_s_and_the_showcase_s_schedules_land_as_named(
    schedule: dict[str, object], label: str, period: float
) -> None:
    assert frequency_of(schedule) == Frequency(label, period)


@pytest.mark.parametrize(
    ("minutes", "label"),
    [
        (1, "Every 1 min"),
        (59, "Every 59 min"),
        (60, "Every hour"),
        (720, "Every 12 h"),
        (1380, "Every 23 h"),
        (1440, "Daily"),
        (4320, "Every 3 days"),
        (10080, "Every 7 days"),
        (525600, "Every 365 days"),  # a year: the longest named
        # Not a whole number of hours or days: the minutes.
        (90, "Every 90 min"),
        (1500, "Every 1500 min"),
        (2160, "Every 2160 min"),
    ],
)
def test_an_interval_is_named_in_the_largest_unit_that_divides_it(minutes: int, label: str) -> None:
    assert frequency_of(_interval(minutes)) == Frequency(label, float(minutes))


@pytest.mark.parametrize(
    ("expr", "label", "period"),
    [
        # The minute ``*/N`` with N a divisor of 60, every other field open.
        ("*/15 * * * *", "Every 15 min", 15),
        ("*/30 * * * *", "Every 30 min", 30),
        ("*/20 * * * *", "Every 20 min", 20),
        ("*/1 * * * *", "Every 1 min", 1),
        # A fixed minute: every hour, every N hours.
        ("0 * * * *", "Every hour", 60),
        ("17 * * * *", "Every hour", 60),
        ("0 */2 * * *", "Every 2 h", 120),
        ("0 */8 * * *", "Every 8 h", 480),
        ("0 */12 * * *", "Every 12 h", 720),
        ("0 */1 * * *", "Every hour", 60),  # the same group as ``0 * * * *``
        # A fixed minute at k listed hours of every day.
        ("0 5 * * *", "Daily", 1440),
        ("30 21 * * *", "Daily", 1440),
        ("0 9,13,18 * * *", "3 times a day", 480),
        ("0 0,6,12,18 * * *", "4 times a day", 360),
        # One hour on some days of the week.
        ("0 12 * * 6", "Weekly", 10080),
        ("0 2 * * 0", "Weekly", 10080),
        ("0 2 * * 7", "Weekly", 10080),  # Sunday as 7
        ("0 2 * * 0,7", "Weekly", 10080),  # Sunday twice is one day
        ("0 8 * * 2,4", "Twice a week", 5040),
        ("0 8 * * 1-2", "Twice a week", 5040),
        ("45 8 * * 1-5", "Weekdays", 2016),
        ("45 8 * * 1,2,3,4,5", "Weekdays", 2016),
        ("0 8 * * 1,3,5", "Other", math.inf),
        ("0 8 * * 1-3", "Other", math.inf),
        ("0 8 * * 0-6", "Other", math.inf),
        # One hour on a day of the month.
        ("0 9 1 * *", "Monthly", 43200),
        ("0 9 31 * *", "Monthly", 43200),
    ],
)
def test_a_cron_expression_is_named_by_its_shape(expr: str, label: str, period: float) -> None:
    assert frequency_of(_cron(expr)) == Frequency(label, period)


@pytest.mark.parametrize(
    "expr",
    [
        "*/15 9 * * *",  # a step in the minute with a fixed hour
        "*/15 * * * 1-5",  # a step in the minute on some days
        "0 9-17 * * *",  # a range of hours
        "0 */6 * * 1",  # an hour step on some days
        "* * * * *",
        "0 5,6 * * 1",  # several hours on some days
        "0 5,6 1 * *",  # several hours on a day of the month
        "0 9 1 * 1",  # a day of the month and of the week
        "0 9 1,15 * *",  # two days of the month
        "0 9 * 1 *",  # one month
        "0 9 1 1 *",  # once a year
        "0 9 * * */2",  # a step in the day of the week
        "0 9 * * 5-1",  # a range backwards
        "0 9 * * 1-",
        # A step that does not divide its cycle: the runs do not come evenly.
        "*/45 * * * *",  # :00 and :45
        "*/7 * * * *",
        "*/59 * * * *",
        "0 */23 * * *",  # 00:00 and 23:00
        "0 */5 * * *",
        "0 */7 * * *",
    ],
)
def test_any_other_cron_expression_is_other(expr: str) -> None:
    assert frequency_of(_cron(expr)) == OTHER


@pytest.mark.parametrize(
    "schedule",
    [
        _cron("0 9 * *"),  # four fields
        _cron("0 9 * * * 2026"),  # six fields
        _cron("*/0 * * * *"),
        _cron("0 */0 * * *"),
        _cron("*/60 * * * *"),
        _cron("0 */24 * * *"),
        _cron("60 9 * * *"),
        _cron("0 24 * * *"),
        _cron("0 9 32 * *"),
        _cron("0 9 * * 8"),
        _cron("a b c d e"),  # letters
        _cron("0 6 * * mon"),
        _cron("0 ٣ * * *"),  # a digit beyond ASCII
        _cron("0 9,x * * *"),
        _cron(""),
        _cron(None),
        _cron(905),
        {"kind": "cron"},  # no expression
        _interval(None),
        {"kind": "interval", "display": "every 30m"},  # a missing ``minutes``
        _interval(0),
        _interval(-30),
        _interval(30.0),
        _interval("30"),
        _interval(True),
        _interval(525601),  # over a year
        _interval(10**400),  # past a float's range: no OverflowError
        _cron("0 " + "9" * 5000 + " * * *"),  # past int's digit limit: no ValueError
        {"kind": "monthly", "expr": "0 9 1 * *"},  # an unknown kind
        {"expr": "0 9 * * *"},  # no kind
        {},
        "0 9 * * *",  # a schedule stored as text
        ["cron", "0 9 * * *"],  # not a mapping
        None,
    ],
)
def test_a_malformed_or_missing_schedule_is_other_and_orders_last(schedule: object) -> None:
    frequency = frequency_of(schedule)

    assert frequency == OTHER
    assert (frequency.label, frequency.period) == ("Other", math.inf)


def test_an_interval_need_not_divide_anything() -> None:
    """The engine's interval runs every ``minutes`` from the last run, so 45 is every 45 minutes;
    only a cron step has to divide its cycle."""
    assert frequency_of(_interval(45)) == Frequency("Every 45 min", 45)
    assert frequency_of(_interval(1380)) == Frequency("Every 23 h", 1380)
    assert frequency_of(_cron("*/45 * * * *")) == OTHER


def test_the_display_never_names_the_frequency() -> None:
    """The owner's job: shown as "every monday 6am" by the engine, run by ``0 6 * * 1``; and a
    display that says every 30 minutes on a daily expression."""
    assert frequency_of(_cron("0 6 * * 1", "every monday 6am")).label == "Weekly"
    assert frequency_of(_cron("0 9 * * *", "every 30m")).label == "Daily"
    assert frequency_of({"kind": "interval", "minutes": 120, "display": "daily"}).label == (
        "Every 2 h"
    )


def test_once_orders_before_every_rhythm_and_other_after() -> None:
    schedules = [_cron("0 9 1 * *"), _cron("*/1 * * * *"), ONCE, _cron("bad"), _interval(2880)]

    ordered = sorted(map(frequency_of, schedules), key=lambda frequency: frequency.period)

    assert [f.label for f in ordered] == ["Once", "Every 1 min", "Every 2 days", "Monthly", "Other"]
