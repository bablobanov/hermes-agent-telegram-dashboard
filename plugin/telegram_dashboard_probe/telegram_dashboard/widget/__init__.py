"""The widget mode (1.2.0-beta.1, a preview outside the catalog): the dashboard as one pinned
Telegram HTML widget (Bot API 10.4) in the owner's private chat with the bot, beside the message
in the topic, which it does not touch.

``WidgetMode`` is one per registration of the plugin: the widget (``mode.Widget``), its path to
Telegram and its loop. The path is read off the engine at registration: its public widget verbs
when ``ctx.platform_actions`` has them (``public_path``), else the gateway's own Telegram client
(``private_path``), never both. The screen is the dashboard's own (``view``), in packets of at
most 1024 bytes (``packets``), drawn by ``widget.html`` (sent as ``htmlmin`` leaves it: Telegram
takes at most 4096 bytes of it after zlib).
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from datetime import datetime, tzinfo
from pathlib import Path
from typing import Any

from ..render import ScreenParts
from ..schema import DashboardSnapshot
from . import htmlmin, private_path, public_path
from .mode import Tap, Widget, WidgetSettings, caption_of, read_settings
from .view import View
from .view import view as screen_of

logger = logging.getLogger("hermes.plugins.telegram_dashboard_probe")

HTML_PATH = Path(__file__).with_name("widget.html")

__all__ = ["HTML_PATH", "WidgetMode", "WidgetSettings", "html", "read_settings"]


def html() -> str:
    """The widget's HTML as it is sent."""
    return htmlmin.minify(HTML_PATH.read_text(encoding="utf-8"))


class WidgetMode:
    """The widget of one registration: built at ``register()``, wired at the factory, run as a
    task of the gateway beside the topic's tick, retired at the unload."""

    def __init__(
        self,
        ctx: Any,
        settings: WidgetSettings,
        *,
        collect: Callable[[], Awaitable[View]],
        zone: Callable[[], tzinfo],
        version: str,
        started: float | None = None,
    ) -> None:
        actions = public_path.actions_of(ctx)
        self.path = public_path.NAME if actions is not None else private_path.NAME
        self.settings = settings
        self.loop: asyncio.AbstractEventLoop | None = None
        self.retired = False
        self.widget = Widget(
            settings,
            getattr(ctx, "state", None),
            html(),
            caption_of(version),
            source=collect,
            zone=zone,
            started=started,
        )
        if actions is not None:
            self.widget.transport = public_path.PublicTransport(actions)
            self._hook(ctx)
        else:
            self.widget.transport = private_path.PrivateTransport()
        reason = "" if actions is not None else " (no platform actions)"
        logger.info(
            "widget: path %s%s, chat %s, every %s s",
            self.path,
            reason,
            settings.chat_id,
            settings.period_seconds,
        )

    @staticmethod
    def view(snapshot: DashboardSnapshot, parts: ScreenParts, now: datetime, zone: tzinfo) -> View:
        """The widget's screen from one collection and its parts (``view.view``)."""
        return screen_of(snapshot, parts, now, zone)

    def _entry(self) -> Callable[[Tap], Awaitable[None]] | None:
        return None if self.retired else self.widget.on_tap

    def _hook(self, ctx: Any) -> None:
        register = getattr(ctx, "register_hook", None)
        if not callable(register):
            logger.warning("widget: the engine takes no hooks; widget taps will not arrive")
            return
        try:
            register(public_path.HOOK, public_path.Hook(self._entry, lambda: self.loop))
        except Exception as exc:
            logger.warning("widget: tap hook not registered: %s", type(exc).__name__)

    def wire(self, native: Any) -> None:
        """At the adapter's ``connect()``, before polling: the private path's update type and
        handler; nothing on the public path."""
        if self.path != private_path.NAME or self.retired:
            return
        try:
            private_path.wire(native)
        except Exception as exc:  # the factory starts the topic's screen too: never stop it
            logger.warning("widget: not wired: %s", type(exc).__name__)

    async def run(self, serves: Callable[[], bool], connected: Callable[[], bool]) -> None:
        """The widget's loop on the gateway loop, while ``serves()``; it ticks once the adapter is
        ``connected()`` (and, on the private path, its application runs)."""
        self.loop = asyncio.get_running_loop()
        if self.path == private_path.NAME:
            private_path.start(self.widget.on_tap)

            def ready() -> bool:
                return connected() and private_path.ready()

        else:
            ready = connected
        await self.widget.run(lambda: serves() and not self.retired, ready)

    def retire(self) -> None:
        """The engine unloaded this registration: no more taps for it; the private path takes its
        update type out (the next registration puts it back)."""
        self.retired = True
        if self.path == private_path.NAME:
            private_path.stop(self.widget.on_tap)
