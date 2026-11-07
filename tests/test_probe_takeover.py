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

        assert first_a.task.done()  # one loop per profile, even with no unload before it
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


def test_an_earlier_registration_writes_nothing_once_a_newer_one_claimed_the_profile(
    tmp_path: Path,
) -> None:
    """The earlier registration's loop may publish (a ``wire`` that passed its ownership check, a
    reconnect taken up) after the newer one claimed the profile on the load thread and before its
    takeover ran. That write must not hand the profile back: the newer registration's takeover
    would then refuse (not the owner) while the unload retires the earlier one, and no loop would
    be left until the next reconnect. The entry keeps the newer owner, the adapter and the
    generation it had."""
    plugin = load_plugin()
    ctx = _context(tmp_path / "home")
    first = plugin.register(ctx)
    adapter = FakeAdapter()

    async def scenario() -> None:
        first.wire(object(), adapter)
        await until(lambda: adapter.html_edits)
        second = plugin.register(ctx)  # claims the profile; its takeover is scheduled, not run
        first.latest, first.generation = FakeAdapter(), 5  # what a late publish would carry

        first._publish()

        entry = sys.modules[LIVE_MODULE].profiles[first.key]
        assert entry["owner"]() is second
        assert entry["adapter"]() is adapter and entry["generation"] == 1
        await until(lambda: second.task is not None and first.task.done())  # taken over
        assert second.latest is adapter and _loops() == [second.task]
        second.task.cancel()

    asyncio.run(scenario())


def test_a_registration_unloaded_before_its_takeover_ran_takes_nothing_over(
    tmp_path: Path,
) -> None:
    """The engine unloads the new registration (``ctx.on_unload``) before the gateway loop ran
    the takeover it scheduled: nothing starts. The earlier loop, which no longer serves the
    profile, ends at its next tick: no loop at all, as after the engine's own unload of both."""
    plugin = load_plugin()
    ctx = _context(tmp_path / "home")
    callbacks: list[Any] = []

    def on_unload(callback: Any) -> object:
        callbacks.append(callback)
        return object()  # the engine's PluginRegistration

    ctx.on_unload = on_unload  # type: ignore[attr-defined]
    first = plugin.register(ctx)
    adapter = FakeAdapter()

    async def scenario() -> None:
        first.wire(object(), adapter)
        await until(lambda: adapter.html_edits)
        second = plugin.register(ctx)  # the takeover is scheduled on this loop, not run yet
        callbacks[-1]()  # the engine's unload of the second registration

        await asyncio.sleep(0.05)

        assert second.task is None
        assert _loops() == [] and first.task.done() and not first.task.cancelled()
        second.wire(object(), adapter)  # its factory does nothing either
        assert second.task is None

    asyncio.run(scenario())
    assert callbacks[0].__self__ is first and callbacks[1].__self__ is not first


def test_a_replacement_s_connect_as_the_factory_is_registered_wires_the_new_registration(
    tmp_path: Path,
) -> None:
    """Review M1: a replacement adapter's ``connect()`` calls the factory the moment the engine
    registers it. The profile is claimed before that, so the call is the new registration's
    own: it runs on the replacement, and the takeover of the old adapter it scheduled does
    nothing."""
    plugin = load_plugin()
    ctx = _context(tmp_path / "home")
    first = plugin.register(ctx)
    old, new = FakeAdapter(), FakeAdapter()

    async def scenario() -> None:
        first.wire(object(), old)
        await until(lambda: old.html_edits)
        first.retire()  # the engine's unload of the first registration
        first.task.cancel()
        again = _context(tmp_path / "home")

        def register_platform_handler(platform: str, factory: Any) -> None:
            again.factories.append((platform, factory))
            factory(object(), new)  # connect() of a replacement, right now

        again.register_platform_handler = register_platform_handler  # type: ignore[method-assign]
        second = plugin.register(again)
        await until(lambda: len(new.html_edits) >= 2)
        await asyncio.sleep(0.05)

        assert second.latest is new and second.generation_of(new) == 2  # the count goes on
        assert _loops() == [second.task]
        second.task.cancel()

    asyncio.run(scenario())


