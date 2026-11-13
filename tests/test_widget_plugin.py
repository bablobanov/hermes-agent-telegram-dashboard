"""The widget mode inside the plugin (1.2.0-beta.1), on fakes: the setting turns it on, its loop is
a second task of the engine beside the topic's tick, a takeover (issue #12) ends the earlier
registration's widget loop too, the unload retires it, and its collections have their own
workers and caches with the limits asked at most every 15 minutes."""

from __future__ import annotations

import asyncio
import os
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from probe_fakes import CHAT, FakeAdapter, FakeContext, load_plugin, until

from telegram_dashboard import states
from telegram_dashboard.widget import private_path

LIVE_MODULE = "_telegram_dashboard_probe_live"
TASK_NAME = "telegram_dashboard_probe:tick"
WIDGET_TASK_NAME = "telegram_dashboard_probe:widget"
OWNER = 777000111  # fixture user id, not a real one


@pytest.fixture(autouse=True)
def fresh(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[None]:
    for name in [name for name in os.environ if name.startswith("HERMES_DASHBOARD_PROBE_")]:
        monkeypatch.delenv(name)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    for module in (LIVE_MODULE, private_path.MARKER):
        sys.modules.pop(module, None)
    try:
        yield
    finally:
        for module in (LIVE_MODULE, private_path.MARKER):
            sys.modules.pop(module, None)


class Bot:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def do_api_request(self, method: str, api_kwargs: dict[str, Any]) -> Any:
        self.calls.append((method, api_kwargs))
        return {"message_id": 4242} if method == "sendWidget" else True

    async def pin_chat_message(self, **params: Any) -> bool:
        self.calls.append(("pin_chat_message", params))
        return True


class App:
    """The PTB application the adapter hands the factory."""

    def __init__(self) -> None:
        self.bot = Bot()
        self.running = True
        self.handlers: list[Any] = []

    def add_handler(self, handler: Any, group: int = 0) -> None:
        self.handlers.append((handler, group))


def settings(tmp_path: Path, **widget: Any) -> dict[str, Any]:
    found = {
        "chat_id": CHAT,
        "period_seconds": 0.01,
        "hermes_home": str(tmp_path),
        "limits_enabled": False,
    }
    if widget:
        found["widget"] = widget
    return found


def tasks(name: str) -> list[asyncio.Task[Any]]:
    return [task for task in asyncio.all_tasks() if not task.done() and task.get_name() == name]


def test_the_widget_is_off_unless_its_setting_turns_it_on(tmp_path: Path) -> None:
    plugin = load_plugin()
    runtime = plugin.register(FakeContext(settings(tmp_path)))
    assert runtime is not None and runtime.settings.widget is None and runtime.widget is None

    async def scenario() -> None:
        runtime.wire(App(), FakeAdapter())
        await asyncio.sleep(0.02)
        assert len(tasks(TASK_NAME)) == 1 and tasks(WIDGET_TASK_NAME) == []
        runtime.task.cancel()

    asyncio.run(scenario())


def test_a_bad_widget_setting_leaves_the_topic_dashboard_running(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    plugin = load_plugin()
    ctx = FakeContext({**settings(tmp_path), "widget": "yes"})
    runtime = plugin.register(ctx)
    assert runtime is not None and runtime.widget is None
    assert "settings.widget must be a mapping" in caplog.text
    assert ctx.factories and ctx.factories[0][0] == "telegram"


def test_the_widget_runs_as_a_second_task_and_sends_through_the_handed_application(
    tmp_path: Path,
) -> None:
    plugin = load_plugin()
    ctx = FakeContext(settings(tmp_path, enabled=True, chat_id=str(OWNER), period_seconds=60))
    runtime = plugin.register(ctx)
    assert runtime is not None and runtime.widget is not None
    assert runtime.widget.path == "private"  # the fake context has no platform actions
    app = App()

    async def scenario() -> None:
        runtime.wire(app, FakeAdapter())
        await until(
            lambda: [name for name, _ in app.bot.calls][:2] == ["sendWidget", "pin_chat_message"]
        )
        assert len(tasks(TASK_NAME)) == 1 and len(tasks(WIDGET_TASK_NAME)) == 1
        sent = app.bot.calls[0][1]
        assert sent["chat_id"] == OWNER and sent["caption"].startswith("Hermes dashboard ")
        assert ctx.state.data["widget"]["message_id"] == 4242
        assert "probe" in ctx.state.data  # the topic's record beside it, untouched
        runtime.task.cancel()
        runtime.widget_task.cancel()

    asyncio.run(scenario())


def test_a_takeover_ends_the_earlier_registrations_widget_loop(tmp_path: Path) -> None:
    plugin = load_plugin()
    ctx = FakeContext(settings(tmp_path, enabled=True, chat_id=OWNER))
    first = plugin.register(ctx)
    app = App()

    async def scenario() -> None:
        first.wire(app, FakeAdapter())
        # The earlier widget sent its message and sleeps its period: only a cancel ends it now.
        await until(lambda: "pin_chat_message" in [name for name, _ in app.bot.calls])
        entry = sys.modules[LIVE_MODULE].profiles[first.key]
        assert entry["widget_task"] is first.widget_task
        second = plugin.register(ctx)  # no unload in between: the takeover alone
        await until(lambda: second.widget_task is not None and first.widget_task.done())
        assert first.widget_task.cancelled()  # by the takeover, not by its own next round
        assert tasks(WIDGET_TASK_NAME) == [second.widget_task]
        await until(lambda: first.task.done())  # the topic's loop, cancelled mid-collection
        assert tasks(TASK_NAME) == [second.task]
        second.task.cancel()
        second.widget_task.cancel()

    asyncio.run(scenario())


def test_a_widget_loop_the_engine_refuses_leaves_the_topic_and_the_registry(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    plugin = load_plugin()

    class Refusing(FakeContext):
        def spawn_task(self, coro: Any, name: str | None = None) -> asyncio.Task[None]:
            if name == WIDGET_TASK_NAME:
                raise RuntimeError("no more tasks")
            return super().spawn_task(coro, name)

    ctx = Refusing(settings(tmp_path, enabled=True, chat_id=OWNER))
    runtime = plugin.register(ctx)

    async def scenario() -> None:
        runtime.wire(App(), FakeAdapter())
        await asyncio.sleep(0.02)
        assert runtime.task is not None and runtime.widget_task is None
        assert sys.modules[LIVE_MODULE].profiles[runtime.key]["task"] is runtime.task
        runtime.task.cancel()

    asyncio.run(scenario())
    assert "widget loop not started: RuntimeError" in caplog.text


def test_the_unload_retires_the_widget(tmp_path: Path) -> None:
    plugin = load_plugin()
    runtime = plugin.register(FakeContext(settings(tmp_path, enabled=True, chat_id=OWNER)))
    runtime.retire()
    assert runtime.widget.retired is True
    assert runtime._still_serves() is False


def test_the_widget_collects_with_its_own_workers_and_caches_and_the_limits_floor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plugin = load_plugin()
    runtime = plugin.register(FakeContext(settings(tmp_path, enabled=True, chat_id=OWNER)))
    state = next(found for found in states.all_states() if found.number == 14)
    seen: list[dict[str, Any]] = []

    async def collect_all_async(env: Any, runner: Any, **kwargs: Any) -> Any:
        seen.append(kwargs)
        return state.snapshot

    monkeypatch.setattr(runtime.dashboard.collect, "collect_all_async", collect_all_async)
    screen = asyncio.run(runtime.widget_view())
    asyncio.run(runtime.widget_view())
    first, second = seen
    assert first["flights"] is runtime.widget_flights is not runtime.flights
    assert first["facade_interval_seconds"] == plugin.WIDGET_REFRESH_FLOOR_SECONDS
    assert first["external_interval_seconds"] == plugin.WIDGET_REFRESH_FLOOR_SECONDS
    assert first["period_seconds"] == 300.0
    arguments = {
        "version": "version_cache",
        "facade": "facade_caches",
        "external": "external_caches",
        "gemini": "gemini_cache",
        "cron": "cron_cache",
        "traffic": "traffic_cache",
    }
    for name, argument in arguments.items():
        assert first[argument] is runtime.widget_caches[name]  # in memory, not the topic's record
    assert second["facade_caches"] is first["facade_caches"]  # held across ticks
    assert screen.headline and screen.cron is not None


def test_a_longer_limits_interval_in_the_settings_is_kept(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plugin = load_plugin()
    ctx = FakeContext(
        {**settings(tmp_path, enabled=True, chat_id=OWNER), "limits_refresh_seconds": 3600}
    )
    runtime = plugin.register(ctx)
    state = next(found for found in states.all_states() if found.number == 1)
    seen: list[dict[str, Any]] = []

    async def collect_all_async(env: Any, runner: Any, **kwargs: Any) -> Any:
        seen.append(kwargs)
        return state.snapshot

    monkeypatch.setattr(runtime.dashboard.collect, "collect_all_async", collect_all_async)
    asyncio.run(runtime.widget_view())
    assert seen[0]["external_interval_seconds"] == 3600
    shorter = plugin.register(
        FakeContext(
            {
                **settings(tmp_path, enabled=True, chat_id=OWNER),
                "limits_refresh_seconds": 120,
                "facade_refresh_seconds": 120,
            }
        )
    )
    monkeypatch.setattr(shorter.dashboard.collect, "collect_all_async", collect_all_async)
    asyncio.run(shorter.widget_view())
    assert seen[1]["external_interval_seconds"] == plugin.WIDGET_REFRESH_FLOOR_SECONDS
    assert seen[1]["facade_interval_seconds"] == plugin.WIDGET_REFRESH_FLOOR_SECONDS
