"""``collect_all_async``: the composition a gateway plugin runs on every tick.

Three properties the synchronous ``collect_all`` cannot give a plugin: the event loop is never
blocked by a drift command or a provider API, a source whose collector raises unexpectedly
degrades to ``unavailable`` with a named incident instead of killing the tick, and a deadline hit
on one source leaves the other sources' answers intact.
"""

from __future__ import annotations

import asyncio
import json
import time
from datetime import UTC, datetime
from pathlib import Path

import pytest

from telegram_dashboard import collect
from telegram_dashboard.collect import CommandResult, collect_all_async
from telegram_dashboard.compat import Environment

NOW = datetime(2026, 9, 9, 21, 0, tzinfo=UTC)
DRIFT_CLEAN = (
    "[1] only on the server: 0\n[2] only in the baseline: 0\nkeys_changed=0 keys_total=474\n"
)


class BlockingRunner:
    """A drift command that holds the calling thread, the way a real subprocess does."""

    def __init__(self, seconds: float, result: CommandResult) -> None:
        self.seconds = seconds
        self.result = result

    def run(self, argv, *, timeout_seconds):
        time.sleep(self.seconds)
        return self.result


def _env(tmp_path: Path, *, limits_enabled: bool = False) -> Environment:
    payload = {
        "pid": 4242,
        "gateway_state": "running",
        "updated_at": "2026-09-09T20:58:00+00:00",
        "platforms": {"telegram": {"state": "running", "writer_pid": 4242}},
    }
    (tmp_path / "gateway_state.json").write_text(json.dumps(payload), encoding="utf-8")
    return Environment(
        hermes_home=tmp_path, drift_command=("check_drift",), limits_enabled=limits_enabled
    )


@pytest.fixture
def pid_alive(monkeypatch: pytest.MonkeyPatch) -> None:
    """Liveness cannot be asked on Windows; the composition is under test, not the probe."""
    real = collect.collect_gateway
    monkeypatch.setattr(
        collect, "collect_gateway", lambda env, *, now: real(env, now=now, pid_probe=lambda _: True)
    )


async def _count_loop_iterations(stop: asyncio.Event) -> int:
    ticks = 0
    while not stop.is_set():
        ticks += 1
        await asyncio.sleep(0.01)
    return ticks


def test_a_blocking_drift_command_does_not_freeze_the_event_loop(tmp_path: Path) -> None:
    runner = BlockingRunner(0.3, CommandResult(0, DRIFT_CLEAN, ""))

    async def scenario() -> tuple[int, object]:
        stop = asyncio.Event()
        counter = asyncio.create_task(_count_loop_iterations(stop))
        snapshot = await collect_all_async(_env(tmp_path), runner, now=NOW)
        stop.set()
        return await counter, snapshot

    ticks, snapshot = asyncio.run(scenario())

    assert ticks >= 10, f"the loop iterated only {ticks} times during a 0.3 s drift command"
    assert snapshot.drift is not None and snapshot.drift.state == "clean"
    assert snapshot.drift.changed_keys == 0 and snapshot.drift.total_keys == 474


def test_a_drift_deadline_is_one_unavailable_source_and_the_rest_still_answers(
    tmp_path: Path, pid_alive: None
) -> None:
    runner = BlockingRunner(0.5, CommandResult(0, DRIFT_CLEAN, ""))

    snapshot = asyncio.run(
        collect_all_async(_env(tmp_path), runner, now=NOW, drift_timeout_seconds=0.05)
    )

    by_name = {source.name: source for source in snapshot.sources}
    assert by_name["drift"].state == "unavailable"
    assert "deadline" in (by_name["drift"].detail or "")
    assert snapshot.drift is not None and snapshot.drift.state == "unknown"
    assert snapshot.drift.changed_keys is None  # never zero for "we do not know"
    assert by_name["gateway_state"].state == "fresh"
    assert snapshot.gateway is not None and snapshot.gateway.process == "running"


