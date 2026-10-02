"""One attempt per interval at a provider's quota surface, cached in the caller's durable dict.

Shared by every provider the dashboard reads on its own cadence (Grok, Kimi). The policy is the
same for all of them: one attempt per ``interval_seconds``, success or failure; a failed attempt
is "no data" with its reason until the next attempt; a number is never shown when it is older
than the interval allows. The cache belongs to the caller (the plugin keeps one per provider in
its record), so the interval survives a restart and the state file shows when the provider was
last asked.

The GET these providers share lives here too: it carries the provider's token and does not
follow a redirect (``http_get``).
"""

from __future__ import annotations

import urllib.error
import urllib.request
from collections.abc import Callable
from datetime import datetime
from typing import Any

from .timeparse import age_seconds, parse_timestamp

Fetch = Callable[..., dict[str, Any]]


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """A redirect is not followed: urllib would carry the token on to wherever it points."""

    def redirect_request(self, *_args: Any, **_kwargs: Any) -> None:
        return None


def http_get(url: str, headers: dict[str, str], *, timeout: float) -> tuple[int, str]:
    """One GET with a provider's token. A 3xx comes back as its own status (``HTTP 302`` on the
    line): urllib copies every header but the body's onto the next request, ``Authorization``
    included, whatever host ``Location`` names. The environment's proxies apply as before."""
    opener = urllib.request.build_opener(_NoRedirect())
    request = urllib.request.Request(url, headers=headers)
    try:
        with opener.open(request, timeout=timeout) as response:
            return int(response.status), response.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return int(exc.code), exc.read(2000).decode("utf-8", "replace")


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
    """``item`` as is while its number is young enough; otherwise "no data" with the reason.

    The guard behind the policy: whatever put an old number into the cache (a clock jump, a
    hand-edited state file), the screen does not show it under a fresh stamp."""
    if item.get("status") != "available":
        return item
    fetched = parse_timestamp(item.get("fetched_at"))
    age = age_seconds(fetched, now) if fetched is not None else None
    if age is None or age < 0 or age > limit_seconds:
        return {**item, "status": "unavailable", "reason": "cached number too old", "windows": []}
    return item
