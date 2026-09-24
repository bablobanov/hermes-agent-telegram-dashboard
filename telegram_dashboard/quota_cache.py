"""One attempt per interval at a provider's quota surface, cached in the caller's durable dict.

Shared by every provider the dashboard reads on its own cadence (Grok, Kimi). The policy is the
same for all of them: one attempt per ``interval_seconds``, success or failure; a failed attempt
is "нет данных" with its reason until the next attempt; a number is never shown when it is older
than the interval allows. The cache belongs to the caller (the plugin keeps one per provider in
its record), so the interval survives a restart and the state file shows when the provider was
last asked.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from typing import Any

from .timeparse import age_seconds, parse_timestamp

Fetch = Callable[..., dict[str, Any]]


def tick(
    cache: dict[str, Any], *, now: datetime, interval_seconds: float, fetch: Fetch
) -> dict[str, Any]:
    """The item for this tick: from ``cache`` while the last attempt is younger than the
    interval, else one new attempt recorded into ``cache`` (mutated in place; the caller owns
    the durable copy). A cached number older than the interval is not a number."""
    attempted = parse_timestamp(cache.get("attempted_at"))
    cached = cache.get("item")
    if attempted is not None and isinstance(cached, dict):
        age = age_seconds(attempted, now)
        if age is not None and 0 <= age < interval_seconds:
            return not_older_than(cached, now=now, limit_seconds=interval_seconds)
    item = fetch(now=now)
    cache["attempted_at"] = now.isoformat()
    cache["item"] = item
    return item


def not_older_than(item: dict[str, Any], *, now: datetime, limit_seconds: float) -> dict[str, Any]:
    """``item`` as is while its number is young enough; otherwise "нет данных" with the reason.

    The guard behind the policy: whatever put an old number into the cache (a clock jump, a
    hand-edited state file), the screen does not show it under a fresh stamp."""
    if item.get("status") != "available":
        return item
    fetched = parse_timestamp(item.get("fetched_at"))
    age = age_seconds(fetched, now) if fetched is not None else None
    if age is None or age < 0 or age > limit_seconds:
        return {**item, "status": "unavailable", "reason": "число в кэше устарело", "windows": []}
    return item
