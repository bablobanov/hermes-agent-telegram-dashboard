"""The widget's two paths to Telegram and the choice between them (``widget/public_path.py``,
``widget/private_path.py``, ``widget.WidgetMode``), on stand-ins: a fake ``telegram`` module for
PTB, a fake ``ctx.platform_actions`` for the engine's verbs. The real engine and PTB: in
test_widget_engine.py.
"""

from __future__ import annotations

import asyncio
import logging
import sys
import types
from datetime import UTC
from typing import Any

import pytest

from telegram_dashboard.widget import WidgetMode, mode, private_path, public_path
from telegram_dashboard.widget.mode import Refused, Tap, WidgetSettings
from telegram_dashboard.widget.view import View

CHAT = 777000111  # fixture user id, not a real one


@pytest.fixture
def fake_ptb(monkeypatch: pytest.MonkeyPatch) -> types.SimpleNamespace:
    """``telegram.Update.ALL_TYPES`` and ``telegram.ext.BaseHandler`` as PTB 22.8 has them, and a
    fresh process marker."""
    update = type("Update", (), {"ALL_TYPES": ["message", "callback_query"]})

    class BaseHandler:
        def __init__(self, callback: Any) -> None:
            self.callback = callback

    telegram = types.ModuleType("telegram")
    telegram.Update = update  # type: ignore[attr-defined]
    ext = types.ModuleType("telegram.ext")
    ext.BaseHandler = BaseHandler  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "telegram", telegram)
    monkeypatch.setitem(sys.modules, "telegram.ext", ext)
    monkeypatch.delitem(sys.modules, private_path.MARKER, raising=False)
    return types.SimpleNamespace(update=update)


class App:
    """The PTB ``Application`` the adapter hands the factory: its handlers and its bot."""

    def __init__(self) -> None:
        self.handlers: list[tuple[Any, int]] = []
        self.running = True
        self.bot = Bot()

    def add_handler(self, handler: Any, group: int = 0) -> None:
        self.handlers.append((handler, group))


class Bot:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def do_api_request(self, method: str, api_kwargs: dict[str, Any]) -> Any:
        self.calls.append((method, api_kwargs))
        return {"message_id": 321} if method == "sendWidget" else True

    async def pin_chat_message(self, **params: Any) -> bool:
        self.calls.append(("pin_chat_message", params))
        return True

    async def unpin_chat_message(self, **params: Any) -> bool:
        self.calls.append(("unpin_chat_message", params))
        return True


class Actions:
    """``ctx.platform_actions`` of an engine with the widget verbs (NousResearch/hermes-agent#152186):
    structured results, never a raise; ``refuse`` stands for a capability not granted."""

    def __init__(self, refuse: bool = False) -> None:
        self.calls: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = []
        self.refuse = refuse
        self.fail: dict[str, dict[str, Any]] = {}

    def _result(self, verb: str, args: tuple[Any, ...], kwargs: dict[str, Any], **ok: Any) -> Any:
        self.calls.append((verb, args, kwargs))
        if self.refuse:
            return {"ok": False, "error": "capability_not_granted", "detail": "lacks it"}
        return self.fail.get(verb) or {"ok": True, "action": verb, **ok}

    async def send_widget(self, *args: Any, **kwargs: Any) -> Any:
        return self._result("send_widget", args, kwargs, message_id="654")

    async def edit_widget_data(self, *args: Any, **kwargs: Any) -> Any:
        return self._result("edit_widget_data", args, kwargs, modified=True)

    async def answer_widget_query(self, *args: Any, **kwargs: Any) -> Any:
        return self._result("answer_widget_query", args, kwargs)


class Ctx:
    def __init__(self, actions: Any = None) -> None:
        self.state = None
        self.hooks: list[tuple[str, Any]] = []
        if actions is not None:
            self.platform_actions = actions

    def register_hook(self, name: str, callback: Any) -> None:
        self.hooks.append((name, callback))


def run(coro: Any) -> Any:
    return asyncio.run(coro)


# ----------------------------------------------------------------------------- the private path


def test_the_update_type_is_added_once_and_taken_out(fake_ptb: types.SimpleNamespace) -> None:
    kinds = fake_ptb.update.ALL_TYPES
    assert private_path.extend_update_types() is True
    assert private_path.extend_update_types() is False  # already there
    assert kinds.count("widget_query") == 1
    private_path.drop_update_type()
    assert "widget_query" not in kinds


