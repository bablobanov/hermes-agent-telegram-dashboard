"""Issue #12 on fakes: the registry a registration hands the next one of its profile.

``tests/test_probe_reload.py`` runs the engine's own forced re-discovery and is skipped without
Hermes. Here, in every run: the registry key (the profile's own state file, never the process's
``HERMES_HOME``), profile isolation, the factory of an earlier registration, a registration
unloaded before its takeover ran, and the registry format across releases.
"""

from __future__ import annotations

import asyncio
import os
import sys
import threading
import types
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from probe_fakes import CHAT, FakeAdapter, FakeContext, FakeState, load_plugin, until

LIVE_MODULE = "_telegram_dashboard_probe_live"
TASK_NAME = "telegram_dashboard_probe:tick"


@pytest.fixture(autouse=True)
def fresh_registry() -> Iterator[None]:
    sys.modules.pop(LIVE_MODULE, None)
    try:
        yield
    finally:
        sys.modules.pop(LIVE_MODULE, None)


@pytest.fixture(autouse=True)
def quiet_settings(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    for name in [name for name in os.environ if name.startswith("HERMES_DASHBOARD_PROBE_")]:
        monkeypatch.delenv(name)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "process-home"))


class PathState(FakeState):
    """The engine's PluginState has a ``path``: ``<home>/plugin-data/<namespace>/state.json``."""

    def __init__(self, path: Path) -> None:
        super().__init__()
        self.path = path


def _context(home: Path, *, with_state_path: bool = True) -> FakeContext:
    ctx = FakeContext(
        {"chat_id": CHAT, "period_seconds": 0.01, "hermes_home": str(home), "limits_enabled": False}
    )
    if with_state_path:
        ctx.state = PathState(home / "plugin-data" / "agent-plugin-probe-0000" / "state.json")
    return ctx


def _key(path: Path) -> str:
    return os.path.normcase(str(path.resolve()))


def _loops() -> list[asyncio.Task[Any]]:
    return [
        task for task in asyncio.all_tasks() if not task.done() and task.get_name() == TASK_NAME
    ]


def test_the_key_is_the_profile_s_own_state_file(tmp_path: Path) -> None:
    plugin = load_plugin()
    ctx = _context(tmp_path / "home")
    # The settings name another home and the process another still: the state file decides.
    ctx._settings["hermes_home"] = str(tmp_path / "configured")

    runtime = plugin.register(ctx)

    assert runtime is not None
    assert runtime.key == _key(ctx.state.path)
    spelled = tmp_path / "home" / "plugin-data" / ".." / "plugin-data" / "agent-plugin-probe-0000"
    other = _context(tmp_path / "home")
    other.state = PathState(spelled / "state.json")
    assert plugin.profile_key(other, runtime.settings) == runtime.key
    assert plugin.profile_key(_context(tmp_path / "alpha"), runtime.settings) != runtime.key


def test_without_a_state_file_the_key_is_the_settings_home_never_the_process_s(
    tmp_path: Path,
) -> None:
    plugin = load_plugin()
    ctx = _context(tmp_path / "configured", with_state_path=False)

    runtime = plugin.register(ctx)

    assert runtime is not None
    assert runtime.key == _key(tmp_path / "configured")
    assert runtime.key != _key(Path(os.environ["HERMES_HOME"]))


def test_a_second_registration_takes_the_loop_over_in_its_own_profile_only(
    tmp_path: Path,
) -> None:
    """Two profiles, each wired; the plugin registered again for one of them only (on a worker
    thread, as the engine's loader runs ``register()``). That profile's loop moves to the new
    registration on the adapter it already had; the other profile's loop is untouched."""
    plugin = load_plugin()
    ctx_a, ctx_b = _context(tmp_path / "a"), _context(tmp_path / "b")
    first_a, first_b = plugin.register(ctx_a), plugin.register(ctx_b)
    adapter_a, adapter_b = FakeAdapter(), FakeAdapter()

    async def scenario() -> None:
        first_a.wire(object(), adapter_a)
        first_b.wire(object(), adapter_b)
        await until(lambda: adapter_a.html_edits and adapter_b.html_edits)
        loop_b = first_b.task

        second_a = await asyncio.to_thread(load_plugin().register, ctx_a)
        await until(lambda: second_a.task is not None and first_a.task.done())

        assert first_a.task.cancelled()  # one loop per profile, even with no unload before it
        assert second_a.latest is adapter_a and second_a.generation == 1
        assert first_b.task is loop_b and not loop_b.done()
        assert sorted(map(id, _loops())) == sorted(map(id, (second_a.task, loop_b)))
        seen_b = len(adapter_b.texts)
        before = len(adapter_a.html_edits)
        await until(lambda: len(adapter_a.html_edits) > before + 1)
        assert len(adapter_a.sent) == 1  # the same message, edited by the new registration
        assert len(adapter_b.texts) > seen_b
        for task in _loops():
            task.cancel()

    asyncio.run(scenario())
    profiles = sys.modules[LIVE_MODULE].profiles
    assert set(profiles) == {first_a.key, first_b.key} and first_a.key != first_b.key


def test_the_factory_of_an_earlier_registration_is_ignored(tmp_path: Path) -> None:
    plugin = load_plugin()
    ctx = _context(tmp_path / "home")
    first = plugin.register(ctx)
    adapter = FakeAdapter()

    async def scenario() -> None:
        first.wire(object(), adapter)
        await until(lambda: adapter.html_edits)
        second = plugin.register(ctx)
        await until(lambda: second.task is not None)
        stopped = first.task

        first.wire(object(), FakeAdapter())  # a stale reference to the old factory

        assert first.task is stopped and first.latest is adapter
        await asyncio.sleep(0.05)
        assert _loops() == [second.task]
        second.task.cancel()

    asyncio.run(scenario())


