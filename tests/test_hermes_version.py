"""The Hermes version line: the version the running gateway serves against the latest upstream
release, read by the plugin itself from api.github.com at most once per 15 minutes.

Pinned here: the version is the one the gateway imported (a capability, never a version gate);
the number of releases behind is a position on upstream's list, never arithmetic on version
numbers; every way GitHub can fail is a reason that never quotes the answer (a rate-limit
message carries the caller's IP), and a failed check keeps the last answer read; a new release
is seen within one check, by its version and not by the ETag; a 304 reads no list; a limit
running low or out waits for GitHub's reset.
"""

from __future__ import annotations

import asyncio
import json
import sys
import threading
import time
import types
import urllib.error
import urllib.request
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from telegram_dashboard import hermes_version as hv
from telegram_dashboard.collect import CommandResult, collect_all_async
from telegram_dashboard.compat import Environment
from telegram_dashboard.render import render_dashboard
from telegram_dashboard.schema import VersionSummary
from telegram_dashboard.workers import Flights

NOW = datetime(2026, 9, 25, 16, 40, tzinfo=UTC)


def _release(version: str, published: str, *, tag: str, **extra: Any) -> dict[str, Any]:
    """One entry the way the releases API answered on 2026-09-25 (fields not read dropped)."""
    return {
        "tag_name": tag,
        "name": f"Hermes Agent v{version} ({tag})",
        "draft": False,
        "prerelease": False,
        "published_at": published,
        "html_url": f"https://github.com/NousResearch/hermes-agent/releases/tag/{tag}",
        **extra,
    }


RELEASES = [
    _release("0.21.5", "2026-09-24T10:09:38Z", tag="v2026.9.24"),
    _release("0.21.4", "2026-09-21T18:10:55Z", tag="v2026.9.21"),
    _release("0.21.3", "2026-09-14T16:04:14Z", tag="v2026.9.14"),
    _release("0.21.2", "2026-09-11T19:20:31Z", tag="v2026.9.11"),
    _release("0.21.1", "2026-09-07T22:17:01Z", tag="v2026.9.7"),
]
LATEST = RELEASES[0]


Answer = tuple[int, str] | tuple[int, str, dict[str, str]]


class FakeHttp:
    """Answers per URL (status, body and, when a test means them, the response headers);
    records every request so a test can count attempts."""

    def __init__(self, answers: dict[str, Answer | BaseException]) -> None:
        self.answers = answers
        self.calls: list[tuple[str, dict[str, str]]] = []

    def __call__(self, url: str, headers: dict[str, str]) -> tuple[int, str, dict[str, str]]:
        self.calls.append((url, dict(headers)))
        answer = self.answers[url]
        if isinstance(answer, BaseException):
            raise answer
        return (answer[0], answer[1], answer[2] if len(answer) == 3 else {})


def _http(
    latest: Answer | BaseException | None = None,
    listing: Answer | BaseException | None = None,
) -> FakeHttp:
    return FakeHttp(
        {
            hv.LATEST_URL: latest if latest is not None else (200, json.dumps(LATEST)),
            hv.LIST_URL: listing if listing is not None else (200, json.dumps(RELEASES)),
        }
    )


def _gateway(version: object) -> dict[str, object]:
    return {"hermes_cli": types.SimpleNamespace(__version__=version)}


# ----------------------------------------------------------------------------- running version


def test_the_running_version_is_the_constant_of_the_module_the_gateway_imported() -> None:
    assert hv.running_version(_gateway("0.21.3")) == ("0.21.3", None)


def test_without_the_hermes_module_the_version_is_not_on_this_installation() -> None:
    assert hv.running_version({}) == (None, "not on this installation")


@pytest.mark.parametrize("value", [None, 21, "", "   "])
def test_a_version_that_is_not_a_non_empty_string_is_not_on_this_installation(
    value: object,
) -> None:
    assert hv.running_version(_gateway(value)) == (None, "not on this installation")


def _lazy_gateway(
    stamp: object = None, *, started: object = None, error: BaseException | None = None
) -> tuple[dict[str, object], list[str]]:
    """Hermes 0.21.6: no constant; the module's __getattr__ reads the install stamp from disk on
    every access. ``started``: the identity the gateway resolved at start-up, when it did."""
    reads: list[str] = []
    module = types.ModuleType("hermes_cli")

    def lazy(name: str) -> object:
        if name != "__version__":
            raise AttributeError(name)
        reads.append(name)
        if error is not None:
            raise error
        return stamp

    module.__getattr__ = lazy  # type: ignore[attr-defined]
    modules: dict[str, object] = {"hermes_cli": module}
    if started is not None:
        info = types.SimpleNamespace(base_version=started)
        modules["hermes_cli.version_info"] = types.SimpleNamespace(_cached_version_info=info)
    return modules, reads