def test_without_ptb_nothing_is_touched(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sys.modules, "telegram", None)
    assert private_path.extend_update_types() is False
    private_path.drop_update_type()


def test_a_query_is_read_from_api_kwargs_or_a_native_object() -> None:
    query = {
        "id": "7",
        "from": {"id": CHAT},
        "chat": {"id": CHAT},
        "message_id": 5,
        "data": "refresh",
    }
    raw = types.SimpleNamespace(api_kwargs={"widget_query": query})
    native = types.SimpleNamespace(
        api_kwargs={}, widget_query=types.SimpleNamespace(to_dict=lambda: query)
    )
    assert private_path.widget_query(raw) == query
    assert private_path.widget_query(native) == query
    assert private_path.widget_query(types.SimpleNamespace(api_kwargs={"message": {}})) is None
    assert private_path.tap_of(query) == Tap("7", CHAT, CHAT, 5, "refresh")


def test_a_query_without_a_sender_is_nobodys_tap() -> None:
    tap = private_path.tap_of({"id": "7", "chat": {"id": CHAT}, "message_id": 5, "data": "refresh"})
    assert tap is not None and tap.sender_id == -1
    assert private_path.tap_of({"message_id": 5}) is None
    assert (
        private_path.tap_of({"id": "7", "from": {"id": CHAT}, "message_id": True}).message_id
        is None
    )


def test_the_factory_adds_the_handler_once_per_application(fake_ptb: types.SimpleNamespace) -> None:
    first, second = App(), App()
    private_path.wire(first)
    private_path.wire(first)
    private_path.wire(second)  # a reconnect hands a new application
    assert len(first.handlers) == 1 and first.handlers[0][1] == private_path.HANDLER_GROUP
    assert len(second.handlers) == 1
    assert private_path.marker()["app"] is second
    assert "widget_query" in fake_ptb.update.ALL_TYPES


def test_the_handler_matches_only_a_widget_query_and_calls_the_newest_entry(
    fake_ptb: types.SimpleNamespace,
) -> None:
    app = App()
    private_path.wire(app)
    handler = app.handlers[0][0]
    query = {
        "id": "7",
        "from": {"id": CHAT},
        "chat": {"id": CHAT},
        "message_id": 5,
        "data": "details",
    }
    update = types.SimpleNamespace(api_kwargs={"widget_query": query})
    assert handler.check_update(update) is True
    assert handler.check_update(types.SimpleNamespace(api_kwargs={})) is False
    got: list[tuple[str, Tap]] = []

    async def old(tap: Tap) -> None:
        got.append(("old", tap))

    async def new(tap: Tap) -> None:
        got.append(("new", tap))

    private_path.start(old)
    private_path.start(new)  # a reload: the newest registration's entry point
    run(handler.callback(update, None))
    assert got == [("new", Tap("7", CHAT, CHAT, 5, "details"))]


def test_an_unload_takes_off_only_its_own_entry_and_the_type(
    fake_ptb: types.SimpleNamespace,
) -> None:
    async def old(tap: Tap) -> None:
        return None

    async def new(tap: Tap) -> None:
        return None

    private_path.wire(App())
    private_path.start(new)
    private_path.stop(old)  # an earlier registration's unload, late
    assert private_path.marker()["entry"] is new
    assert "widget_query" not in fake_ptb.update.ALL_TYPES
    private_path.start(new)
    assert "widget_query" in fake_ptb.update.ALL_TYPES
    private_path.stop(new)
    assert private_path.marker()["entry"] is None


def test_the_private_calls_go_through_the_applications_bot(fake_ptb: types.SimpleNamespace) -> None:
    app = App()
    private_path.wire(app)
    transport = private_path.PrivateTransport()
    message = run(transport.send(CHAT, "<b/>", chat_data="{}", public_data="[]", caption="c"))
    run(transport.edit(CHAT, message, chat_data="{}", public_data=None))
    run(transport.edit(CHAT, message, chat_data="{}", public_data="[1]"))
    run(transport.answer("q1", '{"n":1}'))
    assert run(transport.pin(CHAT, message)) is True
    run(transport.unpin(CHAT, 100))
    names = [name for name, _ in app.bot.calls]
    assert names == [
        "sendWidget",
        "editMessageWidgetData",
        "editMessageWidgetData",
        "answerWidgetQuery",
        "pin_chat_message",
        "unpin_chat_message",
    ]
    sent = app.bot.calls[0][1]
    assert sent["allow_queries"] is True and sent["disable_notification"] is True
    assert "public_data" not in app.bot.calls[1][1] and app.bot.calls[2][1]["public_data"] == "[1]"
    assert app.bot.calls[3][1] == {"widget_query_id": "q1", "data": '{"n":1}'}
    assert message == 321


