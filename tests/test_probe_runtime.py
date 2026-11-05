"""The probe plugin's own logic on fakes: no engine, no network, runs in the ordinary suite.

``tests/test_probe_plugin.py`` proves the chain against the real engine objects and is skipped
without Hermes. The instruments below (a failed tick reaching the record, the period sanity check)
are plugin-internal, so they are pinned here where every run exercises them. The screen itself
is covered by ``tests/test_probe_screen.py``; the fakes live in ``probe_fakes.py``.
"""

from __future__ import annotations

import asyncio
import math
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from probe_fakes import (
    CHAT,
    FakeAdapter,
    FakeContext,
    KwargsOnlyAdapter,
    PlainOnlyAdapter,
    PublicHtmlAdapter,
    load_plugin,
    until,
)

_load_plugin = load_plugin
_until = until


def test_the_html_verb_is_read_off_the_signature_never_off_a_version() -> None:
    """A parameter named exactly ``parse_mode``, positional-or-keyword or keyword-only, on a
    coroutine function: that is "takes a parse mode". ``**kwargs``, a missing parameter, a plain
    function or no verb at all are not."""
    plugin = _load_plugin()
    takes = plugin._takes_parse_mode

    async def positional(chat: str, message_id: str, text: str, parse_mode: Any = None) -> None:
        pass

    async def keyword_only(
        chat: str, message_id: str, text: str, *, finalize: bool = False, parse_mode: Any = None
    ) -> None:
        pass

    async def swallows(chat: str, message_id: str, text: str, **kwargs: Any) -> None:
        pass

    async def without(chat: str, message_id: str, text: str, *, finalize: bool = False) -> None:
        pass

    def plain_function(chat: str, message_id: str, text: str, parse_mode: Any = None) -> None:
        pass

    assert takes(positional) and takes(keyword_only)
    assert not takes(swallows) and not takes(without) and not takes(plain_function)
    assert not takes(None) and not takes(object()) and not takes(print)

    assert plugin._html_verb(PublicHtmlAdapter())[0] == "edit_message"
    assert plugin._html_verb(FakeAdapter())[0] == "_edit_text"
    assert plugin._html_verb(KwargsOnlyAdapter())[0] == "_edit_text"
    assert plugin._html_verb(PlainOnlyAdapter()) is None


