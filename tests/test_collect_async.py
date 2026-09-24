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
DRIFT_CLEAN = "[1] изменённые: 0\n[2] лишние: 0\n[3] отсутствующие: 0\n[4] типы: 0\nключей 474\n"


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
    assert "дедлайн" in (by_name["drift"].detail or "")
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
    """``checked_at`` on a deadline would render as "проверено HH:MM": a check that did not
    finish must not look like one that did."""
    runner = BlockingRunner(0.5, CommandResult(0, DRIFT_CLEAN, ""))

    snapshot = asyncio.run(
        collect_all_async(_env(tmp_path), runner, now=NOW, drift_timeout_seconds=0.05)
    )

    assert snapshot.drift is not None
    assert snapshot.drift.state == "unknown" and snapshot.drift.checked_at is None
    assert "дедлайн" in (snapshot.drift.detail or "")


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
    assert drift_of(first).state == "unavailable" and "дедлайн" in (drift_of(first).detail or "")
    assert drift_of(second).state == "unavailable"
    assert "ещё выполняется" in (drift_of(second).detail or "")
    assert drift_of(third).state == "fresh"
