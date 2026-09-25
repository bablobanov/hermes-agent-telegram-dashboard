"""Kimi Code quota: the windows the Kimi Code platform serves to its own clients.

The surface is ``GET <base>/v1/usages`` on the host the engine already talks to for inference
(``hermes_cli.auth.resolve_api_key_provider_credentials("kimi-coding")`` gives the key and the
base URL by the same rules the runtime applies: ``.env`` first, then the credential pool, then the
key-prefix redirect). The shape is the one the official client parses
(``@moonshot-ai/kimi-code-oauth``, ``managed-usage.ts``): ``usages`` keyed by window with
``used_ratio`` in 0..1 and an RFC 3339 ``reset_time``; a legacy plan carries ``limit_5h`` and
``limit_7d``, a new plan ``limit_5h`` and ``limit_month_total`` (``limit_month_code`` is the code
share of the monthly total, not a window of its own). Not in Kimi's docs; the number is the
provider's own, the same class as the Grok reader and the engine's Anthropic and Codex fetchers.

The request carries the client header the engine sends to this host (``agent_init.py``,
``_HOST_DEFAULT_HEADERS``), so the server sees the client family it already serves. Nothing here
logs a body or a key; nothing here touches the credential pool's rotation, so a failed request
cannot mark the pool exhausted. Policy and cache: ``quota_cache.tick``.
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

PROVIDER_KEY = "kimi"
SOURCE = "kimi_code_usages"
ENGINE_PROVIDER = "kimi-coding"
USAGES_PATH = "/v1/usages"
CLIENT_HEADER = ("User-Agent", "claude-code/0.1.0")
DEFAULT_INTERVAL_SECONDS = 900.0
HTTP_TIMEOUT_SECONDS = 10.0
# One request plus thread start-up.
TICK_TIMEOUT_SECONDS = 15.0
# Payload key -> label on the screen, in the order the official client shows them.
WINDOWS: tuple[tuple[str, str], ...] = (
    ("limit_5h", "5h"),
    ("limit_7d", "week"),
    ("limit_month_total", "month"),
)

CredentialResolver = Callable[[], tuple[str, str]]
HttpGet = Callable[[str, dict[str, str]], tuple[int, str]]


class ShapeError(ValueError):
    """The answer is not the usages object this module knows how to read."""


# ----------------------------------------------------------------------------- I/O adapters


def resolve_credentials() -> tuple[str, str]:
    """The key and base URL the engine resolves for ``kimi-coding`` inference. Imported at call
    time: outside the gateway there is no engine."""
    module = importlib.import_module("hermes_cli.auth")
    creds = module.resolve_api_key_provider_credentials(ENGINE_PROVIDER)
    key = str(creds.get("api_key") or "").strip()
    base_url = str(creds.get("base_url") or "").strip()
    if not key:
        raise LookupError("resolver returned no api key")
    if not base_url:
        raise LookupError("resolver returned no base url")
    return key, base_url


def usages_url(base_url: str) -> str:
    """``/v1/usages`` beside the inference base URL, whether it ends in ``/v1`` or not."""
    base = base_url.strip().rstrip("/")
    if base.endswith("/v1"):
        base = base[: -len("/v1")]
    return base + USAGES_PATH


def http_get(url: str, headers: dict[str, str]) -> tuple[int, str]:
    request = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT_SECONDS) as response:
            return int(response.status), response.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return int(exc.code), exc.read(2000).decode("utf-8", "replace")


# ----------------------------------------------------------------------------- parsing


def parse_usages(payload: object) -> list[dict[str, Any]]:
    """The known windows as payload item windows; ``ShapeError`` names what is wrong."""
    usages = payload.get("usages") if isinstance(payload, dict) else None
    if not isinstance(usages, dict):
        raise ShapeError("answer without usages")
    windows: list[dict[str, Any]] = []
    for key, label in WINDOWS:
        entry = usages.get(key)
        if entry is None:
            continue
        if not isinstance(entry, dict):
            raise ShapeError(f"{key} not an object")
        ratio = entry.get("used_ratio")
        if isinstance(ratio, bool) or not isinstance(ratio, int | float):
            raise ShapeError(f"{key}.used_ratio not a number")
        if not math.isfinite(ratio):
            raise ShapeError(f"{key}.used_ratio not a number")
        if not 0 <= ratio <= 1:
            raise ShapeError(f"{key}.used_ratio outside 0..1: {ratio:g}")
        reset = entry.get("reset_time")
        if reset is not None and parse_timestamp(reset) is None:
            raise ShapeError(f"{key}.reset_time unreadable")
        windows.append(
            {"label": label, "used_percent": float(ratio) * 100.0, "reset_at": reset or None}
        )
    if not windows:
        raise ShapeError("usages without known windows")
    return windows


# ----------------------------------------------------------------------------- one attempt


def fetch_item(
    *,
    now: datetime,
    resolve: CredentialResolver = resolve_credentials,
    get: HttpGet = http_get,
) -> dict[str, Any]:
    """One attempt at the usages surface, as a payload item like ``limits.py`` builds; never
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
        key, base_url = resolve()
    except ImportError:
        item["reason"] = "Kimi resolver not available on this installation"
        return item
    except Exception as exc:
        item["reason"] = f"Kimi key: {_public(exc)}"
        return item
    headers = {
        "Authorization": f"Bearer {key}",
        CLIENT_HEADER[0]: CLIENT_HEADER[1],
        "Accept": "application/json",
    }
    try:
        status, body = get(usages_url(base_url), headers)
    except Exception as exc:
        item["reason"] = f"request failed: {_public(exc)}"
        return item
    if status != 200:
        item["reason"] = f"HTTP {status}"
        return item
    try:
        windows = parse_usages(json.loads(body))
    except ValueError as exc:  # ShapeError and a body that is not JSON
        item["reason"] = f"answer shape: {_public(exc) or 'not JSON'}"
        return item
    item["status"] = "available"
    item["fetched_at"] = now.isoformat()
    item["windows"] = windows
    return item


def _public(exc: BaseException) -> str:
    return sanitize_public_text(str(exc) or type(exc).__name__, limit=60)
