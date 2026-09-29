"""External limit sources: a local HTTP endpoint that answers in contract 1.

The public extension point of 0.8.0. The plugin asks ``GET <url>`` on loopback, optionally with
``Authorization: Bearer <value of key_env>``, and reads the answer described in the README
("External limit sources"): provider, state, windows with scope, severity and reset, plan and
login date. Nothing here knows who answers; any local process can.

Guards: the URL is loopback http only; no proxy from the environment, no redirect, 64 KB at most.
The key is read from the environment here and nowhere else, and never from a bot token
variable: the engine's Telegram adapter reads ``TELEGRAM_BOT_TOKEN``, the cron path of this
dashboard reads ``HERMES_DASHBOARD_BOT_TOKEN``, and any ``*_BOT_TOKEN`` is refused with them. The
plugin never reads the bot token itself (the catalog review, hermes-agent#122408).
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Any
from urllib.parse import urlsplit

from .policy import sanitize_public_text
from .timeparse import is_from_the_future, parse_timestamp

CONTRACT = 1
MAX_SOURCES = 4
MAX_WINDOWS = 6
MAX_BODY_BYTES = 64 * 1024
DEFAULT_TIMEOUT_SECONDS = 20.0
MIN_TIMEOUT_SECONDS = 1.0
MAX_TIMEOUT_SECONDS = 120.0
# The tick waits for a source no longer than for Grok or Kimi. The request itself runs on in its
# worker with the source's own timeout; a late answer is the next tick's line.
TICK_TIMEOUT_SECONDS = 25.0
LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})
BOT_TOKEN_VARIABLES = frozenset({"TELEGRAM_BOT_TOKEN", "HERMES_DASHBOARD_BOT_TOKEN"})
BOT_TOKEN_SUFFIX = "_BOT_TOKEN"
BOT_TOKEN_REFUSAL = "key_env names a bot token variable"
SEVERITIES = ("normal", "warning", "critical")
SOURCE_LABEL = "external"
_VARIABLE_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,63}")

HttpGet = Callable[[str, dict[str, str], float], tuple[int, bytes]]


@dataclass(frozen=True, slots=True)
class Source:
    """One configured entry. ``problem`` set: the entry was refused and is never asked."""

    number: int
    url: str
    key_env: str | None
    timeout_seconds: float
    problem: str | None = None

    @property
    def key(self) -> str:
        """The name of the source's cache and worker: stable per URL, the URL itself unshown."""
        seed = self.url or f"entry {self.number}"
        return hashlib.sha256(seed.encode("utf-8")).hexdigest()[:8]


def is_bot_token_variable(name: str) -> bool:
    upper = name.upper()
    return upper in BOT_TOKEN_VARIABLES or upper.endswith(BOT_TOKEN_SUFFIX)


# ----------------------------------------------------------------------------- the setting


def read_sources(raw: object) -> tuple[Source, ...]:
    """``limits_sources`` from the config as entries; a bad entry is kept with its problem so
    the screen can say why, never dropped silently. Never raises."""
    if raw is None or raw == "" or (isinstance(raw, list | tuple) and not raw):
        return ()
    if not isinstance(raw, list | tuple):
        return (Source(1, "", None, DEFAULT_TIMEOUT_SECONDS, "limits_sources must be a list"),)
    sources = [_read_entry(number, entry) for number, entry in enumerate(raw, start=1)]
    extra = [replace(s, problem=f"more than {MAX_SOURCES} sources") for s in sources[MAX_SOURCES:]]
    return (*sources[:MAX_SOURCES], *extra)


def _read_entry(number: int, entry: object) -> Source:
    if not isinstance(entry, Mapping):
        return Source(number, "", None, DEFAULT_TIMEOUT_SECONDS, "entry is not a mapping")
    url = entry.get("url")
    url_text = url.strip() if isinstance(url, str) else ""
    key_env = entry.get("key_env")
    problem = _url_problem(url_text) or _key_env_problem(key_env)
    return Source(
        number,
        url_text,
        key_env if isinstance(key_env, str) and key_env else None,
        _timeout(entry.get("timeout_seconds")),
        problem,
    )


def _url_problem(url: str) -> str | None:
    if not url:
        return "url missing"
    try:
        parts = urlsplit(url)
        parts.port  # noqa: B018 - a port outside 0..65535 raises here
    except ValueError:
        return "url unreadable"
    if parts.scheme != "http":
        return "url must be http on loopback"
    if parts.username is not None or parts.password is not None:
        return "url must not carry credentials"
    if (parts.hostname or "") not in LOOPBACK_HOSTS:
        return "url must be on loopback"
    return None


def _key_env_problem(key_env: object) -> str | None:
    if key_env is None or key_env == "":
        return None
    if not isinstance(key_env, str) or not _VARIABLE_NAME.fullmatch(key_env):
        return "key_env is not a variable name"
    if is_bot_token_variable(key_env):
        return BOT_TOKEN_REFUSAL
    return None


def _timeout(raw: object) -> float:
    if isinstance(raw, bool) or not isinstance(raw, int | float) or not _finite(raw):
        return DEFAULT_TIMEOUT_SECONDS
    return min(max(float(raw), MIN_TIMEOUT_SECONDS), MAX_TIMEOUT_SECONDS)


def _finite(value: int | float) -> bool:
    try:
        return math.isfinite(value)
    except OverflowError:
        return False


