"""The Hermes version the running gateway serves, and the latest release upstream.

The line only informs: which Hermes the gateway runs, which release NousResearch/hermes-agent
marks Latest, how many releases lie between them. Nothing here updates anything or suggests a
command: updating Hermes is a process (a window by the operator's own runbook), not a restart.

The running version is the one of the code the gateway imported at start-up, found in
``sys.modules``: a capability, never a version gate. Up to 0.21.5 it is the constant
``hermes_cli.__version__``. From 0.21.6 there is no constant: the module's ``__getattr__`` reads
the install stamp from disk on every access, so after an update on disk it names the new code,
not the running one, and without a stamp it says ``0.0.0`` (``unknown`` for a checkout git cannot
place). The identity the gateway resolved at start-up
(``hermes_cli.version_info._cached_version_info.base_version``, filled by the gateway's status
writer) comes first there: process state, no I/O, no git. The stamp is the last resort, and any
error of the engine's ``__getattr__`` is a reason, never a crash; a placeholder is "no version
stamp", never a version. Two sources that look equivalent are wrong here and never used:
``importlib.metadata`` (an editable install keeps the dist-info of install time) and
``hermes_cli.build_info.get_code_identity(refresh=True)`` (inside the gateway it would restamp
the gateway's own ``code_sha``, and ``hermes update`` would take a stale process for a fresh one).

Upstream is read by the plugin itself, in its own worker, with plain stdlib ``urllib``: no LLM,
no agent, no agent tool. Unauthenticated GETs to api.github.com, at most one check per 15
minutes (``tick``): ``releases/latest`` with ``If-None-Match`` for the release upstream calls
Latest, and the list for the position of ours only when Latest names another version. A new
release is on the screen within one 30-minute tick, or 15 minutes at a shorter period. The
version is the one in the release name (``Hermes Agent v0.21.6``, ``Hermes Agent v0.21.5
(v2026.9.24)``); releases behind are counted on the list, never computed from version numbers.
A failed check keeps the last answer read; a limit running low waits for GitHub's reset.
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
from datetime import datetime, timedelta
from typing import Any

from .policy import sanitize_public_text
from .schema import VersionSummary
from .timeparse import age_seconds, parse_timestamp

SOURCE = "github_releases"
REPOSITORY = "NousResearch/hermes-agent"
PER_PAGE = 100
API_ROOT = "https://api.github.com/"
LATEST_URL = f"{API_ROOT}repos/{REPOSITORY}/releases/latest"
LIST_URL = f"{API_ROOT}repos/{REPOSITORY}/releases?per_page={PER_PAGE}"
HEADERS = {
    "Accept": "application/vnd.github+json",
    "X-GitHub-Api-Version": "2022-11-28",
    "User-Agent": "hermes-agent-telegram-dashboard",
}
# At most one check per 15 minutes, whatever the tick's period (60 s by default): 4 requests an
# hour of the 60 GitHub gives an address without a token, which the engine or a script on the
# same address may share. Without a token a 304 counts against that limit too (measured on
# 2026-10-08: three 304s, remaining 35 -> 32): the cadence keeps the budget, the ETag saves
# the body.
INTERVAL_SECONDS = 900.0
# Fewer requests than this left in GitHub's window: wait for its reset, leave the rest to others.
RATE_RESERVE = 10
# A reset or a Retry-After further away than this is not believed.
MAX_WAIT_SECONDS = 3600.0
KEPT_HEADERS = ("etag", "x-ratelimit-remaining", "x-ratelimit-reset", "retry-after")
# urllib's timeout bounds each socket operation, not a whole request: a slow body can take
# longer. The worker deadline only releases the tick ("no answer within 25 s"); the worker then
# finishes on its own and its attempt lands in the cache like any other.
HTTP_TIMEOUT_SECONDS = 10.0
TICK_TIMEOUT_SECONDS = 25.0
# The list carries every release's notes (about 1 MB for 36 releases on 2026-09-25); a body past
# this is cut, and a cut body is not JSON.
MAX_BODY_BYTES = 16_000_000
NOT_HERE = "not on this installation"
UNSTAMPED = "no version stamp on this installation"
LOOKUP_FAILED = "version lookup failed"
# What the engine says when it does not know its own version (0.21.6: no install stamp, or a
# checkout git cannot place).
PLACEHOLDERS = frozenset({"0.0.0", "unknown"})
# ``Hermes Agent v0.21.5 (v2026.9.24)``: the version ends at a space or at the end of the name.
_NAME_VERSION = re.compile(r"Hermes Agent v(\d+(?:\.\d+){1,3})(?=\s|$)")
# A tag in the release scheme (``v0.21.6``), never a date (``v2026.9.24``): a major of 999 at most.
_TAG_VERSION = re.compile(r"v((?:0|[1-9]\d{0,2})(?:\.\d+){2})")
_VERSION = re.compile(r"\d+(?:\.\d+){1,3}")

HttpGet = Callable[[str, dict[str, str]], tuple[int, str, Mapping[str, str]]]
Fetch = Callable[..., dict[str, Any]]
Release = dict[str, str]


class ShapeError(ValueError):
    """The answer is not the release shape this module knows how to read."""


class _Failed(Exception):
    """An attempt that ends as "no data"; the message is the public reason."""


class _RateLimited(_Failed):
    """GitHub's limit for this address is out."""


