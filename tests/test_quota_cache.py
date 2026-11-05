"""The one-attempt-per-interval policy every external limit source runs on (``quota_cache.tick``,
called by ``collect.external_tick``): a cached number is never older than the interval, and a
failed attempt is "no data" with its reason, not retried before the interval.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from telegram_dashboard.quota_cache import DUE_TOLERANCE_SECONDS, tick

NOW = datetime(2026, 10, 2, 12, 0, tzinfo=UTC)


class CountingFetch:
    def __init__(self, item: dict[str, Any]) -> None:
        self.item = item
        self.calls = 0

    def __call__(self, *, now: datetime) -> dict[str, Any]:
        self.calls += 1
        return {
            **self.item,
            "fetched_at": now.isoformat() if self.item["status"] == "available" else None,
        }


AVAILABLE = {
    "provider": "x",
    "status": "available",
    "reason": None,
    "source": "external",
    "fetched_at": None,
    "windows": [{"label": "7d", "used_percent": 27.0, "reset_at": "2026-10-07T19:25:30+00:00"}],
}
FAILED = {**AVAILABLE, "status": "unavailable", "reason": "HTTP 503", "windows": []}


def test_the_source_is_asked_once_per_interval_and_served_from_the_cache_between() -> None:
    fetch = CountingFetch(AVAILABLE)
    cache: dict[str, Any] = {}

    first = tick(cache, now=NOW, interval_seconds=900, fetch=fetch)
    second = tick(cache, now=NOW + timedelta(minutes=5), interval_seconds=900, fetch=fetch)
    third = tick(cache, now=NOW + timedelta(minutes=14), interval_seconds=900, fetch=fetch)
    fourth = tick(cache, now=NOW + timedelta(minutes=15), interval_seconds=900, fetch=fetch)

    assert fetch.calls == 2
    assert first["fetched_at"] == second["fetched_at"] == third["fetched_at"] == NOW.isoformat()
    assert fourth["fetched_at"] == (NOW + timedelta(minutes=15)).isoformat()
    assert cache["attempted_at"] == fourth["fetched_at"] and cache["item"] == fourth


def test_a_failed_attempt_stays_no_data_until_the_next_interval_not_the_next_tick() -> None:
    fetch = CountingFetch(FAILED)
    cache: dict[str, Any] = {}

    items = [
        tick(cache, now=NOW + timedelta(minutes=m), interval_seconds=900, fetch=fetch)
        for m in (0, 5, 10, 15)
    ]

    assert fetch.calls == 2  # minute 0 and minute 15, not every tick
    assert all(item["status"] == "unavailable" and item["reason"] == "HTTP 503" for item in items)


def test_an_attempt_due_within_the_tolerance_goes_out_on_this_tick() -> None:
    """A tick that wakes a fraction of a second before the interval ends asks now; one more than
    ``DUE_TOLERANCE_SECONDS`` early is served from the cache."""
    fetch = CountingFetch(AVAILABLE)
    cache: dict[str, Any] = {}
    tick(cache, now=NOW, interval_seconds=300, fetch=fetch)

    early = tick(cache, now=NOW + timedelta(seconds=294), interval_seconds=300, fetch=fetch)
    assert fetch.calls == 1 and early["fetched_at"] == NOW.isoformat()

    due = tick(cache, now=NOW + timedelta(seconds=299.6), interval_seconds=300, fetch=fetch)
    assert fetch.calls == 2
    assert due["fetched_at"] == (NOW + timedelta(seconds=299.6)).isoformat()
    assert DUE_TOLERANCE_SECONDS == 5.0


def test_ticks_that_wake_early_keep_the_interval_instead_of_slipping_a_period() -> None:
    """A day of ticks at a 60 s period, each waking up to a second early (the loop's clock
    against the wall clock), at an interval of 300 s: 288 attempts, one per 300 s. Without the
    tolerance an attempt due on the fifth tick slips to the sixth whenever that tick wakes
    earlier than the one that asked."""
    fetch = CountingFetch(AVAILABLE)
    cache: dict[str, Any] = {}

    for k in range(24 * 60):
        early = (k * 7919 % 1000) / 1000  # a fixed spread of 0 to 0.999 s
        now = NOW + timedelta(seconds=60 * k - (early if k else 0))
        tick(cache, now=now, interval_seconds=300, fetch=fetch)

    assert fetch.calls == 288


def test_a_cached_number_older_than_the_interval_is_not_a_number() -> None:
    """The guard behind the policy: whatever put an old number into the cache (a clock jump, a
    hand-edited state file), the screen does not show it under a fresh stamp."""
    old = {**AVAILABLE, "fetched_at": (NOW - timedelta(hours=2)).isoformat()}
    cache = {"attempted_at": NOW.isoformat(), "item": old}
    fetch = CountingFetch(AVAILABLE)

    item = tick(cache, now=NOW + timedelta(minutes=1), interval_seconds=900, fetch=fetch)

    assert fetch.calls == 0
    assert item["status"] == "unavailable" and item["reason"] == "cached number too old"
    assert item["windows"] == []


def test_a_number_stamped_just_after_the_tick_is_a_number_one_far_ahead_is_not() -> None:
    """A worker that returns between the tick's moment and the tick's own check stamps its
    answer a fraction of a second after ``now``: that is the clock-skew tolerance
    ``last_answer`` allows, not an old number. A stamp beyond it is a clock that ran away."""
    from telegram_dashboard.quota_cache import not_older_than
    from telegram_dashboard.timeparse import CLOCK_SKEW_TOLERANCE_SECONDS

    late = {**AVAILABLE, "fetched_at": (NOW + timedelta(seconds=0.2)).isoformat()}
    ahead = CLOCK_SKEW_TOLERANCE_SECONDS + 60
    runaway = {**AVAILABLE, "fetched_at": (NOW + timedelta(seconds=ahead)).isoformat()}

    assert not_older_than(late, now=NOW, limit_seconds=300) == late
    assert not_older_than(runaway, now=NOW, limit_seconds=300)["reason"] == "cached number too old"
