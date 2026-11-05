import asyncio
import importlib.util
import json
import sys
import threading
import time
from datetime import UTC, datetime, timedelta
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
from telegram_dashboard.schema import CapacitySummary, QuotaMetric, Refusal

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

ИТОГ: дрейфа нет, эталон описывает прод точно
keys_changed=0 keys_total=474
"""
DRIFT_DIRTY = (
    DRIFT_CLEAN.replace("РАСХОДЯТСЯ: 0", "РАСХОДЯТСЯ: 2")
    .replace("эталон не знает: 0", "эталон не знает: 1")
    .replace("keys_changed=0", "keys_changed=3")
)


def test_drift_output_parsing() -> None:
    clean = parse_drift_output(DRIFT_CLEAN, 0, checked_at=NOW.isoformat())
    assert (clean.state, clean.changed_keys, clean.total_keys) == ("clean", 0, 474)

    dirty = parse_drift_output(DRIFT_DIRTY, 1, checked_at=NOW.isoformat())
    assert (dirty.state, dirty.changed_keys, dirty.total_keys) == ("drift", 3, 474)

    assert parse_drift_output("ОШИБКА чтения", 2, checked_at=None).state == "unknown"
    assert parse_drift_output("", None, checked_at=None).state == "unknown"
    assert parse_drift_output("garbage", 1, checked_at=None).state == "unknown"


def test_drift_numbers_come_from_the_counts_line_not_the_words() -> None:
    # The counts line alone is enough: the words above it are never read.
    alone = parse_drift_output("keys_changed=2 keys_total=481\n", 1, checked_at=None)
    assert (alone.state, alone.changed_keys, alone.total_keys) == ("drift", 2, 481)

    # Output from before the counts line: the sections still count, the total word does not.
    before = DRIFT_CLEAN.replace("keys_changed=0 keys_total=474\n", "")
    old = parse_drift_output(before, 0, checked_at=None)
    assert (old.state, old.changed_keys, old.total_keys) == ("clean", 0, None)


def test_drift_from_command_and_report(tmp_path: Path) -> None:
    runner = FakeRunner(CommandResult(1, DRIFT_DIRTY, ""))
    env = Environment(hermes_home=tmp_path, drift_command=("python", "check_drift.py"))

    summary, source, incidents = collect_drift(env, runner, now=NOW)

    assert summary.state == "drift" and source.state == "fresh"
    assert incidents[0].incident_id == "config:drift"
    assert incidents[0].title == "Config drift: 3 of 474 keys"
    assert runner.calls == [("python", "check_drift.py")]


def test_drift_without_a_total_counts_the_keys_in_words(tmp_path: Path) -> None:
    # Another script's sections without the counts line: no total, the count stands alone.
    one_key = "[1] only on the server: 1\n[2] only in the baseline: 0\n[3] values differ: 0\n"
    runner = FakeRunner(CommandResult(1, one_key, ""))
    env = Environment(hermes_home=tmp_path, drift_command=("python", "check_drift.py"))

    summary, _, incidents = collect_drift(env, runner, now=NOW)

    assert summary.state == "drift" and summary.changed_keys == 1 and summary.total_keys is None
    assert incidents[0].title == "Config drift: 1 key"

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
    # Gemini is no facade provider: its line comes from the engine's log (``gemini_log.py``).
    assert kinds == {"Claude": "official", "Codex": "unavailable"}
    assert capacity.quotas[0].windows[0].used_percent == 37.5
    assert source.state == "fresh" and probe.status == "supported"


def test_the_facade_s_none_is_named_as_no_answer_not_as_a_missing_token() -> None:
    """Issue #14: the facade answers ``None`` alike for no credential and for a call that
    failed (an HTTP 401, 429 or 5xx, a network error), so a ``None`` line that is still shown
    says both, never "no account token"."""
    payload = {
        "ok": True,
        "providers": [
            {"provider": "claude", "status": "unavailable", "reason": "none", "windows": []},
            {"provider": "codex", "status": "unavailable", "reason": "AuthError", "windows": []},
        ],
    }

    capacity, _, _ = parse_limits_payload(payload, now=NOW)

    assert capacity.quotas[0].detail == "no answer: no credential or the call failed"
    assert capacity.quotas[1].detail == "AuthError"
    assert [q.provider for q in capacity.quotas] == ["Claude", "Codex"]


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
        "gemini_log": "unsupported",
        "drift": "unsupported",
        "backup": "unsupported",
        "cron": "unsupported",
        "cron_runs": "unsupported",
        "telegram_traffic": "unsupported",
        "context_files": "unsupported",
        "memory": "unsupported",
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


# ---------------------------------------------------------------- plans, scope, severity (0.8.0)


def test_the_facade_s_plan_reaches_the_metric(tmp_path: Path) -> None:
    """``AccountUsageSnapshot.plan`` (Codex: ``Prolite``, title-cased by the facade) was read and
    thrown away before 0.8.0; the details show it now."""

    def fetch(provider: str):
        return _snapshot(plan="Prolite" if provider == "openai-codex" else None)

    capacity, _source, _probe = collect_limits(
        Environment(hermes_home=tmp_path), now=NOW, resolve=lambda: fetch
    )

    plans = {quota.provider: quota.plan for quota in capacity.quotas}
    assert plans["Codex"] == "Prolite" and plans["Claude"] is None


def test_an_item_s_plan_scope_severity_and_login_ride_to_the_metric() -> None:
    from telegram_dashboard.collect import external_part

    item = {
        "provider": "x",
        "status": "available",
        "reason": None,
        "source": "external",
        "fetched_at": NOW.isoformat(),
        "plan": "Max 5x",
        "login_expires_at": "2026-10-27T21:07:24Z",
        "windows": [
            {
                "label": "week",
                "used_percent": 100,
                "reset_at": None,
                "scope": "Fable",
                "severity": "critical",
            },
            {"label": "week", "used_percent": 86, "reset_at": None, "severity": "loud"},
        ],
    }
    metric, source, _incidents = external_part(_ext_source(), {}, item, now=NOW, show_seconds=1800)

    assert (metric.kind, metric.plan, metric.login_expires_at) == (
        "official",
        "Max 5x",
        "2026-10-27T21:07:24Z",
    )
    assert [(w.scope, w.severity) for w in metric.windows] == [("Fable", "critical"), (None, None)]
    assert source.state == "fresh"


def test_an_expired_login_is_its_own_kind_and_the_source_answered() -> None:
    from telegram_dashboard.collect import external_part

    item = {
        "provider": "x",
        "status": "expired",
        "reason": None,
        "source": "external",
        "fetched_at": NOW.isoformat(),
        "plan": "Max 5x",
        "login_expires_at": "2026-09-01T00:00:00Z",
        "windows": [],
    }
    metric, source, _incidents = external_part(_ext_source(), {}, item, now=NOW, show_seconds=1800)

    assert metric.kind == "expired" and metric.plan == "Max 5x"
    assert metric.login_expires_at == "2026-09-01T00:00:00Z"
    assert source.state == "fresh"


# ---------------------------------------------------------------- external limit sources (0.8.0)

EXT_URL = "http://127.0.0.1:18080/v1/usage"
EXT_ANSWER = {
    "contract": 1,
    "provider": "Claude",
    "state": "ok",
    "reason": None,
    "plan": "Max 5x",
    "login_expires_at": "2026-10-27T21:07:24Z",
    "fetched_at": "2026-09-09T20:59:00Z",
    "windows": [
        {
            "label": "session",
            "used_percent": 42,
            "resets_at": None,
            "scope": None,
            "severity": "normal",
        },
    ],
}


def _ext_source(url: str = EXT_URL, **fields):
    from telegram_dashboard.external import read_sources

    (source,) = read_sources([{"url": url, **fields}])
    return source


def _ext_fetch(answer, calls=None):
    from telegram_dashboard.external import parse_contract

    def fetch(source, *, now):
        if calls is not None:
            calls.append(source.url)
        return parse_contract(answer, now=now)

    return fetch


def _ext(answer, cache=None, *, now=NOW, source=None, calls=None):
    from telegram_dashboard.collect import collect_external

    return collect_external(
        source or _ext_source(),
        {} if cache is None else cache,
        now=now,
        interval_seconds=900,
        show_seconds=3600,
        fetch=_ext_fetch(answer, calls),
    )


def _facade_capacity():
    capacity, _source, _probe = parse_limits_payload(
        {
            "ok": True,
            "reason": None,
            "providers": [
                {"provider": "claude", "status": "unavailable", "reason": "none", "windows": []},
                {
                    "provider": "codex",
                    "status": "available",
                    "source": "usage_api",
                    "fetched_at": NOW.isoformat(),
                    "windows": [{"label": "Session", "used_percent": 12.0, "reset_at": None}],
                },
            ],
        },
        now=NOW,
    )
    return capacity


def test_an_external_source_takes_the_built_in_line_s_place() -> None:
    """A source naming a built-in provider replaces that line where it stands; the facade's
    "Claude · no data" never shows beside it."""
    from telegram_dashboard.collect import merge_external

    metric, source, incidents = _ext(EXT_ANSWER)
    merged = merge_external(_facade_capacity(), [metric])

    assert [q.provider for q in merged.quotas] == ["Claude", "Codex"]
    assert merged.quotas[0].kind == "official" and merged.quotas[0].plan == "Max 5x"
    assert (source.name, source.state, incidents) == ("Claude limits", "fresh", ())


def test_a_new_provider_goes_before_gemini() -> None:
    from telegram_dashboard.collect import merge_external, merge_quotas

    metric, _source, _incidents = _ext({**EXT_ANSWER, "provider": "Mistral"})
    merged = merge_external(merge_quotas(_facade_capacity(), _GEMINI_LINE), [metric])

    assert [q.provider for q in merged.quotas] == ["Claude", "Codex", "Mistral", "Gemini"]


def test_lines_merged_in_sit_after_the_facade_providers_and_before_gemini() -> None:
    """``merge_quotas`` keeps the facade's lines first and Gemini last; the lines it is given go
    between, in the order given."""
    from telegram_dashboard.collect import merge_quotas

    capacity = CapacitySummary(
        (
            QuotaMetric("Claude", "unavailable", detail="x"),
            QuotaMetric("Codex", "unavailable", detail="x"),
            QuotaMetric("Gemini", "unsupported", detail="source not confirmed"),
        )
    )

    merged = merge_quotas(
        capacity,
        QuotaMetric("Mistral", "unavailable", detail="HTTP 503"),
        QuotaMetric("Qwen", "unavailable", detail="HTTP 503"),
    )

    assert [q.provider for q in merged.quotas] == ["Claude", "Codex", "Mistral", "Qwen", "Gemini"]


def test_login_events_come_from_the_contract_and_survive_a_failed_tick() -> None:
    expired, expired_source, expired_events = _ext(
        {**EXT_ANSWER, "state": "login_expired", "windows": []}
    )
    assert expired.kind == "expired" and expired_source.state == "fresh"
    assert [(i.incident_id, i.severity, i.title) for i in expired_events] == [
        ("claude:login_expired", "warning", "Claude login expired")
    ]

    cache: dict = {}
    soon = {**EXT_ANSWER, "login_expires_at": "2026-09-11T20:00:00Z"}
    _metric, _source, events = _ext(soon, cache)
    assert [(i.incident_id, i.title) for i in events] == [
        ("claude:login_expiring", "Claude login expires in 2 days")
    ]

    # Decision 7: the source fails on the 28th day; the warning does not disappear with it.
    later = NOW.replace(hour=22)
    down = {"contract": 1, "provider": "Claude", "state": "unavailable", "reason": "HTTP 503"}
    cache["attempted_at"] = "2026-09-09T00:00:00+00:00"
    metric, source, events = _ext({**down, "windows": []}, cache, now=later)
    assert source.state == "unavailable" and metric.login_expires_at == "2026-09-11T20:00:00+00:00"
    assert [i.incident_id for i in events] == ["claude:login_expiring"]

    _metric, _source, calm = _ext({**EXT_ANSWER, "login_expires_at": "2026-12-01T00:00:00Z"})
    assert calm == ()


def test_a_failing_source_names_itself_by_number_until_it_has_answered() -> None:
    down = {"contract": 1, "provider": None, "state": "ok"}
    metric, source, _events = _ext(down)
    assert (metric.provider, metric.kind, source.state) == (
        "Source 1",
        "unavailable",
        "unavailable",
    )
    assert source.name == "Source 1 limits" and metric.detail == "answer without provider"

    cache = {"provider": "Claude", "attempted_at": "2026-09-09T00:00:00+00:00"}
    metric, _source, _events = _ext(down, cache)
    assert metric.provider == "Claude"


def test_a_refused_entry_is_never_fetched_and_says_why() -> None:
    calls: list[str] = []
    metric, source, _events = _ext(
        EXT_ANSWER, source=_ext_source("http://10.0.0.5:18080/", key_env="X"), calls=calls
    )

    assert calls == []
    assert (metric.kind, metric.detail, source.state) == (
        "unavailable",
        "url must be on loopback",
        "unavailable",
    )


def test_each_url_keeps_its_own_cache_and_the_cron_path_reads_them_too(tmp_path: Path) -> None:
    from telegram_dashboard.external import read_sources

    entries = ({"url": EXT_URL}, {"url": "http://127.0.0.1:18081/limits"})
    env = Environment(hermes_home=tmp_path, limits_sources=entries)
    caches: dict = {}
    calls: list[str] = []

    snapshot = collect_all(
        env,
        FakeRunner(CommandResult(0, "", "")),
        now=NOW,
        resolve_limits=lambda: None,
        external_caches=caches,
        external_fetch=_ext_fetch(EXT_ANSWER, calls),
    )

    assert set(caches) == {source.key for source in read_sources(list(entries))}
    assert sorted(calls) == sorted(entry["url"] for entry in entries)
    assert [q.provider for q in snapshot.capacity.quotas].count("Claude") == 2


# ---------------------------------------------------------------- Gemini 429s from the log (0.8.1)

_GEMINI_REASON = "Google reports Gemini quota only with billing enabled"
_GEMINI_LINE = QuotaMetric("Gemini", "unsupported", detail=_GEMINI_REASON, refusal=Refusal())
_TTS_429 = (
    "ERROR tools.tts_tool: TTS generation failed (gemini): Gemini TTS API error (HTTP 429): "
    "Quota exceeded, limit: 10, model: gemini-2.5-flash-preview-tts. Please retry in {retry}s."
)


def _gemini_log(home: Path, *, minutes_ago: float, retry: str = "41.53") -> str:
    """A 429 in the engine's log, stamped the way the engine stamps it: the host's local time
    without an offset. Returns the moment as the collector reports it."""
    moment = NOW - timedelta(minutes=minutes_ago)
    stamp = moment.astimezone().strftime("%Y-%m-%d %H:%M:%S,000")
    (home / "logs").mkdir()
    entry = f"{stamp} {_TTS_429.format(retry=retry)}\n"
    (home / "logs" / "errors.log").write_text(entry, encoding="utf-8")
    return moment.isoformat()


def test_the_cron_path_reads_the_gemini_log_and_closes_the_limits_with_it(tmp_path: Path) -> None:
    at = _gemini_log(tmp_path, minutes_ago=5)
    cache: dict = {}

    snapshot = collect_all(
        Environment(hermes_home=tmp_path),
        FakeRunner(CommandResult(0, "", "")),
        now=NOW,
        resolve_limits=lambda: None,
        gemini_cache=cache,
    )

    gemini = snapshot.capacity.quotas[-1]
    assert (gemini.provider, gemini.kind, gemini.detail) == (
        "Gemini",
        "unsupported",
        _GEMINI_REASON,
    )
    assert gemini.refusal is not None
    assert (gemini.refusal.at, gemini.refusal.limit) == (at, 10)
    assert [q.provider for q in snapshot.capacity.quotas].count("Gemini") == 1
    names = [source.name for source in snapshot.sources]
    assert names.index("gemini_log") == names.index("drift") - 1
    assert snapshot.sources[names.index("gemini_log")].state == "fresh"
    assert cache["last_429"]["at"] == at
    # A per-minute 429 is no event.
    assert not any(i.incident_id.startswith("gemini:") for i in snapshot.incidents)


def test_limits_off_leave_the_gemini_log_unread(tmp_path: Path) -> None:
    _gemini_log(tmp_path, minutes_ago=5)
    cache: dict = {}

    snapshot = collect_all(
        Environment(hermes_home=tmp_path, limits_enabled=False),
        FakeRunner(CommandResult(0, "", "")),
        now=NOW,
        gemini_cache=cache,
    )

    gemini = snapshot.capacity.quotas[-1]
    assert (gemini.provider, gemini.detail, gemini.refusal) == (
        "Gemini",
        "limits disabled in config",
        None,
    )
    assert [q.provider for q in snapshot.capacity.quotas].count("Gemini") == 1
    source = next(s for s in snapshot.sources if s.name == "gemini_log")
    assert (source.state, source.detail) == ("unsupported", "disabled")
    assert cache == {}


def test_an_external_gemini_source_keeps_the_429_the_log_saw() -> None:
    """Decision 9: a source answering for Gemini takes the line's place with its numbers; the
    refusal the log saw moves to the new line, so the details and the event stay."""
    from telegram_dashboard.collect import merge_external, merge_quotas

    seen = Refusal(at="2026-09-09T20:55:00+00:00", limit=10)
    logged = QuotaMetric("Gemini", "unsupported", detail=_GEMINI_REASON, refusal=seen)
    metric, _source, _incidents = _ext({**EXT_ANSWER, "provider": "Gemini"})

    merged = merge_external(merge_quotas(_facade_capacity(), logged), [metric])

    assert [q.provider for q in merged.quotas] == ["Claude", "Codex", "Gemini"]
    assert merged.quotas[-1].kind == "official"
    assert merged.quotas[-1].refusal == seen


def test_the_cron_path_turns_a_daily_429_into_an_event(tmp_path: Path) -> None:
    """A retry of four hours is the daily quota: the event and the yellow status until the
    reset, on the cron path as on the tick."""
    at = _gemini_log(tmp_path, minutes_ago=5, retry="14580")

    snapshot = collect_all(
        Environment(hermes_home=tmp_path),
        FakeRunner(CommandResult(0, "", "")),
        now=NOW,
        resolve_limits=lambda: None,
    )

    events = [(i.incident_id, i.severity, i.title) for i in snapshot.incidents]
    assert ("gemini:day_quota", "warning", "Gemini daily limit used up") in events
    assert snapshot.overall == "warning"
    gemini = next(q for q in snapshot.capacity.quotas if q.provider == "Gemini")
    assert gemini.refusal is not None and gemini.refusal.at == at


def test_a_per_minute_429_leaves_a_fresh_screen_green_and_a_daily_one_turns_it_yellow(
    tmp_path: Path,
) -> None:
    """Decision of 29.09 at the level the status is derived: every other source fresh, a
    per-minute 429 adds no event and the screen stays green; a daily one is the event."""
    from telegram_dashboard.collect import build_snapshot
    from telegram_dashboard.gemini_log import collect_gemini
    from telegram_dashboard.schema import CapacitySummary, SourceObservation

    gateway = SourceObservation("gateway_state", "official", "fresh", observed_at=NOW.isoformat())
    overall = {}
    for retry in ("41.53", "14580"):
        home = tmp_path / retry
        home.mkdir()
        _gemini_log(home, minutes_ago=5, retry=retry)
        line, source, incidents = collect_gemini(Environment(hermes_home=home), {}, now=NOW)
        snapshot = build_snapshot(
            now=NOW,
            gateway=None,
            drift=None,
            capacity=CapacitySummary((line,)),
            sources=(gateway, source),
            incidents=incidents,
        )
        overall[retry] = snapshot.overall

    assert overall == {"41.53": "normal", "14580": "warning"}


def test_the_gemini_log_follows_the_external_sources_and_its_line_closes_the_block(
    tmp_path: Path,
) -> None:
    (tmp_path / "logs").mkdir()
    env = Environment(hermes_home=tmp_path, limits_sources=({"url": EXT_URL},))

    snapshot = collect_all(
        env,
        FakeRunner(CommandResult(0, "", "")),
        now=NOW,
        resolve_limits=lambda: None,
        external_fetch=_ext_fetch({**EXT_ANSWER, "provider": "Mistral"}),
    )

    names = [source.name for source in snapshot.sources]
    assert names.index("Mistral limits") < names.index("gemini_log") < names.index("drift")
    assert [q.provider for q in snapshot.capacity.quotas][-2:] == ["Mistral", "Gemini"]


@pytest.mark.parametrize("limits_enabled", [False, True], ids=["limits-off", "no-facade"])
def test_every_provider_has_one_line_when_the_facade_cannot_answer(
    tmp_path: Path, limits_enabled: bool
) -> None:
    """Limits off, or no facade on this installation: the block's verdict reaches every line,
    once."""
    snapshot = collect_all(
        Environment(hermes_home=tmp_path, limits_enabled=limits_enabled),
        FakeRunner(CommandResult(0, "", "")),
        now=NOW,
        resolve_limits=lambda: None,
    )

    providers = [q.provider for q in snapshot.capacity.quotas]
    assert providers == ["Claude", "Codex", "Gemini"]


# ---------------------------------------------------------------- cron (0.9.0)


def test_the_cron_line_on_the_cron_path_takes_its_streak_from_the_history(tmp_path: Path) -> None:
    """The synchronous composition reads jobs.json and the history in one go: the delivery
    failure is on the line with the streak the history counts, both sources fresh, the record
    stamped."""
    from datetime import timedelta

    from cron_fixtures import make_db

    cron = tmp_path / "cron"
    cron.mkdir()
    job = {
        "id": "b1",
        "name": "daily-digest",
        "enabled": True,
        "state": "scheduled",
        "last_status": "delivery_failed",
        "last_delivery_error": "Not connected",
        "last_run_at": (NOW - timedelta(minutes=5)).isoformat(),
    }
    (cron / "jobs.json").write_text(json.dumps({"jobs": [job]}), encoding="utf-8")
    make_db(
        tmp_path,
        [
            ("b1", "completed", "failed", (NOW - timedelta(minutes=5)).isoformat()),
            ("b1", "completed", "failed", (NOW - timedelta(minutes=35)).isoformat()),
        ],
    )
    cache: dict = {}

    snapshot = collect_all(
        Environment(hermes_home=tmp_path),
        FakeRunner(CommandResult(0, "", "")),
        now=NOW,
        resolve_limits=lambda: None,
        cron_cache=cache,
    )

    assert snapshot.cron is not None and snapshot.cron.state == "failing"
    assert snapshot.cron.failing[0].streak == 2
    assert [i.title for i in snapshot.incidents] == ["Cron undelivered: daily-digest"]
    states = {s.name: s.state for s in snapshot.sources}
    assert states["cron"] == "fresh" and states["cron_runs"] == "fresh"
    assert cache["runs_checked_at"] == NOW.isoformat()


# ---------------------------------------------------------------- the usage facade on a cadence (#16)
#
# With the record's cache each facade provider is asked once per ``facade_refresh_seconds``,
# success or failure, like an external source; between attempts its line is the last answer
# with its own ``fetched_at``. Without a record (the cron path) every run asks, as before.


class _Facade:
    """A facade whose snapshots are stamped by the test's clock; it counts its calls."""

    def __init__(self, answer=None) -> None:
        self.now = NOW
        self.calls: list[str] = []
        self.answer = answer or (lambda provider, now: _snapshot(fetched_at=now))

    def __call__(self, provider: str):
        self.calls.append(provider)
        return self.answer(provider, self.now)


