"""The plugin tick delivers the first screen instead of ``tick N``: on fakes, no engine.

Load-bearing property, from the task card: a tick whose collection or render fails must STILL
edit the message. Leaving yesterday's text on a pinned dashboard is worse than an empty screen,
because it looks exactly like a calm system.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
from datetime import UTC
from pathlib import Path
from typing import Any

import pytest
from probe_fakes import CHAT, PLUGIN_DIR, FakeAdapter, FakeContext, load_plugin, until

from telegram_dashboard.render import TELEGRAM_TEXT_LIMIT
from telegram_dashboard.states import all_states

_STATUS_LABELS = {
    "normal": "🟢 Норма",
    "warning": "🟡 Требует внимания",
    "critical": "🔴 Требует внимания",
    "unknown": "⚪ Состояние неизвестно",
}
STATES = all_states()


def _utf16_units(text: str) -> int:
    return len(text.encode("utf-16-le")) // 2


def _home(tmp_path: Path) -> Path:
    # Our own pid: alive on any host where liveness can be asked, so the screen's content does
    # not depend on what else happens to run on the machine.
    payload = {
        "pid": os.getpid(),
        "gateway_state": "running",
        "updated_at": "2026-09-09T20:58:00+00:00",
        "platforms": {"telegram": {"state": "running", "writer_pid": 4242}},
    }
    (tmp_path / "gateway_state.json").write_text(json.dumps(payload), encoding="utf-8")
    return tmp_path


def _settings(monkeypatch: pytest.MonkeyPatch, home: Path, **overrides: Any) -> dict[str, Any]:
    for name in (
        "HERMES_DASHBOARD_PROBE_CHAT",
        "HERMES_DASHBOARD_PROBE_PERIOD",
        "HERMES_DASHBOARD_PROBE_DRIFT_REPORT",
        "HERMES_DASHBOARD_PROBE_LIMITS",
        "HERMES_DASHBOARD_PROBE_LIMITS_REFRESH",
        "HERMES_DASHBOARD_PROBE_TZ",
        "HERMES_HOME",
    ):
        monkeypatch.delenv(name, raising=False)
    return {
        "chat_id": CHAT,
        "period_seconds": 0.01,
        "hermes_home": str(home),
        "limits_enabled": False,
        **overrides,
    }


def _run(runtime: Any, adapter: FakeAdapter, scenario: Any) -> None:
    async def body() -> None:
        runtime.wire(object(), adapter)
        try:
            await scenario()
        finally:
            runtime.task.cancel()

    asyncio.run(body())


def test_the_tick_delivers_the_screen_not_a_counter(monkeypatch, tmp_path: Path) -> None:
    plugin = load_plugin()
    ctx = FakeContext(_settings(monkeypatch, _home(tmp_path)))
    runtime = plugin.register(ctx)
    assert runtime is not None
    adapter = FakeAdapter()

    async def scenario() -> None:
        await until(lambda: len(adapter.edits) >= 1)

    _run(runtime, adapter, scenario)

    text = adapter.edits[-1]
    assert text.splitlines()[0] == "HERMES DASHBOARD"
    assert "Данные: " in text and "Сообщение подтверждено: " in text
    assert "Обновлено: " in text
    assert "Gateway: " in text
    assert "ЛИМИТЫ" in text and "нет данных" in text  # limits disabled: named, never zero
    assert "dashboard probe · tick" not in text
    assert "#" not in text
    assert _utf16_units(text) <= TELEGRAM_TEXT_LIMIT
    assert ctx.state.data["probe"]["last_status"] == "edited"


@pytest.mark.parametrize("stage", ["collector", "render"])
def test_a_failed_stage_still_edits_the_message_with_a_loud_stamp(
    monkeypatch, tmp_path: Path, stage: str
) -> None:
    """Yesterday's text must not survive a tick whose screen could not be built, whether the
    collection or the render is what failed."""
    plugin = load_plugin()
    ctx = FakeContext(_settings(monkeypatch, _home(tmp_path)))
    runtime = plugin.register(ctx)
    assert runtime is not None
    adapter = FakeAdapter()
    real_collector = runtime.collector
    real_render = runtime.dashboard.render.render_dashboard

    async def boom_collector(now: Any) -> Any:
        raise RuntimeError("chat_id=-100999 in the message")

    def boom_render(*args: Any, **kwargs: Any) -> str:
        raise RuntimeError("chat_id=-100999 in the message")

    def break_it() -> None:
        if stage == "collector":
            runtime.collector = boom_collector
        else:
            monkeypatch.setattr(runtime.dashboard.render, "render_dashboard", boom_render)

    def mend_it() -> None:
        runtime.collector = real_collector
        monkeypatch.setattr(runtime.dashboard.render, "render_dashboard", real_render)

    async def scenario() -> None:
        await until(lambda: len(adapter.edits) >= 1)
        edits_before = len(adapter.edits)
        break_it()
        await until(lambda: len(adapter.edits) > edits_before + 1)
        text = adapter.edits[-1]
        assert text.startswith("⚠️")
        assert "RuntimeError" in text and "-100999" not in text
        assert "Обновлено: " in text
        assert ctx.state.data["probe"]["last_status"] == "edited"
        assert ctx.state.data["probe"]["last_render_error"] == "RuntimeError"

        mend_it()
        await until(lambda: adapter.edits[-1].startswith("HERMES DASHBOARD"))
        assert ctx.state.data["probe"]["last_render_error"] is None

    _run(runtime, adapter, scenario)


def test_without_the_dashboard_package_the_message_says_so(monkeypatch, tmp_path: Path) -> None:
    plugin = load_plugin()
    monkeypatch.setattr(plugin, "import_dashboard", lambda: None)
    ctx = FakeContext(_settings(monkeypatch, _home(tmp_path)))
    runtime = plugin.register(ctx)
    assert runtime is not None
    adapter = FakeAdapter()

    async def scenario() -> None:
        await until(lambda: len(adapter.edits) >= 1)

    _run(runtime, adapter, scenario)

    text = adapter.edits[-1]
    assert text.startswith("⚠️")
    assert "telegram_dashboard" in text
    assert "Обновлено: " in text


@pytest.mark.parametrize("state", STATES[:10], ids=[f"{s.number:02d}" for s in STATES[:10]])
def test_each_research_state_reaches_telegram_through_the_tick(
    monkeypatch, tmp_path: Path, state: Any
) -> None:
    """Section 11 of the research, through the plugin's own path: collector → render → plain
    text → ``edit_message``. A smoke run over the ten snapshots: each reaches Telegram intact,
    with its status label, its incidents and no markup. (Whether a state's status is derived
    correctly is ``derive_overall``'s job, tested with the collectors; here it is injected.)"""
    plugin = load_plugin()
    ctx = FakeContext(_settings(monkeypatch, _home(tmp_path)))
    runtime = plugin.register(ctx)
    assert runtime is not None
    adapter = FakeAdapter()

    async def snapshot(now: Any) -> Any:
        return state.snapshot

    runtime.collector = snapshot

    async def scenario() -> None:
        await until(lambda: len(adapter.edits) >= 1)

    _run(runtime, adapter, scenario)

    text = adapter.edits[-1]
    lines = text.splitlines()
    assert lines[0] == "HERMES DASHBOARD"
    assert lines[1] == _STATUS_LABELS[state.expect_overall]
    for incident in state.snapshot.incidents:
        assert incident.title in text
    if state.expect_overall != "normal":
        assert "🟢 Норма" not in text
    assert "Данные: 21:00 UTC" in text
    assert "#" not in text and "<b>" not in text
    assert _utf16_units(text) <= TELEGRAM_TEXT_LIMIT


def test_state_11_a_record_not_confirmed_for_two_periods_banners_the_first_line(
    monkeypatch, tmp_path: Path
) -> None:
    plugin = load_plugin()
    ctx = FakeContext(_settings(monkeypatch, _home(tmp_path), period_seconds=60))
    ctx.state.set(
        "probe",
        {
            "message_id": "4242",
            "chat_id": CHAT,
            "last_status": "edited",
            "last_confirmed_at": "2026-09-09T20:40:00+00:00",
            "last_attempt_at": "2026-09-09T20:58:00+00:00",
        },
    )
    runtime = plugin.register(ctx)
    assert runtime is not None
    adapter = FakeAdapter()

    async def snapshot(now: Any) -> Any:
        return STATES[10].snapshot

    runtime.collector = snapshot

    async def scenario() -> None:
        await until(lambda: len(adapter.edits) >= 1)

    _run(runtime, adapter, scenario)

    text = adapter.edits[-1]
    assert text.startswith("🔴 ДАШБОРД УСТАРЕЛ")
    assert "🟢 Норма" in text  # the data is fine; the message is the problem, and both are said
    assert not adapter.sent  # the remembered message was edited, not replaced


def test_state_12_a_lost_record_is_announced_and_the_message_is_sent_anew(
    monkeypatch, tmp_path: Path
) -> None:
    """The text is composed before the send that recreates the message, so it must speak of
    the message it will become: any text that lands IS the recreation, and a first line saying
    "НЕ восстановлено" on it would be false for a whole period."""
    plugin = load_plugin()
    ctx = FakeContext(_settings(monkeypatch, _home(tmp_path), period_seconds=60))
    ctx.state.set(
        "probe",
        {
            "message_id": None,
            "chat_id": CHAT,
            "last_status": "send_failed",
            "last_error": "Not connected",
            "last_confirmed_at": "2026-09-09T20:40:00+00:00",
            "lost_at": "2026-09-09T21:00:00+00:00",
        },
    )
    runtime = plugin.register(ctx)
    assert runtime is not None
    adapter = FakeAdapter()

    async def snapshot(now: Any) -> Any:
        return STATES[11].snapshot

    runtime.collector = snapshot

    async def scenario() -> None:
        await until(lambda: len(adapter.sent) >= 1)

    _run(runtime, adapter, scenario)

    text = adapter.sent[0]
    assert text.startswith("🔴 Закреплённое сообщение пропало (21:00 UTC), создано заново")
    assert runtime.record["message_id"] == "101"
    assert runtime.record["recreated_at"]


def test_settings_carry_the_collector_configuration(monkeypatch, tmp_path: Path) -> None:
    plugin = load_plugin()
    settings = plugin.read_settings(
        FakeContext(
            _settings(
                monkeypatch,
                tmp_path,
                drift_command=["python3", "/opt/example/check_drift.py"],
                drift_report="~/drift.json",
                limits_enabled="false",
                display_timezone="Europe/Moscow",
            )
        )
    )

    assert settings is not None
    assert settings.hermes_home == tmp_path
    assert settings.drift_command == ("python3", "/opt/example/check_drift.py")
    assert settings.drift_report == Path("~/drift.json").expanduser()
    assert settings.limits_enabled is False
    assert settings.display_timezone == "Europe/Moscow"


def test_settings_fall_back_to_the_environment_and_to_the_engine_default_home(
    monkeypatch, tmp_path: Path
) -> None:
    plugin = load_plugin()
    base = _settings(monkeypatch, tmp_path)
    del base["hermes_home"], base["limits_enabled"]
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "engine-home"))
    monkeypatch.setenv("HERMES_DASHBOARD_PROBE_LIMITS", "0")
    monkeypatch.setenv("HERMES_DASHBOARD_PROBE_TZ", "Nowhere/Invalid")

    settings = plugin.read_settings(FakeContext(base))

    assert settings is not None
    assert settings.hermes_home == tmp_path / "engine-home"
    assert settings.limits_enabled is False
    assert settings.drift_command is None and settings.drift_report is None
    assert plugin.display_zone(settings) is UTC  # unknown zone name degrades, never raises