def test_on_0_21_6_the_version_comes_from_the_install_stamp_when_nothing_else_knows_it() -> None:
    modules, reads = _lazy_gateway("0.21.6")
    assert hv.running_version(modules) == ("0.21.6", None)
    assert reads == ["__version__"]


def test_on_0_21_6_the_identity_of_the_running_process_wins_over_the_stamp_on_disk() -> None:
    """An update rewrote the stamp, the gateway has not restarted: the line names the code that
    runs, and the stamp is not even read."""
    modules, reads = _lazy_gateway("0.21.7", started="0.21.6")
    assert hv.running_version(modules) == ("0.21.6", None)
    assert reads == []


@pytest.mark.parametrize("stamp", ["0.0.0", "unknown", " UNKNOWN "])
def test_a_placeholder_is_no_version_stamp_never_a_version(stamp: str) -> None:
    """No install stamp: 0.21.6 says 0.0.0 (a manual clone, a local docker build, a Nix build,
    a failed install tail), or unknown for a checkout git cannot place."""
    assert hv.running_version(_lazy_gateway(stamp)[0]) == (None, hv.UNSTAMPED)
    assert hv.running_version(_lazy_gateway(started=stamp)[0]) == (None, hv.UNSTAMPED)


@pytest.mark.parametrize(
    "error",
    [
        ModuleNotFoundError("No module named 'hermes_cli.steward'"),
        ImportError("cannot import name 'repo_root' from 'pm.paths' (/home/op/hermes/pm/paths.py)"),
        RuntimeError("Symlink loop from '/home/op/.hermes/hermes-agent'"),
        OSError("[Errno 13] Permission denied: '/home/op/hermes-agent/install-stamp.json'"),
    ],
    ids=["import", "import name", "resolve", "os"],
)
def test_any_error_of_the_engine_s_version_lookup_is_a_reason_that_quotes_nothing(
    error: BaseException,
) -> None:
    """getattr with a default swallows AttributeError only; 0.21.6's __getattr__ can raise an
    import error, a resolve error or an OS error, each with a path in its message."""
    assert hv.running_version(_lazy_gateway(error=error)[0]) == (None, hv.LOOKUP_FAILED)


def test_with_the_installed_engine_the_line_never_shows_a_placeholder() -> None:
    """The real hermes_cli of this interpreter: 0.21.3 a constant, 0.21.6 an editable tree
    without a stamp (0.0.0 from its __getattr__)."""
    pytest.importorskip("hermes_cli", reason="Hermes is not installed in this interpreter")
    version, reason = hv.running_version(sys.modules)
    assert version not in hv.PLACEHOLDERS
    assert (version is None) == (reason is not None)


# ----------------------------------------------------------------------------- one attempt


def test_one_attempt_reads_latest_and_the_list_with_no_token() -> None:
    http = _http()

    item = hv.fetch_item(now=NOW, get=http)

    assert item["status"] == "available"
    assert item["reason"] is None
    assert item["fetched_at"] == item["checked_at"] == NOW.isoformat()
    assert item["latest"] == {"version": "0.21.5", "published_at": "2026-09-24T10:09:38Z"}
    assert [entry["version"] for entry in item["releases"]] == [
        "0.21.5",
        "0.21.4",
        "0.21.3",
        "0.21.2",
        "0.21.1",
    ]
    assert [url for url, _ in http.calls] == [hv.LATEST_URL, hv.LIST_URL]
    for url, headers in http.calls:
        assert url.startswith("https://api.github.com/repos/NousResearch/hermes-agent/")
        assert "Authorization" not in headers
        assert headers["User-Agent"] == "hermes-agent-telegram-dashboard"
        assert headers["Accept"] == "application/vnd.github+json"


def test_drafts_and_prereleases_are_not_releases_to_count() -> None:
    listing = [
        RELEASES[0],
        _release("0.21.5", "2026-09-23T08:00:00Z", tag="v2026.9.23-rc1", prerelease=True),
        _release("0.21.4", "2026-09-22T08:00:00Z", tag="draft", draft=True),
        *RELEASES[1:],
    ]

    item = hv.fetch_item(now=NOW, get=_http(listing=(200, json.dumps(listing))))

    assert [entry["version"] for entry in item["releases"]] == [
        "0.21.5",
        "0.21.4",
        "0.21.3",
        "0.21.2",
        "0.21.1",
    ]


RATE_LIMIT_BODY = json.dumps(
    {
        "message": "API rate limit exceeded for 203.0.113.7. (But here's the good news: "
        "Authenticated requests get a higher rate limit.)",
        "documentation_url": "https://docs.github.com/rest/overview/rate-limits",
    }
)


def _named(name: str) -> str:
    return json.dumps({**LATEST, "name": name})


