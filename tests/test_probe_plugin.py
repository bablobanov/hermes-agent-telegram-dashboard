"""Executes the plugin delivery chain against the REAL engine objects of the Hermes clone.

Real: ``PluginManager``, ``PluginContext.register_platform_handler``, ``PluginContext.spawn_task``,
``TelegramAdapter`` (python-telegram-bot installed), ``BasePlatformAdapter._wire_plugin_handlers``
(what ``connect()`` calls), ``TelegramAdapter.send`` / ``edit_message``, ``PluginState`` on disk.
Replaced: the PTB ``Bot`` object on the adapter and the upstream release check, so no request
leaves the machine; conftest's network guard refuses any that would. The network and a real
reconnect of a live gateway are the only links not exercised here.

Needs the engine importable (its venv or PYTHONPATH to the clone); otherwise the module is skipped
so the ordinary suite keeps running without Hermes. conftest points HERMES_HOME at a temporary dir
for the whole session before collection, so before any engine import, and probe_env gives each
test a profile of its own: nothing touches a real profile.
"""

from __future__ import annotations

import asyncio
import importlib.util
import os
import sys
import time
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

os.environ.pop("HERMES_ENABLE_PROJECT_PLUGINS", None)

pytest.importorskip("hermes_cli.plugins", reason="Hermes engine not importable in this interpreter")
pytest.importorskip("telegram", reason="python-telegram-bot not installed")

from gateway.config import Platform, PlatformConfig
from hermes_cli.plugins import PluginContext, PluginManager, PluginManifest
from plugins.platforms.telegram.adapter import TelegramAdapter
from telegram.error import BadRequest

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
# on): a send waits for the slot, an interim edit_message is skipped while it is busy. A wait
# that spans an edit_message AND a send after it takes two slots: the lost-message test takes
# 2.1-2.2 s on 0.22.2 and 0.22.7 with the held-back edits deferred (each test in a profile of
# its own; 1.96-2.07 s before that change). The 3 s default of ``_until`` predates the slot
# and stays for the waits that cross no slot (HTML edits go through _edit_text, which the slot
# does not gate; measured at 0.1 s and under).
SLOT_WAIT = 10.0


# The status line with its data stamp, or a banner above it.
SCREEN_FIRST_LINES = ("🟢", "🟡", "🔴", "⚪", "⚠️")


def _opens_with_status(text: str) -> bool:
    """Since 1.0.0 the HTML status line is bold: an optional ``<b>`` and nothing else before the
    status emoji or the banner."""
    return text.removeprefix("<b>").startswith(SCREEN_FIRST_LINES)


@pytest.fixture
def probe_env(monkeypatch, tmp_path: Path):
    # A profile of its own: the engine's PluginState keeps the delivery record under
    # <HERMES_HOME>/plugin-data/, and a message id an earlier test left there would change the
    # next test's path (an edit of "its" message where a first send was meant).
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
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


