"""Issue #12 against the REAL engine: the plugin registered again by a forced re-discovery.

A forced re-discovery (``discover_and_load(force=True)``: the gateway's ``reload-plugins`` verb that
``hermes plugins install/enable/update`` sends, a tool's forced provider lookup) unloads the plugin,
which cancels its tick task, and runs ``register()`` again with a new runtime. The Telegram adapter
wired the first registration's factory and never calls the new one on the same client. Before the
fix that left no loop at all: no message, the record frozen at ``waiting``.

Real: discovery from profile homes (``plugin.yaml`` and ``config.yaml`` under each), the multiplex
scope of the secondary profiles (``_profile_runtime_scope``), the runner's re-wire subscription
(``GatewayPluginRewireMixin`` on a stub runner), ``TelegramAdapter._wire_plugin_handlers`` with the
engine's own ``get_plugin_manager()`` (no patch), ``spawn_task``, ``PluginState`` on disk, and the
two triggers: ``discover_plugins(True)`` and the ``reload-plugins`` verb, each on a worker thread as
the engine runs them. Replaced: the PTB ``Bot`` (a mock). conftest refuses any connect or lookup off
loopback, and the plugin copies leave the package beside them out, so the plugin takes the package
the tests stub (no release check reaches GitHub).

Needs the engine importable; otherwise the module is skipped.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import sys
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

os.environ.pop("HERMES_ENABLE_PROJECT_PLUGINS", None)

pytest.importorskip("hermes_cli.plugins", reason="Hermes engine not importable in this interpreter")
pytest.importorskip("telegram", reason="python-telegram-bot not installed")

from gateway.config import Platform, PlatformConfig
from gateway.run import _profile_runtime_scope
from gateway.run_plugin_rewire import GatewayPluginRewireMixin, reload_plugins_verb
from hermes_cli.plugins import (
    _reset_plugin_managers_for_tests,
    discover_plugins,
    get_plugin_manager,
)
from plugins.platforms.telegram.adapter import TelegramAdapter

PLUGIN = "telegram_dashboard_probe"
PLUGIN_DIR = Path(__file__).resolve().parents[1] / "plugin" / PLUGIN
TASK_NAME = "telegram_dashboard_probe:tick"
LIVE_MODULE = "_telegram_dashboard_probe_live"
CHAT = "-1001000000001"  # fixture value, not a real chat
SETTLE = 10.0  # a send waits for the engine's per-chat slot (1 s); HTML edits do not


@dataclass(frozen=True)
class Homes:
    """The launch profile's home and two secondaries under it, as a multiplexing gateway has."""

    root: Path

    @property
    def alpha(self) -> Path:
        return self.root / "profiles" / "alpha"

    @property
    def beta(self) -> Path:
        return self.root / "profiles" / "beta"


def _configure(home: Path, *, enabled: bool, disabled: bool = False) -> None:
    """The plugin's config entry, written as an operator writes it. Not enabled: absent from
    ``plugins.enabled``; disabled: listed in ``plugins.disabled`` too, as ``hermes plugins
    disable`` leaves it."""
    allowed = f"\n    - {PLUGIN}" if enabled else " []"
    denied = f"  disabled:\n    - {PLUGIN}\n" if disabled else ""
    (home / "config.yaml").write_text(
        f"plugins:\n  enabled:{allowed}\n{denied}  entries:\n    {PLUGIN}:\n"
        f"      settings:\n        chat_id: '{CHAT}'\n        period_seconds: 0.03\n"
        "        limits_enabled: 'false'\n        context_files: 'false'\n        memory: 'false'\n",
        encoding="utf-8",
    )


def _install(home: Path, *, enabled: bool) -> None:
    """The plugin folder without the package beside it: the plugin then imports the package the
    tests import, which conftest stubs, under a name a re-discovery does not evict."""
    shutil.copytree(
        PLUGIN_DIR,
        home / "plugins" / PLUGIN,
        ignore=shutil.ignore_patterns("__pycache__", "telegram_dashboard"),
    )
    _configure(home, enabled=enabled)


