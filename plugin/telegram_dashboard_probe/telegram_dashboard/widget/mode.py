"""The widget mode: one pinned Telegram HTML widget (Bot API 10.4) in the owner's private chat.

Every ``period_seconds`` the mode collects the screen with the dashboard's own collector and
rewrites only the widget's data: the Status tab in ``chat_data``, the Cron tab in
``public_data``. A tap in the widget is a ``widget_query``:

- ``refresh``: a collection (at most ``REFRESH_WAIT`` seconds), answered with the packet and
  written into the message; a slower collection is answered with the screen at hand and written
  when it ends
- ``details``: answered at once with the Details of the last collection
- ``cron:<frequency>``, ``cron:paused``, ``cron:done``: a folded group's jobs, at once
- ``theme:<name>``: the theme chosen in the widget, kept in the plugin's state (the setting is
  the default; a choice made under another default gives way to it), answered with its colours
  and written into the message

The calls go through a ``Transport``: the engine's public widget verbs when it has them
(``public_path``), else the gateway's own Telegram client (``private_path``). The message is the
same while the HTML and the caption are (both are fixed per release); another release sends a
new one, pins it and unpins the old one once the pin holds. Right after a start, while the
screen says the gateway is still coming up, the next collection comes after ``SETTLE_DELAY``.

Errors are logged by class only: a network error's text can carry the request URL with a token.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import timedelta
from typing import Any, Protocol

from . import packets
from .view import View, unavailable

logger = logging.getLogger("hermes.plugins.telegram_dashboard_probe")

DEFAULT_PERIOD = 300  # the topic's dashboard ticks every 30 minutes; Refresh gives a fresh screen
MIN_PERIOD, MAX_PERIOD = 60, 3600
REFRESH_WAIT = 10.0  # inside the 15 s Telegram gives the answer
CRON_QUERY_LIMIT = 64  # ``cron:`` and a frequency's words ("Every 15 min", "Twice a week")
STALE_PERIODS = 2  # no fresh collection for this many periods: the screen says "Stale data"
STATE_MESSAGE = "widget"
STATE_THEME = "widget_theme"
SETTLE_SECONDS = 180.0  # the process's first minutes, while the gateway comes up
SETTLE_DELAY = 20.0
WAIT_READY_SECONDS = 1.0
WAIT_SAID_SECONDS = 60.0  # waiting this long for the path is said once in the journal
DEFAULT_RETRY_SECONDS = 5.0
UNPIN_TRIES = 3  # the old message's unpin, once per tick, before it is left to the owner
LOST_PHRASES = ("message to edit not found", "message_id_invalid", "message not found")
WAIT_PHRASES = ("retry after", "retry in", "too many requests", "flood control")

Sleep = Callable[[float], Awaitable[None]]


class Refused(Exception):
    """The engine refused the mode's calls (a capability not granted): nothing works until the
    owner grants it, so the mode stops and says once what to grant."""


@dataclass(frozen=True)
class Tap:
    """One ``widget_query``, whichever path brought it: ``sender_id`` is ``None`` where the path
    does not say (the public event is authorized as its sender by the gateway)."""

    query_id: str
    chat_id: int | None
    sender_id: int | None
    message_id: int | None
    data: object


class Transport(Protocol):
    """The four Bot API 10.4 calls and the pin; a refusal raises (``outcome`` reads it)."""

    name: str

    async def send(
        self, chat_id: int, html: str, *, chat_data: str, public_data: str, caption: str
    ) -> int: ...

    async def edit(
        self, chat_id: int, message_id: int, *, chat_data: str, public_data: str | None
    ) -> None: ...

    async def answer(self, query_id: str, data: str) -> None: ...

    async def pin(self, chat_id: int, message_id: int) -> bool: ...

    async def unpin(self, chat_id: int, message_id: int) -> None: ...


# ----------------------------------------------------------------------------- settings


@dataclass(frozen=True)
class WidgetSettings:
    """``widget`` in the plugin's settings: ``enabled`` (off unless true), ``chat_id`` (the
    owner's user id: widgets live in private chats), ``period_seconds``, ``theme``."""

    chat_id: int
    period_seconds: int = DEFAULT_PERIOD
    theme: str = packets.DEFAULT_THEME


def valid_chat_id(value: object) -> int | None:
    """A private chat is a positive user id; the config may keep it as text."""
    if isinstance(value, bool):
        return None
    if isinstance(value, str) and value.strip().isdigit():
        value = int(value.strip())
    return value if isinstance(value, int) and value > 0 else None


