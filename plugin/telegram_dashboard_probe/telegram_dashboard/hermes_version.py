"""The Hermes version the running gateway serves, and the latest release upstream.

The line only informs: which Hermes the gateway runs, which release NousResearch/hermes-agent
marks Latest, how many releases lie between them. Nothing here updates anything or suggests a
command: updating Hermes is a process (a window by the operator's own runbook), not a restart.

The running version is ``hermes_cli.__version__`` of the module the gateway imported at start-up,
found in ``sys.modules``: a capability, never a version gate. Two sources that look equivalent
are wrong here and never used: ``importlib.metadata`` (an editable install keeps the dist-info
of install time) and ``hermes_cli.build_info.get_code_identity(refresh=True)`` (inside the
gateway it would restamp the gateway's own ``code_sha``, and ``hermes update`` would take a
stale process for a fresh one).

Upstream is read by the plugin itself, in its own worker, with plain stdlib ``urllib``: no LLM,
no agent, no agent tool. Two unauthenticated GETs to api.github.com at most once a day
(``quota_cache.tick``): ``releases/latest`` for the release upstream calls Latest, and the list
for the position of ours. The version is the one in the release name (``Hermes Agent v0.21.5
(v2026.9.24)``); releases behind are counted on the list, never computed from version numbers.
The engine's own update check (``banner.check_for_updates``) counts commits behind ``main`` and
is not used. A reason never quotes an answer: GitHub's rate-limit message carries the caller's
IP.
"""

from __future__ import annotations

import json
import re
import sys
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import replace
from datetime import datetime
from typing import Any

from .policy import sanitize_public_text
from .schema import VersionSummary
from .timeparse import parse_timestamp

SOURCE = "github_releases"
REPOSITORY = "NousResearch/hermes-agent"
PER_PAGE = 100
LATEST_URL = f"https://api.github.com/repos/{REPOSITORY}/releases/latest"
LIST_URL = f"https://api.github.com/repos/{REPOSITORY}/releases?per_page={PER_PAGE}"
HEADERS = {
    "Accept": "application/vnd.github+json",
    "X-GitHub-Api-Version": "2022-11-28",
    "User-Agent": "hermes-agent-telegram-dashboard",
}
INTERVAL_SECONDS = 86400.0
HTTP_TIMEOUT_SECONDS = 10.0
# The worker deadline covers both requests plus thread start-up.
TICK_TIMEOUT_SECONDS = 25.0
# The list carries every release's notes (about 1 MB for 36 releases on 2026-09-25); a body past
# this is cut, and a cut body is not JSON.
MAX_BODY_BYTES = 16_000_000
NOT_HERE = "not on this installation"
# ``Hermes Agent v0.21.5 (v2026.9.24)``: the version ends at a space or at the end of the name.
_NAME_VERSION = re.compile(r"Hermes Agent v(\d+(?:\.\d+){1,3})(?=\s|$)")
_VERSION = re.compile(r"\d+(?:\.\d+){1,3}")

HttpGet = Callable[[str, dict[str, str]], tuple[int, str]]
Release = dict[str, str]


class ShapeError(ValueError):
    """The answer is not the release shape this module knows how to read."""


class _Failed(Exception):
    """An attempt that ends as "no data"; the message is the public reason."""


# ----------------------------------------------------------------------------- running version


def running_version(
    modules: Mapping[str, object] = sys.modules,
) -> tuple[str | None, str | None]:
    """``(version, None)`` from the Hermes module the gateway imported, else ``(None, reason)``.

    Looked up, never imported: an import here would read the files on disk, which after a pull
    are not what the process serves."""
    module = modules.get("hermes_cli")
    version = getattr(module, "__version__", None) if module is not None else None
    if not isinstance(version, str) or not version.strip():
        return None, NOT_HERE
    return sanitize_public_text(version.strip(), limit=24), None


# ----------------------------------------------------------------------------- one attempt


def http_get(url: str, headers: dict[str, str]) -> tuple[int, str]:
    if not url.startswith("https://api.github.com/"):
        raise ValueError("only api.github.com is read")
    request = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT_SECONDS) as response:
            return int(response.status), response.read(MAX_BODY_BYTES).decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return int(exc.code), exc.read(2000).decode("utf-8", "replace")


