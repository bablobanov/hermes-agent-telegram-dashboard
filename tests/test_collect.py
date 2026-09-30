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
from telegram_dashboard.schema import QuotaMetric, Refusal

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
        "gemini_log": "unsupported",
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
    from telegram_dashboard.collect import collect_quota

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
    metric, source = collect_quota(
        "x", "Claude", {}, now=NOW, interval_seconds=900, fetch=lambda now: item
    )

    assert (metric.kind, metric.plan, metric.login_expires_at) == (
        "official",
        "Max 5x",
        "2026-10-27T21:07:24Z",
    )
    assert [(w.scope, w.severity) for w in metric.windows] == [("Fable", "critical"), (None, None)]
    assert source.state == "fresh"


def test_an_expired_login_is_its_own_kind_and_the_source_answered() -> None:
    from telegram_dashboard.collect import collect_quota

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
    metric, source = collect_quota(
        "x", "Claude", {}, now=NOW, interval_seconds=900, fetch=lambda now: item
    )

    assert metric.kind == "expired" and metric.plan == "Max 5x"
    assert metric.login_expires_at == "2026-09-01T00:00:00Z"
    assert source.state == "fresh"


def test_grok_s_plan_is_no_longer_dropped() -> None:
    from telegram_dashboard.collect import collect_grok

    item = {
        "provider": "grok",
        "status": "available",
        "reason": None,
        "source": "grok_cli_billing",
        "fetched_at": NOW.isoformat(),
        "plan": "SuperGrok",
        "windows": [{"label": "7d", "used_percent": 31.0, "reset_at": None}],
    }
    metric, _source = collect_grok({}, now=NOW, interval_seconds=900, fetch=lambda now: item)

    assert metric.plan == "SuperGrok"


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
    assert source.state == "unavailable" and metric.login_expires_at == "2026-09-11T20:00:00Z"
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
    once. Grok and Kimi used to be in the facade's verdict and again as lines of their own."""
    snapshot = collect_all(
        Environment(hermes_home=tmp_path, limits_enabled=limits_enabled),
        FakeRunner(CommandResult(0, "", "")),
        now=NOW,
        resolve_limits=lambda: None,
    )

    providers = [q.provider for q in snapshot.capacity.quotas]
    assert providers == ["Claude", "Codex", "Grok", "Kimi", "Gemini"]