# ----------------------------------------------------------------------------- running version


def running_version(
    modules: Mapping[str, object] = sys.modules,
) -> tuple[str | None, str | None]:
    """``(version, None)`` from the Hermes the gateway imported, else ``(None, reason)``.

    Looked up, never imported: an import here would read the files on disk, which after a pull
    are not what the process serves."""
    module = modules.get("hermes_cli")
    if module is None:
        return None, NOT_HERE
    version = _constant(module)
    if version is None:
        version = _started_identity(modules)
    if version is None:
        try:
            version = getattr(module, "__version__", None)
        except Exception:  # the engine's __getattr__: an import, a path, a file; never quoted
            return None, LOOKUP_FAILED
    if not isinstance(version, str) or not version.strip():
        return None, NOT_HERE
    if version.strip().lower() in PLACEHOLDERS:
        return None, UNSTAMPED
    return sanitize_public_text(version.strip(), limit=24), None


def _constant(module: object) -> object:
    """``__version__`` as a value of the module itself (0.21.1-0.21.5); ``None`` when the module
    only serves it through ``__getattr__`` (0.21.6), which this never triggers."""
    try:
        return vars(module).get("__version__")
    except TypeError:  # an object without a __dict__
        return None


def _started_identity(modules: Mapping[str, object]) -> object:
    """The version the gateway resolved at start-up (0.21.6), ``None`` before it did."""
    try:
        info = getattr(modules.get("hermes_cli.version_info"), "_cached_version_info", None)
        return getattr(info, "base_version", None)
    except Exception:  # a property that raises is no identity
        return None


# ----------------------------------------------------------------------------- one check


class _StayOnApi(urllib.request.HTTPRedirectHandler):
    """A redirect is followed only while it stays on api.github.com; anywhere else it ends the
    attempt as its own HTTP status."""

    def redirect_request(
        self,
        req: urllib.request.Request,
        fp: Any,
        code: int,
        msg: str,
        headers: Any,
        newurl: str,
    ) -> urllib.request.Request | None:
        if not newurl.startswith(API_ROOT):
            raise urllib.error.HTTPError(newurl, code, "redirect off api.github.com", headers, fp)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


_OPENER = urllib.request.build_opener(_StayOnApi)


def http_get(url: str, headers: dict[str, str]) -> tuple[int, str, dict[str, str]]:
    """One GET: the status, the body and the few headers this module reads. urllib raises a 304
    like any non-2xx; it comes back here as its status."""
    if not url.startswith(API_ROOT):
        raise ValueError("only api.github.com is read")
    request = urllib.request.Request(url, headers=headers)
    try:
        with _OPENER.open(request, timeout=HTTP_TIMEOUT_SECONDS) as response:
            body = response.read(MAX_BODY_BYTES).decode("utf-8", "replace")
            return int(response.status), body, _kept(response.headers)
    except urllib.error.HTTPError as exc:
        body = (exc.read(2000) if exc.fp else b"").decode("utf-8", "replace")
        return int(exc.code), body, _kept(exc.headers)


def _kept(headers: Any) -> dict[str, str]:
    kept: dict[str, str] = {}
    for name in KEPT_HEADERS:
        value = headers.get(name) if headers is not None else None
        if isinstance(value, str) and value.strip():
            kept[name] = value.strip()[:200]
    return kept