@pytest.mark.parametrize(
    ("latest", "listing", "reason"),
    [
        (urllib.error.URLError("no route"), None, "request failed: URLError"),
        (TimeoutError("timed out"), None, "request failed: TimeoutError"),
        (None, urllib.error.URLError("reset"), "request failed: URLError"),
        ((429, "{}"), None, "GitHub rate limit"),
        ((403, RATE_LIMIT_BODY), None, "GitHub rate limit"),
        (None, (403, RATE_LIMIT_BODY), "GitHub rate limit"),
        ((403, json.dumps({"message": "Forbidden"})), None, "HTTP 403"),
        ((500, ""), None, "HTTP 500"),
        ((404, "not found"), None, "HTTP 404"),
        ((200, "<html>maintenance</html>"), None, "answer shape: not JSON"),
        ((200, "[]"), None, "answer shape: release is not an object"),
        ((200, _named("Hermes Agent (v2026.9.24)")), None, "answer shape: no version in name"),
        ((200, _named("Hermes Agent v0.21.5<b>")), None, "answer shape: no version in name"),
        ((200, _named("Hermes v0.21.5")), None, "answer shape: no version in name"),
        (
            (200, json.dumps({**LATEST, "published_at": "yesterday"})),
            None,
            "answer shape: release date unreadable",
        ),
        (None, (200, json.dumps({"message": "x"})), "answer shape: release list is not a list"),
        (
            None,
            (200, json.dumps(RELEASES[1:])),
            "answer shape: Latest is not on the release list",
        ),
        (
            None,
            (200, json.dumps([LATEST, {**RELEASES[1], "name": "Hermes Agent"}])),
            "answer shape: no version in name",
        ),
    ],
)
def test_every_failure_is_no_data_with_a_reason_that_never_quotes_the_answer(
    latest: Answer | BaseException | None,
    listing: Answer | BaseException | None,
    reason: str,
) -> None:
    item = hv.fetch_item(now=NOW, get=_http(latest, listing))

    assert item["status"] == "unavailable"
    assert item["reason"] == reason
    assert item["fetched_at"] is None
    assert item["checked_at"] == NOW.isoformat()
    assert item["latest"] is None
    assert item["releases"] == []
    assert "203.0.113.7" not in json.dumps(item)


def test_an_attempt_never_raises_even_on_a_base_exception_from_the_transport() -> None:
    item = hv.fetch_item(now=NOW, get=_http(latest=RecursionError("deep")))

    assert item["status"] == "unavailable"
    assert item["reason"] == "request failed: RecursionError"


# ----------------------------------------------------------------------------- the summary


def _available(**overrides: Any) -> dict[str, Any]:
    return {**hv.fetch_item(now=NOW, get=_http()), **overrides}


def test_two_releases_behind_is_a_position_on_the_list() -> None:
    summary = hv.summarize(_available(), "0.21.3", None)

    assert summary == VersionSummary(
        running="0.21.3",
        latest="0.21.5",
        running_published_at="2026-09-14T16:04:14Z",
        latest_published_at="2026-09-24T10:09:38Z",
        behind=2,
        list_size=5,
        checked_at=NOW.isoformat(),
        confirmed_at=NOW.isoformat(),
    )


def test_the_same_release_is_zero_behind() -> None:
    summary = hv.summarize(_available(), "0.21.5", None)

    assert summary.behind == 0
    assert summary.running_published_at == summary.latest_published_at


def test_a_release_newer_than_latest_is_a_negative_count() -> None:
    newer = _release("0.21.6", "2026-09-26T10:00:00Z", tag="v2026.9.26")
    item = hv.fetch_item(now=NOW, get=_http(listing=(200, json.dumps([newer, *RELEASES]))))

    summary = hv.summarize(item, "0.21.6", None)

    assert summary.latest == "0.21.5"
    assert summary.behind == -1


def test_a_version_not_on_the_list_has_no_count_and_no_date() -> None:
    summary = hv.summarize(_available(), "0.20.0", None)

    assert summary.latest == "0.21.5"
    assert summary.behind is None
    assert summary.running_published_at is None
    assert summary.list_size == 5


def test_an_unknown_running_version_keeps_what_upstream_said() -> None:
    summary = hv.summarize(_available(), None, "not on this installation")

    assert summary.running is None
    assert summary.local_reason == "not on this installation"
    assert summary.latest == "0.21.5"
    assert summary.behind is None


def test_a_failed_check_is_no_latest_with_its_reason_and_time() -> None:
    item = hv.fetch_item(now=NOW, get=_http(latest=(429, "{}")))

    summary = hv.summarize(item, "0.21.3", None)

    assert summary == VersionSummary(
        running="0.21.3",
        checked_at=NOW.isoformat(),
        reason="GitHub rate limit",
    )


