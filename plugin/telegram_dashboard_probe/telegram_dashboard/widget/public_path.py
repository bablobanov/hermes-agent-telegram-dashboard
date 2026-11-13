"""The public path to Telegram: the engine's widget verbs and its ``widget_query`` event
(NousResearch/hermes-agent#152186).

``ctx.platform_actions.send_widget`` / ``edit_widget_data`` / ``answer_widget_query`` do the Bot
API calls inside the engine, behind the capability ``gateway.platform_actions``; a tap reaches
the plugin as the hook ``gateway_platform_event`` with ``event_type="widget_query"``, after the
gateway authorized its sender. The path is chosen by the engine itself, never by its version
(as the dashboard picks its HTML verb): the three verbs there, the path is public. Then the
private path is not touched at all, so no tap is answered twice.

The engine runs the hook's callback synchronously (on its own thread or the caller's), so the
callback only hands the tap to the gateway loop the widget runs on and returns.

The verbs answer ``{"ok": False, "error": code, "detail": text}`` instead of raising; a refused
capability stops the mode with one WARNING naming what to grant. The verbs have no pin: the
owner pins the widget by hand (the mode says so once).
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Mapping
from typing import Any

from .mode import Refused, Tap, valid_chat_id

logger = logging.getLogger("hermes.plugins.telegram_dashboard_probe")

PLATFORM = "telegram"
HOOK = "gateway_platform_event"
EVENT = "widget_query"
VERBS = ("send_widget", "edit_widget_data", "answer_widget_query")
NAME = "public"
GRANT = "plugins.entries.telegram_dashboard_probe.allow_platform_actions: true"


def actions_of(ctx: Any) -> Any | None:
    """``ctx.platform_actions`` when it has the three widget verbs, else ``None``."""
    try:
        actions = getattr(ctx, "platform_actions", None)
    except Exception as exc:  # a facade that raises on access is no facade
        logger.warning("widget: platform actions not read: %s", type(exc).__name__)
        return None
    return actions if all(callable(getattr(actions, verb, None)) for verb in VERBS) else None


class ActionFailed(Exception):
    """A verb's refusal: its error code and Telegram's words (``outcome`` reads them)."""


def checked(result: object, verb: str) -> Mapping[str, Any]:
    if isinstance(result, Mapping) and result.get("ok") is True:
        return result
    code = result.get("error") if isinstance(result, Mapping) else None
    detail = result.get("detail") if isinstance(result, Mapping) else None
    if code == "capability_not_granted":
        raise Refused(f"the engine's platform actions are not granted (set {GRANT})")
    raise ActionFailed(f"{verb}: {code or 'no answer'}: {str(detail or '')[:200]}")


class PublicTransport:
    """The calls through the engine's verbs."""

    name = NAME

    def __init__(self, actions: Any) -> None:
        self._actions = actions

    async def send(
        self, chat_id: int, html: str, *, chat_data: str, public_data: str, caption: str
    ) -> int:
        result = checked(
            await self._actions.send_widget(
                PLATFORM,
                str(chat_id),
                html,
                chat_data=chat_data,
                public_data=public_data,
                caption=caption,
                allow_queries=True,
                disable_notification=True,
            ),
            "send_widget",
        )
        return int(result["message_id"])

    async def edit(
        self, chat_id: int, message_id: int, *, chat_data: str, public_data: str | None
    ) -> None:
        checked(
            await self._actions.edit_widget_data(
                PLATFORM,
                str(chat_id),
                str(message_id),
                chat_data=chat_data,
                public_data=public_data,
            ),
            "edit_widget_data",
        )

    async def answer(self, query_id: str, data: str) -> None:
        checked(
            await self._actions.answer_widget_query(PLATFORM, query_id, data=data),
            "answer_widget_query",
        )

    async def pin(self, chat_id: int, message_id: int) -> bool:
        return False

    async def unpin(self, chat_id: int, message_id: int) -> None:
        return None


def tap_of(payload: object) -> Tap | None:
    """The tap in the event's payload (its ids are text); ``None`` without a query id."""
    if not isinstance(payload, Mapping):
        return None
    query_id = payload.get("query_id")
    if not isinstance(query_id, str) or not query_id:
        return None
    message = payload.get("message_id")
    message_id = int(message) if isinstance(message, str) and message.isdigit() else None
    return Tap(
        query_id, valid_chat_id(payload.get("chat_id")), None, message_id, payload.get("data")
    )


class Hook:
    """The ``gateway_platform_event`` callback: a ``widget_query`` handed to the gateway loop of
    the widget's tick (``loop()``), to the entry point (``entry()``) of the newest registration."""

    def __init__(
        self,
        entry: Callable[[], Callable[[Tap], Any] | None],
        loop: Callable[[], asyncio.AbstractEventLoop | None],
    ) -> None:
        self._entry = entry
        self._loop = loop

    def __call__(
        self, platform: object = None, event_type: object = None, payload: object = None, **_: Any
    ) -> None:
        if platform != PLATFORM or event_type != EVENT:
            return
        tap, entry, loop = tap_of(payload), self._entry(), self._loop()
        if tap is None or entry is None or loop is None or loop.is_closed():
            return
        coroutine = entry(tap)
        try:
            future = asyncio.run_coroutine_threadsafe(coroutine, loop)
        except RuntimeError as exc:  # the loop closed after the check
            coroutine.close()  # never awaited: closed here, not warned about by the collector
            logger.info("widget: tap not handed over: %s", type(exc).__name__)
            return
        future.add_done_callback(_said)


def _said(future: Any) -> None:
    """A tap that failed past its own handling: its class in the journal, never its text."""
    if future.cancelled():
        return
    exc = future.exception()
    if exc is not None:
        logger.warning("widget: tap failed: %s", type(exc).__name__)