def valid_period(value: object) -> int:
    if isinstance(value, str) and value.strip().isdigit():
        value = int(value.strip())
    if value is None:
        return DEFAULT_PERIOD
    if isinstance(value, int) and not isinstance(value, bool) and MIN_PERIOD <= value <= MAX_PERIOD:
        return value
    logger.warning(
        "widget: period_seconds is not %s..%s; using %s", MIN_PERIOD, MAX_PERIOD, DEFAULT_PERIOD
    )
    return DEFAULT_PERIOD


def valid_theme(value: object) -> str:
    """A theme of ``packets.THEMES``; anything else, of any type, is the default with a WARNING."""
    if value is None or (isinstance(value, str) and not value.strip()):
        return packets.DEFAULT_THEME
    name = value.strip().lower() if isinstance(value, str) else None
    if name in packets.THEMES:
        return str(name)
    themes = ", ".join(packets.THEMES)
    logger.warning("widget: theme is not one of %s; using %s", themes, packets.DEFAULT_THEME)
    return packets.DEFAULT_THEME


FALSE_WORDS = frozenset({"0", "false", "no", "off", ""})


def read_settings(raw: object) -> WidgetSettings | None:
    """The ``widget`` mapping of the settings; ``None`` when it is absent, not on, or names no
    chat (said once)."""
    if raw is None:
        return None
    if not isinstance(raw, dict):
        logger.warning("widget: settings.widget must be a mapping; widget off")
        return None
    enabled = raw.get("enabled")
    on = enabled is True or (
        isinstance(enabled, str) and enabled.strip().lower() not in FALSE_WORDS
    )
    if not on:
        return None
    chat_id = valid_chat_id(raw.get("chat_id"))
    if chat_id is None:
        logger.warning("widget: widget.chat_id is not a user id; widget off")
        return None
    return WidgetSettings(
        chat_id, valid_period(raw.get("period_seconds")), valid_theme(raw.get("theme"))
    )


# ----------------------------------------------------------------------------- errors


def outcome(exc: BaseException) -> str:
    """``refused`` (the engine said the mode may not), ``done`` (an edit that changed nothing),
    ``gone`` (the message is not there), ``wait`` (flood control) or ``failed``. Only the class
    and Telegram's fixed phrases are looked at."""
    if isinstance(exc, Refused):
        return "refused"
    if type(exc).__name__ == "RetryAfter":
        return "wait"
    text = str(exc).lower()
    if "not modified" in text:
        return "done"
    if any(phrase in text for phrase in LOST_PHRASES):
        return "gone"
    if any(phrase in text for phrase in WAIT_PHRASES):
        return "wait"
    return "failed"


def retry_seconds(exc: BaseException) -> float:
    value = getattr(exc, "retry_after", DEFAULT_RETRY_SECONDS)
    if isinstance(value, timedelta):
        value = value.total_seconds()
    if isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0:
        return float(value)
    return DEFAULT_RETRY_SECONDS


def caption_of(version: str) -> str:
    """The message's caption: the package's version (12.11: which version is on the screen)."""
    return f"Hermes dashboard {version} · widget"


# ----------------------------------------------------------------------------- the widget


