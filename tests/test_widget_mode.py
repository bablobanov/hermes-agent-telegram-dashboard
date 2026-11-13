"""The widget mode's runtime (``widget/mode.py``) on a fake transport: no engine, no network.

The transport records every call; a test makes one of its calls raise to stand for Telegram's or
the engine's refusal. The screen is a fixed ``View`` (the data tests are in test_widget_view.py
and test_widget_packets.py), so these tests are about the message: sent once and pinned, its data
edited on every tick, the pin tried again, the taps answered.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Callable
from datetime import UTC, timedelta
from typing import Any

import pytest

from telegram_dashboard.widget import mode, packets
from telegram_dashboard.widget.mode import Refused, Tap, Widget, WidgetSettings
from telegram_dashboard.widget.view import CronView, View

CHAT = 777000111  # fixture user id, not a real one


class State:
    """The engine's plugin state: JSON values under string keys; ``broken`` makes it raise."""

    def __init__(self) -> None:
        self.data: dict[str, Any] = {}
        self.broken = False
        self.writes = 0

    def get(self, key: str, default: Any = None) -> Any:
        if self.broken:
            raise OSError("state unreadable")
        return self.data.get(key, default)

    def set(self, key: str, value: Any) -> None:
        if self.broken:
            raise OSError("disk full")
        self.writes += 1
        self.data[key] = json.loads(json.dumps(value))


class RetryAfter(Exception):
    """PTB's flood control, by its class name as ``outcome`` reads it."""

    def __init__(self, seconds: float) -> None:
        super().__init__(f"Flood control exceeded. Retry in {seconds} seconds")
        self.retry_after = timedelta(seconds=seconds)


class Transport:
    name = "fake"

    def __init__(self, *, can_pin: bool = True) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.can_pin = can_pin
        self.fail: dict[str, Callable[[], BaseException]] = {}
        self.next_id = 500

    def _call(self, method: str, **params: Any) -> None:
        self.calls.append((method, params))
        make = self.fail.get(method)
        if make is not None:
            raise make()

    async def send(
        self, chat_id: int, html: str, *, chat_data: str, public_data: str, caption: str
    ) -> int:
        self._call(
            "send", chat_id=chat_id, chat_data=chat_data, public_data=public_data, caption=caption
        )
        self.next_id += 1
        return self.next_id

    async def edit(
        self, chat_id: int, message_id: int, *, chat_data: str, public_data: str | None
    ) -> None:
        self._call(
            "edit",
            chat_id=chat_id,
            message_id=message_id,
            chat_data=chat_data,
            public_data=public_data,
        )

    async def answer(self, query_id: str, data: str) -> None:
        self._call("answer", query_id=query_id, data=data)

    async def pin(self, chat_id: int, message_id: int) -> bool:
        self._call("pin", chat_id=chat_id, message_id=message_id)
        return self.can_pin

    async def unpin(self, chat_id: int, message_id: int) -> None:
        self._call("unpin", chat_id=chat_id, message_id=message_id)

    def methods(self) -> list[str]:
        return [method for method, _ in self.calls]


def screen(head: str = "Healthy", jobs: int = 2) -> View:
    rows = tuple([f"job {i}", i * 10, f"{10 + i}:00"] for i in range(jobs))
    return View(
        "normal",
        head,
        (("works", ("✓ Gateway ✓ Telegram",)),),
        (("telegram", ("Telegram connected",)),),
        cron=CronView(
            "2 active · ticker 10:00", groups=(("Every 30 min", rows),), paused=(["p", "Nov 2"],)
        ),
        zone=UTC,
    )


