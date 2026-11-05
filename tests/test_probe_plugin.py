"""Executes the plugin delivery chain against the REAL engine objects of the Hermes clone.

Real: ``PluginManager``, ``PluginContext.register_platform_handler``, ``PluginContext.spawn_task``,
``TelegramAdapter`` (python-telegram-bot installed), ``BasePlatformAdapter._wire_plugin_handlers``
(what ``connect()`` calls), ``TelegramAdapter.send`` / ``edit_message``, ``PluginState`` on disk.
Replaced: the PTB ``Bot`` object on the adapter and the upstream release check, so no request
leaves the machine; conftest's network guard refuses any that would. The network and a real
reconnect of a live gateway are the only links not exercised here.

Needs the engine importable (its venv or PYTHONPATH to the clone); otherwise the module is skipped
so the ordinary suite keeps running without Hermes. HERMES_HOME is pointed at a temp dir BEFORE any
engine import so nothing touches a real profile.
"""

from __future__ import annotations

import asyncio
import importlib.util
import os
import sys
import tempfile
import time
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

_HOME = Path(tempfile.mkdtemp(prefix="hermes-probe-home-"))
os.environ["HERMES_HOME"] = str(_HOME)
os.environ.pop("HERMES_ENABLE_PROJECT_PLUGINS", None)

pytest.importorskip("hermes_cli.plugins", reason="Hermes engine not importable in this interpreter")
pytest.importorskip("telegram", reason="python-telegram-bot not installed")

from gateway.config import Platform, PlatformConfig  # noqa: E402
from hermes_cli.plugins import PluginContext, PluginManager, PluginManifest  # noqa: E402
from plugins.platforms.telegram.adapter import TelegramAdapter  # noqa: E402
from telegram.error import BadRequest  # noqa: E402

PLUGIN_DIR = Path(__file__).resolve().parents[1] / "plugin" / "telegram_dashboard_probe"
TASK_NAME = "telegram_dashboard_probe:tick"
CHAT = "-1001000000001"  # fixture value, not a real chat
THREAD = "77"