def test_a_load_the_engine_abandoned_takes_nothing_over(tmp_path: Path) -> None:
    """Review M4: past ``plugins.load_timeout_seconds`` the engine counts the plugin as failed
    and its registrars answer ``None``. Such a ``register()`` claims nothing: the profile's loop
    stays with the registration that has it."""
    plugin = load_plugin()
    ctx = _context(tmp_path / "home")
    first = plugin.register(ctx)
    adapter = FakeAdapter()

    async def scenario() -> None:
        first.wire(object(), adapter)
        await until(lambda: adapter.html_edits)
        late = _context(tmp_path / "home")
        late.on_unload = lambda callback: None  # type: ignore[attr-defined]

        second = plugin.register(late)
        await asyncio.sleep(0.05)

        assert second is not None and second.task is None
        assert _loops() == [first.task]
        assert sys.modules[LIVE_MODULE].profiles[first.key]["owner"]() is first
        first.task.cancel()

    asyncio.run(scenario())


def test_a_loop_whose_registration_no_longer_serves_the_profile_ends_at_its_next_tick(
    tmp_path: Path,
) -> None:
    """Review M3 (a): a reconnect wires the new registration before its takeover ran, and the
    earlier loop is still alive (no unload before it, as on fakes). The takeover then has
    nothing to do, and the earlier loop ends by itself: one loop per profile."""
    plugin = load_plugin()
    ctx = _context(tmp_path / "home")
    first = plugin.register(ctx)
    old, new = FakeAdapter(), FakeAdapter()

    async def scenario() -> None:
        first.wire(object(), old)
        await until(lambda: old.html_edits)
        second = plugin.register(ctx)  # the takeover is scheduled, not run yet
        second.wire(object(), new)  # a reconnect wires the new registration first

        await until(lambda: first.task.done())
        await asyncio.sleep(0.05)

        assert not first.task.cancelled()  # it ended on its own check
        assert _loops() == [second.task] and second.latest is new
        seen = len(old.texts)
        await until(lambda: len(new.html_edits) >= 2)
        assert len(old.texts) == seen
        second.task.cancel()

    asyncio.run(scenario())


def test_a_loop_spawned_just_after_its_registration_was_unloaded_never_ticks(
    tmp_path: Path,
) -> None:
    """Review M3 (b): the engine's unload lands between the takeover's checks and its
    ``spawn_task`` (emulated inside ``spawn_task``). The task exists, but the loop ends before
    its first tick: nothing outlives the unload."""
    plugin = load_plugin()
    ctx = _context(tmp_path / "home")
    first = plugin.register(ctx)
    adapter = FakeAdapter()
    second_of: list[Any] = []

    async def scenario() -> None:
        first.wire(object(), adapter)
        await until(lambda: adapter.html_edits)
        first.retire()  # the engine's unload of the first registration
        first.task.cancel()
        again = _context(tmp_path / "home")
        spawn = again.spawn_task

        def spawn_after_unload(coro: Any, name: str | None = None) -> asyncio.Task[None]:
            second_of[0].retire()  # the engine unloads the new registration right here
            return spawn(coro, name=name)

        again.spawn_task = spawn_after_unload  # type: ignore[method-assign]
        second_of.append(plugin.register(again))
        await until(lambda: second_of[0].task is not None and second_of[0].task.done())
        edits = len(adapter.texts)
        await asyncio.sleep(0.05)

        assert not second_of[0].task.cancelled() and _loops() == []
        assert len(adapter.texts) == edits

    asyncio.run(scenario())


def test_the_launch_profile_takes_over_when_the_earlier_adapter_is_gone(tmp_path: Path) -> None:
    """Review M2: the earlier registration's adapter is gone (collected after the runner
    replaced it inside the reload). For the launch profile the registry keeps the runner, so
    the new registration starts a loop anyway and delivers through the adapter the runner
    publishes. A profile that is not marked as the launch profile's has nothing to take over."""
    plugin = load_plugin()
    ctx = _context(tmp_path / "home")
    first = plugin.register(ctx)
    old = FakeAdapter()  # its runner publishes it: the launch profile's adapter

    async def scenario() -> None:
        first.wire(object(), old)
        await until(lambda: old.html_edits)
        first.retire()  # the engine's unload
        first.task.cancel()
        replacement = FakeAdapter()
        old.gateway_runner.adapters["telegram"] = replacement  # published inside the reload
        entry = sys.modules[LIVE_MODULE].profiles[first.key]
        assert entry["primary"] is True and entry["runner"]() is old.gateway_runner
        entry["adapter"] = lambda: None  # the old adapter collected

        second = await asyncio.to_thread(load_plugin().register, _context(tmp_path / "home"))
        await until(lambda: len(replacement.html_edits) >= 1)

        assert _loops() == [second.task] and second.latest is replacement
        second.task.cancel()

    asyncio.run(scenario())