def _facade_tick(tmp_path: Path, facade: _Facade, caches: dict, at: datetime):
    facade.now = at
    return collect_limits(
        Environment(hermes_home=tmp_path),
        now=at,
        resolve=lambda: facade,
        caches=caches,
        interval_seconds=300,
    )


def test_the_facade_is_asked_once_per_cadence_and_the_record_serves_the_ticks_between(
    tmp_path: Path,
) -> None:
    facade = _Facade()
    caches: dict = {}

    lines = [
        _facade_tick(tmp_path, facade, caches, NOW + timedelta(seconds=s))[0]
        for s in (0, 60, 120, 180, 240)
    ]

    assert facade.calls == ["anthropic", "openai-codex"]
    assert all(q.kind == "official" for line in lines for q in line.quotas)
    assert all(q.fetched_at == NOW.isoformat() for line in lines for q in line.quotas)

    later, source, _probe = _facade_tick(tmp_path, facade, caches, NOW + timedelta(seconds=300))

    assert facade.calls == ["anthropic", "openai-codex"] * 2
    assert later.quotas[0].fetched_at == (NOW + timedelta(seconds=300)).isoformat()
    assert source.state == "fresh"
    assert set(caches) == {"claude", "codex"}
    assert caches["codex"]["attempted_at"] == (NOW + timedelta(seconds=300)).isoformat()


