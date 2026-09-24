from datetime import UTC, datetime

from telegram_dashboard.normalize import classify_freshness, derive_overall
from telegram_dashboard.schema import Coverage, Incident, SourceObservation


def test_unavailable_source_prevents_healthy_overall() -> None:
    overall = derive_overall(
        coverage=Coverage(expected_profiles=2, observed_profiles=2),
        sources=(
            SourceObservation(
                name="cron:worker",
                authority="official",
                state="unavailable",
            ),
        ),
        incidents=(),
    )

    assert overall == "unknown"


def test_known_critical_incident_is_not_hidden_by_unknown_source() -> None:
    overall = derive_overall(
        coverage=Coverage(
            expected_profiles=2,
            observed_profiles=1,
            failed_sources=("profile:worker",),
        ),
        sources=(),
        incidents=(
            Incident(
                incident_id="cron:delivery",
                severity="critical",
                title="Не доставлен результат",
            ),
        ),
    )

    assert overall == "critical"


def test_warning_incident_sets_warning_overall() -> None:
    overall = derive_overall(
        coverage=Coverage(expected_profiles=1, observed_profiles=1),
        sources=(SourceObservation(name="cron", authority="local", state="fresh"),),
        incidents=(
            Incident(
                incident_id="context:risk",
                severity="warning",
                title="Контекст одной сессии выше порога",
            ),
        ),
    )

    assert overall == "warning"


def test_observation_older_than_ttl_is_stale() -> None:
    state = classify_freshness(
        "2026-09-09T00:00:00Z",
        now=datetime(2026, 9, 9, 0, 3, tzinfo=UTC),
        ttl_seconds=120,
    )

    assert state == "stale"


def test_invalid_observation_time_is_unavailable() -> None:
    state = classify_freshness(
        "not-a-time",
        now=datetime(2026, 9, 9, tzinfo=UTC),
        ttl_seconds=120,
    )

    assert state == "unavailable"


def test_observation_dated_in_the_future_is_unavailable_not_fresh() -> None:
    """An ``observed_at`` ahead of ``now`` cannot be proven; it must not count as fresh data."""
    state = classify_freshness(
        "2027-09-09T00:00:00Z",
        now=datetime(2026, 9, 9, tzinfo=UTC),
        ttl_seconds=120,
    )

    assert state == "unavailable"


def test_observation_within_clock_skew_tolerance_is_fresh() -> None:
    state = classify_freshness(
        "2026-09-09T00:00:30Z",
        now=datetime(2026, 9, 9, tzinfo=UTC),
        ttl_seconds=120,
    )

    assert state == "fresh"