def fetch_item(
    *, now: datetime, get: HttpGet = http_get, previous: object = None
) -> dict[str, Any]:
    """One check of upstream's releases, as a cache item; never raises.

    ``previous`` is the item of the last check. Its answer goes out as ``If-None-Match`` and
    stays the answer while Latest names the same release (a 304, or a 200 for the same release
    whose notes were edited after publication): the list is read only when Latest names another
    version. A failed check keeps the last answer and says why. ``checked_at`` is this attempt,
    ``fetched_at`` the last time an answer was read or confirmed; ``retry_after`` is set when
    GitHub's limit for this address runs low or out, and no check goes out before it."""
    known = known_answer(previous)
    seen: dict[str, str] = {}
    limited = False
    try:
        item = _check(get, known, now, seen)
    except _RateLimited:
        limited = True
        item = _failed(known, now, "GitHub rate limit")
    except _Failed as exc:
        item = _failed(known, now, str(exc))
    except ShapeError as exc:
        item = _failed(known, now, f"answer shape: {exc}")
    except Exception as exc:  # a parsing surprise is this attempt's reason, cached like any other
        item = _failed(known, now, f"answer shape: {type(exc).__name__}")
    item["rate_remaining"] = _whole(seen.get("x-ratelimit-remaining"))
    item["retry_after"] = _retry_after(seen, now, limited=limited)
    return item


def _check(
    get: HttpGet, known: dict[str, Any] | None, now: datetime, seen: dict[str, str]
) -> dict[str, Any]:
    etag = known["etag"] if known is not None else None
    status, body = _ask(get, LATEST_URL, etag, seen)
    if status == 304:
        if known is None:
            raise ShapeError("not modified, and nothing cached")
        return _answered(known["latest"], known["releases"], now, seen.get("etag") or etag)
    latest = parse_release(_json(body))
    etag = seen.get("etag")
    if known is not None and known["latest"] == latest:
        return _answered(latest, known["releases"], now, etag)
    _status, listing = _ask(get, LIST_URL, None, seen)
    releases = parse_list(_json(listing))
    if latest not in releases:
        raise ShapeError("Latest is not on the release list")
    return _answered(latest, releases, now, etag)


def _ask(get: HttpGet, url: str, etag: str | None, seen: dict[str, str]) -> tuple[int, str]:
    headers = dict(HEADERS)
    if etag:
        headers["If-None-Match"] = etag
    try:
        status, body, answered = get(url, headers)
    except Exception as exc:
        raise _Failed(f"request failed: {type(exc).__name__}") from exc
    if isinstance(answered, Mapping):  # a header one answer lacks stays the earlier one's
        seen.update({str(k).lower(): v for k, v in answered.items() if isinstance(v, str)})
    if status == 429 or (status == 403 and _rate_limited(body)):
        raise _RateLimited("GitHub rate limit")
    if status == 304 and etag:
        return status, ""
    if status != 200:
        raise _Failed(f"HTTP {status}")
    return status, body


def _json(body: str) -> object:
    try:
        return json.loads(body)
    except ValueError as exc:
        raise ShapeError("not JSON") from exc
    except RecursionError as exc:
        raise ShapeError("JSON nested too deep") from exc


def _rate_limited(body: str) -> bool:
    try:
        message = json.loads(body).get("message")
    except Exception:
        return False
    return isinstance(message, str) and "rate limit" in message.lower()


def _answered(
    latest: Release, releases: list[Release], now: datetime, etag: str | None
) -> dict[str, Any]:
    return {
        "status": "available",
        "reason": None,
        "source": SOURCE,
        "fetched_at": now.isoformat(),
        "checked_at": now.isoformat(),
        "latest": latest,
        "releases": releases,
        "etag": etag,
    }


def _failed(known: dict[str, Any] | None, now: datetime, reason: str) -> dict[str, Any]:
    """No answer this time: the last one stays, with the reason and the time of this attempt."""
    if known is None:
        return {
            "status": "unavailable",
            "reason": reason,
            "source": SOURCE,
            "fetched_at": None,
            "checked_at": now.isoformat(),
            "latest": None,
            "releases": [],
            "etag": None,
        }
    return {**known, "status": "available", "reason": reason, "checked_at": now.isoformat()}


def known_answer(item: object) -> dict[str, Any] | None:
    """The last answer an item carries, checked as a cached item is; ``None`` when it carries
    none or one in a shape this code does not know."""
    if not isinstance(item, dict) or item.get("status") != "available":
        return None
    try:
        latest = _cached_release(item.get("latest"))
        releases = [_cached_release(entry) for entry in _cached_list(item.get("releases"))]
    except ShapeError:
        return None
    fetched = item.get("fetched_at")
    if latest not in releases or not isinstance(fetched, str) or parse_timestamp(fetched) is None:
        return None
    etag = item.get("etag")
    return {
        "status": "available",
        "reason": None,
        "source": SOURCE,
        "fetched_at": fetched,
        "checked_at": item.get("checked_at"),
        "latest": latest,
        "releases": releases,
        "etag": etag if isinstance(etag, str) and etag else None,
    }


