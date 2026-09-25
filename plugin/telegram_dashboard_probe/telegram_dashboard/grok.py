"""Grok weekly pool: the subscription quota xAI serves to its own Grok CLI client.

The surface is not in xAI's docs; the number is the provider's own, the same class as the engine's
Anthropic and Codex fetchers (``api/oauth/usage``, the ChatGPT backend). It is read with the
token the engine already holds, through the engine's own resolver, so the screen shows the quota
of exactly the grant inference uses. Probed on the installation on 2026-09-12: the host the engine
talks to for inference (``api.x.ai``) answers 404 here; the CLI proxy host is required.

Policy: one attempt per ``interval_seconds``, success or failure. A failure is "no data" with
its reason until the next attempt; a number is never shown when it is older than the interval
allows, and the line carries its own stamp (``quota_cache``). Nothing here logs a body or a
token.
"""

from __future__ import annotations

import importlib
import json
import logging
import math
import urllib.error
import urllib.request
from collections.abc import Callable
from datetime import datetime
from typing import Any

from .policy import sanitize_public_text
from .quota_cache import Fetch as Fetch
from .quota_cache import tick as tick
from .timeparse import parse_timestamp

logger = logging.getLogger(__name__)

PROVIDER_KEY = "grok"
SOURCE = "grok_cli_billing"
BILLING_URL = "https://cli-chat-proxy.grok.com/v1/billing?format=credits"
SETTINGS_URL = "https://cli-chat-proxy.grok.com/v1/settings"
WEEKLY_PERIOD = "USAGE_PERIOD_TYPE_WEEKLY"
CLIENT_HEADER = ("x-xai-token-auth", "xai-grok-cli")
DEFAULT_INTERVAL_SECONDS = 900.0
HTTP_TIMEOUT_SECONDS = 10.0
# The worker deadline covers both requests plus thread start-up.
TICK_TIMEOUT_SECONDS = 25.0
WINDOW_LABEL = "week"
NOT_STARTED_NOTE = "usage not started"

TokenResolver = Callable[[], str]
HttpGet = Callable[[str, dict[str, str]], tuple[int, str]]


class ShapeError(ValueError):
    """The answer is not the weekly pool this module knows how to read."""


# ----------------------------------------------------------------------------- I/O adapters


def resolve_token() -> str:
    """The access token of the grant the engine uses for xAI inference, refreshed the way the
    engine refreshes it. Imported at call time: outside the gateway there is no engine."""
    module = importlib.import_module("hermes_cli.auth_xai")
    creds = module.resolve_xai_oauth_runtime_credentials()
    token = str(creds.get("api_key") or "").strip()
    if not token:
        raise LookupError("resolver returned no access token")
    return token


def http_get(url: str, headers: dict[str, str]) -> tuple[int, str]:
    request = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT_SECONDS) as response:
            return int(response.status), response.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return int(exc.code), exc.read(2000).decode("utf-8", "replace")


# ----------------------------------------------------------------------------- parsing


def parse_weekly(payload: object, *, now: datetime) -> dict[str, Any]:
    """The weekly window as a payload item window; ``ShapeError`` names what is missing.

    Right after the weekly reset the proxy leaves ``creditUsagePercent`` out altogether until the
    first request of the new period (probe of 2026-09-25). That answer is a state, not a number:
    the window says so in words (``note``, no percent), and only while every other sign agrees
    (``_not_started``). A zero would be a number the provider never gave."""
    config = payload.get("config") if isinstance(payload, dict) else None
    if not isinstance(config, dict):
        raise ShapeError("answer without config")
    period = config.get("currentPeriod")
    if not isinstance(period, dict) or period.get("type") != WEEKLY_PERIOD:
        period_type = period.get("type") if isinstance(period, dict) else None
        raise ShapeError(f"period not weekly: {sanitize_public_text(str(period_type), limit=32)}")
    window: dict[str, Any] = {"label": WINDOW_LABEL}
    if "creditUsagePercent" in config:
        window["used_percent"] = _percent(config["creditUsagePercent"])
    elif _not_started(config, period, now):
        window.update(used_percent=None, note=NOT_STARTED_NOTE)
    else:
        raise ShapeError("no creditUsagePercent")
    window["reset_at"] = _reset_date(config, period)
    return window