# ----------------------------------------------------------------------------- one attempt


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """A redirect is not followed: urllib would carry the key on to wherever it points."""

    def redirect_request(self, *_args: Any, **_kwargs: Any) -> None:
        return None


def http_get(url: str, headers: dict[str, str], timeout: float) -> tuple[int, bytes]:
    """One GET without proxies and redirects; the body of an error answer is not read."""
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())
    request = urllib.request.Request(url, headers=headers)
    try:
        with opener.open(request, timeout=timeout) as response:
            return int(response.status), bytes(response.read(MAX_BODY_BYTES + 1))
    except urllib.error.HTTPError as exc:
        exc.close()
        return int(exc.code), b""


def fetch_item(
    source: Source,
    *,
    now: datetime,
    get: HttpGet = http_get,
    environ: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """One attempt at the source as a payload item like ``grok.py`` and ``kimi.py`` build, plus
    ``provider``, ``plan``, ``login_expires_at`` and per-window ``scope`` and ``severity``.
    Never raises."""
    item = _empty_item()
    if source.problem:
        item["reason"] = source.problem
        return item
    headers = {"Accept": "application/json"}
    if source.key_env:
        # read_sources refuses these names already; checked again so that an entry built any
        # other way cannot read a bot token either.
        if is_bot_token_variable(source.key_env):
            item["reason"] = BOT_TOKEN_REFUSAL
            return item
        key = (os.environ if environ is None else environ).get(source.key_env, "")
        if not key:
            item["reason"] = f"{source.key_env} not set"
            return item
        headers["Authorization"] = f"Bearer {key}"
    try:
        status, body = get(source.url, headers, source.timeout_seconds)
    except Exception as exc:
        item["reason"] = f"request failed: {_public(exc)}"
        return item
    return _read_answer(status, body, now=now)


def _read_answer(status: int, body: bytes, *, now: datetime) -> dict[str, Any]:
    item = _empty_item()
    if status != 200:
        item["reason"] = f"HTTP {status}"
        return item
    if len(body) > MAX_BODY_BYTES:
        item["reason"] = "answer over 64 KB"
        return item
    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        item["reason"] = "answer shape: not JSON"
        return item
    return parse_contract(payload, now=now)


# ----------------------------------------------------------------------------- the contract


def parse_contract(payload: object, *, now: datetime) -> dict[str, Any]:
    """Contract 1 as a payload item. Unknown keys are ignored, every string is sanitized, a
    shape the contract does not allow is "no data" with the reason."""
    item = _empty_item()
    if not isinstance(payload, dict):
        item["reason"] = "answer shape: not an object"
        return item
    version = payload.get("contract")
    if version != CONTRACT or isinstance(version, bool):
        item["reason"] = f"contract {sanitize_public_text(str(version), limit=8)} not supported"
        return item
    provider = _text(payload.get("provider"), 24)
    if provider is None:
        item["reason"] = "answer without provider"
        return item
    item.update(
        provider=provider,
        plan=_text(payload.get("plan"), 24),
        login_expires_at=_moment(payload.get("login_expires_at")),
    )
    return _with_state(item, payload, now=now)


def _with_state(item: dict[str, Any], payload: dict[str, Any], *, now: datetime) -> dict[str, Any]:
    state = payload.get("state")
    if state == "unavailable":
        item["reason"] = _text(payload.get("reason"), 60) or "source unavailable"
        return item
    if state == "login_expired":
        item.update(status="expired", fetched_at=_fetched(payload, now))
        return item
    if state != "ok":
        item["reason"] = "answer shape: state"
        return item
    windows = payload.get("windows")
    if not isinstance(windows, list):
        item["reason"] = "answer shape: windows"
        return item
    item.update(
        status="available",
        fetched_at=_fetched(payload, now),
        windows=[_window(raw) for raw in windows if isinstance(raw, dict)][:MAX_WINDOWS],
    )
    return item


def _window(raw: dict[str, Any]) -> dict[str, Any]:
    severity = raw.get("severity")
    return {
        "label": _text(raw.get("label"), 24) or "window",
        "used_percent": _percent(raw.get("used_percent")),
        "reset_at": _moment(raw.get("resets_at")),
        "scope": _text(raw.get("scope"), 24),
        "severity": severity if severity in SEVERITIES else None,
    }


def _percent(used: object) -> float | None:
    """A share from 0 to 100; anything else is no number, never a clamped one."""
    if isinstance(used, bool) or not isinstance(used, int | float) or not _finite(used):
        return None
    return float(used) if 0 <= used <= 100 else None


def _fetched(payload: dict[str, Any], now: datetime) -> str:
    """The source's own stamp of its numbers; the time of reading when it has none or it is
    ahead of the clock (a stamp from the future cannot be proven)."""
    stamp = payload.get("fetched_at")
    moment = parse_timestamp(stamp)
    if moment is None or is_from_the_future((now - moment).total_seconds()):
        return now.isoformat()
    return str(stamp)


def _moment(value: object) -> str | None:
    return value if isinstance(value, str) and parse_timestamp(value) is not None else None


def _text(value: object, limit: int) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    return sanitize_public_text(value.strip(), limit=limit) or None


def _empty_item() -> dict[str, Any]:
    return {
        "provider": None,
        "status": "unavailable",
        "reason": None,
        "source": SOURCE_LABEL,
        "fetched_at": None,
        "windows": [],
    }


def _public(exc: BaseException) -> str:
    return sanitize_public_text(str(exc) or type(exc).__name__, limit=60)