def test_the_record_keeps_the_attempt_the_item_the_last_answer_and_the_plan(
    tmp_path: Path,
) -> None:
    """Per provider: ``attempted_at``, the item exactly as the reader builds it, ``answered_at``
    (the last snapshot the facade gave) and ``plan``. The facade's ``None`` is an attempt
    without an answer, counted in ``none_attempts`` (issue #14) until the next answer."""

    def answer(provider: str, now: datetime):
        return _snapshot(fetched_at=now, plan="Prolite") if provider == "openai-codex" else None

    caches: dict = {}

    _facade_tick(tmp_path, _Facade(answer), caches, NOW)

    assert caches["codex"] == {
        "attempted_at": NOW.isoformat(),
        "answered_at": NOW.isoformat(),
        "plan": "Prolite",
        "item": {
            "provider": "codex",
            "status": "available",
            "reason": None,
            "source": "anthropic-oauth",
            "fetched_at": NOW.isoformat(),
            "plan": "Prolite",
            "windows": [
                {"label": "5h", "used_percent": 37.5, "reset_at": "2026-09-10T00:00:00+00:00"}
            ],
        },
    }
    assert caches["claude"] == {
        "attempted_at": NOW.isoformat(),
        "none_attempts": 1,
        "item": {
            "provider": "claude",
            "status": "unavailable",
            "reason": "none",
            "source": None,
            "fetched_at": None,
            "windows": [],
        },
    }