@pytest.mark.parametrize(
    "item",
    [
        None,
        "not a dict",
        {"status": "available", "checked_at": NOW.isoformat()},
        {"status": "available", "latest": {"version": 5}, "releases": [], "checked_at": "x"},
        {"status": "available", "latest": LATEST, "releases": "no", "checked_at": None},
    ],
)
def test_a_cached_item_in_an_unknown_shape_is_no_data_never_a_crash(item: object) -> None:
    summary = hv.summarize(item, "0.21.3", None)

    assert summary.running == "0.21.3"
    assert summary.latest is None
    assert summary.behind is None
    assert summary.reason


# ----------------------------------------------------------------------------- the cadence


class CountingFetch:
    def __init__(self, item: dict[str, Any]) -> None:
        self.item = item
        self.calls = 0
        self.previous: list[object] = []

    def __call__(self, *, now: datetime, previous: object = None) -> dict[str, Any]:
        self.calls += 1
        self.previous.append(previous)
        return {**self.item, "checked_at": now.isoformat()}


def test_one_check_per_interval_the_cache_serves_the_ticks_between() -> None:
    fetch = CountingFetch(_available())
    cache: dict[str, Any] = {}

    hv.tick(cache, now=NOW, interval_seconds=hv.INTERVAL_SECONDS, fetch=fetch)
    hv.tick(
        cache,
        now=NOW + timedelta(minutes=14, seconds=59),
        interval_seconds=hv.INTERVAL_SECONDS,
        fetch=fetch,
    )
    assert fetch.calls == 1

    hv.tick(
        cache, now=NOW + timedelta(minutes=15), interval_seconds=hv.INTERVAL_SECONDS, fetch=fetch
    )
    assert fetch.calls == 2
    assert fetch.previous[0] is None
    assert isinstance(fetch.previous[1], dict)  # each check is built on the last one


def test_a_failed_check_is_retried_after_the_interval_not_a_day() -> None:
    failed = hv.fetch_item(now=NOW, get=_http(latest=(500, "")))
    fetch = CountingFetch(failed)
    cache: dict[str, Any] = {}

    hv.tick(cache, now=NOW, interval_seconds=hv.INTERVAL_SECONDS, fetch=fetch)
    later = hv.tick(
        cache, now=NOW + timedelta(minutes=10), interval_seconds=hv.INTERVAL_SECONDS, fetch=fetch
    )
    assert fetch.calls == 1
    assert later["reason"] == "HTTP 500"

    hv.tick(
        cache, now=NOW + timedelta(minutes=16), interval_seconds=hv.INTERVAL_SECONDS, fetch=fetch
    )
    assert fetch.calls == 2


def test_the_cadence_keeps_well_inside_github_s_hourly_limit_without_a_token() -> None:
    """60 requests an hour per address without a token, a 304 included; the engine or a script
    may ask from the same address. One check a quarter of an hour is 4, a new release adds one
    list; the deadline outlasts two socket timeouts."""
    assert hv.INTERVAL_SECONDS == 900
    assert 3600 / hv.INTERVAL_SECONDS + 1 <= 60 - hv.RATE_RESERVE
    assert hv.TICK_TIMEOUT_SECONDS > 2 * hv.HTTP_TIMEOUT_SECONDS
    assert hv.PER_PAGE == 100


# ----------------------------------------------------------------------------- the collector
#
# The plugin makes the request itself, in its own worker thread under a deadline, through the
# same ``Flights`` as Grok and Kimi: no LLM, no agent session, no agent tool. A hung or failing
# GitHub costs this one line its answer and nothing else: not the gateway's event loop (which
# the agent shares), not the rest of the screen.


def _env(tmp_path: Path) -> Environment:
    payload = {
        "pid": 4242,
        "gateway_state": "running",
        "updated_at": "2026-09-25T16:38:00+00:00",
        "platforms": {"telegram": {"state": "running", "writer_pid": 4242}},
    }
    (tmp_path / "gateway_state.json").write_text(json.dumps(payload), encoding="utf-8")
    return Environment(hermes_home=tmp_path, limits_enabled=False)


class Runner:
    def run(self, argv, *, timeout_seconds):
        return CommandResult(0, "", "")


def _local() -> tuple[str | None, str | None]:
    return "0.21.3", None


def _collect(tmp_path: Path, **kwargs: Any) -> Any:
    return asyncio.run(collect_all_async(_env(tmp_path), Runner(), now=NOW, **kwargs))


def test_the_line_exists_only_where_a_durable_cache_is_kept(tmp_path: Path) -> None:
    fetch = CountingFetch(_available())

    without = _collect(tmp_path, version_fetch=fetch, version_local=_local)
    cache: dict[str, Any] = {}
    with_cache = _collect(tmp_path, version_cache=cache, version_fetch=fetch, version_local=_local)

    assert without.version is None
    assert fetch.calls == 1
    assert with_cache.version.running == "0.21.3"
    assert with_cache.version.latest == "0.21.5"
    assert with_cache.version.behind == 2
    assert cache["item"]["status"] == "available"