def _percent(used: object) -> float:
    if isinstance(used, bool) or not isinstance(used, int | float) or not math.isfinite(used):
        raise ShapeError("creditUsagePercent not a number")
    if not 0 <= used <= 100:
        raise ShapeError(f"creditUsagePercent outside 0..100: {used:g}")
    return float(used)


def _not_started(config: dict[str, Any], period: dict[str, Any], now: datetime) -> bool:
    """The week nobody has spent from yet: the period is the current one and on-demand spend is
    an explicit zero. Anything less and the missing percent is a changed shape, named as such."""
    start = _bound(period.get("start"), config.get("billingPeriodStart"))
    end = _bound(period.get("end"), config.get("billingPeriodEnd"))
    spent = config.get("onDemandUsed")
    spent_value: object = spent.get("val") if isinstance(spent, dict) else None
    if start is None or end is None or not start <= now < end:
        return False
    return spent_value == 0 and not isinstance(spent_value, bool)


def _bound(own: object, billing: object) -> datetime | None:
    """A period bound from ``currentPeriod``, else from the billing period it mirrors."""
    return parse_timestamp(own) or parse_timestamp(billing)


def _reset_date(config: dict[str, Any], period: dict[str, Any]) -> str:
    end = period.get("end")
    if parse_timestamp(end) is None:
        end = config.get("billingPeriodEnd")
    if parse_timestamp(end) is None:
        raise ShapeError("reset date unreadable")
    return str(end)


def parse_tier(payload: object) -> str | None:
    tier = payload.get("subscription_tier_display") if isinstance(payload, dict) else None
    if not isinstance(tier, str) or not tier.strip():
        return None
    return sanitize_public_text(tier, limit=24)


# ----------------------------------------------------------------------------- one attempt


def fetch_item(
    *, now: datetime, resolve: TokenResolver = resolve_token, get: HttpGet = http_get
) -> dict[str, Any]:
    """One attempt at the billing surface, as a payload item like ``limits.py`` builds; never
    raises."""
    item: dict[str, Any] = {
        "provider": PROVIDER_KEY,
        "status": "unavailable",
        "reason": None,
        "source": SOURCE,
        "fetched_at": None,
        "windows": [],
    }
    try:
        token = resolve()
    except ImportError:
        item["reason"] = "xAI resolver not available on this installation"
        return item
    except Exception as exc:
        item["reason"] = f"xAI token: {_public(exc)}"
        return item
    headers = {"Authorization": f"Bearer {token}", CLIENT_HEADER[0]: CLIENT_HEADER[1]}
    headers["Accept"] = "application/json"
    try:
        status, body = get(BILLING_URL, headers)
    except Exception as exc:
        item["reason"] = f"request failed: {_public(exc)}"
        return item
    if status != 200:
        item["reason"] = f"HTTP {status}"
        return item
    try:
        window = parse_weekly(json.loads(body), now=now)
    except ValueError as exc:  # ShapeError and a body that is not JSON
        item["reason"] = f"answer shape: {_public(exc) or 'not JSON'}"
        return item
    tier = _tier(headers, get)
    if tier:
        window["label"] = f"{tier} {WINDOW_LABEL}"
    item["status"] = "available"
    item["fetched_at"] = now.isoformat()
    item["windows"] = [window]
    return item


def _tier(headers: dict[str, str], get: HttpGet) -> str | None:
    """The plan name; optional, its absence never costs the number."""
    try:
        status, body = get(SETTINGS_URL, headers)
        return parse_tier(json.loads(body)) if status == 200 else None
    except Exception as exc:
        logger.debug("grok tier not read: %s", type(exc).__name__)
        return None


def _public(exc: BaseException) -> str:
    return sanitize_public_text(str(exc) or type(exc).__name__, limit=60)


# The cache policy (one attempt per interval, a number never older than the interval) lives in
# ``quota_cache.tick``; it is re-exported here because the Grok tests and the collectors call it
# as ``grok.tick``.