def test_a_snapshot_without_numbers_is_an_answer_and_a_failure_is_not(tmp_path: Path) -> None:
    """Claude on an API key answers with ``unavailable_reason`` and no windows: the account is
    there, ``answered_at`` moves. An exception is an attempt without an answer, kept by its
    class name, never its text."""

    def answer(provider: str, now: datetime):
        if provider == "openai-codex":
            raise RuntimeError("refresh failed for the account in this message")
        reason = "Anthropic account limits are only available for OAuth-backed Claude accounts."
        return _snapshot(available=False, windows=(), fetched_at=now, unavailable_reason=reason)

    caches: dict = {}

    capacity, source, _probe = _facade_tick(tmp_path, _Facade(answer), caches, NOW)

    assert caches["claude"]["answered_at"] == NOW.isoformat()
    assert "answered_at" not in caches["codex"]
    assert caches["codex"]["item"]["reason"] == "RuntimeError"
    assert "refresh failed" not in json.dumps(caches)
    assert [q.kind for q in capacity.quotas] == ["unavailable", "unavailable"]
    assert capacity.quotas[1].detail == "RuntimeError"
    assert source.state == "unavailable"


def test_a_failed_attempt_is_no_data_for_the_whole_cadence_and_stays_with_its_provider(
    tmp_path: Path,
) -> None:
    def answer(provider: str, now: datetime):
        if provider == "openai-codex":
            raise RuntimeError("token refresh failed")
        return _snapshot(fetched_at=now)

    facade = _Facade(answer)
    caches: dict = {}

    for s in (0, 60, 120, 180, 240):
        capacity, source, _probe = _facade_tick(
            tmp_path, facade, caches, NOW + timedelta(seconds=s)
        )
        claude, codex = capacity.quotas
        assert (claude.kind, codex.kind, codex.detail) == (
            "official",
            "unavailable",
            "RuntimeError",
        )
        assert source.state == "fresh"
    assert facade.calls == ["anthropic", "openai-codex"]

    _facade_tick(tmp_path, facade, caches, NOW + timedelta(seconds=300))
    assert facade.calls.count("openai-codex") == 2


def test_the_plan_outlives_a_failed_attempt(tmp_path: Path) -> None:
    def answer(provider: str, now: datetime):
        if now != NOW:
            return None
        return _snapshot(fetched_at=now, plan="Prolite" if provider == "openai-codex" else "Max 5x")

    caches: dict = {}
    facade = _Facade(answer)

    _facade_tick(tmp_path, facade, caches, NOW)
    capacity, _source, _probe = _facade_tick(tmp_path, facade, caches, NOW + timedelta(minutes=5))

    assert [(q.provider, q.kind, q.plan) for q in capacity.quotas] == [
        ("Claude", "unavailable", "Max 5x"),
        ("Codex", "unavailable", "Prolite"),
    ]
    assert caches["codex"]["item"]["reason"] == "none" and caches["codex"]["plan"] == "Prolite"


def test_a_cached_number_older_than_the_cadence_is_not_a_number(tmp_path: Path) -> None:
    """A clock jump or a hand-edited record: the attempt is recent, its number is not."""
    old = (NOW - timedelta(hours=2)).isoformat()

    def cached(key: str) -> dict:
        window = {"label": "5h", "used_percent": 40.0, "reset_at": None}
        item = {"provider": key, "status": "available", "fetched_at": old, "windows": [window]}
        return {"attempted_at": (NOW - timedelta(minutes=1)).isoformat(), "item": item}

    caches = {"claude": cached("claude"), "codex": cached("codex")}
    facade = _Facade()

    capacity, source, _probe = _facade_tick(tmp_path, facade, caches, NOW)

    assert facade.calls == []
    assert [(q.kind, q.detail) for q in capacity.quotas] == [
        ("unavailable", "cached number too old")
    ] * 2
    assert source.state == "unavailable"


def test_a_long_cadence_keeps_its_number_fresh_between_attempts(tmp_path: Path) -> None:
    """B1 review M5, the cadence half of the ``limits`` ttl rule: at
    ``facade_refresh_seconds=1800`` and a 60 s period a number read 1000 s ago is between two
    attempts, and the source stays ``fresh`` (ttl = max(900 s, 2 x cadence)); a ttl that forgot
    the cadence would flash ``limits (stale)`` until the next attempt."""
    from telegram_dashboard.workers import Flights

    at = (NOW - timedelta(seconds=1000)).isoformat()

    def cached(key: str) -> dict:
        window = {"label": "5h", "used_percent": 40.0, "reset_at": None}
        item = {"provider": key, "status": "available", "fetched_at": at, "windows": [window]}
        return {"attempted_at": at, "item": item, "answered_at": at}

    caches = {"claude": cached("claude"), "codex": cached("codex")}
    facade = _Facade()

    capacity, source, _probe = asyncio.run(
        collect_limits_async(
            Environment(hermes_home=tmp_path),
            now=NOW,
            resolve=lambda: facade,
            timeout_seconds=5,
            flights=Flights(),
            caches=caches,
            interval_seconds=1800,
            period_seconds=60,
        )
    )

    assert facade.calls == []
    assert [q.kind for q in capacity.quotas] == ["official", "official"]
    assert (source.state, source.observed_at) == ("fresh", at)


def test_a_crash_after_the_facade_call_is_an_attempt_kept_for_the_cadence(tmp_path: Path) -> None:
    """Our own code failing on an answer (here windows that are no list) must not turn the
    cadence into every tick: the attempt is recorded with the class name, like the release
    check's (``collect._version_attempt``)."""
    facade = _Facade(lambda provider, now: _snapshot(fetched_at=now, windows=42))
    caches: dict = {}

    for s in (0, 60, 120):
        capacity, _source, _probe = _facade_tick(
            tmp_path, facade, caches, NOW + timedelta(seconds=s)
        )
        assert [q.detail for q in capacity.quotas] == ["collector crashed: TypeError"] * 2

    assert facade.calls == ["anthropic", "openai-codex"]
    assert caches["claude"]["attempted_at"] == NOW.isoformat()
    assert caches["claude"]["answered_at"] == NOW.isoformat()  # the facade did answer