def test_the_package_beside_the_plugin_wins_over_the_installed_one(tmp_path: Path) -> None:
    """The deployment copies ``telegram_dashboard/`` into the plugin folder; the engine loads a
    directory plugin as a package with ``__path__``, so the copy resolves as a relative import."""
    plugin_dir = tmp_path / "telegram_dashboard_probe"
    shutil.copytree(PLUGIN_DIR, plugin_dir)
    shutil.copytree(PLUGIN_DIR.parents[1] / "telegram_dashboard", plugin_dir / "telegram_dashboard")

    plugin = load_plugin("hermes_plugins.vendored_probe", plugin_dir)
    dashboard = plugin.import_dashboard()

    assert dashboard is not None
    assert dashboard.origin == "vendored"
    assert dashboard.render.__name__ == "hermes_plugins.vendored_probe.telegram_dashboard.render"

    installed = load_plugin().import_dashboard()
    assert installed is not None and installed.origin == "installed"
    assert installed.render.__name__ == "telegram_dashboard.render"


def test_a_healthy_cadence_never_shows_the_lagging_banner(monkeypatch, tmp_path: Path) -> None:
    """The record is read at the start of a tick and confirmed at its end. Without a fixed
    cadence (sleep what is left of the period, not the whole period) and a little slack, every
    healthy tick reads as one period plus its own duration old, and the banner cries wolf."""
    plugin = load_plugin()
    ctx = FakeContext(_settings(monkeypatch, _home(tmp_path), period_seconds=1))
    runtime = plugin.register(ctx)
    assert runtime is not None
    adapter = FakeAdapter()

    async def slow(now: Any) -> Any:
        await asyncio.sleep(0.3)
        return STATES[0].snapshot

    runtime.collector = slow

    async def scenario() -> None:
        await until(lambda: len(adapter.edits) >= 2, timeout=6.0)

    _run(runtime, adapter, scenario)

    first_lines = [text.splitlines()[0] for text in adapter.edits]
    assert first_lines == ["HERMES DASHBOARD"] * len(first_lines), first_lines


