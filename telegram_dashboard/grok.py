"""Grok weekly pool: the subscription quota xAI serves to its own Grok CLI client.

The surface is not in xAI's docs; the number is the provider's own, the same class as the engine's
Anthropic and Codex fetchers (``api/oauth/usage``, the ChatGPT backend). It is read with the
token the engine already holds, through the engine's own resolver, so the screen shows the quota
of exactly the grant inference uses. Probed on the installation on 2026-09-12: the host the engine
talks to for inference (``api.x.ai``) answers 404 here; the CLI proxy host is required.

Policy: one attempt per ``interval_seconds``, success or failure. A failure is "нет данных" with
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
WINDOW_LABEL = "неделя"

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


def parse_weekly(payload: object) -> dict[str, Any]:
    """The weekly window as a payload item window; ``ShapeError`` names what is missing."""
    config = payload.get("config") if isinstance(payload, dict) else None
    if not isinstance(config, dict):
        raise ShapeError("ответ без config")
    period = config.get("currentPeriod")
    period_type = period.get("type") if isinstance(period, dict) else None
    if period_type != WEEKLY_PERIOD:
        raise ShapeError(f"период не недельный: {sanitize_public_text(str(period_type), limit=32)}")
    used = config.get("creditUsagePercent")
    if isinstance(used, bool) or not isinstance(used, int | float) or not math.isfinite(used):
        raise ShapeError("creditUsagePercent не число")
    if not 0 <= used <= 100:
        raise ShapeError(f"creditUsagePercent вне 0..100: {used:g}")
    end = period.get("end") if isinstance(period, dict) else None
    if parse_timestamp(end) is None:
        end = config.get("billingPeriodEnd")
    if parse_timestamp(end) is None:
        raise ShapeError("дата сброса нечитаема")
    return {"label": WINDOW_LABEL, "used_percent": float(used), "reset_at": end}


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
        item["reason"] = "резолвер xAI недоступен на этой установке"
        return item
    except Exception as exc:
        item["reason"] = f"токен xAI: {_public(exc)}"
        return item
    headers = {"Authorization": f"Bearer {token}", CLIENT_HEADER[0]: CLIENT_HEADER[1]}
    headers["Accept"] = "application/json"
    try:
        status, body = get(BILLING_URL, headers)
    except Exception as exc:
        item["reason"] = f"запрос не прошёл: {_public(exc)}"
        return item
    if status != 200:
        item["reason"] = f"HTTP {status}"
        return item
    try:
        window = parse_weekly(json.loads(body))
    except ValueError as exc:  # ShapeError and a body that is not JSON
        item["reason"] = f"форма ответа: {_public(exc) or 'не JSON'}"
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