def test_a_system_exit_from_the_facade_is_an_attempt_and_still_reaches_the_worker_boundary(
    tmp_path: Path,
) -> None:
    """B1 review M4: a ``BaseException`` outside ``Exception`` out of the facade is recorded as
    an attempt like any crash, then re-raised: the worker boundary turns it into this tick's
    miss, and the next ticks do not ask that provider again before its interval."""
    from telegram_dashboard.workers import Flights

    calls: list[str] = []

    def fetch(provider: str):
        calls.append(provider)
        if provider == "anthropic":
            raise SystemExit(3)
        return _snapshot(fetched_at=NOW)

    caches: dict = {}
    flights = Flights()

    def tick(seconds: float):
        return collect_limits_async(
            Environment(hermes_home=tmp_path),
            now=NOW + timedelta(seconds=seconds),
            resolve=lambda: fetch,
            timeout_seconds=5,
            flights=flights,
            caches=caches,
            interval_seconds=300,
        )

    async def scenario():
        return [(await tick(s))[0] for s in (0, 60, 120)]

    first, second, third = asyncio.run(scenario())

    assert calls == ["anthropic", "openai-codex"]
    assert caches["claude"]["attempted_at"] == NOW.isoformat()
    assert caches["claude"]["item"]["reason"] == "collector crashed: SystemExit"
    assert "answered_at" not in caches["claude"]
    assert [(q.provider, q.detail) for q in first.quotas] == [
        ("Claude", "collector crashed: SystemExit"),
        ("Codex", "error: SystemExit"),
    ]
    for later in (second, third):
        assert later.quotas[0].detail == "collector crashed: SystemExit"
        assert later.quotas[1].kind == "official"


# A string a token could look like; planted, never real.
_PLANTED = "sk-ant-oat01-PLANTEDfakeTOKENforThisTestOnly0000"


def test_identity_raw_details_and_a_token_never_reach_the_record_or_the_screen(
    tmp_path: Path,
) -> None:
    """Of a snapshot only the fields the screen uses are read: the account's principal
    (``identity``), the whole answer (``raw``), the details (a credit balance) and a
    token-looking string in any of them stay out of the record's ``facade_cache``, the snapshot
    and the screen."""
    from telegram_dashboard.render import render_dashboard, to_telegram_html

    def answer(provider: str, now: datetime):
        return _snapshot(
            fetched_at=now,
            plan="Prolite",
            identity="principal-4242@example.invalid",
            raw={"access_token": _PLANTED, "account_id": "principal-4242"},
            details={"credits_balance": "1234.56", "refresh_token": _PLANTED},
            access_token=_PLANTED,
        )

    caches: dict = {}

    snapshot = collect_all(
        Environment(hermes_home=tmp_path),
        FakeRunner(CommandResult(0, "", "")),
        now=NOW,
        resolve_limits=lambda: _Facade(answer),
        facade_caches=caches,
    )

    stored = json.dumps(caches)
    screen = render_dashboard(snapshot, now=NOW)
    assert "Prolite" in stored and "Codex 38%" in screen  # what is read reaches both
    for text in (stored, screen, to_telegram_html(screen), repr(snapshot)):
        for planted in (
            "sk-ant-oat01",
            "PLANTED",
            "principal-4242",
            "1234.56",
            "credits",
            "_token",
        ):
            assert planted not in text, planted


def test_a_token_in_a_field_that_is_read_is_masked_and_cut_before_the_record(
    tmp_path: Path,
) -> None:
    """B1 review M2: the fields ``limits.py`` does read (the reason, the source, the plan, a
    window's label) are cleaned and cut to the screen's lengths before they reach
    ``facade_cache``, as an external source's are: a token-looking string there never lands in
    the state file, and a 500-character plan is kept as the 24 the screen shows."""
    window = SimpleNamespace(label=f"5h {_PLANTED}", used_percent=10.0, reset_at=None)

    def answer(provider: str, now: datetime):
        if provider == "anthropic":
            return _snapshot(
                available=False,
                windows=(),
                fetched_at=now,
                unavailable_reason=f"refused for key {_PLANTED}",
                source=f"oauth {_PLANTED}",
            )
        return _snapshot(fetched_at=now, plan="P" * 500, windows=(window,))

    caches: dict = {}

    capacity, _source, _probe = _facade_tick(tmp_path, _Facade(answer), caches, NOW)

    stored = json.dumps(caches)
    assert "PLANTED" not in stored and "sk-ant-oat01" not in stored
    assert caches["claude"]["item"]["reason"] == "refused for key [secret]"
    assert caches["claude"]["item"]["source"] == "oauth [secret]"
    assert caches["codex"]["item"]["windows"][0]["label"] == "5h [secret]"
    assert caches["codex"]["plan"] == "P" * 23 + "…" == caches["codex"]["item"]["plan"]
    assert capacity.quotas[0].detail == "refused for key [secret]"
    assert capacity.quotas[1].plan == "P" * 23 + "…"


def test_the_cron_path_without_a_record_asks_the_facade_on_every_run(tmp_path: Path) -> None:
    facade = _Facade()

    for _ in range(2):
        collect_all(
            Environment(hermes_home=tmp_path),
            FakeRunner(CommandResult(0, "", "")),
            now=NOW,
            resolve_limits=lambda: facade,
        )

    assert facade.calls == ["anthropic", "openai-codex"] * 2


def test_a_facade_that_cannot_be_imported_is_unsupported_and_writes_no_record(
    tmp_path: Path,
) -> None:
    """Neither form leaves an entry in the record when there is no facade to ask: the tick's
    form makes the provider dicts on the loop before its worker starts and drops them again
    when the worker asked nobody (B1 review M7)."""

    def broken_resolver():
        raise ImportError("engine half-installed")

    sync_caches: dict = {}
    async_caches: dict = {}

    async def tick(resolve):
        return await collect_limits_async(
            Environment(hermes_home=tmp_path),
            now=NOW,
            resolve=resolve,
            timeout_seconds=5,
            caches=async_caches,
        )

    for resolve in (lambda: None, broken_resolver):
        _capacity, source, probe = collect_limits(
            Environment(hermes_home=tmp_path), now=NOW, resolve=resolve, caches=sync_caches
        )
        assert probe.status == "unsupported" and source.state == "unsupported"
        _capacity, source, probe = asyncio.run(tick(resolve))
        assert probe.status == "unsupported" and source.state == "unsupported"

    assert sync_caches == {} and async_caches == {}


def test_a_facade_worker_abandoned_by_its_deadline_records_its_attempt_when_it_returns() -> None:
    """The cadence on the worker's side: a deadline or a busy worker shows the last answer or
    "no data" with the reason; the abandoned worker writes its attempt into the record when it
    returns, and the next tick serves it without calling again."""
    from probe_fakes import until

    from telegram_dashboard.limits import Cadence, fetch_limits_payload_off_loop
    from telegram_dashboard.workers import Flights

    calls: list[str] = []

    def fetch(provider: str):
        calls.append(provider)
        time.sleep(0.2)
        return _snapshot(fetched_at=NOW)

    flights, caches = Flights(), {}

    def ask(seconds: float, timeout: float):
        cadence = Cadence(caches, NOW + timedelta(seconds=seconds), 300.0, 600.0)
        return fetch_limits_payload_off_loop(
            lambda: fetch, timeout_seconds=timeout, flights=flights, cadence=cadence
        )

    async def scenario():
        first = await ask(0, 0.05)
        second = await ask(60, 0.05)
        await until(lambda: not flights.busy("limits"))
        third = await ask(120, 2.0)
        return first, second, third

    first, second, third = asyncio.run(scenario())

    assert [p["reason"] for p in first["providers"]] == [
        "timeout: facade did not answer within 0.05s"
    ] * 2
    assert [p["reason"] for p in second["providers"]] == [
        "busy: the previous facade call has not returned yet"
    ] * 2
    assert [p["status"] for p in third["providers"]] == ["available"] * 2
    assert calls == ["anthropic", "openai-codex"]
    assert caches["claude"]["attempted_at"] == NOW.isoformat()