def test_the_line_is_not_a_source_and_moves_neither_status_nor_coverage(tmp_path: Path) -> None:
    failed = hv.fetch_item(now=NOW, get=_http(latest=(429, "{}")))

    plain = _collect(tmp_path)
    with_line = _collect(
        tmp_path, version_cache={}, version_fetch=CountingFetch(failed), version_local=_local
    )

    assert with_line.version.reason == "GitHub rate limit"
    assert [s.name for s in with_line.sources] == [s.name for s in plain.sources]
    assert with_line.overall == plain.overall
    assert with_line.coverage == plain.coverage
    assert with_line.incidents == plain.incidents


def test_a_hung_github_stalls_neither_the_loop_nor_the_rest_of_the_screen(
    tmp_path: Path,
) -> None:
    release = threading.Event()
    calls: list[datetime] = []

    def hung(*, now: datetime, previous: object = None) -> dict[str, Any]:
        calls.append(now)
        release.wait(10)
        return _available(checked_at=now.isoformat())

    async def scenario() -> tuple[Any, Any, Any, int, float]:
        plain = await collect_all_async(_env(tmp_path), Runner(), now=NOW, flights=Flights())
        flights = Flights()
        beats = 0
        done = asyncio.Event()

        async def heartbeat() -> None:
            nonlocal beats
            while not done.is_set():
                await asyncio.sleep(0.01)
                beats += 1

        pulse = asyncio.ensure_future(heartbeat())
        started = time.monotonic()
        env = _env(tmp_path)
        common: dict[str, Any] = {
            "flights": flights,
            "version_cache": {},
            "version_fetch": hung,
            "version_local": _local,
            "version_timeout_seconds": 0.3,
        }
        first = await collect_all_async(env, Runner(), now=NOW, **common)
        second = await collect_all_async(env, Runner(), now=NOW + timedelta(minutes=5), **common)
        elapsed = time.monotonic() - started
        done.set()
        await pulse
        release.set()  # the abandoned worker returns; asyncio.run waits for it on shutdown
        return plain, first, second, beats, elapsed

    plain, first, second, beats, elapsed = asyncio.run(scenario())

    assert first.version.reason == "no answer within 0.3 s"
    assert first.version.running == "0.21.3"
    assert second.version.reason == "previous request has not returned"
    assert len(calls) == 1  # the busy worker is not started a second time
    assert elapsed < 3
    assert beats >= 10  # the loop kept running while GitHub hung
    # The rest of the screen is exactly what a tick without the line collects.
    assert first.gateway == plain.gateway and first.gateway is not None
    assert first.backup == plain.backup and first.drift == plain.drift
    assert first.capacity == plain.capacity and first.capacity.quotas
    assert first.sources == plain.sources and first.incidents == plain.incidents
    render_dashboard(first, now=NOW, zone=UTC, period_seconds=300)


def test_a_crashing_check_is_an_incident_and_the_screen_still_renders(tmp_path: Path) -> None:
    def crash(*, now: datetime, previous: object = None) -> dict[str, Any]:
        raise RuntimeError("boom at /var/lib/secret/path")

    snapshot = _collect(tmp_path, version_cache={}, version_fetch=crash, version_local=_local)

    assert snapshot.version.running == "0.21.3"
    assert snapshot.version.reason == "collector crashed: RuntimeError"
    titles = [incident.title for incident in snapshot.incidents]
    assert "Collector hermes_version crashed (RuntimeError)" in titles
    text = render_dashboard(snapshot, now=NOW, zone=UTC, period_seconds=300)
    assert "/var/lib/secret" not in text


def test_a_crashing_version_read_keeps_the_upstream_answer(tmp_path: Path) -> None:
    def broken() -> tuple[str | None, str | None]:
        raise AttributeError("no hermes here")

    snapshot = _collect(
        tmp_path,
        version_cache={},
        version_fetch=CountingFetch(_available()),
        version_local=broken,
    )

    assert snapshot.version.running is None
    assert snapshot.version.local_reason == "collector crashed: AttributeError"
    assert snapshot.version.latest == "0.21.5"
    titles = [incident.title for incident in snapshot.incidents]
    assert "Collector hermes_version crashed (AttributeError)" in titles


# ----------------------------------------------------------------------------- review 25.09


def test_a_body_nested_too_deep_is_a_cached_reason_never_an_exception() -> None:
    deep = "[" * 200_000

    item = hv.fetch_item(now=NOW, get=_http(latest=(200, deep)))
    refused = hv.fetch_item(now=NOW, get=_http(latest=(403, deep)))

    assert item["status"] == "unavailable"
    assert item["reason"] == "answer shape: JSON nested too deep"
    assert refused["reason"] == "HTTP 403"