# The engine's version parser passes re.split's maxsplit positionally: a DeprecationWarning on
# every comparison this test makes (plugins_manifest.py, 0.22.2 and 0.22.7), the engine's own.
@pytest.mark.filterwarnings("ignore:'maxsplit' is passed as positional argument:DeprecationWarning")
@pytest.mark.parametrize("shape", ["as installed", "as the CI job sees it"])
def test_the_engine_s_own_gate_reads_the_manifest_s_floor(monkeypatch, shape: str) -> None:
    """``requires_hermes`` is what the engine checks before it imports the plugin (from 0.21.3
    on, hermes_cli/plugins_manifest.py ``requires_hermes_error``). The gate is put to the
    manifest at the release under test: ``HERMES_ENGINE_TAG`` when set (the CI engine job sets
    it from its matrix, because its checkout is a commit with no tags and the engine tells its
    own version as ``unknown``, which passes any spec), else the version the engine reports (an
    install from a tag, as locally). When the engine knows its version, it must be that release.
    The engine's own comparison also refuses a release below the floor. The second case runs the
    CI job's shape wherever the engine knows its version: it reports ``unknown``, and the tag
    names the release."""
    import re

    from hermes_cli import plugins_manifest
    from hermes_cli.plugins_manifest import requires_hermes_error, version_satisfies

    if shape == "as the CI job sees it":
        installed = plugins_manifest.running_hermes_version()
        tag = os.environ.get("HERMES_ENGINE_TAG") or f"v{installed}"
        monkeypatch.setenv("HERMES_ENGINE_TAG", tag)
        monkeypatch.setattr(plugins_manifest, "running_hermes_version", lambda: "unknown")

    manifest = (PLUGIN_DIR / "plugin.yaml").read_text(encoding="utf-8")
    declared = re.search(r'^requires_hermes: "([^"]+)"$', manifest, re.MULTILINE)
    assert declared is not None
    spec = declared.group(1)
    assert spec == ">=0.22.2"
    assert version_satisfies(spec, "0.22.2") and version_satisfies(spec, "0.22.7")
    assert not version_satisfies(spec, "0.22.1") and not version_satisfies(spec, "0.21.6")

    reported = plugins_manifest.running_hermes_version()
    tag = os.environ.get("HERMES_ENGINE_TAG", "").strip().removeprefix("v")
    release = tag or reported
    if tag and reported != "unknown":
        assert reported == tag, f"the engine reports {reported}, the job tests {tag}"
    if not re.fullmatch(r"\d+\.\d+\.\d+", release):
        pytest.skip(
            "the engine tells its version as unknown and HERMES_ENGINE_TAG names none: "
            "its gate would let any manifest through"
        )

    # The engine's own gate function, with the running version it reads set to the release.
    monkeypatch.setattr(plugins_manifest, "running_hermes_version", lambda: release)
    assert requires_hermes_error({"requires_hermes": spec}) is None
    monkeypatch.setattr(plugins_manifest, "running_hermes_version", lambda: "0.22.1")
    assert requires_hermes_error({"requires_hermes": spec}) == (
        "requires hermes >=0.22.2, running 0.22.1"
    )


def test_lost_message_is_recreated_once_and_recorded(
    probe_env, caplog: pytest.LogCaptureFixture
) -> None:
    manager, ctx = _context()
    runtime = _load_plugin().register(ctx)
    assert runtime is not None
    runner = SimpleNamespace(adapters={})

    async def scenario() -> None:
        adapter, bot = _adapter(101, runner)
        _wire(adapter, manager)
        adapter._mark_connected()
        runner.adapters[Platform.TELEGRAM] = adapter
        # The plugin's record, not the Bot mock's count: the engine awaits the Bot call through a
        # deadline wrapper (_await_with_thread_deadline), so the count rises a few loop turns
        # before the edit returns to the plugin and its verdict is set.
        await _until(lambda: runtime.record.get("last_status") == "edited")
        assert bot.edit_message_text.await_count >= 1
        assert bot.send_message.await_count == 1  # message 101, sent in this test's profile
        assert runtime.html is True  # the first edit went out as HTML

        bot.edit_message_text.side_effect = BadRequest("Message to edit not found")
        bot.send_message.return_value = MagicMock(message_id=102)
        # The HTML edit fails: the message is gone. The plain edit_message the plugin falls back
        # to is held back by the engine's chat slot while the first send still holds it, and
        # answers success with raw_response {"skipped": True}: the plugin takes that as a
        # deferred edit, with no "HTML refused" verdict, and tries again on the next tick. When
        # the slot frees, the plain edit fires and confirms the loss, and the new send waits for
        # the slot that edit took (SLOT_WAIT).
        await _until(lambda: bot.send_message.await_count >= 2, timeout=SLOT_WAIT)
        await _until(lambda: runtime.record.get("message_id") == "102", timeout=SLOT_WAIT)

        assert bot.send_message.await_count == 2  # recreated once
        assert runtime.record["lost_at"] and runtime.record["recreated_at"]
        assert runtime.record["last_status"] == "created"
        # No false verdict on the form: a failed HTML edit and a plain one that never fired (or
        # failed) say nothing about HTML.
        assert runtime.html is True and runtime.record.get("html_error") is None
        assert not [r for r in caplog.records if "HTML edit refused" in r.getMessage()]
        manager.unload()

    asyncio.run(scenario())