def _settings(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, **overrides: Any) -> dict[str, Any]:
    """A home that is a temp dir and no limits: the tick must never read a real profile or
    call the usage facade from a test, whichever interpreter runs it. HERMES_HOME names the same
    temp dir: deleted, engine code the tick reaches would resolve the real profile."""
    for name in ("HERMES_DASHBOARD_PROBE_CHAT", "HERMES_DASHBOARD_PROBE_PERIOD"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    return {
        "chat_id": CHAT,
        "period_seconds": 0.01,
        "hermes_home": str(tmp_path),
        "limits_enabled": False,
        **overrides,
    }


def test_a_failed_tick_reaches_the_record_and_the_loop_survives(
    monkeypatch, tmp_path: Path
) -> None:
    """``last_status`` must not stay ``edited`` while every tick raises: that is a dead updater
    that looks alive to anyone reading the state file."""
    plugin = _load_plugin()
    ctx = FakeContext(_settings(monkeypatch, tmp_path))
    runtime = plugin.register(ctx)
    assert runtime is not None
    adapter = FakeAdapter()

    async def scenario() -> None:
        runtime.wire(object(), adapter)
        await _until(lambda: runtime.record.get("last_status") == "edited")

        async def boom(*args: Any, **kwargs: Any) -> Any:
            raise RuntimeError("chat_id=-100999 secret detail")

        # Both verbs raise: the HTML one is caught and answered with the plain one, whose
        # exception is the tick's own failure.
        adapter._edit_text = boom  # type: ignore[method-assign]
        adapter.edit_message = boom  # type: ignore[method-assign]
        await _until(lambda: runtime.record.get("last_status") == "tick_failed")
        assert runtime.record["last_error"] == "RuntimeError"  # class only, no message text
        assert ctx.state.data["probe"]["last_status"] == "tick_failed"

        adapter._edit_text = FakeAdapter._edit_text.__get__(adapter)  # type: ignore[method-assign]
        adapter.edit_message = FakeAdapter.edit_message.__get__(adapter)  # type: ignore[method-assign]
        await _until(lambda: runtime.record.get("last_status") == "edited")
        runtime.task.cancel()

    asyncio.run(scenario())


@pytest.mark.parametrize("raw", ["inf", "-inf", "nan", "1e400"])
def test_non_finite_period_falls_back_to_the_default(monkeypatch, tmp_path: Path, raw: str) -> None:
    """``float("inf")`` is a valid float: ``sleep(inf)`` parks the loop forever while the old
    message stays on screen."""
    plugin = _load_plugin()
    settings = plugin.read_settings(
        FakeContext(_settings(monkeypatch, tmp_path, period_seconds=raw))
    )

    assert settings is not None
    assert math.isfinite(settings.period_seconds)
    assert settings.period_seconds == plugin.DEFAULT_PERIOD_SECONDS


def test_period_is_clamped_to_a_sane_range(monkeypatch, tmp_path: Path) -> None:
    plugin = _load_plugin()
    huge = plugin.read_settings(FakeContext(_settings(monkeypatch, tmp_path, period_seconds="1e9")))
    tiny = plugin.read_settings(FakeContext(_settings(monkeypatch, tmp_path, period_seconds="-5")))

    assert huge is not None and huge.period_seconds == plugin.MAX_PERIOD_SECONDS
    assert tiny is not None and tiny.period_seconds == plugin.MIN_PERIOD_SECONDS


def test_an_adapter_never_handed_to_the_registration_is_never_used(
    monkeypatch, tmp_path: Path
) -> None:
    """A served secondary profile's adapter under the multiplex: its runner's map publishes the
    launch profile's adapter for the platform. The screen goes through the profile's own adapter;
    while that one is down the tick waits rather than borrow another profile's bot; a replacement
    handed to the registration (a reconnect's ``connect()``) is used."""
    plugin = _load_plugin()
    runtime = plugin.register(FakeContext(_settings(monkeypatch, tmp_path)))
    assert runtime is not None
    launch, own = FakeAdapter(), FakeAdapter()
    own.gateway_runner = launch.gateway_runner  # adapters["telegram"] is the launch profile's

    async def scenario() -> None:
        runtime.wire(object(), own)
        await _until(lambda: len(own.html_edits) >= 1)
        assert runtime.live_adapter() is own and launch.texts == []

        own.is_connected = False
        assert runtime.live_adapter() is None  # waiting, not the launch profile's bot
        replacement = FakeAdapter()
        replacement.gateway_runner = launch.gateway_runner
        runtime.wire(object(), replacement)
        assert runtime.live_adapter() is replacement
        launch.gateway_runner.adapters["telegram"] = replacement  # the runner publishes it now
        assert runtime.live_adapter() is replacement
        runtime.task.cancel()

    asyncio.run(scenario())
    assert launch.texts == []


def test_the_launch_profile_takes_up_an_adapter_the_runner_published_unhanded(
    monkeypatch, tmp_path: Path
) -> None:
    """The launch profile's registration (an adapter handed to it is the one the runner
    publishes) takes up a connected published adapter it was never handed: Telegram reconnected
    inside a reload, with no factory of the plugin to call. A secondary profile's registration
    never does (``test_an_adapter_never_handed_to_the_registration_is_never_used``)."""
    plugin = _load_plugin()
    runtime = plugin.register(FakeContext(_settings(monkeypatch, tmp_path)))
    assert runtime is not None
    first = FakeAdapter()  # its runner publishes it: the launch profile's adapter

    async def scenario() -> None:
        runtime.wire(object(), first)
        await _until(lambda: len(first.html_edits) >= 1)
        assert runtime.primary
        replacement = FakeAdapter()
        first.is_connected = False
        first.gateway_runner.adapters["telegram"] = replacement  # published, never wired

        await _until(lambda: len(replacement.html_edits) >= 1)

        assert runtime.latest is replacement and runtime.generation_of(replacement) == 2
        assert runtime.record["adapter_generation"] == 2 and replacement.sent == []
        runtime.task.cancel()

    asyncio.run(scenario())


class _SlotAdapter:
    """Just what ``live_adapter`` reads, in slots: a freed one's address comes back on the very
    next one allocated (pymalloc), which is how a gone adapter's ``id`` reappears."""

    __slots__ = ("__weakref__", "gateway_runner", "is_connected", "platform")

    def __init__(self, runner: Any) -> None:
        self.gateway_runner, self.platform, self.is_connected = runner, "telegram", True


def _same_address(old: int, runner: Any) -> _SlotAdapter | None:
    kept: list[_SlotAdapter] = []
    for _ in range(10_000):
        candidate = _SlotAdapter(runner)
        if id(candidate) == old:
            return candidate
        kept.append(candidate)
    return None


def test_a_gone_adapter_s_id_on_another_object_does_not_make_it_handed(
    monkeypatch, tmp_path: Path
) -> None:
    """A registration that is not the launch profile's was handed an adapter that is gone now;
    the runner publishes a new object at the same address (the same ``id``), connected. It is
    no adapter of this registration: the tick waits rather than send through it."""
    plugin = _load_plugin()
    runtime = plugin.register(FakeContext(_settings(monkeypatch, tmp_path)))
    assert runtime is not None
    runner = SimpleNamespace(adapters={})

    async def scenario() -> None:
        gone: Any = _SlotAdapter(runner)
        runtime.wire(object(), gone)
        runtime.task.cancel()  # before its first tick
        later = _SlotAdapter(runner)
        later.is_connected = False
        runtime.wire(object(), later)  # a reconnect: the registration moves on
        old = id(gone)
        del gone

        stranger = _same_address(old, runner)
        assert stranger is not None, "the freed adapter's address did not come back"
        runner.adapters["telegram"] = stranger

        assert runtime.live_adapter() is None and not runtime.primary
        assert runtime.generation_of(stranger) == 0

    asyncio.run(scenario())