def test_the_private_path_waits_for_a_running_application(fake_ptb: types.SimpleNamespace) -> None:
    assert private_path.ready() is False
    app = App()
    app.running = False
    private_path.wire(app)
    assert private_path.ready() is False
    app.running = True
    assert private_path.ready() is True
    with pytest.raises(ConnectionError):
        private_path.PrivateTransport(lambda: None)._bot()


# ----------------------------------------------------------------------------- the public path


def test_the_public_path_needs_all_three_verbs() -> None:
    assert public_path.actions_of(Ctx(Actions())) is not None
    partial = types.SimpleNamespace(send_widget=Actions().send_widget, add_reaction=lambda: None)
    assert public_path.actions_of(Ctx(partial)) is None
    assert public_path.actions_of(Ctx()) is None

    class Raising:
        @property
        def platform_actions(self) -> Any:
            raise RuntimeError("no facade")

    assert public_path.actions_of(Raising()) is None


def test_the_public_calls_go_through_the_verbs_with_text_ids() -> None:
    actions = Actions()
    transport = public_path.PublicTransport(actions)
    message = run(transport.send(CHAT, "<b/>", chat_data="{}", public_data="[]", caption="c"))
    run(transport.edit(CHAT, message, chat_data="{}", public_data=None))
    run(transport.answer("q1", "{}"))
    assert run(transport.pin(CHAT, message)) is False  # the verbs have no pin
    assert message == 654
    verb, args, kwargs = actions.calls[0]
    assert verb == "send_widget" and args == ("telegram", str(CHAT), "<b/>")
    assert kwargs["allow_queries"] is True and kwargs["disable_notification"] is True
    assert actions.calls[1][1] == ("telegram", str(CHAT), "654")
    assert actions.calls[2][1:] == (("telegram", "q1"), {"data": "{}"})


def test_a_refused_capability_is_refused_and_says_what_to_grant() -> None:
    transport = public_path.PublicTransport(Actions(refuse=True))
    with pytest.raises(Refused, match="allow_platform_actions"):
        run(transport.send(CHAT, "<b/>", chat_data="{}", public_data="[]", caption="c"))


def test_a_verbs_failure_keeps_telegrams_words_for_outcome() -> None:
    actions = Actions()
    actions.fail["edit_widget_data"] = {
        "ok": False,
        "error": "action_failed",
        "detail": "Message to edit not found",
    }
    transport = public_path.PublicTransport(actions)
    with pytest.raises(public_path.ActionFailed) as caught:
        run(transport.edit(CHAT, 5, chat_data="{}", public_data=None))
    assert mode.outcome(caught.value) == "gone"
    actions.fail["edit_widget_data"] = {"ok": False, "error": "adapter_disconnected"}
    with pytest.raises(public_path.ActionFailed) as caught:
        run(transport.edit(CHAT, 5, chat_data="{}", public_data=None))
    assert mode.outcome(caught.value) == "failed"


def test_the_event_payload_becomes_a_tap() -> None:
    payload = {
        "query_id": "9",
        "chat_id": str(CHAT),
        "message_id": "41",
        "data": "refresh",
        "thread_id": None,
    }
    assert public_path.tap_of(payload) == Tap("9", CHAT, None, 41, "refresh")
    assert public_path.tap_of({"chat_id": "1"}) is None
    assert public_path.tap_of("not a mapping") is None
    assert public_path.tap_of({**payload, "message_id": "x"}).message_id is None


def test_the_hook_hands_a_widget_query_to_the_gateway_loop_and_nothing_else() -> None:
    async def scenario() -> list[Tap]:
        got: list[Tap] = []

        async def entry(tap: Tap) -> None:
            got.append(tap)

        loop = asyncio.get_running_loop()
        hook = public_path.Hook(lambda: entry, lambda: loop)
        payload = {"query_id": "9", "chat_id": str(CHAT), "message_id": "41", "data": "details"}
        await asyncio.to_thread(hook, "telegram", "widget_query", payload, telemetry=1)
        hook("telegram", "reaction", payload)
        hook("discord", "widget_query", payload)
        hook("telegram", "widget_query", {"chat_id": "1"})
        public_path.Hook(lambda: None, lambda: loop)("telegram", "widget_query", payload)
        await asyncio.sleep(0.05)
        return got

    assert run(scenario()) == [Tap("9", CHAT, None, 41, "details")]


