"""Regression for the date defect: a valid UTC timestamp can be unrepresentable after a zone shift,
and OverflowError is not a ValueError. Nothing here may raise."""

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import pytest

from telegram_dashboard.normalize import classify_freshness
from telegram_dashboard.render import render_dashboard
from telegram_dashboard.schema import CapacitySummary, DashboardSnapshot, QuotaMetric, QuotaWindow
from telegram_dashboard.timeparse import age_seconds, format_in_zone, parse_timestamp, to_zone

MOSCOW = ZoneInfo("Europe/Moscow")
NOW = datetime(2026, 9, 9, 21, 0, tzinfo=UTC)


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
                    windows=(QuotaWindow("5 ч", 37.0, "9999-12-31T23:59:59+00:00"),),
                ),
            )
        ),
    )

    rendered = render_dashboard(snapshot, zone=MOSCOW)

    assert "OpenAI: Остаток 25%" in rendered
    assert "Reset: дата нечитаема" in rendered
    assert "Claude: 5 ч 37% · сброс: дата нечитаема" in rendered
    assert "Данные: время неизвестно" in rendered