def test_an_edit_the_engine_holds_back_for_a_busy_chat_is_deferred_and_the_next_tick_edits(
    probe_env, monkeypatch, caplog: pytest.LogCaptureFixture
) -> None:
    """The engine's shared per-chat slot (adapter.py, #116312): while a reply in the same chat
    holds it, an interim edit_message edits nothing and answers success with raw_response
    {"skipped": True}. Here Telegram refuses the HTML form, so the tick falls to that plain verb
    while the slot is held: the record says deferred, not edited, keeps its last confirmation,
    and no "HTML refused" verdict comes of it; the first tick the slot is free, the plain edit
    reaches the Bot and the verdict follows. The loop ticks once (the message is created), then
    sleeps an hour; the test drives the two ticks after it by hand."""
    monkeypatch.setenv("HERMES_DASHBOARD_PROBE_PERIOD", "3600")
    manager, ctx = _context()
    runtime = _load_plugin().register(ctx)
    assert runtime is not None
    runner = SimpleNamespace(adapters={})
    from telegram_dashboard.__main__ import run_check

    def refuse_html(**kwargs):
        if kwargs.get("parse_mode") is not None:
            raise BadRequest("Can't parse entities: unsupported start tag at byte offset 0")
        return True

    def plain_edits(bot: MagicMock) -> list:
        return [c for c in bot.edit_message_text.await_args_list if "parse_mode" not in c.kwargs]

    async def scenario() -> None:
        adapter, bot = _adapter(101, runner)
        _wire(adapter, manager)
        adapter._mark_connected()
        runner.adapters[Platform.TELEGRAM] = adapter
        await _until(lambda: runtime.record.get("last_status") == "created")
        created = runtime.record["last_confirmed_at"]
        bot.edit_message_text.side_effect = refuse_html

        # An agent's reply in the same chat holds the slot: the engine's own call, with its
        # tunable budget raised so that no slow tick outlives it.
        adapter._telegram_chat_outbound_slot_secs = 60.0
        adapter._hold_chat_outbound_slot(CHAT)
        await runtime.tick()

        assert bot.edit_message_text.await_count == 1  # the refused HTML edit, nothing else
        assert plain_edits(bot) == []  # the engine held the plain edit back
        record = ctx.state.get("probe")
        assert (record["last_status"], record["last_error"]) == ("deferred", None)
        assert record["last_confirmed_at"] == created
        assert runtime.html is None and "html_error" not in record
        assert record["screen_format"] == "plain"  # as the creation left it
        # Not a failure by itself: the watchdog judges by the last confirmation.
        assert run_check({"plugin_state_path": str(ctx.state.path), "period_seconds": 300}) == 0

        # The reply is out and the slot is free: the next tick's plain edit goes through.
        adapter._telegram_chat_outbound_slot_secs = 0.0
        adapter._hold_chat_outbound_slot(CHAT)
        await runtime.tick()

        assert [c.kwargs["message_id"] for c in plain_edits(bot)] == [101]
        record = ctx.state.get("probe")
        assert record["last_status"] == "edited" and record["last_confirmed_at"] != created
        assert (record["screen_format"], record["html_error"]) == ("plain", "BadRequest")
        manager.unload()

    with caplog.at_level("INFO", logger="hermes.plugins.telegram_dashboard_probe"):
        asyncio.run(scenario())

    said = [r.getMessage() for r in caplog.records if r.name.endswith("telegram_dashboard_probe")]
    assert sum("deferred" in line for line in said) == 1
    assert sum("HTML edit refused" in line for line in said) == 1  # once the plain edit landed


def test_the_sender_s_id_is_the_one_ptb_keeps_from_get_me(probe_env) -> None:
    """The record keeps the id of the bot that sent the message (``_bot_id``), read off the
    adapter's PTB bot the way PTB keeps it: ``Bot.id``, the user getMe answered at
    ``initialize()``, cached on the object, so no request goes out (conftest's guard would refuse
    one). A bot that has not started raises there, and the plugin reads no id. The PTB bot here
    is made without credentials: only its cached user is set."""
    from telegram import Bot, User

    plugin = _load_plugin()
    adapter, _ = _adapter(101, SimpleNamespace(adapters={}))
    bot = Bot.__new__(Bot)
    object.__setattr__(bot, "_bot_user", None)  # what Bot.__init__ leaves before initialize()
    adapter._bot = bot

    assert plugin._bot_id(adapter) is None
    object.__setattr__(bot, "_bot_user", User(id=4242, first_name="probe", is_bot=True))
    assert plugin._bot_id(adapter) == 4242