class Clock:
    def __init__(self, now: float = 1_800_000_000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


def make(
    *,
    view: View | None = None,
    state: State | None = None,
    transport: Transport | None = None,
    clock: Clock | None = None,
    theme: str = "telegram",
    source: Any = None,
) -> tuple[Widget, Transport, State]:
    state = state or State()
    transport = transport or Transport()
    current = view or screen()

    async def collect() -> View:
        return current

    widget = Widget(
        WidgetSettings(CHAT, 300, theme),
        state,
        "<b>widget</b>",
        mode.caption_of("1.2.0b1"),
        source=source or collect,
        zone=lambda: UTC,
        clock=clock or Clock(),
    )
    widget.transport = transport
    return widget, transport, state


def run(coro: Any) -> Any:
    return asyncio.run(coro)


# ----------------------------------------------------------------------------- settings


def test_the_widget_is_off_unless_its_settings_turn_it_on() -> None:
    assert mode.read_settings(None) is None
    assert mode.read_settings({"chat_id": CHAT}) is None
    assert mode.read_settings({"enabled": False, "chat_id": CHAT}) is None
    assert mode.read_settings({"enabled": "off", "chat_id": CHAT}) is None
    on = mode.read_settings({"enabled": True, "chat_id": str(CHAT)})
    assert on == WidgetSettings(CHAT, mode.DEFAULT_PERIOD, packets.DEFAULT_THEME)
    assert mode.read_settings({"enabled": "yes", "chat_id": CHAT}) == on


def test_a_widget_without_a_user_id_stays_off(caplog: pytest.LogCaptureFixture) -> None:
    for chat in (None, "", "-100123", -5, 0, True, "abc", 1.5):
        assert mode.read_settings({"enabled": True, "chat_id": chat}) is None
    assert "widget.chat_id is not a user id" in caplog.text
    assert mode.read_settings(["enabled"]) is None


def test_the_period_and_the_theme_fall_back_to_their_defaults(
    caplog: pytest.LogCaptureFixture,
) -> None:
    assert mode.valid_period("600") == 600
    for bad in (59, 3601, "x", True, 300.5):
        assert mode.valid_period(bad) == mode.DEFAULT_PERIOD
    assert mode.valid_theme(" Neon ") == "neon"
    # Any type: a list, a mapping or a number in the config never stops the registration.
    for bad in ("sunset", ["neon"], {"name": "neon"}, 5, True):
        assert mode.valid_theme(bad) == packets.DEFAULT_THEME
    assert mode.valid_theme(None) == mode.valid_theme("") == packets.DEFAULT_THEME
    assert "theme is not one of" in caplog.text


# ----------------------------------------------------------------------------- the message


def test_the_first_tick_sends_the_widget_and_pins_it() -> None:
    widget, transport, state = make()
    delay = run(widget.step())
    assert delay == 300.0
    assert transport.methods() == ["send", "pin"]
    sent = transport.calls[0][1]
    assert sent["caption"] == "Hermes dashboard 1.2.0b1 · widget"
    assert json.loads(sent["chat_data"])["st"] == "normal"
    assert json.loads(sent["public_data"])["x"] == list(packets.UI_WORDS)
    stored = state.data[mode.STATE_MESSAGE]
    assert stored["message_id"] == 501 and stored["chat_id"] == CHAT
    assert stored["unpinned"] is None and stored["old"] is None


def test_a_tick_edits_the_status_and_the_cron_tab_only_when_it_changed() -> None:
    widget, transport, _ = make()
    run(widget.step())
    run(widget.step())
    method, params = transport.calls[-1]
    assert method == "edit" and params["message_id"] == 501
    assert params["public_data"] is None  # the Cron tab is the one the message holds
    assert json.loads(params["chat_data"])["n"] == 2


def test_a_new_cron_tab_rides_with_the_edit() -> None:
    views = iter([screen(jobs=2), screen(jobs=3)])

    async def collect() -> View:
        return next(views)

    widget, transport, _ = make(source=collect)
    run(widget.step())
    run(widget.step())
    params = transport.calls[-1][1]
    assert params["public_data"] is not None
    assert len(json.loads(params["public_data"])["g"][0][3]) == 3


def test_a_restart_keeps_the_message_of_the_same_html_and_caption() -> None:
    widget, _, state = make()
    run(widget.step())
    again, transport2, _ = make(state=state)
    run(again.step())
    assert transport2.methods() == ["edit"]
    assert transport2.calls[0][1]["message_id"] == 501


def test_another_release_sends_a_new_message_and_unpins_the_old_once_the_pin_holds() -> None:
    widget, _, state = make()
    run(widget.step())
    state.data[mode.STATE_MESSAGE]["key"] = "an earlier release"
    newer, transport, _ = make(state=state, transport=Transport())
    transport.next_id = 900
    run(newer.step())
    assert transport.methods() == ["send", "pin", "unpin"]
    assert transport.calls[1][1]["message_id"] == 901
    assert transport.calls[2][1]["message_id"] == 501


def test_a_failed_pin_is_tried_again_and_the_old_message_keeps_its_pin_till_then() -> None:
    widget, _, state = make()
    run(widget.step())
    state.data[mode.STATE_MESSAGE]["key"] = "an earlier release"
    transport = Transport()
    transport.next_id = 900
    transport.fail["pin"] = lambda: RuntimeError("Bad Request: not enough rights")
    newer, _, _ = make(state=state, transport=transport)
    run(newer.step())
    assert transport.methods() == ["send", "pin"]  # no unpin: nothing would be pinned
    pending = state.data[mode.STATE_MESSAGE]
    assert pending["message_id"] == pending["unpinned"] == 901 and pending["old"] == 501
    del transport.fail["pin"]
    run(newer.step())
    assert transport.methods()[-3:] == ["edit", "pin", "unpin"]
    assert transport.calls[-1][1]["message_id"] == 501
    assert state.data[mode.STATE_MESSAGE]["unpinned"] is None


def test_a_pending_pin_outlives_a_restart() -> None:
    transport = Transport()
    transport.fail["pin"] = lambda: RuntimeError("Bad Request: not enough rights")
    widget, _, state = make(transport=transport)
    run(widget.step())
    again, transport2, _ = make(state=state)
    run(again.step())
    assert transport2.methods() == ["edit", "pin"]


def test_flood_control_on_the_pin_waits_and_the_pin_stays_pending() -> None:
    transport = Transport()
    transport.fail["pin"] = lambda: RetryAfter(17)
    widget, _, state = make(transport=transport)
    assert run(widget.step()) == 17.0
    assert state.data[mode.STATE_MESSAGE]["unpinned"] == 501
    del transport.fail["pin"]
    run(widget.step())
    assert transport.methods()[-2:] == ["edit", "pin"]
    assert state.data[mode.STATE_MESSAGE]["unpinned"] is None


def test_a_path_that_cannot_pin_says_so_once(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO)
    widget, transport, state = make(transport=Transport(can_pin=False))
    run(widget.step())
    run(widget.step())
    assert transport.methods() == ["send", "pin", "edit"]
    assert caplog.text.count("cannot pin") == 1
    assert state.data[mode.STATE_MESSAGE]["unpinned"] is None


def test_a_message_gone_is_sent_again() -> None:
    widget, transport, state = make()
    run(widget.step())
    transport.fail["edit"] = lambda: RuntimeError("Bad Request: message to edit not found")
    assert run(widget.step()) == 300.0
    assert mode.STATE_MESSAGE in state.data and state.data[mode.STATE_MESSAGE] is None
    del transport.fail["edit"]
    run(widget.step())
    assert transport.methods()[-2:] == ["send", "pin"]


def test_flood_control_waits_as_long_as_telegram_says() -> None:
    widget, transport, _ = make()
    run(widget.step())
    transport.fail["edit"] = lambda: RetryAfter(42)
    assert run(widget.step()) == 42.0


def test_an_edit_that_changes_nothing_is_no_failure(caplog: pytest.LogCaptureFixture) -> None:
    widget, transport, _ = make()
    run(widget.step())
    transport.fail["edit"] = lambda: RuntimeError("Bad Request: message is not modified")
    assert run(widget.step()) == 300.0
    assert "failed" not in caplog.text


def test_a_refused_capability_stops_the_loop_with_one_warning(
    caplog: pytest.LogCaptureFixture,
) -> None:
    transport = Transport()
    transport.fail["send"] = lambda: Refused("grant it")
    widget, _, _ = make(transport=transport)
    sleeps: list[float] = []

    async def sleep(seconds: float) -> None:
        sleeps.append(seconds)

    widget._sleep = sleep
    rounds = iter([True, True, True, False])  # a loop that would go on ends here, not hangs
    run(widget.run(lambda: next(rounds), lambda: True))
    assert transport.methods() == ["send"] and sleeps == []
    assert caplog.text.count("grant it") == 1
    assert widget.stopped
    run(widget.on_tap(Tap("q", CHAT, CHAT, 501, "refresh")))
    assert transport.methods() == ["send"]


def test_the_loop_waits_for_the_path_and_ends_with_its_registration() -> None:
    widget, transport, _ = make()
    ready = iter([False, False, True])
    serves = iter([True, True, True, False])
    sleeps: list[float] = []

    async def sleep(seconds: float) -> None:
        sleeps.append(seconds)

    widget._sleep = sleep
    run(widget.run(lambda: next(serves), lambda: next(ready)))
    assert sleeps == [mode.WAIT_READY_SECONDS, mode.WAIT_READY_SECONDS, 300.0]
    assert transport.methods() == ["send", "pin"]


def test_a_broken_state_never_makes_the_widget_send_twice(caplog: pytest.LogCaptureFixture) -> None:
    state = State()
    state.broken = True
    widget, transport, _ = make(state=state)
    run(widget.step())
    run(widget.step())
    run(widget.step())
    assert transport.methods() == ["send", "pin", "edit", "edit"]
    assert "state not saved" in caplog.text


def test_a_failed_collection_keeps_the_last_screen() -> None:
    calls = iter([screen("Healthy"), RuntimeError("boom")])

    async def collect() -> View:
        item = next(calls)
        if isinstance(item, Exception):
            raise item
        return item

    widget, transport, _ = make(source=collect)
    run(widget.step())
    run(widget.step())
    assert json.loads(transport.calls[-1][1]["chat_data"])["h"] == "Healthy"


def test_a_screen_two_periods_old_says_stale_data() -> None:
    clock = Clock()
    calls = iter([screen("Healthy"), RuntimeError("down"), RuntimeError("down")])

    async def collect() -> View:
        item = next(calls)
        if isinstance(item, Exception):
            raise item
        return item

    widget, transport, _ = make(source=collect, clock=clock)
    run(widget.step())
    clock.now += 2 * 300 + 1
    run(widget.step())
    packet = json.loads(transport.calls[-1][1]["chat_data"])
    assert (packet["st"], packet["h"]) == ("unknown", packets.STALE)


def test_no_collection_yet_is_a_screen_that_says_so() -> None:
    async def collect() -> View:
        raise RuntimeError("never")

    widget, transport, _ = make(source=collect)
    run(widget.step())
    packet = json.loads(transport.calls[0][1]["chat_data"])
    assert packet["st"] == "unknown" and packet["h"] == "Dashboard data unavailable"


def test_while_the_gateway_comes_up_the_next_tick_is_soon() -> None:
    settling = View("unknown", "Gateway starting", settling=True)
    widget, _, _ = make(view=settling)
    assert run(widget.step()) == mode.SETTLE_DELAY
    widget.started -= mode.SETTLE_SECONDS + 1
    assert run(widget.step()) == 300.0


# ----------------------------------------------------------------------------- taps


def answers(transport: Transport) -> list[dict[str, Any]]:
    return [json.loads(params["data"]) for method, params in transport.calls if method == "answer"]


def sent_widget() -> tuple[Widget, Transport, State]:
    widget, transport, state = make()
    run(widget.step())
    transport.calls.clear()
    return widget, transport, state


def test_a_refresh_is_answered_with_a_fresh_packet_and_written() -> None:
    widget, transport, _ = sent_widget()
    run(widget.on_tap(Tap("q1", CHAT, CHAT, 501, "refresh")))
    assert transport.methods() == ["answer", "edit"]
    assert answers(transport)[0]["n"] == json.loads(transport.calls[1][1]["chat_data"])["n"]


def test_the_details_and_a_cron_group_are_answered_at_once_and_nothing_is_written() -> None:
    widget, transport, _ = sent_widget()
    run(widget.on_tap(Tap("q1", CHAT, CHAT, 501, "details")))
    run(widget.on_tap(Tap("q2", CHAT, CHAT, 501, "cron:Every 30 min")))
    run(widget.on_tap(Tap("q3", CHAT, CHAT, 501, "cron:paused")))
    run(widget.on_tap(Tap("q4", CHAT, CHAT, 501, "cron:Weekly")))
    assert transport.methods() == ["answer"] * 4
    details, group, paused, gone = answers(transport)
    assert details["d"][0][0] == "Telegram"
    assert group["r"][0] == ["job 0", "10:00"]
    assert paused["r"] == [["p", "Nov 2"]]
    assert gone == {"e": "gone"}


def test_a_theme_is_kept_answered_with_its_colours_and_written() -> None:
    widget, transport, state = sent_widget()
    run(widget.on_tap(Tap("q1", CHAT, CHAT, 501, "theme:neon")))
    assert transport.methods() == ["answer", "edit"]
    reply = answers(transport)[0]
    assert (reply["th"], reply["pl"], reply["nx"]) == (
        "neon",
        packets.palette("neon"),
        packets.next_theme("neon"),
    )
    written = json.loads(transport.calls[1][1]["chat_data"])
    assert written == reply  # the answer is the screen the message gets, numbered
    assert state.data[mode.STATE_THEME] == {"name": "neon", "default": "telegram"}
    again, transport2, _ = make(state=state)
    run(again.step())
    assert json.loads(transport2.calls[0][1]["chat_data"])["th"] == "neon"


def test_a_tick_built_before_a_theme_choice_is_older_than_the_answer() -> None:
    widget, transport, _ = sent_widget()
    before = widget.packet()  # a tick's packet, built under the old theme
    run(widget.on_tap(Tap("q1", CHAT, CHAT, 501, "theme:neon")))
    reply = answers(transport)[0]
    assert before["th"] == "telegram" and reply["n"] > before["n"]  # the widget keeps the newer


def test_waiting_for_the_path_is_said_once_after_a_minute(caplog: pytest.LogCaptureFixture) -> None:
    widget, _, _ = make()
    rounds = iter([True] * 130 + [False])

    async def sleep(seconds: float) -> None:
        return None

    widget._sleep = sleep
    run(widget.run(lambda: next(rounds), lambda: False))
    assert caplog.text.count("still waiting for Telegram to connect") == 1


def test_a_new_default_theme_in_the_settings_wins_over_an_old_choice() -> None:
    widget, _, state = sent_widget()
    run(widget.on_tap(Tap("q1", CHAT, CHAT, 501, "theme:ember")))
    newer, transport, _ = make(state=state, theme="mono")
    run(newer.step())
    assert json.loads(transport.calls[0][1]["chat_data"])["th"] == "mono"


@pytest.mark.parametrize(
    ("tap", "refusal"),
    [
        (Tap("q", CHAT, 42, 501, "refresh"), "forbidden"),  # another sender
        (Tap("q", 42, None, 501, "refresh"), "forbidden"),  # another chat (the public event)
        (Tap("q", CHAT, CHAT, 400, "refresh"), "stale"),  # an older widget
        (Tap("q", CHAT, CHAT, 501, "theme:sunset"), "unknown"),
        (Tap("q", CHAT, CHAT, 501, "cron:"), "unknown"),
        (Tap("q", CHAT, CHAT, 501, "cron:" + "x" * 64), "unknown"),
        (Tap("q", CHAT, CHAT, 501, 5), "unknown"),
    ],
)
def test_a_tap_that_is_not_the_owners_or_not_known_is_refused(tap: Tap, refusal: str) -> None:
    widget, transport, _ = sent_widget()
    run(widget.on_tap(tap))
    assert transport.methods() == ["answer"]
    assert answers(transport) == [{"e": refusal}]


def test_the_public_event_without_a_sender_is_the_owners_in_his_chat() -> None:
    widget, transport, _ = sent_widget()
    run(widget.on_tap(Tap("q", CHAT, None, 501, "details")))
    assert "e" not in answers(transport)[0]


def test_a_slow_refresh_is_answered_with_the_screen_at_hand_and_written_when_it_ends(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(mode, "REFRESH_WAIT", 0.01)

    async def scenario() -> list[str]:
        gate = asyncio.Event()
        views = iter([screen("Healthy"), screen("Slow")])

        async def collect() -> View:
            view = next(views)
            if view.headline == "Slow":
                await gate.wait()
            return view

        widget, transport, _ = make(source=collect)
        await widget.step()
        transport.calls.clear()
        tap = asyncio.ensure_future(widget.on_tap(Tap("q", CHAT, CHAT, 501, "refresh")))
        await asyncio.sleep(0.05)
        first = answers(transport)
        assert first and first[0]["h"] == "Healthy"  # answered before the collection ended
        gate.set()
        await tap
        return [json.loads(p["chat_data"])["h"] for m, p in transport.calls if m == "edit"]

    assert run(scenario()) == ["Slow"]


def test_an_answer_that_fails_is_logged_and_the_tap_ends(caplog: pytest.LogCaptureFixture) -> None:
    widget, transport, _ = sent_widget()
    transport.fail["answer"] = lambda: RuntimeError("Bad Request: query is too old")
    run(widget.on_tap(Tap("q", CHAT, CHAT, 501, "details")))
    assert "answer failed: RuntimeError" in caplog.text


def test_outcome_reads_only_the_class_and_telegrams_phrases() -> None:
    assert mode.outcome(Refused("x")) == "refused"
    assert mode.outcome(RetryAfter(3)) == "wait"
    assert (
        mode.outcome(RuntimeError("send_widget: action_failed: Too Many Requests: retry after 7"))
        == "wait"
    )
    assert mode.outcome(RuntimeError("Bad Request: message is not modified")) == "done"
    assert mode.outcome(RuntimeError("Bad Request: MESSAGE_ID_INVALID")) == "gone"
    assert mode.outcome(RuntimeError("something else")) == "failed"
    assert mode.retry_seconds(RetryAfter(9)) == 9.0
    assert mode.retry_seconds(RuntimeError("no number")) == mode.DEFAULT_RETRY_SECONDS


# ----------------------------------------------------------------------------- closed, refused, unpinned


def newer_release(transport: Transport) -> tuple[Widget, State]:
    """A widget of another release over a chat that holds the earlier one's message 501."""
    widget, _, state = make()
    run(widget.step())
    state.data[mode.STATE_MESSAGE]["key"] = "an earlier release"
    transport.next_id = 900
    newer, _, _ = make(state=state, transport=transport)
    return newer, state


def test_a_closed_widget_cancels_its_collection_and_writes_nothing() -> None:
    async def scenario() -> tuple[float | None, bool, list[str], int]:
        started, cancelled = asyncio.Event(), asyncio.Event()

        async def collect() -> View:
            started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cancelled.set()
                raise
            return screen()

        widget, transport, state = make(source=collect)
        tick = asyncio.ensure_future(widget.step())
        await started.wait()
        widget.close()
        delay = await tick
        return delay, cancelled.is_set(), transport.methods(), state.writes

    assert run(scenario()) == (None, True, [], 0)


def test_a_refresh_in_flight_at_the_close_is_answered_and_writes_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(mode, "REFRESH_WAIT", 0.01)

    async def scenario() -> tuple[list[str], int]:
        slow = False

        async def collect() -> View:
            if slow:
                await asyncio.Event().wait()
            return screen()

        widget, transport, state = make(source=collect)
        await widget.step()
        transport.calls.clear()
        writes, slow = state.writes, True
        tap = asyncio.ensure_future(widget.on_tap(Tap("q", CHAT, CHAT, 501, "refresh")))
        await asyncio.sleep(0.05)  # answered with the screen at hand; the collection runs on
        widget.close()
        await tap  # ends, never raises the collection's cancellation
        return transport.methods(), state.writes - writes

    assert run(scenario()) == (["answer"], 0)


def test_a_refused_answer_stops_the_widget_with_one_warning(
    caplog: pytest.LogCaptureFixture,
) -> None:
    widget, transport, _ = sent_widget()
    transport.fail["answer"] = lambda: Refused("grant it")
    run(widget.on_tap(Tap("q", CHAT, CHAT, 501, "details")))
    run(widget.on_tap(Tap("q2", CHAT, CHAT, 501, "details")))
    assert widget.stopped and caplog.text.count("grant it") == 1
    calls = len(transport.calls)
    rounds = iter([True, True, False])  # a loop that ignored the stop would tick here
    run(widget.run(lambda: next(rounds), lambda: True))
    assert len(transport.calls) == calls


def test_a_failed_unpin_keeps_the_old_message_and_is_tried_again_after_a_restart() -> None:
    transport = Transport()
    transport.fail["unpin"] = lambda: RuntimeError("Bad Request: timed out")
    newer, state = newer_release(transport)
    run(newer.step())
    assert transport.methods() == ["send", "pin", "unpin"]
    stored = state.data[mode.STATE_MESSAGE]
    assert stored["message_id"] == 901 and stored["unpinned"] is None and stored["old"] == 501
    again, transport2, _ = make(state=state)
    run(again.step())
    assert transport2.methods() == ["edit", "unpin"]  # no second pin
    assert transport2.calls[-1][1]["message_id"] == 501
    assert state.data[mode.STATE_MESSAGE]["old"] is None


def test_an_unpin_that_keeps_failing_is_left_to_the_owner_with_one_warning(
    caplog: pytest.LogCaptureFixture,
) -> None:
    transport = Transport()
    transport.fail["unpin"] = lambda: RuntimeError("Bad Request: not enough rights")
    newer, state = newer_release(transport)
    for _ in range(mode.UNPIN_TRIES + 2):
        run(newer.step())
    assert transport.methods().count("unpin") == mode.UNPIN_TRIES
    assert state.data[mode.STATE_MESSAGE]["old"] is None
    assert caplog.text.count("old widget 501 left pinned") == 1


def test_an_old_message_already_gone_needs_no_unpin() -> None:
    transport = Transport()
    transport.fail["unpin"] = lambda: RuntimeError("Bad Request: message not found")
    newer, state = newer_release(transport)
    run(newer.step())
    run(newer.step())
    assert transport.methods().count("unpin") == 1
    assert state.data[mode.STATE_MESSAGE]["old"] is None