def test_behind_counts_from_latest_s_own_entry_when_its_version_is_listed_twice() -> None:
    again = _release("0.21.5", "2026-09-26T10:00:00Z", tag="v2026.9.26")
    item = hv.fetch_item(now=NOW, get=_http(listing=(200, json.dumps([again, *RELEASES]))))

    summary = hv.summarize(item, "0.21.3", None)

    assert summary.latest_published_at == "2026-09-24T10:09:38Z"
    assert summary.behind == 2


def test_a_cached_latest_missing_from_its_list_is_no_data() -> None:
    item = _available(releases=[{"version": "0.21.3", "published_at": "2026-09-14T16:04:14Z"}])

    summary = hv.summarize(item, "0.21.3", None)

    assert summary.latest is None
    assert summary.reason == "cached answer unreadable"


def test_a_redirect_is_followed_only_while_it_stays_on_api_github_com() -> None:
    handler = hv._StayOnApi()
    request = urllib.request.Request(hv.LATEST_URL)

    moved = handler.redirect_request(
        request, None, 301, "Moved", {}, "https://api.github.com/repositories/1/releases/latest"
    )
    assert moved is not None
    with pytest.raises(urllib.error.HTTPError):
        handler.redirect_request(request, None, 302, "Found", {}, "https://example.com/x")


def test_a_crashing_check_is_cached_for_the_interval_not_retried_every_tick(tmp_path: Path) -> None:
    calls: list[datetime] = []

    def crash(*, now: datetime, previous: object = None) -> dict[str, Any]:
        calls.append(now)
        raise RuntimeError("parser bug")

    cache: dict[str, Any] = {}

    async def two_ticks() -> tuple[Any, Any]:
        flights = Flights()
        common: dict[str, Any] = {
            "flights": flights,
            "version_cache": cache,
            "version_fetch": crash,
            "version_local": _local,
        }
        env = _env(tmp_path)
        first = await collect_all_async(env, Runner(), now=NOW, **common)
        second = await collect_all_async(env, Runner(), now=NOW + timedelta(minutes=5), **common)
        return first, second

    first, second = asyncio.run(two_ticks())

    assert len(calls) == 1
    assert cache["attempted_at"] == NOW.isoformat()
    assert second.version.reason == "collector crashed: RuntimeError"
    crashed = "Collector hermes_version crashed (RuntimeError)"
    assert crashed in [incident.title for incident in first.incidents]
    assert crashed not in [incident.title for incident in second.incidents]  # served from cache


def _facade():
    def fetch(provider: str):
        return None

    return fetch


def test_a_hung_github_does_not_hold_back_the_other_sources(tmp_path: Path) -> None:
    """The check starts with the tick and runs beside every other source: Grok's attempt starts
    while GitHub still hangs. Held back until GitHub gave up, Grok would never start here."""
    grok_started = threading.Event()

    def github(*, now: datetime, previous: object = None) -> dict[str, Any]:
        if grok_started.wait(2):
            return _available(checked_at=now.isoformat())
        return {"status": "unavailable", "reason": "Grok never started", "checked_at": None}

    def grok(*, now: datetime) -> dict[str, Any]:
        grok_started.set()
        return {"provider": "grok", "status": "unavailable", "reason": "x", "windows": []}

    env = Environment(hermes_home=_env(tmp_path).hermes_home, limits_enabled=True)
    snapshot = asyncio.run(
        collect_all_async(
            env,
            Runner(),
            now=NOW,
            resolve_limits=_facade,
            grok_cache={},
            grok_fetch=grok,
            version_cache={},
            version_fetch=github,
            version_local=_local,
            version_timeout_seconds=5,
        )
    )

    assert snapshot.version.latest == "0.21.5", snapshot.version.reason


# ----------------------------------------------------------------------------- a new release
#
# 0.11.0: a new release is on the screen within one check. Each check asks for Latest with the
# last answer's ETag; a 304 reads no list, nor does a 200 for the same release (its notes are
# edited after publication, which changes the ETag); the list is read when Latest names another
# version. A failed check keeps the last answer. GitHub's limit (60 an hour per address without
# a token, a 304 included) is shared with whatever else asks from the address.

V0216 = {
    "tag_name": "v0.21.6",
    "name": "Hermes Agent v0.21.6",
    "draft": False,
    "prerelease": False,
    "published_at": "2026-10-08T11:51:57Z",
}
LATER = NOW + timedelta(days=12, hours=19, minutes=30)  # 2026-10-08 12:10 UTC


def _known(etag: str = 'W/"e1"') -> dict[str, Any]:
    """A check's answer as the cache keeps it: Latest 0.21.5 with its ETag, read at NOW."""
    item = hv.fetch_item(now=NOW, get=_http(latest=(200, json.dumps(LATEST), {"etag": etag})))
    assert item["etag"] == etag
    return item


