"""The fallback path to Telegram: the gateway's own python-telegram-bot client, for an engine
without the public widget verbs (NousResearch/hermes-agent#152186).

Used only when ``ctx.platform_actions`` lacks them; on the public path nothing here runs. A beta
outside the catalog keeps it (the owner's decision of 11.11); the catalog's ``v1.2.0`` drops this
module with its tests once the engine has the verbs.

- the calls: the PTB ``Application`` the adapter hands the plugin's factory (``native``):
  ``do_api_request`` for the Bot API 10.4 methods PTB 22.8 does not have, its own methods for the
  pin, so the bot token never leaves the gateway process
- the updates: the adapter polls with ``allowed_updates=Update.ALL_TYPES``, a PTB list without
  ``widget_query``, and Telegram sends only the listed types. The factory, which the adapter calls
  before polling starts, appends the type to that list (read on every ``getUpdates``); the unload
  takes it out again and the next registration puts it back
- the handler: its own PTB group, so a core handler of group 0 that matches first never swallows
  a tap; it matches only an update that carries ``widget_query``
- a reload: the engine does not call a factory twice on a live client, so the process marker
  (``MARKER``) keeps the application, the gateway loop and the entry point of the newest
  registration; the handler, added once per application, calls whatever entry point is there
"""

from __future__ import annotations

import asyncio
import importlib
import logging
import sys
import threading
import types
from collections.abc import Awaitable, Callable, Mapping
from typing import Any

from .mode import Tap, valid_chat_id

logger = logging.getLogger("hermes.plugins.telegram_dashboard_probe")

MARKER = "_telegram_dashboard_widget_private"
UPDATE_TYPE = "widget_query"
HANDLER_GROUP = -17
NAME = "private"

Entry = Callable[[Tap], Awaitable[None]]

_GUARD = threading.Lock()


def marker() -> dict[str, Any]:
    """The process marker's fields: a module of its own name survives a plugin reload, which
    drops this module's own state. ``app`` the newest application, ``loop`` its event loop,
    ``entry`` the newest registration's entry point, ``added`` whether the type was added here,
    ``handled`` the application that has the handler (a reconnect hands a new one)."""
    with _GUARD:
        found = sys.modules.get(MARKER)
        if not isinstance(found, types.ModuleType):
            found = types.ModuleType(MARKER, "telegram_dashboard_probe widget: the private path")
            sys.modules[MARKER] = found
        fields: dict[str, Any] = vars(found)
        for name in ("app", "loop", "entry", "handled"):
            fields.setdefault(name, None)
        fields.setdefault("added", False)
        return fields


# ----------------------------------------------------------------------------- the update type


def _update_class() -> Any:
    """PTB's ``Update``, imported where it is used: the package itself needs no PTB."""
    try:
        return importlib.import_module("telegram").Update
    except (ImportError, AttributeError):
        return None


def extend_update_types() -> bool:
    """Append ``widget_query`` to PTB's ``Update.ALL_TYPES``. True when this call added it (a PTB
    that knows the type already, or a list that cannot grow, is left as it is)."""
    update = _update_class()
    kinds = getattr(update, "ALL_TYPES", None)
    if not isinstance(kinds, list) or UPDATE_TYPE in kinds:
        return False
    kinds.append(UPDATE_TYPE)
    return True


def drop_update_type() -> None:
    kinds = getattr(_update_class(), "ALL_TYPES", None)
    while isinstance(kinds, list) and UPDATE_TYPE in kinds:
        kinds.remove(UPDATE_TYPE)


def widget_query(update: object) -> dict[str, Any] | None:
    """The query as a dict: PTB 22.8 keeps an unknown update field in ``api_kwargs``; a PTB that
    knows the type gives an object with ``to_dict()``."""
    extra = getattr(update, "api_kwargs", None)
    query = extra.get(UPDATE_TYPE) if isinstance(extra, Mapping) else None
    if query is None:
        native = getattr(update, UPDATE_TYPE, None)
        to_dict = getattr(native, "to_dict", None)
        query = to_dict() if callable(to_dict) else None
    return query if isinstance(query, dict) else None