def _whole(value: str | None) -> int | None:
    try:
        return int(value) if value is not None else None
    except ValueError:
        return None


def _retry_after(seen: Mapping[str, str], now: datetime, *, limited: bool) -> str | None:
    """When the next check may go out, if not on the usual cadence: GitHub's ``Retry-After``; the
    window's reset when the limit is out or fewer than ``RATE_RESERVE`` requests are left (the
    rest belong to whatever else on this address asks GitHub, the engine included); an hour
    when the limit is out and GitHub names no reset. Never further than ``MAX_WAIT_SECONDS``."""
    retry = _whole(seen.get("retry-after"))
    remaining = _whole(seen.get("x-ratelimit-remaining"))
    reset = _whole(seen.get("x-ratelimit-reset"))
    wait: float | None = None
    if retry is not None and retry > 0:
        wait = float(retry)
    elif (limited or (remaining is not None and remaining < RATE_RESERVE)) and reset is not None:
        wait = reset - now.timestamp()
    elif limited:
        wait = MAX_WAIT_SECONDS
    if wait is None or wait <= 0:
        return None
    return (now + timedelta(seconds=min(wait, MAX_WAIT_SECONDS))).isoformat()


def tick(
    cache: dict[str, Any], *, now: datetime, interval_seconds: float, fetch: Fetch
) -> dict[str, Any]:
    """The item for this tick: the cached one while the last attempt is younger than the
    interval, or than GitHub's ``retry_after``; else one new check, built on the cached item
    (mutated into ``cache`` in place; the caller owns the durable copy)."""
    item = cache.get("item")
    attempted = parse_timestamp(cache.get("attempted_at"))
    if attempted is not None and isinstance(item, dict):
        age = age_seconds(attempted, now)
        wait = max(interval_seconds, _wait_after(item.get("retry_after"), attempted))
        if age is not None and 0 <= age < wait:
            return item
    new = fetch(now=now, previous=item)
    cache["attempted_at"] = now.isoformat()
    cache["item"] = new
    return new


def _wait_after(retry_after: object, attempted: datetime) -> float:
    retry = parse_timestamp(retry_after) if isinstance(retry_after, str) else None
    if retry is None:
        return 0.0
    return min(max((retry - attempted).total_seconds(), 0.0), MAX_WAIT_SECONDS)


def parse_release(payload: object) -> Release:
    """``{"version", "published_at"}`` of one release; ``ShapeError`` names what is missing.

    The version is the one in the name (``Hermes Agent v0.21.6``, ``Hermes Agent v0.21.5
    (v2026.9.24)``); a name without one falls back to a tag in the release scheme (``v0.21.6``,
    used since 0.21.6), never to a date tag (``v2026.9.24``, the scheme before it)."""
    if not isinstance(payload, dict):
        raise ShapeError("release is not an object")
    name, tag = payload.get("name"), payload.get("tag_name")
    match = _NAME_VERSION.match(name) if isinstance(name, str) else None
    if match is None and isinstance(tag, str):
        match = _TAG_VERSION.fullmatch(tag)
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
    in a shape this code does not know (a hand-edited state file) is "no data", not a crash. With
    an answer, ``reason`` is why the last check failed, if it did, and ``confirmed_at`` when the
    answer was last read or confirmed."""
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
    if latest not in releases:
        return replace(base, reason="cached answer unreadable")
    ours, behind = _position(releases, running, latest)
    reason, fetched = item.get("reason"), item.get("fetched_at")
    return replace(
        base,
        latest=latest["version"],
        latest_published_at=latest["published_at"],
        running_published_at=ours["published_at"] if ours else None,
        behind=behind,
        list_size=len(releases),
        reason=reason if isinstance(reason, str) and reason else None,
        confirmed_at=fetched if isinstance(fetched, str) else None,
    )


def _position(
    releases: list[Release], running: str | None, latest: Release
) -> tuple[Release | None, int | None]:
    """Our release on the list and how far below Latest it stands (negative: above). Latest is
    its own entry, not the first one with its version string; a running version listed twice
    resolves to the newer entry, the only one a version string can name."""
    versions = [entry["version"] for entry in releases]
    if running is None or running not in versions:
        return None, None
    index = versions.index(running)
    return releases[index], index - releases.index(latest)


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
