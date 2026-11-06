"""The probe plugin's own logic on fakes: no engine, no network, runs in the ordinary suite.

``tests/test_probe_plugin.py`` proves the chain against the real engine objects and is skipped
without Hermes. The instruments below (a failed tick reaching the record, the period sanity check)
are plugin-internal, so they are pinned here where every run exercises them. The screen itself
is covered by ``tests/test_probe_screen.py``; the fakes live in ``probe_fakes.py``.
"""

from __future__ import annotations

import asyncio
import math
import time
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


# ------------------------------------------------------------------ whose bot sent the message
# Fixture numbers for three bots: the launch profile's, a served profile's, and that profile's
# bot after its owner replaced it with another bot (a new bot, so a new id).
LAUNCH_BOT, ALPHA_BOT, NEW_ALPHA_BOT = 1111, 2222, 3333
PROBE_LOGGER = "hermes.plugins.telegram_dashboard_probe"


def _record(ctx: FakeContext, **fields: Any) -> None:
    """A record that names message 4242 in the configured chat, as an earlier release left it."""
    ctx.state.set(
        "probe", {"message_id": "4242", "chat_id": CHAT, "last_status": "edited", **fields}
    )


def _served(launch: FakeAdapter, *adapters: FakeAdapter) -> None:
    """Adapters of a served profile under the multiplex: their runner's map publishes the launch
    profile's adapter for the platform, never theirs, and the runner's verdict says it serves
    several profiles (``config.multiplex_profiles``, what ``GatewayRunner._multiplex_on`` reads)."""
    launch.gateway_runner.config = SimpleNamespace(multiplex_profiles=True)
    for adapter in adapters:
        adapter.gateway_runner = launch.gateway_runner


def _one_profile_runner(adapters: dict[str, Any]) -> SimpleNamespace:
    """The runner of a gateway that serves one profile: the engine settles the verdict at boot
    (``resolve_multiplex_mode``: a single-profile home has nothing to multiplex) and starts no
    secondary profile's adapters then."""
    return SimpleNamespace(adapters=adapters, config=SimpleNamespace(multiplex_profiles=False))


def _started_ago(runtime: Any, seconds: float) -> None:
    """The registration as if it had started ``seconds`` ago, on the clock the wait for a
    message's sender is counted on."""
    runtime.started_monotonic = time.monotonic() - seconds


def test_the_bot_s_id_is_read_off_the_adapter_s_bot_and_nothing_else() -> None:
    """The id of the bot behind an adapter is PTB's ``Bot.id``: the number getMe answered when the
    bot started, kept on the object (no request). A bot that has not started raises there, as
    PTB's does; a mock's attribute is no number; without a bot there is nothing to read."""
    plugin = _load_plugin()

    class Unstarted:
        @property
        def id(self) -> int:
            raise RuntimeError("ExtBot is not properly initialized")

    assert plugin._bot_id(FakeAdapter(bot_id=LAUNCH_BOT)) == LAUNCH_BOT
    assert plugin._bot_id(FakeAdapter()) is None
    for bot in (Unstarted(), SimpleNamespace(id=object()), SimpleNamespace(id=True)):
        adapter = FakeAdapter()
        adapter._bot = bot  # type: ignore[attr-defined]
        assert plugin._bot_id(adapter) is None
    for value in (0, -5, "1111"):
        assert plugin._bot_id(FakeAdapter(bot_id=value)) is None  # type: ignore[arg-type]