class Widget:
    """One widget message in one private chat: collect, send and pin, tick, answer taps."""

    def __init__(
        self,
        settings: WidgetSettings,
        state: Any,
        html: str,
        caption: str,
        *,
        source: Callable[[], Awaitable[View]],
        zone: Callable[[], Any],
        started: float | None = None,
        clock: Callable[[], float] = time.time,
        sleep: Sleep = asyncio.sleep,
    ) -> None:
        self.chat_id = settings.chat_id
        self.period = settings.period_seconds
        self.theme_default = settings.theme
        self.state = state
        self.html = html
        self.caption = caption
        self.key = hashlib.sha256(f"{caption}\n{html}".encode()).hexdigest()
        self._source = source
        self._zone = zone
        self._clock = clock
        self._sleep = sleep
        self.started = clock() if started is None else started
        self.transport: Transport | None = None
        self.tick = 0
        self.message_id: int | None = None
        self._loaded = False
        self._lock: asyncio.Lock | None = None
        self.view: View | None = None
        self.collected = 0  # ms of the view's collection
        self.public_sent: str | None = None
        self._theme: str | None = None
        self._flight: asyncio.Future[None] | None = None
        self._failure: str | None = None
        # The message still to pin and the one to unpin once it holds (a failed pin is tried again
        # on the next tick; the old message keeps its pin till then).
        self.unpinned: int | None = None
        self.old: int | None = None
        self._pin_said = False
        self._unpin_tries = 0
        self.stopped = False
        # Its registration is unloaded or its loop ended: nothing is written from here on.
        self.closed = False

    def lock(self) -> asyncio.Lock:
        if self._lock is None:
            self._lock = asyncio.Lock()
        return self._lock

    def close(self) -> None:
        """On the widget's loop, at its end: nothing written from here on, the collection in
        flight cancelled (a late tap still gets its answer)."""
        self.closed = True
        flight = self._flight
        if flight is not None and not flight.done():
            flight.cancel()

    def now_ms(self) -> int:
        return int(self._clock() * 1000)

    def packet(self) -> packets.Packet:
        """The next tick's packet, timed by its collection (the widget keeps the newest packet by
        it). A screen older than ``STALE_PERIODS`` periods is no longer the dashboard's verdict."""
        self.tick += 1
        view = self.view or self.unavailable("no data collected yet")
        age = self.now_ms() - self.collected if self.collected else 0
        stale = self.view is not None and age > STALE_PERIODS * self.period * 1000
        at = self.collected or self.now_ms()
        return packets.chat_packet(self.tick, at, view, stale=stale, theme=self.theme())

    def public(self) -> str:
        """The Cron tab."""
        view = self.view or unavailable("")
        return packets.encode(packets.cron_packet(view, self.collected or self.now_ms()))

    def unavailable(self, reason: str) -> View:
        """A screen without data keeps the zone of the last screen with data, else the setting."""
        return unavailable(reason, self.view.zone if self.view is not None else self._zone())

    # -- the theme: the setting is the default, a choice in the widget is kept in the state

    def theme(self) -> str:
        if self._theme is None:
            self._theme = self.theme_default
            value = self._read(STATE_THEME)
            if (
                isinstance(value, dict)
                and value.get("name") in packets.THEMES
                and value.get("default") == self.theme_default
            ):
                self._theme = str(value["name"])
        return self._theme

    def choose(self, name: str) -> None:
        """Kept with the default it was chosen under: a new default in the settings wins."""
        self._theme = name
        self._write(STATE_THEME, {"name": name, "default": self.theme_default})

    def _read(self, key: str) -> object:
        try:
            return self.state.get(key) if self.state is not None else None
        except Exception as exc:
            logger.warning("widget: state not read: %s", type(exc).__name__)
            return None

    def _write(self, key: str, value: object) -> None:
        if self.closed:  # a newer registration's state is not this one's to write
            return
        try:
            if self.state is not None:
                self.state.set(key, value)
        except Exception as exc:
            logger.warning("widget: state not saved: %s", type(exc).__name__)

    # -- the collection: one at a time, shared by the tick and a refresh

    def collection(self) -> asyncio.Future[None]:
        if self._flight is None or self._flight.done():
            self._flight = asyncio.ensure_future(self._collect())
        return self._flight

    async def _collect(self) -> None:
        """Never raises: a failure keeps the last screen and its time (with none, a screen that
        says so); one line in the journal per reason."""
        try:
            view = await self._source()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._failed(f"collection failed: {type(exc).__name__}")
            if self.view is None:
                self.view, self.collected = self.unavailable("collection failed"), self.now_ms()
            return
        self.view, self.collected = view, self.now_ms()
        if self._failure is not None:
            logger.info("widget: live data back")
            self._failure = None

    def _failed(self, reason: str) -> None:
        if reason != self._failure:
            logger.warning("widget: %s", reason)
            self._failure = reason

    # -- the message

    def stored(self) -> dict[str, Any] | None:
        value = self._read(STATE_MESSAGE)
        if isinstance(value, dict) and isinstance(value.get("message_id"), int):
            return value
        return None

    def current(self) -> int | None:
        """The message id of this chat's widget with this HTML and caption, or ``None``. Read once:
        a state file that cannot be read or written never makes the mode send again."""
        if not self._loaded:
            self._loaded = True
            value = self.stored()
            if value and value.get("chat_id") == self.chat_id and value.get("key") == self.key:
                self.message_id = value["message_id"]
                pending = value.get("unpinned")
                self.unpinned = pending if pending == self.message_id else None
                old = value.get("old")
                self.old = old if isinstance(old, int) and old != self.message_id else None
        return self.message_id

    def remember(self, message_id: int | None) -> None:
        self.message_id, self._loaded = message_id, True
        value = None
        if message_id is not None:
            value = {
                "chat_id": self.chat_id,
                "message_id": message_id,
                "key": self.key,
                "unpinned": self.unpinned,
                "old": self.old,
            }
        self._write(STATE_MESSAGE, value)

    async def step(self) -> float | None:
        """One tick: collect (outside the lock: a tap is never held behind a collection), then
        write; how long to sleep before the next one, ``None`` when the engine refused the mode or
        the widget is closed. A tick cancelled while it waits leaves the collection to a tap."""
        await asyncio.wait({self.collection()})
        if self.closed:
            return None
        async with self.lock():
            try:
                message_id = self.current()
                if message_id is None:
                    await self.send()
                else:
                    await self.edit(message_id, self.packet())
                await self.pin_pending()
            except Exception as exc:
                return self._after_failure(exc, "tick")
        if (
            self.view is not None
            and self.view.settling
            and self._clock() - self.started < SETTLE_SECONDS
        ):
            return SETTLE_DELAY
        return float(self.period)

    def _after_failure(self, exc: BaseException, where: str) -> float | None:
        kind = outcome(exc)
        if kind == "refused":
            self.stopped = True
            logger.warning("widget: %s; the widget is off till then", exc)
            return None
        if kind == "gone":
            self.remember(None)
            logger.warning("widget: widget message is gone; sending a new one")
            return float(self.period)
        if kind == "wait":
            return retry_seconds(exc)
        if kind == "failed":
            logger.warning("widget: %s failed: %s", where, type(exc).__name__)
        return float(self.period)

    async def send(self) -> None:
        assert self.transport is not None
        previous = self.stored()
        public = self.public()
        message_id = await self.transport.send(
            self.chat_id,
            self.html,
            chat_data=packets.encode(self.packet()),
            public_data=public,
            caption=self.caption,
        )
        self.public_sent = public
        old = (
            previous.get("message_id")
            if previous and previous.get("chat_id") == self.chat_id
            else None
        )
        self.unpinned = message_id
        self.old = old if isinstance(old, int) and old != message_id else None
        self.remember(message_id)
        logger.info("widget: widget sent as message %s", message_id)

    async def pin_pending(self) -> None:
        """The new message's pin, then the old one's unpin, only after the pin holds; a failed
        pin is tried again on the next tick, a failed unpin ``UNPIN_TRIES`` times. A path that
        cannot pin says so once."""
        message_id = self.message_id
        if message_id is None:
            return
        if self.unpinned == message_id and not await self._pin(message_id):
            return
        if self.unpinned is None and self.old is not None:
            await self._unpin_old(message_id, self.old)

    async def _pin(self, message_id: int) -> bool:
        """True once the pin holds; the old message stays in the state till its unpin."""
        assert self.transport is not None
        try:
            pinned = await self.transport.pin(self.chat_id, message_id)
        except Exception as exc:
            if outcome(exc) in ("refused", "gone", "wait"):
                raise
            logger.warning("widget: pin failed: %s; tried again next tick", type(exc).__name__)
            return False
        if not pinned:
            if not self._pin_said:
                self._pin_said = True
                logger.info("widget: this engine cannot pin; pin message %s by hand", message_id)
            self.unpinned = self.old = None
            self.remember(message_id)
            return False
        self.unpinned = None
        self.remember(message_id)
        return True

    async def _unpin_old(self, message_id: int, old: int) -> None:
        assert self.transport is not None
        try:
            await self.transport.unpin(self.chat_id, old)
        except Exception as exc:
            kind, self._unpin_tries = outcome(exc), self._unpin_tries + 1
            if kind in ("failed", "wait") and self._unpin_tries < UNPIN_TRIES:
                logger.info(
                    "widget: old widget not unpinned: %s; tried again next tick",
                    type(exc).__name__,
                )
                return
            if kind != "gone":
                logger.warning(
                    "widget: old widget %s left pinned: %s; unpin it by hand",
                    old,
                    type(exc).__name__,
                )
        self.old, self._unpin_tries = None, 0
        self.remember(message_id)

    async def edit(self, message_id: int, packet: packets.Packet) -> None:
        """The packet; the Cron tab too when it differs from what the message holds."""
        assert self.transport is not None
        public = self.public()
        changed = public != self.public_sent
        try:
            await self.transport.edit(
                self.chat_id,
                message_id,
                chat_data=packets.encode(packet),
                public_data=public if changed else None,
            )
        except Exception as exc:
            if outcome(exc) != "done":
                raise
        if changed:
            self.public_sent = public

    # -- taps

    async def on_tap(self, tap: Tap) -> None:
        """Answer every tap inside Telegram's 15 s, never behind a tick in flight. A refresh
        waits for a collection at most ``REFRESH_WAIT``, answers with the packet and writes it
        unless a tick wrote a newer one meanwhile; a collection still running is written when it
        ends. The Details, a Cron group and a theme are answered at once."""
        if self.transport is None or self.stopped or self.closed:
            return
        try:
            refusal, message_id, kind = self._check(tap)
        except Exception as exc:
            logger.warning("widget: query failed: %s", type(exc).__name__)
            refusal, message_id, kind = {"e": "failed"}, None, ""
        if refusal is None and kind != "refresh":
            await self._answer_now(tap.query_id, message_id, kind)
            return
        flight: asyncio.Future[None] | None = None
        pending = False
        if refusal is None:
            flight = self.collection()
            # ``wait`` neither cancels the collection on its timeout nor raises its cancellation
            await asyncio.wait({flight}, timeout=REFRESH_WAIT)
            pending = not flight.done()  # decided before the answer: it may end while it flies
        reply = refusal or self.packet()
        await self._answer(tap.query_id, reply)
        if refusal is not None or flight is None or message_id is None:
            return
        written: packets.Packet | None = reply
        if pending:
            await asyncio.wait({flight})
            written = None  # the answer went out with the screen at hand: write a fresh packet
        async with self.lock():
            if (
                self.closed
                or self.current() != message_id
                or (written is not None and written.get("n") != self.tick)
            ):
                return
            try:
                await self.edit(message_id, written or self.packet())
            except Exception as exc:
                self._after_failure(exc, "refresh edit")

    async def _answer(self, query_id: str, reply: packets.Packet) -> None:
        assert self.transport is not None
        try:
            await self.transport.answer(query_id, packets.encode(reply))
        except Exception as exc:
            if outcome(exc) == "refused":  # the mode is off till the owner grants it, said once
                self._after_failure(exc, "answer")
                return
            logger.warning("widget: answer failed: %s", type(exc).__name__)

    async def _answer_now(self, query_id: str, message_id: int | None, kind: str) -> None:
        """``details`` and ``cron:…`` from the last collection, nothing written; ``theme:<name>``
        kept, answered with the screen in it, then written into the message for every device."""
        view = self.view or unavailable("")
        if kind == "details":
            reply: packets.Packet = packets.details_packet(view)
        elif kind.startswith("cron:"):
            reply = packets.cron_answer(view, kind[5:])
        else:
            # The whole screen in the new theme, numbered: a tick's packet built before the choice,
            # landing after the answer, is older than it and the widget keeps the answer.
            self.choose(kind.partition(":")[2])
            reply = self.packet()
        await self._answer(query_id, reply)
        if kind == "details" or kind.startswith("cron:") or message_id is None:
            return
        async with self.lock():
            if self.closed or self.current() != message_id:
                return
            try:
                await self.edit(message_id, reply if reply.get("n") == self.tick else self.packet())
            except Exception as exc:
                self._after_failure(exc, "theme edit")

    def _check(self, tap: Tap) -> tuple[packets.Packet | None, int | None, str]:
        """A refusal for a stranger, an old widget or an unknown request; else ``None``, the
        message and the request: ``refresh``, ``details``, ``cron:<group>`` or ``theme:<name>``."""
        message_id = self.current()
        stranger = tap.sender_id is not None and tap.sender_id != self.chat_id
        if stranger or tap.chat_id != self.chat_id:
            return {"e": "forbidden"}, None, ""
        if message_id is None or tap.message_id != message_id:
            return {"e": "stale"}, None, ""
        data = tap.data
        if data in ("refresh", "details") or (
            isinstance(data, str)
            and (
                (data.startswith("theme:") and data[6:] in packets.THEMES)
                or (data.startswith("cron:") and 5 < len(data) <= CRON_QUERY_LIMIT)
            )
        ):
            return None, message_id, str(data)
        return {"e": "unknown"}, None, ""

    # -- the loop

    async def run(self, serves: Callable[[], bool], ready: Callable[[], bool]) -> None:
        """The tick loop on the gateway's event loop: while its registration serves the profile,
        once the path is ready; ended by the engine's refusal, an unload or a takeover."""
        waited = 0.0
        while serves() and not self.stopped and not self.closed:
            if not ready():
                waited += WAIT_READY_SECONDS
                if waited == WAIT_SAID_SECONDS:
                    logger.warning(
                        "widget: still waiting for Telegram to connect; turned on by a reload, it "
                        "starts after the next reconnect or a restart of the gateway"
                    )
                await self._sleep(WAIT_READY_SECONDS)
                continue
            try:
                delay = await self.step()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning("widget: tick failed: %s", type(exc).__name__)
                delay = float(self.period)
            if delay is None:
                return
            await self._sleep(delay)