def test_a_collector_that_exits_the_interpreter_still_gets_a_notice(
    monkeypatch, tmp_path: Path
) -> None:
    """``SystemExit`` is not an ``Exception``; raised in a worker thread it comes back through
    ``to_thread`` and would kill the tick (and the gateway loop) with the old text on screen."""
    plugin = load_plugin()
    ctx = FakeContext(_settings(monkeypatch, _home(tmp_path)))
    runtime = plugin.register(ctx)
    assert runtime is not None
    adapter = FakeAdapter()

    async def exit_(now: Any) -> Any:
        raise SystemExit(3)

    async def scenario() -> None:
        await until(lambda: len(adapter.edits) >= 1)
        edits_before = len(adapter.edits)
        runtime.collector = exit_
        await until(lambda: len(adapter.edits) > edits_before + 1)

    _run(runtime, adapter, scenario)

    assert adapter.edits[-1].startswith("⚠️")
    assert "SystemExit" in adapter.edits[-1]
    assert ctx.state.data["probe"]["last_render_error"] == "SystemExit"


def test_a_zone_name_that_is_a_directory_degrades_to_utc_and_the_plugin_still_registers(
    monkeypatch, tmp_path: Path
) -> None:
    """``ZoneInfo("Europe")`` raises ``PermissionError``/``IsADirectoryError`` where tzdata is a
    package: an ``OSError``, not a ``ZoneInfoNotFoundError``. A typo in ``display_timezone`` must
    not stop the plugin from loading, which would leave yesterday's text pinned."""
    plugin = load_plugin()
    ctx = FakeContext(_settings(monkeypatch, _home(tmp_path), display_timezone="Europe"))

    runtime = plugin.register(ctx)

    assert runtime is not None
    assert runtime.zone is UTC
    assert ctx.factories, "the handler factory was not registered"