def _load_plugin() -> ModuleType:
    spec = importlib.util.spec_from_file_location("probe_under_test", PLUGIN_DIR / "__init__.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["probe_under_test"] = module
    spec.loader.exec_module(module)
    return module


def _context(name: str = "telegram_dashboard_probe") -> tuple[PluginManager, PluginContext]:
    manager = PluginManager()
    manifest = PluginManifest(name=name, version="0.1.0", description="probe")
    return manager, PluginContext(manifest, manager)


def _adapter(message_id: int, runner: object) -> tuple[TelegramAdapter, MagicMock]:
    """A real TelegramAdapter whose PTB Bot is a mock; state as right after connect()."""
    adapter = TelegramAdapter(PlatformConfig(enabled=True, token="test-token", extra={}))
    bot = MagicMock()
    bot.send_message = AsyncMock(return_value=MagicMock(message_id=message_id))
    bot.edit_message_text = AsyncMock(return_value=True)
    adapter._app = MagicMock()
    adapter._bot = bot
    adapter.gateway_runner = runner  # what GatewayRunner._create_adapter does
    return adapter, bot


def _wire(adapter: TelegramAdapter, manager: PluginManager) -> None:
    """Exactly what TelegramAdapter.connect() does at the factory step (adapter.py:2948)."""
    with patch("hermes_cli.plugins.get_plugin_manager", return_value=manager):
        adapter._wire_plugin_handlers(adapter._app)


async def _until(predicate, *, timeout: float = 3.0) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > deadline:
            raise AssertionError("condition not met in time")
        await asyncio.sleep(0.01)


# The engine paces one chat's sends and edits through a shared per-chat slot
# (plugins/platforms/telegram/adapter.py, _TELEGRAM_CHAT_OUTBOUND_BUDGET_SECS = 1.0, from 0.21.6
# on; absent in 0.21.1): a send waits for the slot, an interim edit_message is skipped while it is
# busy. A wait that spans an edit_message AND a send after it takes two slots and more: 2.0 s
# alone, 3.1 s in this file's run, measured on 0.22.2 and 0.22.7. The 3 s default of ``_until``
# predates the slot and stays for the waits that cross no slot (HTML edits go through
# _edit_text, which the slot does not gate; measured at 0.1 s and under).
SLOT_WAIT = 10.0


# The status line with its data stamp, or a banner above it.
SCREEN_FIRST_LINES = ("🟢", "🟡", "🔴", "⚪", "⚠️")


def _opens_with_status(text: str) -> bool:
    """Since 1.0.0 the HTML status line is bold: an optional ``<b>`` and nothing else before the
    status emoji or the banner."""
    return text.removeprefix("<b>").startswith(SCREEN_FIRST_LINES)


@pytest.fixture
def probe_env(monkeypatch):
    monkeypatch.setenv("HERMES_DASHBOARD_PROBE_CHAT", CHAT)
    monkeypatch.setenv("HERMES_DASHBOARD_PROBE_THREAD", THREAD)
    monkeypatch.setenv("HERMES_DASHBOARD_PROBE_PERIOD", "0.03")
    # The facade IS importable in this interpreter and would call provider APIs: never from a test.
    monkeypatch.setenv("HERMES_DASHBOARD_PROBE_LIMITS", "0")
    # The plugin imports the package beside it, here as probe_under_test.telegram_dashboard: a
    # copy that conftest's stub of telegram_dashboard.collect does not reach. Unstubbed, the
    # tick's upstream release check asked api.github.com from a worker thread.
    _load_plugin()
    vendored = importlib.import_module("probe_under_test.telegram_dashboard.collect")
    monkeypatch.setattr(vendored, "version_fetch_item", _no_release_check)


def _no_release_check(*, now, previous=None):
    return {
        "status": "unavailable",
        "reason": "test stub",
        "source": "github_releases",
        "checked_at": now.isoformat(),
    }


def test_register_queues_one_telegram_factory(probe_env) -> None:
    manager, ctx = _context()

    runtime = _load_plugin().register(ctx)

    assert runtime is not None
    assert [name for _, name in manager.get_platform_handler_factories("telegram")] == [
        "telegram_dashboard_probe"
    ]
    assert manager.get_platform_handler_factories("discord") == []


def test_without_a_chat_nothing_is_registered(monkeypatch) -> None:
    monkeypatch.delenv("HERMES_DASHBOARD_PROBE_CHAT", raising=False)
    manager, ctx = _context()

    assert _load_plugin().register(ctx) is None
    assert manager.get_platform_handler_factories("telegram") == []


def test_chain_send_edit_and_survive_adapter_replacement(probe_env) -> None:
    """connect → factory → spawn_task → send → edit; then the runner replaces the adapter."""
    manager, ctx = _context()
    runtime = _load_plugin().register(ctx)
    assert runtime is not None
    runner = SimpleNamespace(adapters={})

    async def scenario() -> None:
        first, bot1 = _adapter(101, runner)
        _wire(first, manager)  # factory fires while connect() is still running
        task = runtime.task
        assert task is not None and not task.done()
        # Nothing may be sent before the adapter reports connected (Bot not initialised yet).
        await asyncio.sleep(0.1)
        bot1.send_message.assert_not_awaited()

        first._mark_connected()
        runner.adapters[Platform.TELEGRAM] = first  # _publish_primary_adapter
        await _until(lambda: bot1.edit_message_text.await_count >= 2)
        bot1.send_message.assert_awaited_once()
        sent = bot1.send_message.await_args.kwargs
        assert sent["chat_id"] == int(CHAT) and sent["message_thread_id"] == int(THREAD)
        edited = bot1.edit_message_text.await_args.kwargs
        assert edited["chat_id"] == int(CHAT) and edited["message_id"] == 101
        assert _opens_with_status(edited["text"]), edited["text"]
        assert "⚪ Unknown" in edited["text"]  # an empty home: nothing is green
        assert "#" not in edited["text"]
        # The engine's public edit_message takes no parse mode (read by signature, not by
        # version), so the edit goes through the adapter's _edit_text with the HTML parse mode:
        # bold headings and the details in a collapsed quote reach the Bot call as such.
        assert edited.get("parse_mode") == "HTML"
        assert (
            "<b>🧠 Limits used</b>" in edited["text"]
            and "<blockquote expandable>" in edited["text"]
        )
        assert runtime.record["screen_format"] == "html"
        assert runtime.record["html_verb"] == "_edit_text"
        assert runtime.record["message_id"] == "101"
        assert runtime.record["adapter_generation"] == 1

        # Reconnect: the old instance is torn down (adapter.py:3171-3172, _mark_disconnected) and
        # the runner builds a NEW adapter whose connect() runs the factory again.
        first._bot = None
        first._app = None
        first._mark_disconnected()
        second, bot2 = _adapter(202, runner)
        _wire(second, manager)
        assert runtime.task is task, "a second factory call must not spawn a second loop"
        assert runtime.generation == 2
        alive = [t for t in asyncio.all_tasks() if not t.done() and t.get_name() == TASK_NAME]
        assert len(alive) == 1, f"expected one probe loop, found {len(alive)}"
        edits_on_old = bot1.edit_message_text.await_count

        second._mark_connected()
        runner.adapters[Platform.TELEGRAM] = second  # _install_reconnected_adapter
        await _until(lambda: bot2.edit_message_text.await_count >= 2)
        bot2.send_message.assert_not_awaited()  # same message, not a new one
        edited = bot2.edit_message_text.await_args.kwargs
        assert edited["message_id"] == 101 and _opens_with_status(edited["text"])
        assert bot1.edit_message_text.await_count == edits_on_old, "old adapter must not be used"
        assert runtime.record["adapter_generation"] == 2

        stored = ctx.state.get("probe")
        assert stored["message_id"] == "101" and stored["last_status"] == "edited"
        # The watchdog reads the very file the engine PluginState just wrote.
        from telegram_dashboard.__main__ import run_check

        assert ctx.state.path.is_file(), ctx.state.path
        assert run_check({"plugin_state_path": str(ctx.state.path), "period_seconds": 300}) == 0

        manager.unload()  # plugin unload cancels supervised tasks
        await asyncio.sleep(0.05)
        assert task.done()

    asyncio.run(scenario())


def test_engine_discovery_loads_the_plugin_from_a_profile(tmp_path: Path, monkeypatch) -> None:
    """The real loader: plugin.yaml under <home>/plugins, opt-in via plugins.enabled, settings from
    plugins.entries.<id>.settings, and the ``telegram_dashboard`` package INSIDE the plugin
    folder, as the repository keeps it. This is the deployment shape, not a hand-built context."""
    import shutil
    import textwrap

    home = tmp_path / "home"
    plugin_home = home / "plugins" / "telegram_dashboard_probe"
    shutil.copytree(PLUGIN_DIR, plugin_home, ignore=shutil.ignore_patterns("__pycache__"))
    # Written as text, as an operator writes it: the engine's core has no PyYAML from 0.21.6 (it
    # reads config.yaml with its own parser), so the test brings no parser of its own. The ids
    # are quoted: unquoted, YAML reads them as numbers.
    (home / "config.yaml").write_text(
        textwrap.dedent(
            f"""\
            plugins:
              enabled:
                - telegram_dashboard_probe
              entries:
                telegram_dashboard_probe:
                  settings:
                    chat_id: '{CHAT}'
                    thread_id: '{THREAD}'
                    period_seconds: 0.03
            """
        ),
        encoding="utf-8",
    )
    for name in ("HERMES_DASHBOARD_PROBE_CHAT", "HERMES_DASHBOARD_PROBE_THREAD"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("HERMES_HOME", str(home))

    manager = PluginManager(scope_key=str(home))
    manager.discover_and_load()

    loaded = {key: plugin for key, plugin in manager._plugins.items() if "probe" in key}
    assert loaded, f"plugin not discovered; keys={sorted(manager._plugins)}"
    ((key, plugin),) = loaded.items()
    assert plugin.enabled and plugin.error is None, (key, plugin.error)
    assert [name for _, name in manager.get_platform_handler_factories("telegram")] == [
        "telegram_dashboard_probe"
    ]
    vendored = [
        name
        for name in sys.modules
        if name.startswith("hermes_plugins.") and name.endswith(".telegram_dashboard.render")
    ]
    assert vendored, "the package beside the plugin was not the one imported"
    manager.unload()


def test_lost_message_is_recreated_once_and_recorded(probe_env) -> None:
    manager, ctx = _context()
    runtime = _load_plugin().register(ctx)
    assert runtime is not None
    runner = SimpleNamespace(adapters={})

    async def scenario() -> None:
        adapter, bot = _adapter(101, runner)
        _wire(adapter, manager)
        adapter._mark_connected()
        runner.adapters[Platform.TELEGRAM] = adapter
        await _until(lambda: bot.edit_message_text.await_count >= 1)

        bot.edit_message_text.side_effect = BadRequest("Message to edit not found")
        bot.send_message.return_value = MagicMock(message_id=102)
        # The HTML edit's "not found" reads as a refused HTML edit first (by design); the plain
        # edit_message after it is skipped while the first send's slot is busy, then fires and
        # confirms the loss, and the new send waits for the slot that edit took (SLOT_WAIT).
        await _until(lambda: bot.send_message.await_count >= 2, timeout=SLOT_WAIT)
        await _until(lambda: runtime.record.get("message_id") == "102", timeout=SLOT_WAIT)

        assert runtime.record["lost_at"]
        assert runtime.record["last_status"] == "created"
        manager.unload()

    asyncio.run(scenario())
