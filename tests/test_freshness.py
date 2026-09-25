from datetime import UTC, datetime, timedelta

from telegram_dashboard.freshness import (
    DeliveryRecord,
    check_exit_code,
    classify_message_freshness,
    confirmation_threshold_seconds,
    message_age_seconds,
    message_banner,
)
from telegram_dashboard.render import render_dashboard
from telegram_dashboard.schema import DashboardSnapshot

NOW = datetime(2026, 9, 9, 21, 0, tzinfo=UTC)
PERIOD = 300


def _record(minutes_ago: int, **extra) -> DeliveryRecord:
    stamp = NOW - timedelta(minutes=minutes_ago)
    return DeliveryRecord(message_id=1, last_confirmed_at=stamp.isoformat(), **extra)


def test_message_freshness_thresholds_follow_two_periods() -> None:
    assert classify_message_freshness(_record(2), now=NOW, period_seconds=PERIOD) == "confirmed"
    assert classify_message_freshness(_record(7), now=NOW, period_seconds=PERIOD) == "lagging"
    assert classify_message_freshness(_record(11), now=NOW, period_seconds=PERIOD) == "stale"


def test_never_delivered_and_lost_are_distinct_from_stale() -> None:
    assert classify_message_freshness(DeliveryRecord(), now=NOW, period_seconds=PERIOD) == "never"
    lost = _record(1, last_error="lost", lost_at=NOW.isoformat())
    assert classify_message_freshness(lost, now=NOW, period_seconds=PERIOD) == "lost"


def test_unparseable_confirmation_time_is_stale_not_confirmed() -> None:
    record = DeliveryRecord(message_id=1, last_confirmed_at="9999-12-31T23:59:59-05:00")
    assert classify_message_freshness(record, now=NOW, period_seconds=PERIOD) == "stale"


def test_exit_codes_for_external_watchdog() -> None:
    assert check_exit_code("confirmed") == 0
    assert check_exit_code("lagging") == 1
    assert check_exit_code("stale") == 2
    assert check_exit_code("lost") == 2


def test_stale_banner_is_loud_and_names_threshold() -> None:
    banner = message_banner("stale", _record(11), now=NOW, period_seconds=PERIOD)
    assert banner is not None
    assert "ДАШБОРД УСТАРЕЛ" in banner
    assert "11 мин" in banner
    assert "порог 10 мин" in banner
    assert message_banner("confirmed", _record(1), now=NOW, period_seconds=PERIOD) is None


def test_data_time_and_message_time_are_separate_lines() -> None:
    """Fresh data in a message nobody delivered: the two stamps must not collapse into one."""
    snapshot = DashboardSnapshot(overall="normal", observed_at=NOW.isoformat())
    rendered = render_dashboard(snapshot, now=NOW, delivery=_record(11), period_seconds=PERIOD)

    lines = rendered.splitlines()
    assert lines[0].startswith("🔴 ДАШБОРД УСТАРЕЛ")
    assert lines[1] == "🟢 Норма · 09.09 21:00 UTC"  # the data stamp, with its date
    assert "> Подтверждено 20:49" in lines
    assert "> Период 5 мин" in lines


def test_confirmation_dated_in_the_future_is_stale_not_confirmed() -> None:
    """A record from a machine whose clock ran ahead, or a corrupted stamp, must not pass as
    confirmed: a negative age is smaller than any period and used to satisfy ``age <= period``."""
    future = NOW + timedelta(days=365)
    record = DeliveryRecord(message_id=1, last_confirmed_at=future.isoformat())

    assert classify_message_freshness(record, now=NOW, period_seconds=PERIOD) == "stale"
    assert check_exit_code(classify_message_freshness(record, now=NOW, period_seconds=PERIOD)) == 2
    banner = message_banner("stale", record, now=NOW, period_seconds=PERIOD)
    assert banner is not None and "будущ" in banner


def test_small_clock_skew_into_the_future_is_still_confirmed() -> None:
    """Two hosts a few seconds apart are normal; only a stamp clearly ahead of ``now`` is wrong."""
    record = DeliveryRecord(
        message_id=1, last_confirmed_at=(NOW + timedelta(seconds=30)).isoformat()
    )
    assert classify_message_freshness(record, now=NOW, period_seconds=PERIOD) == "confirmed"


def _record_seconds(seconds_ago: int) -> DeliveryRecord:
    stamp = NOW - timedelta(seconds=seconds_ago)
    return DeliveryRecord(message_id=1, last_confirmed_at=stamp.isoformat())


def test_one_period_plus_timer_jitter_is_still_confirmed() -> None:
    """An updater that edits once per period reads its own record one period (plus timer jitter)
    later. That is a healthy cadence; without slack every healthy tick would read as lagging and
    the banner would train the reader to ignore it."""
    assert (
        classify_message_freshness(_record_seconds(PERIOD + 20), now=NOW, period_seconds=PERIOD)
        == "confirmed"
    )
    assert (
        classify_message_freshness(_record_seconds(PERIOD + 61), now=NOW, period_seconds=PERIOD)
        == "lagging"
    )
    # A short period keeps a lagging window of its own: the slack is at most half a period.
    assert (
        classify_message_freshness(_record_seconds(80), now=NOW, period_seconds=60) == "confirmed"
    )
    assert classify_message_freshness(_record_seconds(100), now=NOW, period_seconds=60) == "lagging"
    assert classify_message_freshness(_record_seconds(130), now=NOW, period_seconds=60) == "stale"


def test_age_and_threshold_are_the_classifier_s_own_numbers() -> None:
    """The watchdog's repeat policy counts ticks from the same moment the classifier turns
    ``lagging``; both must read one age and one threshold, or the first alert drifts a tick."""
    assert message_age_seconds(_record(7), now=NOW) == 7 * 60
    assert message_age_seconds(DeliveryRecord(), now=NOW) is None
    assert message_age_seconds(DeliveryRecord(last_confirmed_at="not a date"), now=NOW) is None

    threshold = confirmation_threshold_seconds(PERIOD)
    at_threshold = DeliveryRecord(
        message_id=1, last_confirmed_at=(NOW - timedelta(seconds=threshold)).isoformat()
    )
    past_threshold = DeliveryRecord(
        message_id=1, last_confirmed_at=(NOW - timedelta(seconds=threshold + 1)).isoformat()
    )
    assert classify_message_freshness(at_threshold, now=NOW, period_seconds=PERIOD) == "confirmed"
    assert classify_message_freshness(past_threshold, now=NOW, period_seconds=PERIOD) == "lagging"
