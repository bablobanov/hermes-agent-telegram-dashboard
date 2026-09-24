import asyncio
import importlib.util
import json
import sys
import threading
import time
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from telegram_dashboard.collect import (
    CommandResult,
    SubprocessRunner,
    collect_all,
    collect_drift,
    collect_gateway,
    collect_limits,
    collect_limits_async,
    parse_drift_output,
    parse_limits_payload,
)
from telegram_dashboard.compat import Environment

NOW = datetime(2026, 9, 9, 21, 0, tzinfo=UTC)
OLD = "2026-09-08T09:00:00+00:00"  # a quiet gateway last wrote its status a day ago


class FakeRunner:
    def __init__(self, result: CommandResult) -> None:
        self.result = result
        self.calls: list[tuple[str, ...]] = []

    def run(self, argv, *, timeout_seconds):
        self.calls.append(tuple(argv))
        return self.result


def _home(tmp_path: Path, payload) -> Environment:
    (tmp_path / "gateway_state.json").write_text(json.dumps(payload), encoding="utf-8")
    return Environment(hermes_home=tmp_path)


def _gateway_payload(state="running", telegram="connected", **extra):
    payload = {
        "pid": 4242,
        "gateway_state": state,
        "updated_at": OLD,
        "platforms": {"telegram": {"state": telegram, "writer_pid": 4242, **extra}},
    }
    return payload


# ---------------------------------------------------------------- gateway


def test_quiet_gateway_with_old_updated_at_is_not_red(tmp_path: Path) -> None:
    """updated_at only moves on transitions; an old stamp with a live pid is a healthy gateway."""
    summary, source, incidents = collect_gateway(
        _home(tmp_path, _gateway_payload()), now=NOW, pid_probe=lambda _: True
    )

    assert summary.process == "running"
    assert summary.telegram == "connected"
    assert source.state == "fresh"
    assert incidents == ()


def test_dead_pid_is_critical_even_if_file_says_running(tmp_path: Path) -> None:
    summary, source, incidents = collect_gateway(
        _home(tmp_path, _gateway_payload()), now=NOW, pid_probe=lambda _: False
    )

    assert summary.process == "stopped"
    assert source.state == "stale"
    assert [item.incident_id for item in incidents] == ["gateway:dead"]
    assert incidents[0].severity == "critical"


def test_gateway_alive_but_telegram_polling_dead_is_critical(tmp_path: Path) -> None:
    env = _home(
        tmp_path,
        _gateway_payload(
            telegram="failed", error_code="conflict", error_message="token secret-xyz"
        ),
    )

    summary, _source, incidents = collect_gateway(env, now=NOW, pid_probe=lambda _: True)

    assert summary.telegram == "degraded"
    assert incidents[0].incident_id == "telegram:polling"
    assert incidents[0].severity == "critical"
    assert "conflict" in incidents[0].title
    assert "secret" not in incidents[0].title


def test_platform_entry_from_previous_process_is_not_trusted(tmp_path: Path) -> None:
    payload = _gateway_payload(telegram="failed")
    payload["platforms"]["telegram"]["writer_pid"] = 1

    summary, _source, incidents = collect_gateway(
        _home(tmp_path, payload), now=NOW, pid_probe=lambda _: True
    )

    assert summary.telegram == "unknown"
    assert incidents == ()


def test_unknown_pid_liveness_is_unknown_not_green(tmp_path: Path) -> None:
    summary, source, incidents = collect_gateway(
        _home(tmp_path, _gateway_payload()), now=NOW, pid_probe=lambda _: None
    )

    assert summary.process == "unknown"
    assert summary.telegram == "unknown"
    assert source.state == "unavailable"
    assert incidents == ()


def test_missing_and_broken_gateway_state(tmp_path: Path) -> None:
    summary, source, _ = collect_gateway(Environment(hermes_home=tmp_path), now=NOW)
    assert summary.process == "unsupported" and source.state == "unsupported"

    (tmp_path / "gateway_state.json").write_text("{broken", encoding="utf-8")
    summary, source, incidents = collect_gateway(Environment(hermes_home=tmp_path), now=NOW)
    assert summary.process == "unknown" and source.state == "unavailable"
    assert incidents[0].incident_id == "gateway:unreadable"


# ---------------------------------------------------------------- drift

DRIFT_CLEAN = """Сверка эталона с живым конфигом
  эталон: [путь]
          sha256 abc | ключей 474 | строк 900
  живой:  [путь]
          sha256 abc | ключей 474 | строк 900
  файлы совпадают побайтово

[1] ТОЛЬКО НА СЕРВЕРЕ, эталон не знает: 0
[2] ТОЛЬКО В ЭТАЛОНЕ, на сервере нет: 0
[3] ЗНАЧЕНИЯ РАСХОДЯТСЯ: 0
[4] СЕКРЕТЫ, сравнивается только пусто/заполнено: 0
"""
DRIFT_DIRTY = DRIFT_CLEAN.replace("РАСХОДЯТСЯ: 0", "РАСХОДЯТСЯ: 2").replace(
    "эталон не знает: 0", "эталон не знает: 1"
)


