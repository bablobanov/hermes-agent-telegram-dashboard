"""Regression for the date defect: a valid UTC timestamp can be unrepresentable after a zone shift,
and OverflowError is not a ValueError. Nothing here may raise."""

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import pytest

from telegram_dashboard.normalize import classify_freshness
from telegram_dashboard.render import render_dashboard
from telegram_dashboard.schema import CapacitySummary, DashboardSnapshot, QuotaMetric, QuotaWindow
from telegram_dashboard.timeparse import (
    MONTHS,
    age_seconds,
    day_words,
    format_day,
    format_day_time,
    format_in_zone,
    format_stamp,
    parse_timestamp,
    to_zone,
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
    assert lines[0] == "🟢 Healthy · time unknown"