def _later_registry(
    ctx: FakeContext, *, module_version: int, entry: dict[str, Any]
) -> types.ModuleType:
    """The registry module as a later release might leave it, with one entry for the profile."""
    registry = types.ModuleType(LIVE_MODULE)
    registry.version = module_version  # type: ignore[attr-defined]
    registry.lock = threading.Lock()  # type: ignore[attr-defined]
    registry.profiles = {_key(ctx.state.path): entry}  # type: ignore[attr-defined]
    sys.modules[LIVE_MODULE] = registry
    return registry


@pytest.mark.parametrize(
    ("module_version", "entry_version"), [(1, 2), (2, 1)], ids=["entry", "module"]
)
def test_a_registry_a_later_release_wrote_is_neither_taken_over_nor_written(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, module_version: int, entry_version: int
) -> None:
    """A later release may write ``version`` 2 and keys this one does not know, into an entry
    or as the whole module. This release does not take that release's loop over and writes
    nothing into the registry, which keeps every key, and says so once. While that loop runs, a
    factory call of this registration does nothing (one loop per profile); once it has ended,
    this registration serves the profile as before the registry, still writing nothing."""
    plugin = load_plugin()
    ctx = _context(tmp_path / "home")
    adapter, later_owner = FakeAdapter(), object()
    sentinel = object()

    async def scenario() -> None:
        earlier = asyncio.get_running_loop().create_task(asyncio.sleep(60), name=TASK_NAME)
        entry = {
            "version": entry_version,
            "owner": lambda: later_owner,
            "adapter": lambda: adapter,
            "loop": asyncio.get_running_loop(),
            "task": earlier,
            "generation": 7,
            "primary": True,
            "something_new": sentinel,
        }
        kept = dict(entry)
        registry = _later_registry(ctx, module_version=module_version, entry=entry)

        runtime = plugin.register(ctx)
        await asyncio.sleep(0.05)
        assert runtime is not None and runtime.task is None and not earlier.done()
        runtime.wire(object(), FakeAdapter())  # a reconnect while the later loop runs
        assert runtime.task is None

        earlier.cancel()  # that release's loop ends (its unload)
        await until(lambda: earlier.done())
        own = FakeAdapter()
        runtime.wire(object(), own)
        await until(lambda: len(own.html_edits) >= 2)

        assert _loops() == [runtime.task] and adapter.texts == []
        assert registry.profiles == {_key(ctx.state.path): kept}  # type: ignore[attr-defined]
        assert registry.profiles[_key(ctx.state.path)] is entry  # type: ignore[attr-defined]
        assert registry.version == module_version  # type: ignore[attr-defined]
        runtime.task.cancel()

    with caplog.at_level("INFO", logger="hermes.plugins.telegram_dashboard_probe"):
        asyncio.run(scenario())
    said = [r.getMessage() for r in caplog.records if "later release" in r.getMessage()]
    assert len(said) == 1, said


@pytest.mark.parametrize(
    ("entry", "generation"),
    [
        ({"adapter": lambda: None, "generation": 3}, 4),  # gone; the profile's count goes on
        ({"adapter": "not a reference", "loop": "not a loop"}, 1),
        ("not an entry at all", 1),
    ],
)
def test_an_entry_with_nothing_to_take_over_leaves_the_registration_as_before(
    tmp_path: Path, entry: Any, generation: int
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
        assert _loops() == [runtime.task] and runtime.generation == generation
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


def test_the_registry_module_has_a_spec(tmp_path: Path) -> None:
    """``importlib.util.find_spec`` answers for the registry module instead of raising."""
    import importlib.util

    plugin = load_plugin()
    assert plugin.register(_context(tmp_path / "home")) is not None

    spec = importlib.util.find_spec(LIVE_MODULE)

    assert spec is not None and spec.name == LIVE_MODULE