def fetch_item(*, now: datetime, get: HttpGet = http_get) -> dict[str, Any]:
    """One attempt at upstream's releases, as a cache item; never raises. ``checked_at`` is the
    attempt, ``fetched_at`` only an answer that was read."""
    item: dict[str, Any] = {
        "status": "unavailable",
        "reason": None,
        "source": SOURCE,
        "fetched_at": None,
        "checked_at": now.isoformat(),
        "latest": None,
        "releases": [],
    }
    try:
        latest = parse_release(_get_json(get, LATEST_URL))
        releases = parse_list(_get_json(get, LIST_URL))
        if latest not in releases:
            raise ShapeError("Latest is not on the release list")
    except _Failed as exc:
        item["reason"] = str(exc)
        return item
    except ShapeError as exc:
        item["reason"] = f"answer shape: {exc}"
        return item
    item.update(status="available", fetched_at=now.isoformat(), latest=latest, releases=releases)
    return item


def _get_json(get: HttpGet, url: str) -> object:
    try:
        status, body = get(url, dict(HEADERS))
    except Exception as exc:
        raise _Failed(f"request failed: {type(exc).__name__}") from exc
    if status == 429 or (status == 403 and _rate_limited(body)):
        raise _Failed("GitHub rate limit")
    if status != 200:
        raise _Failed(f"HTTP {status}")
    try:
        return json.loads(body)
    except ValueError as exc:
        raise ShapeError("not JSON") from exc


def _rate_limited(body: str) -> bool:
    try:
        message = json.loads(body).get("message")
    except (ValueError, AttributeError):
        return False
    return isinstance(message, str) and "rate limit" in message.lower()


def parse_release(payload: object) -> Release:
    """``{"version", "published_at"}`` of one release; ``ShapeError`` names what is missing."""
    if not isinstance(payload, dict):
        raise ShapeError("release is not an object")
    name = payload.get("name")
    match = _NAME_VERSION.match(name) if isinstance(name, str) else None
    if match is None:
        raise ShapeError("no version in name")
    published = payload.get("published_at")
    if not isinstance(published, str) or parse_timestamp(published) is None:
        raise ShapeError("release date unreadable")
    return {"version": match.group(1), "published_at": published}


def parse_list(payload: object) -> list[Release]:
    """Releases newest first, as upstream lists them; drafts and pre-releases are not counted.
    One entry in an unknown shape fails the list: a skipped entry would miscount."""
    if not isinstance(payload, list):
        raise ShapeError("release list is not a list")
    return [parse_release(entry) for entry in payload if not _draft_or_prerelease(entry)]


def _draft_or_prerelease(entry: object) -> bool:
    return isinstance(entry, dict) and (
        entry.get("draft") is True or entry.get("prerelease") is True
    )


# ----------------------------------------------------------------------------- the summary


def summarize(item: object, running: str | None, local_reason: str | None) -> VersionSummary:
    """The line's facts from one cache item and the running version; never raises. A cached item
    in a shape this code does not know (a hand-edited state file) is "no data", not a crash."""
    base = VersionSummary(running=running, local_reason=local_reason)
    if not isinstance(item, dict):
        return replace(base, reason="not checked yet")
    checked = item.get("checked_at")
    base = replace(base, checked_at=checked if isinstance(checked, str) else None)
    if item.get("status") != "available":
        reason = item.get("reason")
        return replace(base, reason=reason if isinstance(reason, str) and reason else "no answer")
    try:
        latest = _cached_release(item.get("latest"))
        releases = [_cached_release(entry) for entry in _cached_list(item.get("releases"))]
    except ShapeError:
        return replace(base, reason="cached answer unreadable")
    ours, behind = _position(releases, running, latest["version"])
    return replace(
        base,
        latest=latest["version"],
        latest_published_at=latest["published_at"],
        running_published_at=ours["published_at"] if ours else None,
        behind=behind,
        list_size=len(releases),
    )


def _position(
    releases: list[Release], running: str | None, latest: str
) -> tuple[Release | None, int | None]:
    """Our release on the list and how far below Latest it stands (negative: above)."""
    versions = [entry["version"] for entry in releases]
    if running is None or running not in versions or latest not in versions:
        return None, None
    index = versions.index(running)
    return releases[index], index - versions.index(latest)


def _cached_release(value: object) -> Release:
    if not isinstance(value, dict):
        raise ShapeError("release is not an object")
    version, published = value.get("version"), value.get("published_at")
    if not isinstance(version, str) or _VERSION.fullmatch(version) is None:
        raise ShapeError("cached version unreadable")
    if not isinstance(published, str) or parse_timestamp(published) is None:
        raise ShapeError("cached date unreadable")
    return {"version": version, "published_at": published}


def _cached_list(value: object) -> list[object]:
    if not isinstance(value, list):
        raise ShapeError("release list is not a list")
    return value