def test_a_new_release_is_read_with_its_list_on_the_first_check_after_it_appears() -> None:
    previous = _known()
    http = _http(
        latest=(200, json.dumps(V0216), {"etag": 'W/"e2"'}),
        listing=(200, json.dumps([V0216, *RELEASES])),
    )

    item = hv.fetch_item(now=LATER, get=http, previous=previous)

    assert [url for url, _ in http.calls] == [hv.LATEST_URL, hv.LIST_URL]
    assert http.calls[0][1]["If-None-Match"] == 'W/"e1"'
    assert "If-None-Match" not in http.calls[1][1]
    assert item["latest"] == {"version": "0.21.6", "published_at": "2026-10-08T11:51:57Z"}
    assert (item["etag"], item["reason"], item["fetched_at"]) == ('W/"e2"', None, LATER.isoformat())
    summary = hv.summarize(item, "0.21.3", None)
    assert (summary.latest, summary.behind) == ("0.21.6", 3)


def test_a_304_confirms_the_last_answer_and_reads_no_list() -> None:
    previous = _known()
    http = _http(latest=(304, "", {"etag": 'W/"e1"', "x-ratelimit-remaining": "41"}))

    item = hv.fetch_item(now=LATER, get=http, previous=previous)

    assert [url for url, _ in http.calls] == [hv.LATEST_URL]
    assert http.calls[0][1]["If-None-Match"] == 'W/"e1"'
    assert (item["latest"], item["releases"]) == (previous["latest"], previous["releases"])
    assert item["fetched_at"] == item["checked_at"] == LATER.isoformat()
    assert (item["reason"], item["retry_after"], item["rate_remaining"]) == (None, None, 41)


def test_the_same_release_with_its_notes_edited_is_no_new_release() -> None:
    """A new ETag is not a new release: v0.21.6 was published at 11:51:57Z and its notes last
    modified at 13:08:31Z. A new release is a new version."""
    previous = _known()
    http = _http(latest=(200, json.dumps({**LATEST, "body": "notes, edited"}), {"etag": 'W/"e3"'}))

    item = hv.fetch_item(now=LATER, get=http, previous=previous)

    assert [url for url, _ in http.calls] == [hv.LATEST_URL]
    assert (item["latest"], item["etag"]) == (previous["latest"], 'W/"e3"')


def test_an_exhausted_limit_keeps_the_last_answer_and_waits_for_github_s_reset() -> None:
    previous = _known()
    reset = int((LATER + timedelta(minutes=40)).timestamp())
    headers = {"x-ratelimit-remaining": "0", "x-ratelimit-reset": str(reset)}

    item = hv.fetch_item(
        now=LATER, get=_http(latest=(403, RATE_LIMIT_BODY, headers)), previous=previous
    )

    assert (item["status"], item["latest"]) == ("available", previous["latest"])
    assert (item["reason"], item["fetched_at"]) == ("GitHub rate limit", NOW.isoformat())
    assert item["retry_after"] == (LATER + timedelta(minutes=40)).isoformat()
    assert "203.0.113.7" not in json.dumps(item)
    summary = hv.summarize(item, "0.21.3", None)
    assert (summary.latest, summary.behind, summary.reason) == ("0.21.5", 2, "GitHub rate limit")
    assert (summary.confirmed_at, summary.checked_at) == (NOW.isoformat(), LATER.isoformat())

    fetch = CountingFetch(item)
    cache: dict[str, Any] = {"attempted_at": LATER.isoformat(), "item": item}
    for minutes in (16, 39):
        hv.tick(
            cache,
            now=LATER + timedelta(minutes=minutes),
            interval_seconds=hv.INTERVAL_SECONDS,
            fetch=fetch,
        )
    assert fetch.calls == 0
    hv.tick(
        cache, now=LATER + timedelta(minutes=40), interval_seconds=hv.INTERVAL_SECONDS, fetch=fetch
    )
    assert fetch.calls == 1


@pytest.mark.parametrize(
    ("remaining", "waits"), [(hv.RATE_RESERVE - 1, True), (hv.RATE_RESERVE, False)]
)
def test_a_limit_running_low_leaves_the_rest_to_the_engine_until_the_reset(
    remaining: int, waits: bool
) -> None:
    reset = int((NOW + timedelta(minutes=50)).timestamp())
    headers = {"x-ratelimit-remaining": str(remaining), "x-ratelimit-reset": str(reset)}

    item = hv.fetch_item(now=NOW, get=_http(latest=(200, json.dumps(LATEST), headers)))

    assert (item["status"], item["reason"]) == ("available", None)
    expected = (NOW + timedelta(minutes=50)).isoformat() if waits else None
    assert item["retry_after"] == expected