@pytest.mark.parametrize(
    ("collector", "source_name"),
    [
        ("collect_gateway", "gateway_state"),
        ("collect_drift", "drift"),
        ("collect_limits_async", "limits"),
        ("collect_gemini", "gemini_log"),
    ],
)
def test_a_collector_that_raises_degrades_its_source_and_names_itself(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, collector: str, source_name: str
) -> None:
    """A bug in one collector is a warning on the screen, not a dead tick with yesterday's text."""

    def boom(*args, **kwargs):
        raise KeyError("platforms")

    async def boom_async(*args, **kwargs):
        raise KeyError("providers")

    monkeypatch.setattr(collect, collector, boom_async if collector.endswith("_async") else boom)
    # Limits stay enabled so the limits collector is reached, but the facade is never resolved:
    # in an interpreter that has the engine, the default resolver would call provider APIs.
    env = _env(tmp_path, limits_enabled=True)

    snapshot = asyncio.run(
        collect_all_async(
            env,
            BlockingRunner(0, CommandResult(0, DRIFT_CLEAN, "")),
            now=NOW,
            resolve_limits=lambda: None,
        )
    )

    by_name = {source.name: source for source in snapshot.sources}
    assert set(by_name) == {
        "gateway_state",
        "limits",
        "grok_quota",
        "kimi_quota",
        "gemini_log",
        "drift",
        "backup",
    }
    assert by_name[source_name].state == "unavailable"
    assert "KeyError" in (by_name[source_name].detail or "")
    incident_ids = [incident.incident_id for incident in snapshot.incidents]
    assert f"collector:{source_name}" in incident_ids
    assert snapshot.overall in ("warning", "critical")
    if source_name == "gateway_state":
        assert snapshot.gateway is not None and snapshot.gateway.process == "unknown"
    if source_name == "drift":
        assert snapshot.drift is not None and snapshot.drift.state == "unknown"
    if source_name == "limits":
        assert all(quota.kind == "unavailable" for quota in snapshot.capacity.quotas[:2])


def test_no_source_at_all_still_composes_a_snapshot(tmp_path: Path) -> None:
    env = Environment(hermes_home=tmp_path / "missing", limits_enabled=False)

    snapshot = asyncio.run(
        collect_all_async(env, BlockingRunner(0, CommandResult(None, "", "", error="x")), now=NOW)
    )

    assert snapshot.overall == "unknown"
    assert [source.name for source in snapshot.sources] == [
        "gateway_state",
        "limits",
        "grok_quota",
        "kimi_quota",
        "gemini_log",
        "drift",
        "backup",
    ]
    assert all(source.state == "unsupported" for source in snapshot.sources)


class ExitingRunner:
    def run(self, argv, *, timeout_seconds):
        raise SystemExit(3)


def test_a_source_that_exits_the_interpreter_is_unavailable_not_a_dead_tick(
    tmp_path: Path,
) -> None:
    """``SystemExit`` from a worker thread comes back through ``to_thread``; it is not an
    ``Exception`` and would pass every guard, killing the tick with the old text on screen."""
    snapshot = asyncio.run(collect_all_async(_env(tmp_path), ExitingRunner(), now=NOW))

    by_name = {source.name: source for source in snapshot.sources}
    assert by_name["drift"].state == "unavailable"
    assert "SystemExit" in (by_name["drift"].detail or "")
    assert "collector:drift" in [incident.incident_id for incident in snapshot.incidents]


def test_a_drift_deadline_is_not_shown_as_a_completed_check(tmp_path: Path) -> None:
    """``checked_at`` on a deadline would render as "checked HH:MM": a check that did not
    finish must not look like one that did."""
    runner = BlockingRunner(0.5, CommandResult(0, DRIFT_CLEAN, ""))

    snapshot = asyncio.run(
        collect_all_async(_env(tmp_path), runner, now=NOW, drift_timeout_seconds=0.05)
    )

    assert snapshot.drift is not None
    assert snapshot.drift.state == "unknown" and snapshot.drift.checked_at is None
    assert "deadline" in (snapshot.drift.detail or "")


class CountingRunner(BlockingRunner):
    def __init__(self, seconds: float, result: CommandResult) -> None:
        super().__init__(seconds, result)
        self.calls = 0

    def run(self, argv, *, timeout_seconds):
        self.calls += 1
        return super().run(argv, timeout_seconds=timeout_seconds)


