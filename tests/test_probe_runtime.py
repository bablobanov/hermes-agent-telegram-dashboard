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
    call the usage facade from a test, whichever interpreter runs it."""
    for name in ("HERMES_DASHBOARD_PROBE_CHAT", "HERMES_DASHBOARD_PROBE_PERIOD", "HERMES_HOME"):
        monkeypatch.delenv(name, raising=False)
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