def _fresh_engine() -> None:
    sys.modules.pop(LIVE_MODULE, None)
    _reset_plugin_managers_for_tests()


@pytest.fixture
def homes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Homes]:
    """The probe installed in all three profiles and enabled in the launch profile only, as in
    the issue. The registry and the engine's plugin managers start empty and are emptied after."""
    found = Homes(tmp_path / "root")
    for home, enabled in ((found.root, True), (found.alpha, False), (found.beta, False)):
        home.mkdir(parents=True)
        _install(home, enabled=enabled)
    monkeypatch.setenv("HERMES_HOME", str(found.root))
    for name in [name for name in os.environ if name.startswith("HERMES_DASHBOARD_PROBE_")]:
        monkeypatch.delenv(name)
    _fresh_engine()
    try:
        yield found
    finally:
        _fresh_engine()


class Runner(GatewayPluginRewireMixin):
    """What the verb and the re-wire read off the gateway runner: the adapters per profile."""

    def __init__(self, homes: Homes) -> None:
        self.adapters: dict[Any, Any] = {}
        self._profile_adapters: dict[str, dict[Any, Any]] = {}
        self._served_profile_homes = {
            "default": homes.root,
            "alpha": homes.alpha,
            "beta": homes.beta,
        }
        self._primary_profile_name = "default"


def _adapter(runner: Runner, message_id: int) -> tuple[TelegramAdapter, MagicMock]:
    """A real TelegramAdapter whose PTB Bot is a mock, as ``connect()`` has it at the factory
    step: the client built, not initialised, not connected."""
    adapter = TelegramAdapter(PlatformConfig(enabled=True, token="test-token", extra={}))
    bot = MagicMock()
    bot.send_message = AsyncMock(return_value=MagicMock(message_id=message_id))
    bot.edit_message_text = AsyncMock(return_value=True)
    adapter._app = MagicMock()
    adapter._bot = bot
    adapter.gateway_runner = runner  # what GatewayRunner._create_adapter does
    return adapter, bot


@dataclass
class Profile:
    manager: Any
    adapter: TelegramAdapter
    bot: MagicMock


def _start_default(runner: Runner) -> Profile:
    """The launch profile: discovery, the re-wire subscription, then connect()'s factory step."""
    discover_plugins()
    manager = get_plugin_manager()
    runner._subscribe_plugin_rewire(manager)
    adapter, bot = _adapter(runner, 101)
    adapter._wire_plugin_handlers(adapter._app)
    return Profile(manager, adapter, bot)


def _start_secondary(runner: Runner, name: str, home: Path, message_id: int) -> Profile:
    """A served secondary as ``_load_secondary_profile_config`` loads it, and its own bot's
    connect() under its own scope."""
    adapter, bot = _adapter(runner, message_id)
    with _profile_runtime_scope(home, hydrate_secrets=False):
        discover_plugins()
        manager = get_plugin_manager()
        runner._subscribe_plugin_rewire(manager, name, home)
        adapter._wire_plugin_handlers(adapter._app)
    runner._profile_adapters[name] = {Platform.TELEGRAM: adapter}
    return Profile(manager, adapter, bot)


def _connect(runner: Runner, profile: Profile, name: str | None = None) -> None:
    """Telegram finished connecting: the adapter says so and the runner publishes it."""
    profile.adapter._mark_connected()
    if name is None:
        runner.adapters[Platform.TELEGRAM] = profile.adapter


async def _reload(trigger: str, runner: Runner, home: Path | None = None) -> None:
    """The second registration, through the engine on a worker thread."""
    if trigger == "force-discover":
        await asyncio.to_thread(discover_plugins, True)
        return
    verb = reload_plugins_verb(runner, asyncio.get_running_loop())
    answer = await asyncio.to_thread(verb, {"home": str(home)} if home else {})
    assert answer.get("reloaded") is True, answer


def _runtime(manager: Any) -> Any | None:
    """The probe's runtime behind the factory the manager holds now."""
    found = [
        factory.__self__
        for factory, name in manager.get_platform_handler_factories("telegram")
        if name == PLUGIN
    ]
    return found[-1] if found else None