def test_one_provider_s_hang_leaves_the_other_s_failure_row_alone(tmp_path: Path) -> None:
    """B1 review M1: Claude has no credential, its attempt a minute old still stands; Codex is
    due and its call hangs past the deadline, then keeps the worker busy. On both ticks Claude
    keeps its own row, not the other provider's miss, and is not asked again."""
    from telegram_dashboard.workers import Flights

    calls: list[str] = []

    def fetch(provider: str):
        calls.append(provider)
        time.sleep(0.4)
        return _snapshot(fetched_at=NOW)

    claude_item = {
        "provider": "claude",
        "status": "unavailable",
        "reason": "none",
        "source": None,
        "fetched_at": None,
        "windows": [],
    }
    caches: dict = {
        "claude": {"attempted_at": (NOW - timedelta(minutes=1)).isoformat(), "item": claude_item}
    }
    flights = Flights()

    def tick(at: datetime):
        return collect_limits_async(
            Environment(hermes_home=tmp_path),
            now=at,
            resolve=lambda: fetch,
            timeout_seconds=0.05,
            flights=flights,
            caches=caches,
            interval_seconds=300,
            period_seconds=60,
        )

    async def scenario():
        first = await tick(NOW)
        second = await tick(NOW + timedelta(seconds=1))
        return first[0], second[0]

    first, second = asyncio.run(scenario())

    assert calls == ["openai-codex"]
    assert [(q.provider, q.detail) for q in first.quotas] == [
        ("Claude", "no answer: no credential or the call failed"),
        ("Codex", "timeout: facade did not answer within 0.05s"),
    ]
    assert [(q.provider, q.detail) for q in second.quotas] == [
        ("Claude", "no answer: no credential or the call failed"),
        ("Codex", "busy: the previous facade call has not returned yet"),
    ]


def test_a_day_at_a_60_second_period_asks_each_provider_288_times(tmp_path: Path) -> None:
    """The request count of issue #16 on the tick's own path (the worker, the record): a day of
    ticks at the default 60 s period, each waking up to a second early, at the default
    ``facade_refresh_seconds`` (300 s) asks each provider 288 times, not 1,440."""
    from telegram_dashboard.collect import FACADE_DEFAULT_INTERVAL_SECONDS
    from telegram_dashboard.workers import Flights

    facade = _Facade()
    caches: dict = {}
    flights = Flights()

    async def day() -> None:
        for k in range(24 * 60):
            early = (k * 7919 % 1000) / 1000  # a fixed spread of 0 to 0.999 s
            at = NOW + timedelta(seconds=60 * k - (early if k else 0))
            facade.now = at
            await collect_limits_async(
                Environment(hermes_home=tmp_path),
                now=at,
                resolve=lambda: facade,
                timeout_seconds=5,
                flights=flights,
                caches=caches,
                period_seconds=60,
            )

    asyncio.run(day())

    assert FACADE_DEFAULT_INTERVAL_SECONDS == 300.0
    assert facade.calls.count("anthropic") == 288
    assert facade.calls.count("openai-codex") == 288


# ---------------------------------------------------------------- not configured (#14)
#
# The facade answers ``None`` alike for "no credential" and "the call failed". The record
# decides: a provider that never answered is not configured after three ``None`` attempts in a
# row, one that answered before after a week of silence; any answer brings it back. Without a
# record (the cron path) ``None`` is not configured at once.

_NO_ANSWER = "no answer: no credential or the call failed"


def _none_item(key: str) -> dict:
    return {
        "provider": key,
        "status": "unavailable",
        "reason": "none",
        "source": None,
        "fetched_at": None,
        "windows": [],
    }


def _quiet_cache(key: str, nones: int, *, answered: datetime | None = None) -> dict:
    """A provider whose last attempt (due again) was a ``None``."""
    cache = {
        "attempted_at": (NOW - timedelta(seconds=400)).isoformat(),
        "item": _none_item(key),
        "none_attempts": nones,
    }
    if answered is not None:
        cache["answered_at"] = answered.isoformat()
    return cache


def test_a_provider_that_never_answered_is_hidden_after_exactly_the_threshold_of_nones(
    tmp_path: Path,
) -> None:
    """The published promise of #14: a provider that has never answered since the record began
    is a "no data" line with the reason for its first two ``None`` attempts and has no line from
    the third on, between attempts too. It is still asked on its cadence."""
    from telegram_dashboard.limits import NOT_CONFIGURED_AFTER

    def answer(provider: str, now: datetime):
        return None if provider == "anthropic" else _snapshot(fetched_at=now)

    facade = _Facade(answer)
    caches: dict = {}
    seen = []

    for k in range(4):
        at = NOW + timedelta(seconds=300 * k)
        on, on_source, _ = _facade_tick(tmp_path, facade, caches, at)
        between, between_source, _ = _facade_tick(
            tmp_path, facade, caches, at + timedelta(seconds=60)
        )
        claude = (on.quotas[0].kind, on.quotas[0].detail, between.quotas[0].kind)
        seen.append((*claude, caches["claude"]["none_attempts"]))
        assert on_source.state == between_source.state == "fresh"  # Codex has a line

    assert NOT_CONFIGURED_AFTER == 3
    assert seen == [
        ("unavailable", _NO_ANSWER, "unavailable", 1),
        ("unavailable", _NO_ANSWER, "unavailable", 2),
        ("not_configured", "not configured", "not_configured", 3),
        ("not_configured", "not configured", "not_configured", 4),
    ]
    assert "answered_at" not in caches["claude"]
    assert caches["claude"]["item"]["reason"] == "none"  # the record keeps the attempt itself
    assert facade.calls.count("anthropic") == 4


def test_an_answer_brings_a_hidden_provider_back_at_once_and_clears_its_count(
    tmp_path: Path,
) -> None:
    answers = iter([_snapshot(fetched_at=NOW), None])
    facade = _Facade(lambda provider, now: next(answers) if provider == "anthropic" else None)
    caches = {"claude": _quiet_cache("claude", 7)}

    back, _source, _probe = _facade_tick(tmp_path, facade, caches, NOW)

    assert back.quotas[0].kind == "official"
    assert caches["claude"]["answered_at"] == NOW.isoformat()
    assert "none_attempts" not in caches["claude"]

    # It answered a moment ago: its next None is a shown line, the count starts again.
    quiet, _source, _probe = _facade_tick(tmp_path, facade, caches, NOW + timedelta(minutes=5))

    assert (quiet.quotas[0].kind, quiet.quotas[0].detail) == ("unavailable", _NO_ANSWER)
    assert caches["claude"]["none_attempts"] == 1


def test_a_provider_that_answered_then_went_quiet_is_shown_for_seven_days_then_hidden(
    tmp_path: Path,
) -> None:
    """The second half of the promise: a provider that answered before is a "no data" line while
    its last answer is at most seven days old, however many ``None`` attempts followed, and has
    no line after that."""
    from telegram_dashboard.limits import QUIET_SHOWN_SECONDS

    answered = NOW - timedelta(seconds=QUIET_SHOWN_SECONDS) + timedelta(minutes=10)
    caches = {"claude": _quiet_cache("claude", 500, answered=answered)}
    facade = _Facade(lambda provider, now: None if provider == "anthropic" else _snapshot())

    kinds = []
    for seconds in (0, 599, 601, 900):
        capacity, _source, _probe = _facade_tick(
            tmp_path, facade, caches, NOW + timedelta(seconds=seconds)
        )
        kinds.append(capacity.quotas[0].kind)

    assert QUIET_SHOWN_SECONDS == 7 * 86400
    assert kinds == ["unavailable", "unavailable", "not_configured", "not_configured"]
    assert caches["claude"]["answered_at"] == answered.isoformat()