def tap_of(query: Mapping[str, Any]) -> Tap | None:
    """The tap in a query; ``None`` without an id."""
    query_id = query.get("id")
    if not isinstance(query_id, str) or not query_id:
        return None
    sender, chat = query.get("from"), query.get("chat")
    sender_id = valid_chat_id(sender.get("id")) if isinstance(sender, Mapping) else None
    chat_id = valid_chat_id(chat.get("id")) if isinstance(chat, Mapping) else None
    message_id = query.get("message_id")
    return Tap(
        query_id,
        chat_id if chat_id is not None else sender_id,  # a private chat is its user
        sender_id if sender_id is not None else -1,  # no sender is nobody's tap
        message_id if isinstance(message_id, int) and not isinstance(message_id, bool) else None,
        query.get("data"),
    )


# ----------------------------------------------------------------------------- the handler


async def dispatch(update: Any, _context: Any) -> None:
    """The PTB callback, added once per application: it calls the newest entry point."""
    entry = marker()["entry"]
    query = widget_query(update)
    tap = tap_of(query) if query is not None else None
    if entry is not None and tap is not None:
        await entry(tap)


def make_handler(callback: Callable[..., Awaitable[None]]) -> Any:
    """A PTB handler that matches only an update carrying ``widget_query``."""
    base: Any = importlib.import_module("telegram.ext").BaseHandler

    class WidgetQueryHandler(base):  # type: ignore[misc]
        def check_update(self, update: object) -> bool:
            return widget_query(update) is not None

    return WidgetQueryHandler(callback)


def wire(
    native: Any, handler: Callable[[Callable[..., Awaitable[None]]], Any] = make_handler
) -> None:
    """At the factory, before polling starts: the update type, the application and its loop kept,
    the handler added to an application that does not have it yet. Never raises: the factory
    also starts the topic's screen, which a refused handler must not stop (taps then do not
    arrive, said once)."""
    found = marker()
    try:
        loop: asyncio.AbstractEventLoop | None = asyncio.get_running_loop()
    except RuntimeError:
        loop = None
    found["added"] = extend_update_types() or found["added"]
    found["app"], found["loop"] = native, loop
    if found["handled"] is native or not callable(getattr(native, "add_handler", None)):
        return
    try:
        native.add_handler(handler(dispatch), group=HANDLER_GROUP)
    except Exception as exc:
        logger.warning(
            "widget: tap handler not added: %s; taps will not arrive", type(exc).__name__
        )
        return
    found["handled"] = native


def start(entry: Entry) -> None:
    """The newest registration's entry point; the type back after an earlier unload took it."""
    found = marker()
    found["added"] = extend_update_types() or found["added"]
    found["entry"] = entry


def stop(entry: Entry | None = None) -> None:
    """At the unload: the entry point off (only this registration's) and the type with it."""
    found = marker()
    if entry is None or found["entry"] == entry:
        found["entry"] = None
    if found["added"]:
        drop_update_type()
        found["added"] = False


def ready() -> bool:
    app = marker()["app"]
    return app is not None and bool(getattr(app, "running", False))


# ----------------------------------------------------------------------------- the calls


class PrivateTransport:
    """The calls through the application the factory handed over (the newest one)."""

    name = NAME

    def __init__(self, app: Callable[[], Any] = lambda: marker()["app"]) -> None:
        self._app = app

    def _bot(self) -> Any:
        app = self._app()
        if app is None:
            raise ConnectionError("no Telegram application yet")
        return app.bot

    async def _raw(self, method: str, **params: Any) -> Any:
        return await self._bot().do_api_request(method, api_kwargs=params)

    async def send(
        self, chat_id: int, html: str, *, chat_data: str, public_data: str, caption: str
    ) -> int:
        sent = await self._raw(
            "sendWidget",
            chat_id=chat_id,
            html=html,
            chat_data=chat_data,
            public_data=public_data,
            caption=caption,
            allow_queries=True,
            disable_notification=True,
        )
        return int(sent["message_id"])

    async def edit(
        self, chat_id: int, message_id: int, *, chat_data: str, public_data: str | None
    ) -> None:
        params: dict[str, Any] = {
            "chat_id": chat_id,
            "message_id": message_id,
            "chat_data": chat_data,
        }
        if public_data is not None:
            params["public_data"] = public_data
        await self._raw("editMessageWidgetData", **params)

    async def answer(self, query_id: str, data: str) -> None:
        await self._raw("answerWidgetQuery", widget_query_id=query_id, data=data)

    async def pin(self, chat_id: int, message_id: int) -> bool:
        await self._bot().pin_chat_message(
            chat_id=chat_id, message_id=message_id, disable_notification=True
        )
        return True

    async def unpin(self, chat_id: int, message_id: int) -> None:
        await self._bot().unpin_chat_message(chat_id=chat_id, message_id=message_id)
