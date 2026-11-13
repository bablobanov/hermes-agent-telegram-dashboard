"""The widget mode against the real engine and python-telegram-bot (skipped where they are not).

- the private path as the gateway runs it: the engine's ``TelegramApplication`` with its per-chat
  update processor, the plugin's factory before the core handlers (``connect()``), a catch-all
  core handler in group 0 and an observer in group 99, PTB's own ``Updater.start_polling`` with
  ``Update.ALL_TYPES``: a ``widget_query`` from ``getUpdates`` reaches the widget and is answered
  with ``answerWidgetQuery``, and the core handlers still see every update
- the choice of the path on the engine's own ``PlatformActions``: the public path exactly where
  the engine has the widget verbs (NousResearch/hermes-agent#152186), else the private one
- the public path on an engine with the verbs: the event the engine builds from a tap
  (``telegram_widgets.normalize_event``) reaches the widget through the hook, the verbs reach the
  Bot API methods, and a capability not granted stops the mode with what to grant

No network: PTB's requests go to a stub; conftest refuses anything off loopback.
"""

from __future__ import annotations

import asyncio
import contextlib
import importlib
import importlib.util
import inspect
import json
import sys
import time
import types
from datetime import UTC
from pathlib import Path
from typing import Any

import pytest

from telegram_dashboard.widget import WidgetMode, private_path, public_path
from telegram_dashboard.widget.mode import WidgetSettings
from telegram_dashboard.widget.view import View

OWNER = 777000111  # fixture user id, not a real one
NO_VERBS = "the engine has no widget verbs (NousResearch/hermes-agent#152186)"


def _present(name: str) -> bool:
    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, ValueError):
        return False


ENGINE = _present("hermes_cli") and _present("telegram")
pytestmark = pytest.mark.skipif(not ENGINE, reason="Hermes engine not importable here")


@pytest.fixture(autouse=True)
def fresh_marker() -> Any:
    sys.modules.pop(private_path.MARKER, None)
    yield
    private_path.stop()
    sys.modules.pop(private_path.MARKER, None)


class State:
    def __init__(self, data: dict[str, Any] | None = None) -> None:
        self.data = dict(data or {})

    def get(self, key: str, default: Any = None) -> Any:
        return self.data.get(key, default)

    def set(self, key: str, value: Any) -> None:
        self.data[key] = json.loads(json.dumps(value))


class Ctx:
    def __init__(self, actions: Any = None) -> None:
        self.state = State()
        self.hooks: list[tuple[str, Any]] = []
        if actions is not None:
            self.platform_actions = actions

    def register_hook(self, name: str, callback: Any) -> None:
        self.hooks.append((name, callback))


def widget_mode(ctx: Ctx) -> WidgetMode:
    async def collect() -> View:
        return View("normal", "Healthy", details=(("telegram", ("Telegram connected",)),))

    return WidgetMode(
        ctx, WidgetSettings(OWNER), collect=collect, zone=lambda: UTC, version="1.2.0b1"
    )


def query(message_id: int, data: str = "details") -> dict[str, Any]:
    return {
        "id": "q1",
        "from": {"id": OWNER, "is_bot": False, "first_name": "Owner"},
        "chat": {"id": OWNER, "type": "private"},
        "message_id": message_id,
        "data": data,
    }


# ----------------------------------------------------------------------------- the private path


def _stub_request() -> Any:
    from telegram.request import BaseRequest

    class StubRequest(BaseRequest):  # type: ignore[misc]
        """No network: answers the methods the test needs and records the rest; the updates come
        on the first ``getUpdates`` only."""

        def __init__(self) -> None:
            self.calls: list[tuple[str, dict[str, Any]]] = []

        @property
        def read_timeout(self) -> None:
            return None

        async def initialize(self) -> None:
            return None

        async def shutdown(self) -> None:
            return None

        async def do_request(
            self, url: str, method: str, request_data: Any = None, **_: Any
        ) -> tuple[int, bytes]:
            endpoint = url.rsplit("/", 1)[-1]
            params = dict(request_data.parameters) if request_data is not None else {}
            self.calls.append((endpoint, params))
            result: Any = True
            if endpoint == "getMe":
                result = {"id": 1, "is_bot": True, "first_name": "bot", "username": "bot"}
            elif endpoint == "getUpdates":
                first = [e for e, _ in self.calls].count("getUpdates") == 1
                text = {
                    "message_id": 9,
                    "date": 0,
                    "text": "hi",
                    "chat": {"id": OWNER, "type": "private"},
                }
                result = (
                    [{"update_id": 7, "widget_query": query(42)}, {"update_id": 8, "message": text}]
                    if first
                    else []
                )
            return 200, json.dumps({"ok": True, "result": result}).encode("utf-8")

    return StubRequest()


