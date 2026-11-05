"""The one-attempt-per-interval policy every external limit source runs on (``quota_cache.tick``,
called by ``collect.external_tick``): a cached number is never older than the interval, and a
failed attempt is "no data" with its reason, not retried before the interval.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from telegram_dashboard.quota_cache import tick

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