def test_a_registration_unloaded_before_its_takeover_ran_takes_nothing_over(
    tmp_path: Path,
) -> None:
    """The engine unloads the new registration (``ctx.on_unload``) before the gateway loop ran
    the takeover it scheduled: nothing starts, the earlier loop is not cancelled by it."""
    plugin = load_plugin()
    ctx = _context(tmp_path / "home")
    callbacks: list[Any] = []
    ctx.on_unload = callbacks.append  # type: ignore[attr-defined]
    first = plugin.register(ctx)
    adapter = FakeAdapter()

    async def scenario() -> None:
        first.wire(object(), adapter)
        await until(lambda: adapter.html_edits)
        second = plugin.register(ctx)  # the takeover is scheduled on this loop, not run yet
        callbacks[-1]()  # the engine's unload of the second registration

        await asyncio.sleep(0.05)

        assert second.task is None
        assert _loops() == [first.task]
        second.wire(object(), adapter)  # its factory does nothing either
        assert second.task is None
        first.task.cancel()

    asyncio.run(scenario())
    assert callbacks[0].__self__ is first and callbacks[1].__self__ is not first


def test_an_entry_of_a_later_release_is_read_by_the_keys_this_one_knows(tmp_path: Path) -> None:
    """A later release may write ``version`` 2 and keys this one does not know: the takeover uses
    the adapter, loop, task and generation, and the entry it writes says ``version`` 1."""
    plugin = load_plugin()
    ctx = _context(tmp_path / "home")
    adapter = FakeAdapter()

    async def scenario() -> None:
        earlier = asyncio.get_running_loop().create_task(asyncio.sleep(60), name=TASK_NAME)
        registry = types.ModuleType(LIVE_MODULE)
        key = os.path.normcase(str(ctx.state.path.resolve()))
        registry.version = 2  # type: ignore[attr-defined]
        registry.lock = threading.Lock()  # type: ignore[attr-defined]
        registry.profiles = {  # type: ignore[attr-defined]
            key: {
                "version": 2,
                "owner": lambda: None,
                "adapter": lambda: adapter,
                "loop": asyncio.get_running_loop(),
                "task": earlier,
                "generation": 7,
                "something_new": object(),
            }
        }
        sys.modules[LIVE_MODULE] = registry

        runtime = plugin.register(ctx)
        await until(lambda: runtime.task is not None and adapter.html_edits)

        assert earlier.cancelled() and runtime.generation == 7 and runtime.latest is adapter
        entry = registry.profiles[key]  # type: ignore[attr-defined]
        assert entry["version"] == 1 and "something_new" not in entry
        assert entry["owner"]() is runtime and entry["adapter"]() is adapter
        assert entry["task"] is runtime.task and entry["generation"] == 7
        runtime.task.cancel()

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "entry",
    [
        {"adapter": lambda: None, "generation": 3},  # the adapter is gone
        {"adapter": "not a reference", "loop": "not a loop"},
        "not an entry at all",
    ],
)
def test_an_entry_with_nothing_to_take_over_leaves_the_registration_as_before(
    tmp_path: Path, entry: Any
) -> None:
    plugin = load_plugin()
    ctx = _context(tmp_path / "home")
    adapter = FakeAdapter()
    registry = plugin._live()
    assert registry is not None
    registry.profiles[os.path.normcase(str(ctx.state.path.resolve()))] = entry

    async def scenario() -> None:
        runtime = plugin.register(ctx)
        await asyncio.sleep(0.02)
        assert runtime.task is None  # nothing taken over
        runtime.wire(object(), adapter)  # connect() wires it as on a first start
        await until(lambda: adapter.html_edits)
        assert _loops() == [runtime.task] and runtime.generation == 1
        runtime.task.cancel()

    asyncio.run(scenario())


def test_a_closed_loop_in_the_registry_is_not_taken_over(tmp_path: Path) -> None:
    """A gateway loop that has ended (an earlier ``asyncio.run``): the next registration waits for
    ``connect()`` like a first start."""
    plugin = load_plugin()
    ctx = _context(tmp_path / "home")
    first = plugin.register(ctx)
    adapter = FakeAdapter()

    async def first_run() -> None:
        first.wire(object(), adapter)
        await until(lambda: adapter.html_edits)
        first.task.cancel()

    asyncio.run(first_run())

    async def second_run() -> None:
        second = plugin.register(ctx)
        await asyncio.sleep(0.02)
        assert second.task is None

    asyncio.run(second_run())


def test_a_registry_module_of_another_shape_means_no_takeover(tmp_path: Path) -> None:
    """Something else under the registry's name: the plugin works as before #12, no error."""
    plugin = load_plugin()
    sys.modules[LIVE_MODULE] = types.ModuleType(LIVE_MODULE)
    ctx = _context(tmp_path / "home")
    adapter = FakeAdapter()

    async def scenario() -> None:
        first = plugin.register(ctx)
        first.wire(object(), adapter)
        await until(lambda: adapter.html_edits)
        second = plugin.register(ctx)
        await asyncio.sleep(0.02)
        assert second.task is None and _loops() == [first.task]
        first.task.cancel()

    asyncio.run(scenario())
    assert not hasattr(sys.modules[LIVE_MODULE], "profiles")