def _loops() -> list[asyncio.Task[Any]]:
    return [
        task for task in asyncio.all_tasks() if not task.done() and task.get_name() == TASK_NAME
    ]


def _record(home: Path) -> dict[str, Any]:
    (state,) = (home / "plugin-data").glob("*/state.json")
    probe: dict[str, Any] = json.loads(state.read_text(encoding="utf-8"))["probe"]
    return probe


async def _settle(predicate: Callable[[], bool], timeout: float = SETTLE) -> bool:
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > deadline:
            return False
        await asyncio.sleep(0.01)
    return True


def _untouched(profile: Profile) -> None:
    profile.bot.send_message.assert_not_awaited()
    profile.bot.edit_message_text.assert_not_awaited()


@pytest.mark.parametrize("trigger", ["force-discover", "reload-verb"])
def test_a_forced_re_discovery_keeps_one_live_loop(homes: Homes, trigger: str) -> None:
    """The issue's sequence: the factory wired while connect() runs, the first tick waits, the
    plugin is registered again, Telegram connects. One loop delivers: one message, then edits."""

    async def scenario() -> None:
        runner = Runner(homes)
        default = _start_default(runner)
        first = _runtime(default.manager)
        assert first is not None and first.task is not None and not first.task.done()
        others = [
            _start_secondary(runner, "alpha", homes.alpha, 202),
            _start_secondary(runner, "beta", homes.beta, 303),
        ]
        await asyncio.sleep(0.1)  # the first ticks: waiting, Telegram is not connected yet

        await _reload(trigger, runner)
        second = _runtime(default.manager)
        assert second is not None and second is not first
        await _settle(lambda: first.task.done())
        assert first.task.cancelled(), "the unload cancels the first registration's loop"

        _connect(runner, default)
        delivered = await _settle(lambda: default.bot.edit_message_text.await_count >= 2)
        loops = _loops()
        assert delivered, (
            f"no loop delivered after the re-discovery: send={default.bot.send_message.await_count}"
            f" edits={default.bot.edit_message_text.await_count} live loops={len(loops)}"
            f" record={_record(homes.root).get('last_status')!r}"
        )
        assert loops == [second.task], f"expected the second registration's one loop: {loops}"
        default.bot.send_message.assert_awaited_once()
        assert _record(homes.root)["last_status"] == "edited"
        assert _record(homes.root)["message_id"] == "101"
        for other in others:
            _untouched(other)

    asyncio.run(scenario())


def test_each_profile_keeps_its_own_loop_and_message(homes: Homes) -> None:
    """The probe enabled in a secondary profile too: each profile its own registration, loop and
    message through its own bot, before and after each profile is reloaded on its own."""
    _configure(homes.alpha, enabled=True)

    async def scenario() -> None:
        runner = Runner(homes)
        default = _start_default(runner)
        alpha = _start_secondary(runner, "alpha", homes.alpha, 202)
        beta = _start_secondary(runner, "beta", homes.beta, 303)
        _connect(runner, default)
        _connect(runner, alpha, "alpha")
        both = await _settle(
            lambda: min(p.bot.edit_message_text.await_count for p in (default, alpha)) >= 2
        )
        # Each runtime's adapter's runner publishes the launch profile's adapter: alpha's screen
        # must still go out through alpha's own bot.
        sends = (default.bot.send_message.await_count, alpha.bot.send_message.await_count)
        assert both and sends == (1, 1), f"sends (default bot, alpha bot) = {sends}"
        alpha_loop = _runtime(alpha.manager).task

        await _reload("reload-verb", runner)
        assert _runtime(alpha.manager).task is alpha_loop and not alpha_loop.done()
        await _reload("reload-verb", runner, homes.alpha)
        mark = {id(p): p.bot.edit_message_text.await_count for p in (default, alpha)}
        moved = await _settle(
            lambda: all(
                p.bot.edit_message_text.await_count > mark[id(p)] + 1 for p in (default, alpha)
            )
        )

        assert moved, "each profile's screen moves after both reloads"
        runtimes = [_runtime(p.manager) for p in (default, alpha)]
        assert sorted(map(id, _loops())) == sorted(id(r.task) for r in runtimes)
        assert [r.latest for r in runtimes] == [default.adapter, alpha.adapter]
        default.bot.send_message.assert_awaited_once()
        alpha.bot.send_message.assert_awaited_once()
        assert _record(homes.root)["message_id"] == "101"
        assert _record(homes.alpha)["message_id"] == "202"
        _untouched(beta)
        profiles = sys.modules[LIVE_MODULE].profiles
        assert len(profiles) == 2
        assert {profiles[r.key]["owner"]() for r in runtimes} == set(runtimes)

    asyncio.run(scenario())