def test_drift_output_parsing() -> None:
    clean = parse_drift_output(DRIFT_CLEAN, 0, checked_at=NOW.isoformat())
    assert (clean.state, clean.changed_keys, clean.total_keys) == ("clean", 0, 474)

    dirty = parse_drift_output(DRIFT_DIRTY, 1, checked_at=NOW.isoformat())
    assert (dirty.state, dirty.changed_keys, dirty.total_keys) == ("drift", 3, 474)

    assert parse_drift_output("ОШИБКА чтения", 2, checked_at=None).state == "unknown"
    assert parse_drift_output("", None, checked_at=None).state == "unknown"
    assert parse_drift_output("garbage", 1, checked_at=None).state == "unknown"


def test_drift_from_command_and_report(tmp_path: Path) -> None:
    runner = FakeRunner(CommandResult(1, DRIFT_DIRTY, ""))
    env = Environment(hermes_home=tmp_path, drift_command=("python", "check_drift.py"))

    summary, source, incidents = collect_drift(env, runner, now=NOW)

    assert summary.state == "drift" and source.state == "fresh"
    assert incidents[0].incident_id == "config:drift"
    assert "3 из 474" in incidents[0].title
    assert runner.calls == [("python", "check_drift.py")]

    report = tmp_path / "drift.json"
    report.write_text(
        json.dumps(
            {"stdout": DRIFT_CLEAN, "exit_code": 0, "checked_at": "2026-09-09T08:00:00+00:00"}
        ),
        encoding="utf-8",
    )
    summary, source, incidents = collect_drift(
        Environment(hermes_home=tmp_path, drift_report=report), runner, now=NOW
    )
    assert summary.state == "clean" and source.state == "fresh" and incidents == ()

    summary, source, _ = collect_drift(Environment(hermes_home=tmp_path), runner, now=NOW)
    assert summary.state == "unsupported" and source.state == "unsupported"


def test_drift_command_timeout_is_unavailable_not_zero(tmp_path: Path) -> None:
    runner = FakeRunner(CommandResult(None, "", "", timed_out=True, error="timeout"))
    env = Environment(hermes_home=tmp_path, drift_command=("python", "check_drift.py"))

    summary, source, _ = collect_drift(env, runner, now=NOW)

    assert summary.state == "unknown" and summary.changed_keys is None
    assert source.state == "unavailable"


# ---------------------------------------------------------------- limits


def _payload(**overrides):
    base = {
        "ok": True,
        "providers": [
            {
                "provider": "claude",
                "status": "available",
                "source": "anthropic-oauth",
                "fetched_at": "2026-09-09T20:58:00+00:00",
                "windows": [
                    {"label": "5h", "used_percent": 37.5, "reset_at": "2026-09-10T00:00:00+00:00"}
                ],
            },
            {"provider": "codex", "status": "unavailable", "reason": "AuthError", "windows": []},
        ],
    }
    base.update(overrides)
    return base


def test_limits_payload_keeps_official_local_and_unsupported_apart() -> None:
    capacity, source, probe = parse_limits_payload(_payload(), now=NOW)

    kinds = {quota.provider: quota.kind for quota in capacity.quotas}
    assert kinds == {
        "Claude": "official",
        "Codex": "unavailable",
        "Gemini": "unsupported",
    }
    assert capacity.quotas[0].windows[0].used_percent == 37.5
    assert source.state == "fresh" and probe.status == "supported"


def test_limits_import_failure_is_unsupported_not_zero() -> None:
    capacity, source, probe = parse_limits_payload(
        {"ok": False, "reason": "unsupported: import failed (ModuleNotFoundError)"}, now=NOW
    )

    assert probe.status == "unsupported"
    assert source.state == "unsupported"
    assert all(quota.kind == "unsupported" for quota in capacity.quotas)


def test_limits_out_of_range_percent_and_missing_provider() -> None:
    payload = _payload(
        providers=[
            {
                "provider": "claude",
                "status": "available",
                "windows": [{"label": "5h", "used_percent": 140}],
            }
        ]
    )
    capacity, source, _ = parse_limits_payload(payload, now=NOW)

    assert capacity.quotas[0].windows[0].used_percent is None
    assert capacity.quotas[1].kind == "unavailable"
    assert source.state == "unavailable"  # no fetched_at anywhere


def test_limits_without_the_engine_are_unsupported_not_a_crash(tmp_path: Path) -> None:
    """This interpreter has no Hermes: the in-process import fails and the source degrades."""
    if importlib.util.find_spec("agent") is not None:
        pytest.skip("the engine is importable here: the real facade would call provider APIs")
    capacity, source, probe = collect_limits(Environment(hermes_home=tmp_path), now=NOW)

    assert probe.status == "unsupported", probe
    assert source.state == "unsupported"
    assert all(quota.kind == "unsupported" for quota in capacity.quotas)