def test_a_worker_abandoned_by_its_deadline_is_not_started_again_until_it_returns(
    tmp_path: Path,
) -> None:
    """A deadline releases the tick, not the thread. A source that hangs would otherwise add one
    worker per tick to the gateway's shared executor until nothing else can use it."""
    from telegram_dashboard.workers import Flights

    runner = CountingRunner(0.4, CommandResult(0, DRIFT_CLEAN, ""))
    flights = Flights()

    async def scenario() -> tuple[object, object, object]:
        env = _env(tmp_path)
        first = await collect_all_async(
            env, runner, now=NOW, drift_timeout_seconds=0.05, flights=flights
        )
        second = await collect_all_async(
            env, runner, now=NOW, drift_timeout_seconds=0.05, flights=flights
        )
        await asyncio.sleep(0.5)  # the first worker returns
        third = await collect_all_async(
            env, runner, now=NOW, drift_timeout_seconds=1.0, flights=flights
        )
        return first, second, third

    first, second, third = asyncio.run(scenario())

    assert runner.calls == 2, "the hung worker must not be duplicated while it runs"
    drift_of = lambda s: next(src for src in s.sources if src.name == "drift")  # noqa: E731
    assert drift_of(first).state == "unavailable" and "deadline" in (drift_of(first).detail or "")
    assert drift_of(second).state == "unavailable"
    assert "still in progress" in (drift_of(second).detail or "")
    assert drift_of(third).state == "fresh"


# ---------------------------------------------------------------- external limit sources (0.8.0)

EXT_URL = "http://127.0.0.1:18080/v1/usage"


def _ext_env(tmp_path: Path) -> Environment:
    base = _env(tmp_path, limits_enabled=True)
    return Environment(
        hermes_home=base.hermes_home,
        drift_command=base.drift_command,
        limits_enabled=True,
        limits_sources=({"url": EXT_URL},),
    )


def _claude(snapshot):
    return next(q for q in snapshot.capacity.quotas if q.provider == "Claude")


def _ext_state(snapshot):
    return next(s for s in snapshot.sources if s.name == "Claude limits")


def test_a_slow_external_source_never_holds_the_tick_and_the_screen_keeps_its_line(
    tmp_path: Path, pid_alive: None
) -> None:
    """Decision 10 of the subscription plan, scaled down: the source answers in 0.6 s, the tick
    waits for it 0.1 s. The tick ends on time and shows the cached line with its own stamp; the
    worker is not started twice; its answer is the next tick's line."""
    from datetime import timedelta

    from telegram_dashboard import external
    from telegram_dashboard.render import render_dashboard
    from telegram_dashboard.workers import Flights

    calls: list[datetime] = []

    def fetch(source, *, now):
        calls.append(now)
        if len(calls) > 1:
            time.sleep(0.6)
        answer = {
            "contract": 1,
            "provider": "Claude",
            "state": "ok",
            "fetched_at": now.isoformat(),
            "windows": [{"used_percent": 40 + len(calls), "resets_at": None}],
        }
        return external.parse_contract(answer, now=now)

    runner = BlockingRunner(0, CommandResult(0, DRIFT_CLEAN, ""))
    flights, caches = Flights(), {}
    early = NOW - timedelta(minutes=30)

    def tick(at):
        return collect_all_async(
            _ext_env(tmp_path),
            runner,
            now=at,
            resolve_limits=lambda: None,
            flights=flights,
            external_caches=caches,
            external_interval_seconds=300,
            external_timeout_seconds=0.1,
            external_fetch=fetch,
            period_seconds=1800,
        )

    async def scenario():
        first = await tick(early)
        started = time.monotonic()
        second = await tick(NOW)
        took = time.monotonic() - started
        third = await tick(NOW + timedelta(minutes=1))
        await asyncio.sleep(0.8)  # the abandoned worker returns and writes the cache
        fourth = await tick(NOW + timedelta(minutes=2))
        return first, second, took, third, fourth

    first, second, took, third, fourth = asyncio.run(scenario())

    assert took < 0.5, took
    assert _claude(first).windows[0].used_percent == 41.0
    assert _claude(second).fetched_at == early.isoformat()
    assert _claude(second).windows[0].used_percent == 41.0
    assert _ext_state(second).state == "fresh", "one missed tick does not grey the status"
    details = render_dashboard(second, now=NOW).splitlines()
    assert "> Data 21:00 · Claude 20:30" in details
    assert _claude(third).fetched_at == early.isoformat()
    assert len(calls) == 2, "no second worker while the first one runs"
    assert _claude(fourth).fetched_at == NOW.isoformat()
    assert _claude(fourth).windows[0].used_percent == 42.0
    assert len(calls) == 2, "the next tick takes the late answer from the cache"


