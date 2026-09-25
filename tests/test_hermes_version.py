"""The Hermes version line: the version the running gateway serves against the latest upstream
release, read by the plugin itself from api.github.com at most once a day.

Pinned here: the version is the one the gateway imported (a capability, never a version gate);
the number of releases behind is a position on upstream's list, never arithmetic on version
numbers; every way GitHub can fail is "no data" with a reason that never quotes the answer (a
rate-limit message carries the caller's IP); a failed check is not retried before a day passes.
"""

from __future__ import annotations

import asyncio
import json
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
from telegram_dashboard.quota_cache import tick
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


class FakeHttp:
    """Answers per URL; records every request so a test can count attempts."""

    def __init__(self, answers: dict[str, tuple[int, str] | BaseException]) -> None:
        self.answers = answers
        self.calls: list[tuple[str, dict[str, str]]] = []

    def __call__(self, url: str, headers: dict[str, str]) -> tuple[int, str]:
        self.calls.append((url, dict(headers)))
        answer = self.answers[url]
        if isinstance(answer, BaseException):
            raise answer
        return answer


def _http(
    latest: tuple[int, str] | BaseException | None = None,
    listing: tuple[int, str] | BaseException | None = None,
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
    latest: tuple[int, str] | BaseException | None,
    listing: tuple[int, str] | BaseException | None,
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


# ----------------------------------------------------------------------------- once a day


class CountingFetch:
    def __init__(self, item: dict[str, Any]) -> None:
        self.item = item
        self.calls = 0

    def __call__(self, *, now: datetime) -> dict[str, Any]:
        self.calls += 1
        return {**self.item, "checked_at": now.isoformat()}


def test_one_attempt_a_day_the_cache_serves_the_rest() -> None:
    fetch = CountingFetch(_available())
    cache: dict[str, Any] = {}

    tick(cache, now=NOW, interval_seconds=hv.INTERVAL_SECONDS, fetch=fetch)
    tick(
        cache,
        now=NOW + timedelta(hours=23, minutes=59),
        interval_seconds=hv.INTERVAL_SECONDS,
        fetch=fetch,
    )
    assert fetch.calls == 1

    tick(cache, now=NOW + timedelta(days=1), interval_seconds=hv.INTERVAL_SECONDS, fetch=fetch)
    assert fetch.calls == 2


def test_a_failed_check_is_not_retried_before_a_day() -> None:
    failed = hv.fetch_item(now=NOW, get=_http(latest=(429, "{}")))
    fetch = CountingFetch(failed)
    cache: dict[str, Any] = {}

    tick(cache, now=NOW, interval_seconds=hv.INTERVAL_SECONDS, fetch=fetch)
    later = tick(
        cache, now=NOW + timedelta(hours=6), interval_seconds=hv.INTERVAL_SECONDS, fetch=fetch
    )

    assert fetch.calls == 1
    assert later["reason"] == "GitHub rate limit"


def test_the_interval_is_a_day_and_the_deadline_outlasts_two_socket_timeouts() -> None:
    assert hv.INTERVAL_SECONDS == 86400
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

    def hung(*, now: datetime) -> dict[str, Any]:
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
    def crash(*, now: datetime) -> dict[str, Any]:
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


def test_a_crashing_check_is_cached_for_the_day_not_retried_every_tick(tmp_path: Path) -> None:
    calls: list[datetime] = []

    def crash(*, now: datetime) -> dict[str, Any]:
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
    assert [i.title for i in first.incidents] == ["Collector hermes_version crashed (RuntimeError)"]


def _facade():
    def fetch(provider: str):
        return None

    return fetch


def test_a_hung_github_does_not_hold_back_the_other_sources(tmp_path: Path) -> None:
    """The check starts with the tick and runs beside every other source: Grok's attempt starts
    while GitHub still hangs. Held back until GitHub gave up, Grok would never start here."""
    grok_started = threading.Event()

    def github(*, now: datetime) -> dict[str, Any]:
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