@pytest.mark.parametrize(
    ("headers", "seconds"),
    [
        ({"retry-after": "120"}, 120),
        ({"x-ratelimit-remaining": "0", "x-ratelimit-reset": "9999999999"}, 3600),
        ({}, 3600),
    ],
    ids=["Retry-After", "a reset far away", "no reset named"],
)
def test_github_s_wait_is_believed_up_to_an_hour(headers: dict[str, str], seconds: int) -> None:
    item = hv.fetch_item(now=NOW, get=_http(latest=(429, "{}", headers)))

    assert item["retry_after"] == (NOW + timedelta(seconds=seconds)).isoformat()


@pytest.mark.parametrize(
    "failure",
    [urllib.error.URLError("no route"), (500, ""), (200, "<html>maintenance</html>")],
    ids=["network", "server", "shape"],
)
def test_any_failed_check_keeps_the_last_answer_read(failure: Answer | BaseException) -> None:
    item = hv.fetch_item(now=LATER, get=_http(latest=failure), previous=_known())

    summary = hv.summarize(item, "0.21.3", None)

    assert (summary.latest, summary.behind) == ("0.21.5", 2)
    assert summary.reason
    assert (summary.confirmed_at, summary.checked_at) == (NOW.isoformat(), LATER.isoformat())


def test_a_tick_without_an_answer_keeps_the_last_one_in_the_cache() -> None:
    """A deadline, a busy worker, a crash: the line keeps what it knew."""
    from telegram_dashboard import collect

    item = collect._version_missed(LATER, "no answer within 25 s", {"item": _known()})

    summary = hv.summarize(item, "0.21.3", None)
    assert (summary.latest, summary.reason) == ("0.21.5", "no answer within 25 s")
    assert collect._version_missed(LATER, "x", {})["status"] == "unavailable"


def test_an_answer_cached_by_0_10_without_an_etag_is_built_on_without_reading_the_list() -> None:
    old = hv.fetch_item(now=NOW, get=_http())
    for key in ("etag", "rate_remaining", "retry_after"):
        old.pop(key)
    http = _http()

    item = hv.fetch_item(now=LATER, get=http, previous=old)

    assert "If-None-Match" not in http.calls[0][1]
    assert [url for url, _ in http.calls] == [hv.LATEST_URL]
    assert item["latest"] == old["latest"]


@pytest.mark.parametrize(
    "previous",
    [
        None,
        "not a dict",
        {"status": "available", "latest": {"version": 5}, "etag": 'W/"e1"'},
        {"status": "unavailable", "reason": "HTTP 500", "etag": 'W/"e1"'},
    ],
)
def test_without_a_readable_last_answer_the_check_asks_unconditionally(previous: object) -> None:
    http = _http()

    item = hv.fetch_item(now=LATER, get=http, previous=previous)

    assert "If-None-Match" not in http.calls[0][1]
    assert [url for url, _ in http.calls] == [hv.LATEST_URL, hv.LIST_URL]
    assert item["status"] == "available"


def test_the_tag_scheme_change_from_dates_to_versions_reads_one_list() -> None:
    """Up to 0.21.5 the tag was a date (v2026.9.24) and the name carried the version; from
    0.21.6 the tag is the version (v0.21.6) and the name has no date."""
    listing = [V0216, *RELEASES]
    http = _http(latest=(200, json.dumps(V0216)), listing=(200, json.dumps(listing)))

    item = hv.fetch_item(now=LATER, get=http)

    assert [entry["version"] for entry in item["releases"]] == [
        "0.21.6",
        "0.21.5",
        "0.21.4",
        "0.21.3",
        "0.21.2",
        "0.21.1",
    ]


@pytest.mark.parametrize(
    ("name", "tag", "version"),
    [
        ("Hermes Agent v0.21.6", "v0.21.6", "0.21.6"),
        ("Hermes Agent v0.21.5 (v2026.9.24)", "v2026.9.24", "0.21.5"),
        ("Hermes Agent v0.21.6 - The Quicksilver Release", "v0.21.6", "0.21.6"),
        ("Hermes Agent v0.20.0 (2026.8.18)", "v2026.8.18", "0.20.0"),
        ("The Quicksilver Release", "v0.22.0", "0.22.0"),
        ("", "v0.22.1", "0.22.1"),
    ],
)
def test_the_version_is_the_name_s_else_a_tag_in_the_release_scheme(
    name: str, tag: str, version: str
) -> None:
    assert hv.parse_release({**V0216, "name": name, "tag_name": tag})["version"] == version


@pytest.mark.parametrize(
    "tag",
    [
        "v2026.10.1",
        "v2026.9.24",
        "0.22.0",
        "v0.22",
        "v0.22.0-rc1",
        "v0.21.4+canary.20261007T070234Z",
        "v1000.1.1",
    ],
)
def test_a_date_tag_or_a_tag_out_of_the_release_scheme_is_never_a_version(tag: str) -> None:
    with pytest.raises(hv.ShapeError):
        hv.parse_release({**V0216, "name": "Hermes Agent", "tag_name": tag})