def test_a_cached_line_older_than_two_ticks_is_no_data(tmp_path: Path, pid_alive: None) -> None:
    from telegram_dashboard.workers import Flights

    def fetch(source, *, now):
        time.sleep(0.4)
        return {"provider": "Claude", "status": "unavailable", "reason": "late", "windows": []}

    stale_at = "2026-09-09T17:00:00+00:00"
    (source_key,) = [
        s.key
        for s in __import__("telegram_dashboard.external", fromlist=["read_sources"]).read_sources(
            [{"url": EXT_URL}]
        )
    ]
    caches = {
        source_key: {
            "attempted_at": stale_at,
            "provider": "Claude",
            "item": {
                "provider": "Claude",
                "status": "available",
                "reason": None,
                "source": "external",
                "fetched_at": stale_at,
                "windows": [{"label": "w", "used_percent": 5}],
            },
        }
    }

    snapshot = asyncio.run(
        collect_all_async(
            _ext_env(tmp_path),
            BlockingRunner(0, CommandResult(0, DRIFT_CLEAN, "")),
            now=NOW,
            resolve_limits=lambda: None,
            flights=Flights(),
            external_caches=caches,
            external_interval_seconds=300,
            external_timeout_seconds=0.1,
            external_fetch=fetch,
            period_seconds=1800,
        )
    )

    claude = _claude(snapshot)
    assert (claude.kind, claude.detail) == ("unavailable", "no answer within 0.1 s")
    assert _ext_state(snapshot).state == "unavailable"


# ---------------------------------------------------------------- Gemini 429s from the log (0.8.1)


def test_a_slow_gemini_log_never_holds_the_tick_and_the_line_keeps_the_record(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, pid_alive: None
) -> None:
    """Decision of 29.09: the tail is read in a worker under the tick's deadline, never on the
    loop. Scaled down: the read takes 0.4 s, the tick waits 0.05 s. The tick ends on time; the
    log is ``unavailable`` with the reason and the line says what the record remembers; the next
    tick, the worker still reading, says so; once it returns, the log is fresh again."""
    from datetime import timedelta

    from telegram_dashboard.workers import Flights

    real = collect.collect_gemini
    calls: list[datetime] = []

    def slow(env, cache, *, now):
        calls.append(now)
        if len(calls) == 1:
            time.sleep(0.4)
        return real(env, cache, now=now)

    monkeypatch.setattr(collect, "collect_gemini", slow)
    (tmp_path / "logs").mkdir()
    seen = (NOW - timedelta(minutes=5)).isoformat()
    cache = {"last_429": {"at": seen, "limit": 10, "retry_seconds": None, "model": None}}
    flights = Flights()

    def tick():
        return collect_all_async(
            _env(tmp_path, limits_enabled=True),
            BlockingRunner(0, CommandResult(0, DRIFT_CLEAN, "")),
            now=NOW,
            resolve_limits=lambda: None,
            flights=flights,
            gemini_cache=cache,
            gemini_timeout_seconds=0.05,
        )

    def gemini_of(snapshot):
        line = next(q for q in snapshot.capacity.quotas if q.provider == "Gemini")
        return line, next(s for s in snapshot.sources if s.name == "gemini_log")

    async def scenario():
        started = time.monotonic()
        first = await tick()
        took = time.monotonic() - started
        second = await tick()
        await asyncio.sleep(0.5)  # the abandoned worker returns
        third = await tick()
        return first, took, second, third

    first, took, second, third = asyncio.run(scenario())

    assert took < 0.3, took
    line, source = gemini_of(first)
    assert (source.state, source.detail) == ("unavailable", "log read timed out")
    assert line.refusal is not None and line.refusal.at == seen
    line, source = gemini_of(second)
    assert (source.state, source.detail) == ("unavailable", "still reading the log")
    assert line.refusal is not None and line.refusal.at == seen
    assert len(calls) == 2, "the second tick started no reader while the first one ran"
    line, source = gemini_of(third)
    assert source.state == "fresh"
    assert line.refusal is not None and line.refusal.at == seen  # the record outlives the tail
    assert "checked_at" in cache