def test_an_answer_stamped_ahead_of_the_clock_does_not_keep_a_quiet_provider_shown(
    tmp_path: Path,
) -> None:
    """B2 review M-c: an ``answered_at`` ahead of the clock beyond the skew tolerance (the clock
    was set back after an answer) is no usable answer. The count of ``None`` attempts decides,
    as for a provider that never answered, instead of a year of "no data". One a moment ahead,
    within the tolerance, is an answer like any other."""
    from telegram_dashboard.timeparse import CLOCK_SKEW_TOLERANCE_SECONDS

    facade = _Facade(lambda provider, now: None if provider == "anthropic" else _snapshot())
    ahead = NOW + timedelta(days=365)
    caches = {"claude": _quiet_cache("claude", 0, answered=ahead)}

    kinds = []
    for k in range(4):
        capacity, _source, _probe = _facade_tick(
            tmp_path, facade, caches, NOW + timedelta(seconds=300 * k)
        )
        kinds.append(capacity.quotas[0].kind)

    assert kinds == ["unavailable", "unavailable", "not_configured", "not_configured"]
    assert caches["claude"]["answered_at"] == ahead.isoformat()  # a None moves no answer

    near = NOW + timedelta(seconds=CLOCK_SKEW_TOLERANCE_SECONDS - 1)
    caches = {"claude": _quiet_cache("claude", 500, answered=near)}
    capacity, _source, _probe = _facade_tick(tmp_path, facade, caches, NOW)
    assert (capacity.quotas[0].kind, capacity.quotas[0].detail) == ("unavailable", _NO_ANSWER)


def test_an_exception_from_the_facade_neither_counts_nor_answers(tmp_path: Path) -> None:
    """A provider's exception is no ``None`` and no answer (the real facade swallows them; a
    fake or a late ``remember_account_usage`` raises): the count stays, and a provider that was
    hidden stays hidden while one that was shown shows the class name."""

    def answer(provider: str, now: datetime):
        raise RuntimeError("refresh failed")

    caches = {"claude": _quiet_cache("claude", 2), "codex": _quiet_cache("codex", 3)}

    capacity, _source, _probe = _facade_tick(tmp_path, _Facade(answer), caches, NOW)

    assert [(q.kind, q.detail) for q in capacity.quotas] == [
        ("unavailable", "RuntimeError"),
        ("not_configured", "not configured"),
    ]
    assert (caches["claude"]["none_attempts"], caches["codex"]["none_attempts"]) == (2, 3)
    assert "answered_at" not in caches["claude"] and "answered_at" not in caches["codex"]


def test_a_missed_deadline_changes_neither_the_count_nor_the_answer() -> None:
    """A deadline or a busy worker is not an answer and not a ``None``: a hidden provider stays
    hidden, a shown one shows the miss, and neither count moves until the worker returns."""
    import copy

    from probe_fakes import until

    from telegram_dashboard.limits import Cadence, fetch_limits_payload_off_loop
    from telegram_dashboard.workers import Flights

    release = threading.Event()

    def fetch(provider: str):
        release.wait(5)
        return None

    caches = {"claude": _quiet_cache("claude", 3), "codex": _quiet_cache("codex", 2)}
    flights = Flights()

    def ask(seconds: float):
        cadence = Cadence(caches, NOW + timedelta(seconds=seconds), 300.0, 600.0)
        return fetch_limits_payload_off_loop(
            lambda: fetch, timeout_seconds=0.05, flights=flights, cadence=cadence
        )

    async def scenario():
        first = await ask(0)
        second = await ask(60)
        record = copy.deepcopy(caches)
        release.set()
        await until(lambda: not flights.busy("limits"))
        return first, second, record

    first, second, record = asyncio.run(scenario())

    for payload, reason in (
        (first, "timeout: facade did not answer within 0.05s"),
        (second, "busy: the previous facade call has not returned yet"),
    ):
        claude, codex = payload["providers"]
        assert (claude["status"], codex["status"], codex["reason"]) == (
            "not_configured",
            "unavailable",
            reason,
        )
    assert (record["claude"]["none_attempts"], record["codex"]["none_attempts"]) == (3, 2)
    assert "answered_at" not in record["claude"] and "answered_at" not in record["codex"]
    # The worker's own attempt, when it returns, is a None like any other.
    assert (caches["claude"]["none_attempts"], caches["codex"]["none_attempts"]) == (4, 3)


@pytest.mark.parametrize(
    "answered_days_ago", [None, 8], ids=["never-answered", "answered-8-days-ago"]
)
def test_a_listed_provider_keeps_its_line_through_a_missed_deadline_and_a_busy_tick(
    tmp_path: Path, answered_days_ago: int | None
) -> None:
    """B2 review M-d: ``limits_providers: [claude]`` pins Claude on a tick whose worker misses
    its deadline and on the next one while it is still busy, too: a record that would hide an
    unlisted provider (three ``None`` attempts in a row, or a last answer 8 days old) leaves a
    listed one its "no data" line with the miss as the reason."""
    from probe_fakes import until

    from telegram_dashboard.workers import Flights

    release = threading.Event()

    def fetch(provider: str):
        release.wait(5)
        return None

    answered = None if answered_days_ago is None else NOW - timedelta(days=answered_days_ago)
    caches = {"claude": _quiet_cache("claude", 3, answered=answered)}
    env = Environment(hermes_home=tmp_path, limits_providers=("claude",))
    flights = Flights()

    def tick(seconds: float):
        return collect_limits_async(
            env,
            now=NOW + timedelta(seconds=seconds),
            resolve=lambda: fetch,
            timeout_seconds=0.05,
            flights=flights,
            caches=caches,
            interval_seconds=300,
        )

    async def scenario():
        first = await tick(0)
        second = await tick(60)
        release.set()
        await until(lambda: not flights.busy("limits"))
        return first[0], second[0]

    first, second = asyncio.run(scenario())

    for capacity, reason in (
        (first, "timeout: facade did not answer within 0.05s"),
        (second, "busy: the previous facade call has not returned yet"),
    ):
        claude, codex = capacity.quotas
        assert (claude.kind, claude.detail, claude.pinned) == ("unavailable", reason, True)
        assert (codex.kind, codex.detail) == ("not_configured", "off in the dashboard settings")


def test_claude_on_an_api_key_is_an_answer_and_keeps_its_line(tmp_path: Path) -> None:
    """A snapshot without numbers (Claude on an API key) is an answer: the account is there, the
    line stays "no data" with the engine's reason however often it comes."""
    reason = "Anthropic account limits are only available for OAuth-backed Claude accounts."

    def answer(provider: str, now: datetime):
        return _snapshot(available=False, windows=(), fetched_at=now, unavailable_reason=reason)

    facade, caches = _Facade(answer), {}
    for k in range(5):
        capacity, _source, _probe = _facade_tick(
            tmp_path, facade, caches, NOW + timedelta(minutes=5 * k)
        )
        assert capacity.quotas[0].kind == "unavailable"
        assert capacity.quotas[0].detail == reason[:59] + "…"

    assert "none_attempts" not in caches["claude"]


def test_the_cron_path_has_no_memory_and_takes_the_facade_s_none_as_not_configured(
    tmp_path: Path,
) -> None:
    """Without a record (``__main__``) there is nothing to remember: the facade's ``None`` is
    not configured at once; an exception still shows its class name."""

    def answer(provider: str, now: datetime):
        if provider == "openai-codex":
            raise RuntimeError("refresh failed")
        return None

    capacity, source, _probe = collect_limits(
        Environment(hermes_home=tmp_path), now=NOW, resolve=lambda: _Facade(answer)
    )

    assert [(q.provider, q.kind, q.detail) for q in capacity.quotas] == [
        ("Claude", "not_configured", "not configured"),
        ("Codex", "unavailable", "RuntimeError"),
    ]
    assert source is not None and source.state == "unavailable"


def test_no_limits_source_at_all_when_no_facade_provider_has_a_line(tmp_path: Path) -> None:
    """Issue #14, the installation without Claude and Codex: once neither has a line there is
    no ``limits`` source, so it leaves the coverage and no longer holds the status at grey;
    before that it is ``limits (unavailable)`` like any failing source."""
    from telegram_dashboard.collect import build_snapshot
    from telegram_dashboard.schema import SourceObservation

    gateway = SourceObservation("gateway_state", "official", "fresh", observed_at=NOW.isoformat())
    facade = _Facade(lambda provider, now: None)
    caches: dict = {}
    seen = []
    for k in range(3):
        capacity, source, _probe = _facade_tick(
            tmp_path, facade, caches, NOW + timedelta(minutes=5 * k)
        )
        sources = (gateway,) if source is None else (gateway, source)
        snapshot = build_snapshot(
            now=NOW, gateway=None, drift=None, capacity=capacity, sources=sources, incidents=()
        )
        seen.append((None if source is None else source.state, snapshot.overall))

    assert seen == [("unavailable", "unknown"), ("unavailable", "unknown"), (None, "normal")]

    cron = collect_all(
        Environment(hermes_home=tmp_path),
        FakeRunner(CommandResult(0, "", "")),
        now=NOW,
        resolve_limits=lambda: facade,
    )
    assert "limits" not in [s.name for s in cron.sources]
    assert [(q.provider, q.kind) for q in cron.capacity.quotas][:2] == [
        ("Claude", "not_configured"),
        ("Codex", "not_configured"),
    ]