def _processor(admission: Any, adapter: Any) -> Any:
    """The per-chat processor as the installed engine's adapter builds it: from the adapter itself
    on 0.23.1+, from ``extra`` and the name before that."""
    if len(inspect.signature(admission.build_update_processor).parameters) == 1:
        return admission.build_update_processor(adapter)
    return admission.build_update_processor(adapter.config.extra, adapter.name)


def test_a_tap_passes_the_engines_admission_and_real_polling(tmp_path: Path) -> None:
    from telegram import Update
    from telegram.ext import Application, TypeHandler

    admission = importlib.import_module("plugins.platforms.telegram.update_admission")
    # The engine keeps receipts of handled updates on disk and drops a redelivered one: a receipt
    # directory of the test's own, never HERMES_HOME.
    adapter = types.SimpleNamespace(
        _update_admission=None,
        _updates_dispatched_total=0,
        _seen_update_ids={},
        _inflight_update_ids={},
        _update_receipts_loaded=set(),
        _update_receipt_dir=tmp_path / "receipts",
        _update_receipts_dirty=set(),
        _update_receipt_flush=None,
        name="Telegram",
        config=types.SimpleNamespace(extra={}),
        _admit_topic_follow=lambda message: None,
    )
    stub = _stub_request()
    builder = Application.builder().token("1:x").request(stub).get_updates_request(stub)
    builder.application_class(admission.TelegramApplication, {"adapter": adapter})
    builder.concurrent_updates(_processor(admission, adapter))
    app = builder.build()
    ctx = Ctx()
    chosen = widget_mode(ctx)
    assert chosen.path == "private"
    ctx.state.set("widget", {"chat_id": OWNER, "message_id": 42, "key": chosen.widget.key})
    core: list[int] = []
    observed: list[int] = []
    kinds_before = list(Update.ALL_TYPES)

    async def note(target: list[int], update: Any) -> None:
        target.append(update.update_id)

    async def scenario() -> None:
        chosen.wire(app)  # connect(): the plugin's handlers first, then the core ones
        private_path.start(chosen.widget.on_tap)
        await chosen.widget.collection()  # the screen the Details answer comes from
        app.add_handler(TypeHandler(Update, lambda u, c: note(core, u)), group=0)
        app.add_handler(TypeHandler(Update, lambda u, c: note(observed, u)), group=99)
        await app.initialize()
        await app.start()
        await app.updater.start_polling(allowed_updates=Update.ALL_TYPES, poll_interval=0.01)
        deadline = time.monotonic() + 20  # a cold first run compiles the engine's modules
        while time.monotonic() < deadline:
            done = "answerWidgetQuery" in [e for e, _ in stub.calls] and len(observed) == 2
            if done:
                break
            await asyncio.sleep(0.01)
        await app.updater.stop()
        await app.stop()
        chosen.retire()
        await app.shutdown()

    asyncio.run(scenario())
    trail = f"endpoints={[e for e, _ in stub.calls if e != 'getUpdates']} core={core} observed={observed}"
    polled = [params for endpoint, params in stub.calls if endpoint == "getUpdates"]
    assert "widget_query" in polled[0]["allowed_updates"], trail
    answers = [params for endpoint, params in stub.calls if endpoint == "answerWidgetQuery"]
    assert [a["widget_query_id"] for a in answers] == ["q1"], trail
    assert json.loads(answers[0]["data"])["d"][0][0] == "Telegram"
    assert (sorted(core), sorted(observed)) == ([7, 8], [7, 8])
    assert list(Update.ALL_TYPES) == kinds_before  # the unload took the type out again