def test_home_is_taken_from_the_state_path_before_the_environment(
    monkeypatch, tmp_path: Path
) -> None:
    """The engine sets HERMES_HOME in the process only for ``-p profile``; the plugin's own
    state file always lives at ``<home>/plugin-data/<namespace>/state.json``."""
    plugin = load_plugin()
    base = _settings(monkeypatch, tmp_path)
    del base["hermes_home"]
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "somewhere-else"))
    ctx = FakeContext(base)
    ctx.state.path = tmp_path / "home" / "plugin-data" / "agent-plugin-x-0123abcd" / "state.json"

    settings = plugin.read_settings(ctx)

    assert settings is not None
    assert settings.hermes_home == tmp_path / "home"


def test_a_package_without_the_screen_api_is_rejected_at_import(monkeypatch) -> None:
    """An older ``telegram_dashboard`` in the interpreter (no ``to_telegram_plain``) must be
    refused up front, not fail with ``AttributeError`` on every tick."""
    import sys
    from types import ModuleType

    plugin = load_plugin()
    stale = ModuleType("telegram_dashboard.render")
    stale.render_dashboard = lambda *a, **k: ""  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "telegram_dashboard.render", stale)

    assert plugin.import_dashboard() is None


def test_a_persistent_failure_logs_one_traceback_not_one_per_tick(
    monkeypatch, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    plugin = load_plugin()
    ctx = FakeContext(_settings(monkeypatch, _home(tmp_path)))
    runtime = plugin.register(ctx)
    assert runtime is not None
    adapter = FakeAdapter()

    async def boom(now: Any) -> Any:
        raise RuntimeError("same thing every minute")

    runtime.collector = boom

    async def scenario() -> None:
        await until(lambda: len(adapter.edits) >= 4)

    with caplog.at_level("WARNING", logger="hermes.plugins.telegram_dashboard_probe"):
        _run(runtime, adapter, scenario)

    with_traceback = [record for record in caplog.records if record.exc_info]
    assert len(with_traceback) == 1, [record.getMessage() for record in with_traceback]


def test_the_runtime_keeps_one_flights_registry_across_ticks(monkeypatch, tmp_path: Path) -> None:
    """Single-flight only works if the same registry outlives the tick that abandoned a worker."""
    plugin = load_plugin()
    ctx = FakeContext(_settings(monkeypatch, _home(tmp_path)))
    runtime = plugin.register(ctx)
    assert runtime is not None
    adapter = FakeAdapter()
    seen: list[Any] = []
    real = runtime.dashboard.collect.collect_all_async

    async def spy(*args: Any, **kwargs: Any) -> Any:
        seen.append(kwargs.get("flights"))
        return await real(*args, **kwargs)

    monkeypatch.setattr(runtime.dashboard.collect, "collect_all_async", spy)

    async def scenario() -> None:
        await until(lambda: len(seen) >= 3)

    _run(runtime, adapter, scenario)

    assert seen[0] is not None
    assert all(item is seen[0] for item in seen)
    assert type(seen[0]).__name__ == "Flights"


def test_the_limits_refresh_interval_is_a_setting_with_a_floor(monkeypatch, tmp_path: Path) -> None:
    """The Grok pool is asked once per this interval; a value below a minute is not an interval."""
    plugin = load_plugin()
    base = _settings(monkeypatch, tmp_path)

    def refresh(**overrides: Any) -> float:
        settings = plugin.read_settings(FakeContext({**base, **overrides}))
        assert settings is not None
        return settings.limits_refresh_seconds

    assert refresh() == 900.0
    assert refresh(limits_refresh_seconds="1800") == 1800.0
    assert refresh(limits_refresh_seconds="5") == 60.0
    assert refresh(limits_refresh_seconds="soon") == 900.0
    monkeypatch.setenv("HERMES_DASHBOARD_PROBE_LIMITS_REFRESH", "1200")
    assert refresh() == 1200.0


def test_the_tick_hands_the_record_s_own_cache_to_the_collector_and_persists_it(
    monkeypatch, tmp_path: Path
) -> None:
    """The Grok cache lives in the plugin record: observable in the state file, kept across
    restarts, and the same dict object on every tick so the collector's writes land in it."""
    from datetime import datetime

    plugin = load_plugin()
    ctx = FakeContext(
        _settings(monkeypatch, _home(tmp_path), limits_enabled=True, limits_refresh_seconds="1200")
    )
    runtime = plugin.register(ctx)
    assert runtime is not None
    seen: list[dict[str, Any]] = []

    async def spy(*args: Any, **kwargs: Any) -> Any:
        seen.append(kwargs)
        kwargs["grok_cache"]["attempted_at"] = kwargs["now"].isoformat()
        kwargs["kimi_cache"]["attempted_at"] = kwargs["now"].isoformat()
        return "snapshot"

    monkeypatch.setattr(runtime.dashboard.collect, "collect_all_async", spy)
    now = datetime(2026, 9, 12, 13, 40, tzinfo=UTC)

    assert asyncio.run(runtime._collect(now)) == "snapshot"
    assert asyncio.run(runtime._collect(now)) == "snapshot"
    runtime._note(status="edited", error=None)

    assert seen[0]["grok_interval_seconds"] == seen[0]["kimi_interval_seconds"] == 1200.0
    caches = runtime.record["limits_cache"]
    assert seen[0]["grok_cache"] is seen[1]["grok_cache"] is caches["grok"]
    assert seen[0]["kimi_cache"] is seen[1]["kimi_cache"] is caches["kimi"]
    assert seen[0]["grok_cache"] is not seen[0]["kimi_cache"]
    assert ctx.state.data["probe"]["limits_cache"] == {
        "grok": {"attempted_at": now.isoformat()},
        "kimi": {"attempted_at": now.isoformat()},
    }


def test_a_record_from_before_kimi_keeps_its_grok_attempt_under_the_provider_key(
    monkeypatch, tmp_path: Path
) -> None:
    """The layout before Kimi kept Grok's attempt at the top level of ``limits_cache``. A state
    file in that shape (the pilot's) is moved under ``grok`` once, so the restart after the
    deploy keeps its interval instead of asking the proxy again."""
    plugin = load_plugin()
    ctx = FakeContext(_settings(monkeypatch, _home(tmp_path), limits_enabled=True))
    legacy = {"attempted_at": "2026-09-12T13:38:23+00:00", "item": {"provider": "grok"}}
    ctx.state.set("probe", {"limits_cache": dict(legacy)})
    runtime = plugin.register(ctx)
    assert runtime is not None
    runtime._load_record()

    caches = runtime.quota_caches()

    assert caches == {"grok": legacy, "kimi": {}}
    assert runtime.record["limits_cache"] is caches
    assert runtime.quota_caches() is caches  # stable across ticks