# ---------------------------------------------------------------- limits_providers (#14)
#
# The owner's optional list of built-in providers: a provider it names is asked and always has
# its line; one it leaves out is never asked (the facade not called, Gemini's log not read) and
# is named in the details; external sources are not subject to it.


def test_limits_providers_is_read_as_built_in_labels_in_any_case(
    caplog: pytest.LogCaptureFixture,
) -> None:
    import logging

    from telegram_dashboard.compat import parse_limits_providers

    log = logging.getLogger("test.limits_providers")

    assert parse_limits_providers(["Codex", " GEMINI ", "codex"], log) == ("codex", "gemini")
    assert parse_limits_providers(["gemini", "claude"], log) == ("claude", "gemini")
    assert parse_limits_providers(None, log) is None
    assert parse_limits_providers("", log) is None
    assert parse_limits_providers([], log) == ()
    assert caplog.text == ""

    with caplog.at_level("WARNING", logger="test.limits_providers"):
        assert parse_limits_providers(["claude", "grok", 5, "Secret text"], log) == ("claude",)
        assert parse_limits_providers("codex", log) is None
        # B2 review M-b: a list that names no provider at all (a typo) is not set, the
        # automatic rule, never "ask none"; only an empty list written as such asks none.
        assert parse_limits_providers(["codx"], log) is None
        assert parse_limits_providers([None, "Secret text"], log) is None

    warnings = [record.getMessage() for record in caplog.records]
    assert warnings == [
        "limits_providers: entries 2, 3, 4 skipped (each must be claude, codex or gemini)",
        "limits_providers must be a list; ignoring <class 'str'>",
        "limits_providers: entry 1 skipped (each must be claude, codex or gemini); "
        "none left, so the setting is ignored",
        "limits_providers: entries 1, 2 skipped (each must be claude, codex or gemini); "
        "none left, so the setting is ignored",
    ]
    assert "grok" not in caplog.text and "Secret" not in caplog.text and "codx" not in caplog.text


def test_the_fallback_tick_reads_limits_providers_from_its_config(tmp_path: Path) -> None:
    from telegram_dashboard.__main__ import _environment

    home = {"hermes_home": str(tmp_path)}

    assert _environment({**home, "limits_providers": ["Codex"]}).limits_providers == ("codex",)
    assert _environment({**home, "limits_providers": []}).limits_providers == ()
    assert _environment({**home, "limits_providers": ["codx"]}).limits_providers is None
    assert _environment(home).limits_providers is None


def test_a_listed_provider_keeps_its_line_and_one_left_out_is_never_asked(
    tmp_path: Path,
) -> None:
    """``limits_providers: [codex]``: Claude is never asked and has no line; Codex is asked and
    keeps a "no data" line with the reason however many ``None`` attempts it gets, with a record
    and on the cron path alike."""
    facade = _Facade(lambda provider, now: None)
    env = Environment(hermes_home=tmp_path, limits_providers=("codex",))
    caches: dict = {}

    for k in range(4):
        at = NOW + timedelta(minutes=5 * k)
        facade.now = at
        capacity, source, _probe = collect_limits(
            env, now=at, resolve=lambda: facade, caches=caches, interval_seconds=300
        )
        claude, codex = capacity.quotas
        assert (claude.kind, claude.detail) == ("not_configured", "off in the dashboard settings")
        assert (codex.kind, codex.detail, codex.pinned) == ("unavailable", _NO_ANSWER, True)
        assert source is not None and source.state == "unavailable"

    cron, _source, _probe = collect_limits(env, now=NOW, resolve=lambda: facade)

    assert [q.kind for q in cron.quotas] == ["not_configured", "unavailable"]
    assert facade.calls == ["openai-codex"] * 5
    assert set(caches) == {"codex"} and caches["codex"]["none_attempts"] == 4


def test_an_empty_list_asks_no_built_in_provider_at_all(tmp_path: Path) -> None:
    """``limits_providers: []``: neither the facade nor Gemini's log is touched, no limits
    source and no Gemini log source are counted, no Gemini event is raised; the details name
    all three. An external source still has its line."""
    _gemini_log(tmp_path, minutes_ago=5, retry="14580")
    resolved: list[str] = []
    gemini_cache: dict = {}
    facade_caches: dict = {}

    def resolve():
        resolved.append("facade")
        return _Facade()

    snapshot = collect_all(
        Environment(hermes_home=tmp_path, limits_providers=(), limits_sources=({"url": EXT_URL},)),
        FakeRunner(CommandResult(0, "", "")),
        now=NOW,
        resolve_limits=resolve,
        facade_caches=facade_caches,
        gemini_cache=gemini_cache,
        external_fetch=_ext_fetch({**EXT_ANSWER, "provider": "Mistral"}),
    )

    assert resolved == [] and facade_caches == {} and gemini_cache == {}
    assert [(q.provider, q.kind, q.detail) for q in snapshot.capacity.quotas] == [
        ("Claude", "not_configured", "off in the dashboard settings"),
        ("Codex", "not_configured", "off in the dashboard settings"),
        ("Mistral", "official", "external"),
        ("Gemini", "not_configured", "off in the dashboard settings"),
    ]
    names = [source.name for source in snapshot.sources]
    assert "limits" not in names and "gemini_log" not in names and "Mistral limits" in names
    assert not any(i.incident_id.startswith("gemini:") for i in snapshot.incidents)


def test_a_pinned_gemini_is_read_and_marked_and_an_external_source_is_not_subject_to_the_list(
    tmp_path: Path,
) -> None:
    (tmp_path / "logs").mkdir()
    env = Environment(
        hermes_home=tmp_path,
        limits_providers=("gemini",),
        limits_sources=({"url": EXT_URL},),
    )

    snapshot = collect_all(
        env,
        FakeRunner(CommandResult(0, "", "")),
        now=NOW,
        resolve_limits=lambda: _Facade(),
        external_fetch=_ext_fetch(EXT_ANSWER),
    )

    quotas = {q.provider: q for q in snapshot.capacity.quotas}
    # The external source names Claude: it takes the hidden line's place.
    assert (quotas["Claude"].kind, quotas["Claude"].pinned) == ("official", False)
    assert quotas["Codex"].kind == "not_configured"
    assert (quotas["Gemini"].kind, quotas["Gemini"].pinned) == ("unsupported", True)
    assert quotas["Gemini"].refusal == Refusal()
    assert "gemini_log" in [s.name for s in snapshot.sources]


def test_a_listed_provider_without_a_facade_is_unsupported_and_one_left_out_stays_off(
    tmp_path: Path,
) -> None:
    capacity, source, probe = collect_limits(
        Environment(hermes_home=tmp_path, limits_providers=("codex",)),
        now=NOW,
        resolve=lambda: None,
    )

    assert [(q.provider, q.kind) for q in capacity.quotas] == [
        ("Claude", "not_configured"),
        ("Codex", "unsupported"),
    ]
    assert source is not None and source.state == "unsupported"
    assert probe.status == "unsupported"


def test_limits_off_keep_their_lines_whatever_the_list_says(tmp_path: Path) -> None:
    """``limits_enabled: false`` is not this issue's: every line takes the block's verdict, as
    in 1.1.0, list or no list."""
    snapshot = collect_all(
        Environment(hermes_home=tmp_path, limits_enabled=False, limits_providers=("codex",)),
        FakeRunner(CommandResult(0, "", "")),
        now=NOW,
    )

    assert [(q.provider, q.kind) for q in snapshot.capacity.quotas] == [
        ("Claude", "unsupported"),
        ("Codex", "unsupported"),
        ("Gemini", "unsupported"),
    ]