def test_ptb_does_not_know_widget_query_yet() -> None:
    """When it does, the private path adds nothing (``extend_update_types`` answers False) and a
    native ``widget_query`` object is read through ``to_dict()``: this test then says to look."""
    from telegram import Update

    assert isinstance(Update.ALL_TYPES, list)
    assert "widget_query" not in Update.ALL_TYPES, "PTB knows widget_query now: review private_path"


# ----------------------------------------------------------------------------- the choice


def _actions_class() -> Any:
    return importlib.import_module("hermes_cli.platform_actions").PlatformActions


def _has_verbs() -> bool:
    return all(callable(getattr(_actions_class(), verb, None)) for verb in public_path.VERBS)


def test_the_path_is_the_engines_own() -> None:
    ctx = Ctx(_actions_class()("telegram_dashboard_probe"))
    chosen = widget_mode(ctx)
    assert chosen.path == ("public" if _has_verbs() else "private")
    assert bool(ctx.hooks) == _has_verbs()


# ----------------------------------------------------------------------------- the public path


def _widget_adapter(bot: Any) -> Any:
    """The engine's Telegram adapter as far as the widget verbs reach it: its own methods, bound
    to a stand-in that has the bot and the topic memory they use."""
    adapter_module = importlib.import_module("plugins.platforms.telegram.adapter")
    widgets = importlib.import_module("plugins.platforms.telegram.telegram_widgets")
    platform = importlib.import_module("gateway.config").Platform.TELEGRAM
    stand_in = types.SimpleNamespace(
        platform=platform, _bot=bot, _widget_threads=widgets.WidgetThreads()
    )
    for verb in public_path.VERBS:
        bound = getattr(adapter_module.TelegramAdapter, verb).__get__(stand_in)
        setattr(stand_in, verb, bound)
    return stand_in


class Bot:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def do_api_request(self, method: str, api_kwargs: dict[str, Any]) -> Any:
        self.calls.append((method, api_kwargs))
        return {"message_id": 88, "chat": {"id": OWNER}} if method == "sendWidget" else True


def test_the_public_path_goes_through_the_engines_verbs_and_event(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    if not _has_verbs():
        pytest.skip(NO_VERBS)
    widgets = importlib.import_module("plugins.platforms.telegram.telegram_widgets")
    actions_class = _actions_class()
    bot = Bot()
    adapter = _widget_adapter(bot)
    monkeypatch.setattr(actions_class, "_capability_granted", lambda self: True)
    monkeypatch.setattr(actions_class, "_resolve_adapter", lambda self, platform: (adapter, None))
    ctx = Ctx(actions_class("telegram_dashboard_probe"))
    chosen = widget_mode(ctx)
    assert chosen.path == "public"
    hook = ctx.hooks[0][1]

    async def scenario() -> None:
        asleep = asyncio.Event()

        async def nap(seconds: float) -> None:  # after the first tick, till the test ends it
            asleep.set()
            await asyncio.Event().wait()

        chosen.widget._sleep = nap
        loop = asyncio.ensure_future(chosen.run(lambda: True, lambda: True))
        await asleep.wait()  # taps reach the widget while its loop runs
        event = widgets.normalize_event(query(88))
        await asyncio.to_thread(hook, **event)
        deadline = time.monotonic() + 5
        while "answerWidgetQuery" not in [m for m, _ in bot.calls] and time.monotonic() < deadline:
            await asyncio.sleep(0.01)
        loop.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await loop

    asyncio.run(scenario())
    methods = [method for method, _ in bot.calls]
    assert methods == ["sendWidget", "answerWidgetQuery"], methods
    sent = bot.calls[0][1]
    assert sent["chat_id"] == OWNER and sent["allow_queries"] is True
    assert json.loads(bot.calls[1][1]["data"])["d"][0][0] == "Telegram"
    assert ctx.state.data["widget"]["message_id"] == 88


def test_a_capability_not_granted_stops_the_public_path_with_what_to_grant(
    caplog: pytest.LogCaptureFixture,
) -> None:
    if not _has_verbs():
        pytest.skip(NO_VERBS)
    ctx = Ctx(_actions_class()("telegram_dashboard_probe"))  # conftest's home grants nothing
    chosen = widget_mode(ctx)
    asyncio.run(chosen.run(lambda: True, lambda: True))  # returns: the engine refused
    assert chosen.widget.stopped
    assert "allow_platform_actions" in caplog.text