def test_a_disabled_plugin_reloaded_runs_no_loop_and_comes_back_when_enabled(
    homes: Homes,
) -> None:
    """``hermes plugins disable`` then the reload: no registration, so no loop; enabled and
    reloaded again, the loop comes back on the adapter the registry kept and edits the same
    message."""

    async def scenario() -> None:
        runner = Runner(homes)
        default = _start_default(runner)
        _connect(runner, default)
        assert await _settle(lambda: default.bot.edit_message_text.await_count >= 1)

        _configure(homes.root, enabled=False, disabled=True)
        await _reload("reload-verb", runner)
        assert _runtime(default.manager) is None
        await asyncio.sleep(0.2)
        assert _loops() == []
        stopped = default.bot.edit_message_text.await_count
        await asyncio.sleep(0.2)
        assert default.bot.edit_message_text.await_count == stopped

        _configure(homes.root, enabled=True)
        await _reload("reload-verb", runner)
        back = await _settle(lambda: default.bot.edit_message_text.await_count >= stopped + 2)
        assert back, "the loop did not come back after the plugin was enabled again"
        assert _loops() == [_runtime(default.manager).task]
        default.bot.send_message.assert_awaited_once()

    asyncio.run(scenario())


def test_a_reconnect_after_the_takeover_keeps_one_loop(homes: Homes) -> None:
    """The adapter reconnects after the takeover: the new adapter's connect() calls the second
    registration's factory, which keeps its one loop and counts the generation up."""

    async def scenario() -> None:
        runner = Runner(homes)
        default = _start_default(runner)
        _connect(runner, default)
        assert await _settle(lambda: default.bot.edit_message_text.await_count >= 1)
        await _reload("force-discover", runner)
        second = _runtime(default.manager)
        assert await _settle(lambda: second.task is not None)
        loop, generation = second.task, second.generation

        default.adapter._bot = None  # the old client torn down (adapter.py, disconnect)
        default.adapter._app = None
        default.adapter._mark_disconnected()
        replacement = Profile(default.manager, *_adapter(runner, 999))
        replacement.adapter._wire_plugin_handlers(replacement.adapter._app)
        assert second.task is loop and second.generation == generation + 1
        assert _loops() == [loop]

        _connect(runner, replacement)
        assert await _settle(lambda: replacement.bot.edit_message_text.await_count >= 2)
        replacement.bot.send_message.assert_not_awaited()  # the same message, edited
        assert _record(homes.root)["message_id"] == "101"
        assert _record(homes.root)["adapter_generation"] == generation + 1
        assert _loops() == [loop]

    asyncio.run(scenario())


def test_the_earlier_registration_s_factory_is_ignored_after_the_takeover(homes: Homes) -> None:
    """A call into the first registration's factory after the takeover (a stale reference)
    starts nothing: one loop per profile, run by the newest registration."""

    async def scenario() -> None:
        runner = Runner(homes)
        default = _start_default(runner)
        first = _runtime(default.manager)
        _connect(runner, default)
        await _reload("force-discover", runner)
        second = _runtime(default.manager)
        assert await _settle(lambda: second.task is not None and first.task.done())
        cancelled = first.task

        first.wire(default.adapter._app, default.adapter)
        await asyncio.sleep(0.1)

        assert first.task is cancelled and _loops() == [second.task]
        assert sys.modules[LIVE_MODULE].profiles[second.key]["owner"]() is second

    asyncio.run(scenario())