def test_a_gemini_collector_that_crashes_keeps_the_line_from_the_record(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, pid_alive: None
) -> None:
    from datetime import timedelta

    def boom(*args, **kwargs):
        raise KeyError("last_429")

    monkeypatch.setattr(collect, "collect_gemini", boom)
    seen = (NOW - timedelta(minutes=5)).isoformat()
    cache = {"last_429": {"at": seen}}

    snapshot = asyncio.run(
        collect_all_async(
            _env(tmp_path, limits_enabled=True),
            BlockingRunner(0, CommandResult(0, DRIFT_CLEAN, "")),
            now=NOW,
            resolve_limits=lambda: None,
            gemini_cache=cache,
        )
    )

    line = next(q for q in snapshot.capacity.quotas if q.provider == "Gemini")
    assert line.refusal is not None and line.refusal.at == seen
    source = next(s for s in snapshot.sources if s.name == "gemini_log")
    assert (source.state, source.detail) == ("unavailable", "collector crashed: KeyError")
    assert "collector:gemini_log" in [i.incident_id for i in snapshot.incidents]


def test_a_record_that_cannot_be_read_never_escapes_the_tick(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, pid_alive: None
) -> None:
    """``collect_all_async`` never raises. A record too large for a float once did through the
    crash handler, which re-read it; and a record that fails in a way nobody foresaw is no
    refusal, not a dead tick with yesterday's text on the screen."""
    from datetime import timedelta

    from telegram_dashboard import gemini_log

    def boom(*args, **kwargs):
        raise KeyError("last_429")

    monkeypatch.setattr(collect, "collect_gemini", boom)
    seen = (NOW - timedelta(minutes=5)).isoformat()

    def tick(cache):
        return asyncio.run(
            collect_all_async(
                _env(tmp_path, limits_enabled=True),
                BlockingRunner(0, CommandResult(0, DRIFT_CLEAN, "")),
                now=NOW,
                resolve_limits=lambda: None,
                gemini_cache=cache,
            )
        )

    def gemini_of(snapshot):
        return next(q for q in snapshot.capacity.quotas if q.provider == "Gemini")

    huge = tick({"last_429": {"at": seen, "limit": 10**400}})
    assert gemini_of(huge).refusal is not None
    assert (gemini_of(huge).refusal.at, gemini_of(huge).refusal.limit) == (seen, None)

    def unreadable(cache):
        if cache is not None:
            raise ValueError("record")
        return None

    monkeypatch.setattr(gemini_log, "refusal_from_record", unreadable)
    broken = tick({"last_429": {"at": seen}})
    assert gemini_of(broken).refusal is None
    source = next(s for s in broken.sources if s.name == "gemini_log")
    assert (source.state, source.detail) == ("unavailable", "collector crashed: KeyError")


def test_a_daily_429_stays_an_event_while_the_log_read_misses_its_deadline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, pid_alive: None
) -> None:
    """The record carries the daily 429 through a tick whose read ran out of time: the event
    and the yellow status stay until the reset, the source says why it is unavailable."""
    from datetime import timedelta

    from telegram_dashboard.workers import Flights

    def slow(env, cache, *, now):
        time.sleep(0.3)
        return collect.remembered_part(
            cache, collect.SourceObservation("x", "local", "fresh"), now=now
        )

    monkeypatch.setattr(collect, "collect_gemini", slow)
    seen = (NOW - timedelta(hours=1)).isoformat()
    cache = {"last_429": {"at": seen, "retry_seconds": 14580.0}}

    snapshot = asyncio.run(
        collect_all_async(
            _env(tmp_path, limits_enabled=True),
            BlockingRunner(0, CommandResult(0, DRIFT_CLEAN, "")),
            now=NOW,
            resolve_limits=lambda: None,
            flights=Flights(),
            gemini_cache=cache,
            gemini_timeout_seconds=0.05,
        )
    )

    source = next(s for s in snapshot.sources if s.name == "gemini_log")
    assert (source.state, source.detail) == ("unavailable", "log read timed out")
    events = [(i.incident_id, i.title) for i in snapshot.incidents]
    assert ("gemini:day_quota", "Gemini out of quota 1 h ago") in events
    assert snapshot.overall == "warning"