def _snapshot(**overrides):
    window = SimpleNamespace(
        label="5h", used_percent=37.5, reset_at=datetime(2026, 9, 10, 0, 0, tzinfo=UTC)
    )
    base = {
        "available": True,
        "unavailable_reason": None,
        "source": "anthropic-oauth",
        "fetched_at": datetime(2026, 9, 9, 20, 58, tzinfo=UTC),
        "windows": (window,),
    }
    base.update(overrides)
    return SimpleNamespace(**base)


def test_limits_facade_called_per_provider_and_one_failure_stays_local(tmp_path: Path) -> None:
    asked: list[str] = []

    def fetch(provider: str):
        asked.append(provider)
        if provider == "openai-codex":
            raise RuntimeError("token refresh failed")
        return _snapshot()

    capacity, source, probe = collect_limits(
        Environment(hermes_home=tmp_path), now=NOW, resolve=lambda: fetch
    )

    assert asked == ["anthropic", "openai-codex"]
    assert probe.status == "supported" and source.state == "fresh"
    kinds = {quota.provider: quota.kind for quota in capacity.quotas}
    assert kinds["Claude"] == "official" and kinds["Codex"] == "unavailable"
    assert capacity.quotas[1].detail == "RuntimeError"
    assert capacity.quotas[0].windows[0].used_percent == 37.5


def test_limits_async_runs_the_facade_off_the_event_loop(tmp_path: Path) -> None:
    threads: list[str] = []

    def fetch(provider: str):
        threads.append(threading.current_thread().name)
        return _snapshot()

    async def scenario():
        return await collect_limits_async(
            Environment(hermes_home=tmp_path), now=NOW, resolve=lambda: fetch, timeout_seconds=5
        )

    capacity, _source, probe = asyncio.run(scenario())

    assert probe.status == "supported"
    assert capacity.quotas[0].kind == "official"
    assert threads and all(name != threading.main_thread().name for name in threads)


def test_limits_async_deadline_is_one_unavailable_tick(tmp_path: Path) -> None:
    def slow_fetch(provider: str):
        time.sleep(0.5)
        return _snapshot()

    async def scenario():
        return await collect_limits_async(
            Environment(hermes_home=tmp_path),
            now=NOW,
            resolve=lambda: slow_fetch,
            timeout_seconds=0.05,
        )

    capacity, source, probe = asyncio.run(scenario())

    assert probe.status == "unknown" and source.state == "unavailable"
    assert "timeout" in source.detail
    assert all(quota.kind in ("unavailable", "unsupported") for quota in capacity.quotas)


def test_limits_resolver_that_raises_is_unsupported(tmp_path: Path) -> None:
    def broken_resolver():
        raise ImportError("engine half-installed")

    _capacity, source, probe = collect_limits(
        Environment(hermes_home=tmp_path), now=NOW, resolve=broken_resolver
    )

    assert probe.status == "unsupported" and source.state == "unsupported"


def test_subprocess_runner_kills_on_timeout() -> None:
    result = SubprocessRunner().run(
        (sys.executable, "-c", "import time; time.sleep(30)"), timeout_seconds=1
    )

    assert result.timed_out and result.error == "timeout"


def test_subprocess_runner_missing_executable_is_an_error_not_exception() -> None:
    result = SubprocessRunner().run(("definitely-not-a-real-binary-xyz",), timeout_seconds=1)

    assert result.error is not None and result.returncode is None


def test_collect_all_composes_without_any_source(tmp_path: Path) -> None:
    runner = FakeRunner(CommandResult(0, "", ""))

    snapshot = collect_all(
        Environment(hermes_home=tmp_path), runner, now=NOW, resolve_limits=lambda: None
    )

    assert runner.calls == []  # no drift source configured, nothing spawned

    assert snapshot.overall == "unknown"
    assert {source.name: source.state for source in snapshot.sources} == {
        "gateway_state": "unsupported",
        "limits": "unsupported",
        "grok_quota": "unsupported",
        "kimi_quota": "unsupported",
        "drift": "unsupported",
        "backup": "unsupported",
    }


def test_limits_facade_abandoned_by_its_deadline_is_not_called_again_until_it_returns() -> None:
    from telegram_dashboard.limits import fetch_limits_payload_off_loop
    from telegram_dashboard.workers import Flights

    calls: list[str] = []

    def fetch(provider: str):
        calls.append(provider)
        time.sleep(0.4)
        return None

    flights = Flights()

    async def scenario():
        first = await fetch_limits_payload_off_loop(
            lambda: fetch, timeout_seconds=0.05, flights=flights
        )
        second = await fetch_limits_payload_off_loop(
            lambda: fetch, timeout_seconds=0.05, flights=flights
        )
        await asyncio.sleep(1.0)
        third = await fetch_limits_payload_off_loop(
            lambda: fetch, timeout_seconds=2.0, flights=flights
        )
        return first, second, third

    first, second, third = asyncio.run(scenario())

    assert first["ok"] is False and "timeout" in first["reason"]
    assert second["ok"] is False and "busy" in second["reason"]
    assert third["ok"] is True
    assert calls == ["anthropic", "openai-codex", "anthropic", "openai-codex"]