# ----------------------------------------------------------------------------- the choice


def widget_mode(ctx: Ctx) -> WidgetMode:
    async def collect() -> View:
        return View("normal", "Healthy")

    return WidgetMode(
        ctx, WidgetSettings(CHAT), collect=collect, zone=lambda: UTC, version="1.2.0b1"
    )


def test_an_engine_with_the_verbs_takes_the_public_path_and_never_the_private(
    fake_ptb: types.SimpleNamespace, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO)
    ctx = Ctx(Actions())
    chosen = widget_mode(ctx)
    assert chosen.path == "public"
    assert isinstance(chosen.widget.transport, public_path.PublicTransport)
    assert [name for name, _ in ctx.hooks] == ["gateway_platform_event"]
    app = App()
    chosen.wire(app)
    assert app.handlers == [] and "widget_query" not in fake_ptb.update.ALL_TYPES
    assert private_path.MARKER not in sys.modules
    assert "widget: path public, chat" in caplog.text


def test_an_engine_without_the_verbs_takes_the_private_path(
    fake_ptb: types.SimpleNamespace, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO)
    ctx = Ctx()
    chosen = widget_mode(ctx)
    assert chosen.path == "private" and ctx.hooks == []
    assert isinstance(chosen.widget.transport, private_path.PrivateTransport)
    app = App()
    chosen.wire(app)
    assert len(app.handlers) == 1 and "widget_query" in fake_ptb.update.ALL_TYPES
    assert "widget: path private (no platform actions)" in caplog.text
    chosen.retire()
    assert "widget_query" not in fake_ptb.update.ALL_TYPES
    chosen.wire(App())
    assert "widget_query" not in fake_ptb.update.ALL_TYPES  # a retired registration wires nothing


def test_a_retired_registration_answers_no_taps(fake_ptb: types.SimpleNamespace) -> None:
    ctx = Ctx(Actions())
    chosen = widget_mode(ctx)
    hook = ctx.hooks[0][1]
    assert hook._entry() is not None
    chosen.retire()
    assert hook._entry() is None


def test_the_loop_runs_on_the_gateway_loop_and_takes_the_taps_there(
    fake_ptb: types.SimpleNamespace,
) -> None:
    async def scenario() -> list[str]:
        actions = Actions()
        ctx = Ctx(actions)
        chosen = widget_mode(ctx)
        rounds = iter([True, False])

        async def no_wait(seconds: float) -> None:
            return None

        chosen.widget._sleep = no_wait
        await chosen.run(lambda: next(rounds), lambda: True)
        assert chosen.loop is asyncio.get_running_loop()
        hook = ctx.hooks[0][1]
        payload = {"query_id": "9", "chat_id": str(CHAT), "message_id": "654", "data": "details"}
        await asyncio.to_thread(hook, "telegram", "widget_query", payload)
        await asyncio.sleep(0.05)
        return [verb for verb, _, _ in actions.calls]

    assert run(scenario()) == ["send_widget", "answer_widget_query"]


def test_a_hook_the_engine_refuses_leaves_the_ticks(caplog: pytest.LogCaptureFixture) -> None:
    class Refusing(Ctx):
        def register_hook(self, name: str, callback: Any) -> None:
            raise ValueError("unknown hook")

    chosen = widget_mode(Refusing(Actions()))
    assert chosen.path == "public"
    assert "tap hook not registered: ValueError" in caplog.text


def test_a_refused_handler_never_stops_the_factory(
    fake_ptb: types.SimpleNamespace, caplog: pytest.LogCaptureFixture
) -> None:
    def refuse(callback: Any) -> Any:
        raise RuntimeError("no telegram.ext here")

    app = App()
    private_path.wire(app, handler=refuse)
    assert app.handlers == [] and private_path.marker()["handled"] is None
    assert "tap handler not added: RuntimeError" in caplog.text
    private_path.wire(app)  # the next factory call tries again
    assert len(app.handlers) == 1