def test_a_message_another_bot_sent_is_never_edited_and_a_new_one_is_sent(
    monkeypatch, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """The record says which bot sent its message; this profile's bot is another. A bot cannot
    edit another bot's message, and in a private chat the same number names one of its own (an
    agent's reply), which an edit would overwrite and report as a healthy screen. The tick
    treats the message as lost: it says why, sends a new one and keeps its sender."""
    plugin = _load_plugin()
    ctx = FakeContext(_settings(monkeypatch, tmp_path))
    _record(ctx, bot_id=LAUNCH_BOT)
    runtime = plugin.register(ctx)
    assert runtime is not None
    launch, own = FakeAdapter(bot_id=LAUNCH_BOT), FakeAdapter(bot_id=ALPHA_BOT, message_id="202")
    _served(launch, own)

    async def scenario() -> None:
        runtime.wire(object(), own)
        await _until(lambda: len(own.html_edits) >= 1)
        runtime.task.cancel()

    with caplog.at_level("INFO", logger=PROBE_LOGGER):
        asyncio.run(scenario())

    assert "4242" not in own.targets and launch.texts == []
    assert own.texts[0] == own.sent[0] and len(own.sent) == 1
    stored = ctx.state.data["probe"]
    assert (stored["message_id"], stored["bot_id"]) == ("202", ALPHA_BOT)
    assert stored["lost_at"] and stored["recreated_at"]
    said = [r.getMessage() for r in caplog.records if "4242" in r.getMessage()]
    assert len(said) == 1 and "another bot" in said[0] and "sending a new one" in said[0], said
    assert str(LAUNCH_BOT) not in caplog.text and str(ALPHA_BOT) not in caplog.text


@pytest.mark.parametrize("past_the_wait", [False, True])
def test_the_launch_profile_keeps_editing_a_message_from_before_the_sender_was_kept(
    monkeypatch, tmp_path: Path, past_the_wait: bool
) -> None:
    """A record from 1.0.0 keeps no sender. The launch profile sent its message through its own
    bot then as now: the message is edited as before and the record gets the bot's id. The wait
    for a sender (``SENDER_WAIT_SECONDS``) does not touch it: its runner publishes its adapter."""
    plugin = _load_plugin()
    ctx = FakeContext(_settings(monkeypatch, tmp_path))
    _record(ctx)
    runtime = plugin.register(ctx)
    assert runtime is not None
    adapter = FakeAdapter(bot_id=LAUNCH_BOT)  # its runner publishes it: the launch profile's
    if past_the_wait:
        _started_ago(runtime, plugin.SENDER_WAIT_SECONDS + 1)

    async def scenario() -> None:
        runtime.wire(object(), adapter)
        await _until(lambda: len(adapter.html_edits) >= 2)
        runtime.task.cancel()

    asyncio.run(scenario())

    assert adapter.sent == [] and set(adapter.targets) == {"4242"}
    stored = ctx.state.data["probe"]
    assert (stored["message_id"], stored["bot_id"]) == ("4242", LAUNCH_BOT)
    assert "lost_at" not in stored


@pytest.mark.parametrize("past_the_wait", [False, True])
@pytest.mark.parametrize("published", ["nothing yet", "an adapter never handed here"])
def test_a_single_profile_gateway_keeps_editing_its_message_from_1_0_0(
    monkeypatch, tmp_path: Path, past_the_wait: bool, published: str
) -> None:
    """The update from 1.0.0 on a gateway that serves one profile, the common case: the record
    names message 4242 and no sender. Such a runner starts no other profile's adapters, so the
    message was sent by this profile's bot: the first tick edits it and keeps the bot's id, with
    no wait and no new message, whatever the runner's map holds yet. Its map may be empty (a
    start before every platform answered, Telegram unreachable at boot, a raised connect bound),
    and may still hold an adapter never handed here (the one a reconnect is replacing). Neither
    tells anything on one profile, before or past ``SENDER_WAIT_SECONDS``."""
    plugin = _load_plugin()
    ctx = FakeContext(_settings(monkeypatch, tmp_path))
    _record(ctx)
    runtime = plugin.register(ctx)
    assert runtime is not None
    adapter = FakeAdapter(bot_id=LAUNCH_BOT, message_id="202")
    stale = FakeAdapter(bot_id=LAUNCH_BOT, message_id="303")
    map_ = {} if published == "nothing yet" else {"telegram": stale}
    adapter.gateway_runner = _one_profile_runner(map_)
    if past_the_wait:
        _started_ago(runtime, plugin.SENDER_WAIT_SECONDS + 1)
    statuses: list[tuple[Any, Any]] = []
    note = runtime._note

    def spy(**fields: Any) -> None:
        statuses.append((fields.get("status"), fields.get("error")))
        note(**fields)

    runtime._note = spy

    async def scenario() -> None:
        runtime.wire(object(), adapter)
        await _until(lambda: len(adapter.html_edits) >= 2)
        runtime.task.cancel()

    asyncio.run(scenario())

    assert adapter.sent == [] and set(adapter.targets) == {"4242"}
    assert stale.texts == [] and stale.targets == []
    assert statuses and all(status == "edited" for status, _ in statuses), statuses
    stored = ctx.state.data["probe"]
    assert (stored["message_id"], stored["bot_id"]) == ("4242", LAUNCH_BOT)
    assert "lost_at" not in stored and stored["last_status"] == "edited"


def test_a_served_profile_never_edits_a_message_from_before_the_sender_was_kept(
    monkeypatch, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A record from 1.0.0 in a served profile of a multiplexing gateway: 1.0.0 sent that
    profile's screen through the launch profile's bot, so the number names another message for
    the profile's own bot. Its registration leaves it alone and sends a new message, which keeps
    the sender; the launch profile's bot is never used."""
    plugin = _load_plugin()
    ctx = FakeContext(_settings(monkeypatch, tmp_path))
    _record(ctx)
    runtime = plugin.register(ctx)
    assert runtime is not None
    launch, own = FakeAdapter(bot_id=LAUNCH_BOT), FakeAdapter(bot_id=ALPHA_BOT, message_id="202")
    _served(launch, own)

    async def scenario() -> None:
        runtime.wire(object(), own)
        await _until(lambda: len(own.html_edits) >= 1)
        runtime.task.cancel()

    with caplog.at_level("INFO", logger=PROBE_LOGGER):
        asyncio.run(scenario())

    assert "4242" not in own.targets and launch.texts == [] and len(own.sent) == 1
    assert set(own.targets) == {"202"}
    stored = ctx.state.data["probe"]
    assert (stored["message_id"], stored["bot_id"]) == ("202", ALPHA_BOT)
    said = [r.getMessage() for r in caplog.records if "4242" in r.getMessage()]
    assert len(said) == 1 and "launch profile" in said[0] and "sending a new one" in said[0]


def test_a_profile_whose_bot_was_replaced_starts_a_new_message(monkeypatch, tmp_path: Path) -> None:
    """The profile's bot is replaced by another (a reconnect with the new bot): the message the
    old bot sent is not the new bot's to edit. The new bot sends its own, and edits that one."""
    plugin = _load_plugin()
    ctx = FakeContext(_settings(monkeypatch, tmp_path))
    runtime = plugin.register(ctx)
    assert runtime is not None
    old = FakeAdapter(bot_id=ALPHA_BOT)  # the launch profile's adapter, message 101

    async def scenario() -> None:
        runtime.wire(object(), old)
        await _until(lambda: len(old.html_edits) >= 1)
        assert runtime.record["bot_id"] == ALPHA_BOT
        new = FakeAdapter(bot_id=NEW_ALPHA_BOT, message_id="202")
        new.gateway_runner = old.gateway_runner
        old.is_connected = False
        old.gateway_runner.adapters["telegram"] = new
        runtime.wire(object(), new)

        await _until(lambda: len(new.html_edits) >= 2)
        runtime.task.cancel()
        assert "101" not in new.targets and new.texts[0] == new.sent[0] and len(new.sent) == 1

    asyncio.run(scenario())

    stored = ctx.state.data["probe"]
    assert (stored["message_id"], stored["bot_id"]) == ("202", NEW_ALPHA_BOT)


@pytest.mark.parametrize("verdict", ["multiplexing", "none told"])
def test_a_message_from_before_the_sender_was_kept_waits_for_the_runner_s_adapters(
    monkeypatch, tmp_path: Path, verdict: str
) -> None:
    """At a start the runner installs the adapters that connected only once every platform has
    answered, so for a while its map is empty. On a gateway that serves several profiles, or
    one whose runner tells no verdict, the launch profile cannot be told from a served one then.
    A record without a sender is neither edited nor replaced: the tick waits. Once the runner
    publishes this adapter, it is the launch profile's: edited and stamped."""
    plugin = _load_plugin()
    ctx = FakeContext(_settings(monkeypatch, tmp_path))
    _record(ctx)
    runtime = plugin.register(ctx)
    assert runtime is not None
    adapter = FakeAdapter(bot_id=LAUNCH_BOT)
    runner = SimpleNamespace(adapters={})
    if verdict == "multiplexing":
        runner.config = SimpleNamespace(multiplex_profiles=True)
    adapter.gateway_runner = runner

    async def scenario() -> None:
        runtime.wire(object(), adapter)
        await _until(lambda: runtime.record.get("ticks", 0) >= 3)
        assert runtime.record["last_status"] == "waiting"
        assert adapter.targets == [] and adapter.sent == []
        runner.adapters["telegram"] = adapter

        await _until(lambda: len(adapter.html_edits) >= 1)
        runtime.task.cancel()

    asyncio.run(scenario())

    assert adapter.sent == [] and set(adapter.targets) == {"4242"}
    assert ctx.state.data["probe"]["bot_id"] == LAUNCH_BOT


def test_a_message_from_before_the_sender_was_kept_is_replaced_once_the_wait_runs_out(
    monkeypatch, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A served profile on a gateway whose launch profile installs no adapter at all (under the
    multiplex a platform whose token is only in a secondary profile's ``.env`` is skipped for it,
    or its only platform failed at start): the runner's map stays empty and never tells whose bot
    sent a record without a sender. Within ``SENDER_WAIT_SECONDS`` of the registration's start
    the tick waits, editing and sending nothing. Past it the message is taken as another bot's:
    the tick says why, sends a new one from the profile's own bot and keeps that bot's id. The
    old message is never edited."""
    plugin = _load_plugin()
    ctx = FakeContext(_settings(monkeypatch, tmp_path))
    _record(ctx)
    runtime = plugin.register(ctx)
    assert runtime is not None
    own = FakeAdapter(bot_id=ALPHA_BOT, message_id="202")
    own.gateway_runner = SimpleNamespace(  # the launch profile installs nothing
        adapters={}, config=SimpleNamespace(multiplex_profiles=True)
    )

    async def scenario() -> None:
        _started_ago(runtime, plugin.SENDER_WAIT_SECONDS - 60)
        runtime.wire(object(), own)
        await _until(lambda: runtime.ticks >= 3)
        assert runtime.record["last_status"] == "waiting"
        assert runtime.record["last_error"] == plugin.SENDER_UNKNOWN
        assert own.targets == [] and own.sent == []

        _started_ago(runtime, plugin.SENDER_WAIT_SECONDS + 1)
        waited = runtime.ticks
        await _until(lambda: runtime.ticks >= waited + 3)
        runtime.task.cancel()

    with caplog.at_level("INFO", logger=PROBE_LOGGER):
        asyncio.run(scenario())

    assert len(own.sent) == 1 and own.texts[0] == own.sent[0]
    assert "4242" not in own.targets and set(own.targets) == {"202"}
    stored = ctx.state.data["probe"]
    assert (stored["message_id"], stored["bot_id"]) == ("202", ALPHA_BOT)
    assert stored["last_status"] == "edited" and stored["lost_at"] and stored["recreated_at"]
    said = [r.getMessage() for r in caplog.records if "4242" in r.getMessage()]
    assert len(said) == 1 and "no sender" in said[0] and "sending a new one" in said[0], said
    assert str(ALPHA_BOT) not in caplog.text
