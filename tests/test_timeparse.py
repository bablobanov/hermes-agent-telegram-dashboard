"""Regression for the date defect: a valid UTC timestamp can be unrepresentable after a zone shift,
and OverflowError is not a ValueError. Nothing here may raise."""

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from telegram_dashboard.normalize import classify_freshness
from telegram_dashboard.render import render_dashboard
from telegram_dashboard.schema import CapacitySummary, DashboardSnapshot, QuotaMetric, QuotaWindow
from telegram_dashboard.timeparse import (
    MONTHS,
    WEEKDAYS,
    age_seconds,
    day_words,
    format_day,
    format_day_time,
    format_in_zone,
    format_short,
    format_stamp,
    parse_timestamp,
    to_zone,
    weekday_word,
)

MOSCOW = ZoneInfo("Europe/Moscow")
YEKATERINBURG = ZoneInfo("Asia/Yekaterinburg")  # "+05" as its abbreviation, like the live host
NOW = datetime(2026, 9, 9, 21, 0, tzinfo=UTC)


def test_month_table_has_twelve_english_abbreviations() -> None:
    assert len(MONTHS) == 12
    assert (MONTHS[0], MONTHS[8], MONTHS[11]) == ("Jan", "Sep", "Dec")


def test_day_words_drop_the_leading_zero_of_the_day() -> None:
    assert day_words(datetime(2026, 9, 5, tzinfo=UTC)) == "Sep 5"
    assert day_words(datetime(2026, 10, 25, tzinfo=UTC)) == "Oct 25"


class _ForeignLocale(datetime):
    """A moment on a host whose locale is not English: ``%a`` and ``%b`` answer in its words.
    A helper that asked ``strftime`` for a word would print them, whatever the test host is."""

    def strftime(self, fmt: str) -> str:
        return super().strftime(fmt.replace("%a", "sam.").replace("%b", "oct."))


def test_weekday_table_has_seven_english_abbreviations_monday_first() -> None:
    assert WEEKDAYS == ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
    # 2026-09-21 is a Monday: a whole week from it, in the table's order.
    week = [weekday_word(datetime(2026, 9, 21 + i, tzinfo=UTC)) for i in range(7)]
    assert week == list(WEEKDAYS)


def test_weekday_words_do_not_follow_the_process_locale() -> None:
    reference = _ForeignLocale(2026, 10, 24, 21, 0, tzinfo=UTC)
    saturday = _ForeignLocale(2026, 10, 31, 9, 0, tzinfo=UTC)

    assert reference.strftime("%a %b") == "sam. oct."  # the fake host speaks French
    assert weekday_word(reference) == "Sat"
    assert format_short(reference + timedelta(days=1), reference, UTC) == "Sun 21:00"
    assert format_short(saturday, reference, UTC) == "Oct 31"


def test_format_short_without_a_reference_names_the_day() -> None:
    """No moment to measure against (a screen without a time): the day, never a guess of one."""
    assert format_short("2026-09-26T21:00:00Z", None, UTC) == "Sep 26"
    assert format_short("2026-09-26T21:00:00Z", datetime(2026, 9, 26, 20, 0), UTC) == "Sep 26"


@pytest.mark.parametrize(
    "value",
    ["not-a-time", "", None, 12345, datetime.max.replace(tzinfo=UTC)],
    ids=["garbage", "empty", "none", "number", "overflow-in-zone"],
)
def test_format_short_returns_none_instead_of_raising(value: object) -> None:
    assert format_short(value, NOW, MOSCOW) is None


def test_day_helpers_follow_the_zone_and_its_abbreviation() -> None:
    stamp = "2026-09-25T11:18:00Z"

    assert format_day(stamp, YEKATERINBURG) == "Sep 25"
    assert format_day_time(stamp, YEKATERINBURG) == "Sep 25 16:18"
    assert format_stamp(stamp, YEKATERINBURG) == "Sep 25 16:18 +05"
    assert format_stamp(stamp, UTC) == "Sep 25 11:18 UTC"


def test_day_helpers_cross_midnight_in_the_zone() -> None:
    # 22:30 UTC on Oct 1 is already Oct 2 in Moscow: the day comes from the zone, not from UTC.
    assert format_day("2026-10-01T22:30:00Z", MOSCOW) == "Oct 2"
    assert format_day_time("2026-10-01T22:30:00Z", MOSCOW) == "Oct 2 01:30"


@pytest.mark.parametrize(
    "value",
    ["not-a-time", "", None, 12345, datetime.max.replace(tzinfo=UTC)],
    ids=["garbage", "empty", "none", "number", "overflow-in-zone"],
)
def test_day_helpers_return_none_instead_of_raising(value: object) -> None:
    assert format_day(value, MOSCOW) is None
    assert format_day_time(value, MOSCOW) is None
    assert format_stamp(value, MOSCOW) is None


def test_datetime_max_utc_overflows_in_moscow_without_raising() -> None:
    moment = datetime.max.replace(tzinfo=UTC)

    with pytest.raises(OverflowError):
        moment.astimezone(MOSCOW)  # the raw defect, kept as documentation

    assert to_zone(moment, MOSCOW) is None
    assert format_in_zone(moment, MOSCOW) is None


@pytest.mark.parametrize(
    "text",
    [
        "9999-12-31T23:59:59-05:00",  # overflows when shifted to UTC
        "0001-01-01T00:00:00+05:00",  # underflows when shifted to UTC
        "not-a-time",
        "",
        "2026-13-45T00:00:00Z",
    ],
)
def test_parse_timestamp_returns_none_instead_of_raising(text: str) -> None:
    assert parse_timestamp(text) is None


def test_parse_timestamp_accepts_z_suffix_and_naive_as_utc() -> None:
    assert parse_timestamp("2026-09-09T00:00:00Z") == datetime(2026, 9, 9, tzinfo=UTC)
    assert parse_timestamp("2026-09-09T00:00:00") == datetime(2026, 9, 9, tzinfo=UTC)
    assert parse_timestamp(12345) is None


def test_age_seconds_is_none_for_unrepresentable_pairs() -> None:
    edge = datetime(9999, 12, 31, 23, 0, tzinfo=ZoneInfo("Etc/GMT+5"))
    assert age_seconds(edge, NOW) is None
    assert age_seconds(datetime(2026, 9, 9, 20, 0, tzinfo=UTC), NOW) == 3600


def test_freshness_treats_overflowing_timestamp_as_unavailable() -> None:
    assert classify_freshness("9999-12-31T23:59:59-05:00", now=NOW, ttl_seconds=60) == "unavailable"


def test_render_survives_edge_reset_dates_and_keeps_other_fields() -> None:
    snapshot = DashboardSnapshot(
        overall="normal",
        observed_at="9999-12-31T23:59:59-05:00",
        capacity=CapacitySummary(
            (
                QuotaMetric(
                    "OpenAI", "official", used=75, limit=100, reset_at="9999-12-31T23:59:59-05:00"
                ),
                QuotaMetric(
                    "Claude",
                    "official",
                    windows=(QuotaWindow("5h", 37.0, "9999-12-31T23:59:59+00:00"),),
                ),
            )
        ),
    )

    rendered = render_dashboard(snapshot, zone=MOSCOW)

    lines = rendered.splitlines()
    # Neither reset can be counted (the data time itself is unreadable): a question mark, and
    # the numbers stay.
    assert "OpenAI 75% (?)" in lines
    assert "Claude 37% (?)" in lines
    assert lines[0] == "#@ 🟢 Healthy · updated time unknown"  # no moment for a tag either
