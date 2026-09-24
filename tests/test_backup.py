"""The backup line: age and verdict of the last ``state.db`` copy, read from the status file the
backup timer's root step writes on every run (success or failure), shown always, not only when
something broke. A screen silent about the norm makes silence indistinguishable from confirmation.

Pinned: the line carries the absolute time, the age in words and the integrity verdict; a failed
run and a stale status are named and become events; an unreadable or missing source is a reason,
never a zero; and a broken status file cannot take the rest of the screen down.
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from telegram_dashboard import backup
from telegram_dashboard.collect import CommandResult, build_snapshot, collect_all_async
from telegram_dashboard.compat import Environment, probe_backup
from telegram_dashboard.render import render_dashboard
from telegram_dashboard.schema import BackupSummary, CapacitySummary, Coverage

NOW = datetime(2026, 9, 21, 21, 56, tzinfo=UTC)
FINISHED = NOW - timedelta(hours=6)  # 15:56 UTC


def _status(**overrides: Any) -> dict[str, Any]:
    """What ``state_db_publish.py publish`` writes on success (fictional values)."""
    status: dict[str, Any] = {
        "ok": True,
        "phase": "done",
        "reason": "",
        "invocation_id": "0123456789abcdef0123456789abcdef",
        "snapshot_id": "20260921-153000-daily",
        "snapshot_ts_utc": "2026-09-21T15:30:00Z",
        "state_db_size": 123456789,
        "sha256": "0" * 64,
        "integrity": "ok",
        "messages_count": 4242,
        "copy_path": "/backups/state.db.daily-2026-09-21",
        "copy_size": 123456789,
        "kept": ["state.db.daily-2026-09-20", "state.db.daily-2026-09-21"],
        "started_at": "2026-09-21T17:55:00+02:00",
        "finished_at": "2026-09-21T17:56:00+02:00",
        "finished_epoch": int(FINISHED.timestamp()),
        "duration_s": 60.0,
    }
    status.update(overrides)
    return status


FAILED_REASON = "hermes-шаг не завершился успехом: SERVICE_RESULT=exit-code EXIT_STATUS=1"


def _failed() -> dict[str, Any]:
    """What ``state_db_publish.py record`` writes when the hermes step failed."""
    return {
        "ok": False,
        "phase": "snapshot",
        "reason": FAILED_REASON,
        "invocation_id": "fedcba9876543210fedcba9876543210",
        "finished_at": "2026-09-21T17:56:00+02:00",
        "finished_epoch": int(FINISHED.timestamp()),
    }


# ----------------------------------------------------------------------------- summarising


def test_a_successful_run_is_ok_with_its_time_and_integrity_verdict() -> None:
    summary, source, incidents = backup.summarize(_status(), now=NOW)

    assert summary.state == "ok"
    assert summary.finished_at == FINISHED.isoformat()
    assert summary.integrity == "ok"
    assert source.name == "backup" and source.state == "fresh"
    assert source.observed_at == FINISHED.isoformat()
    assert incidents == ()


def test_a_failed_run_is_named_and_becomes_an_event() -> None:
    summary, source, incidents = backup.summarize(_failed(), now=NOW)

    assert summary.state == "failed"
    assert summary.phase == "snapshot" and summary.reason == FAILED_REASON
    assert summary.finished_at == FINISHED.isoformat()
    assert source.state == "fresh"  # the status itself is fresh; the run it describes failed
    assert [i.severity for i in incidents] == ["warning"]
    assert incidents[0].title == f"Бэкап не состоялся: snapshot: {FAILED_REASON}"


def test_a_status_older_than_the_watchdog_s_threshold_is_stale_and_an_event() -> None:
    old = NOW - timedelta(hours=30)
    summary, source, incidents = backup.summarize(
        _status(finished_epoch=int(old.timestamp())), now=NOW
    )

    assert summary.state == "ok"
    assert source.state == "stale"
    assert [i.title for i in incidents] == ["Бэкап старше 26 ч: последний прогон 30 ч назад"]


def test_finished_at_is_read_when_finished_epoch_is_missing() -> None:
    status = _status()
    del status["finished_epoch"]

    summary, source, _ = backup.summarize(status, now=NOW)

    assert summary.finished_at == "2026-09-21T15:56:00+00:00"
    assert source.state == "fresh"


def test_a_status_without_a_time_is_unknown_with_the_reason() -> None:
    status = _status()
    del status["finished_epoch"]
    del status["finished_at"]

    summary, source, incidents = backup.summarize(status, now=NOW)

    assert summary.state == "unknown" and summary.detail == "в статусе нет времени"
    assert source.state == "unavailable"
    assert incidents == ()


def test_a_status_dated_in_the_future_is_not_fresh() -> None:
    ahead = NOW + timedelta(hours=2)
    summary, source, _ = backup.summarize(_status(finished_epoch=int(ahead.timestamp())), now=NOW)

    assert summary.state == "unknown" and summary.detail == "статус датирован будущим"
    assert source.state == "unavailable"


@pytest.mark.parametrize(
    ("seconds", "words"),
    [
        (20, "менее минуты назад"),
        (59 * 60, "59 мин назад"),
        (6 * 3600, "6 ч назад"),
        (47 * 3600 + 1800, "47 ч назад"),
        (48 * 3600, "2 дн назад"),
        (10 * 86400, "10 дн назад"),
    ],
)
def test_the_age_is_said_in_words_the_reader_does_not_have_to_compute(
    seconds: int, words: str
) -> None:
    assert backup.describe_age(seconds) == words


# ----------------------------------------------------------------------------- the file


def test_an_unconfigured_source_is_unsupported_not_an_alarm(tmp_path: Path) -> None:
    summary, source, incidents = backup.collect_backup(Environment(hermes_home=tmp_path), now=NOW)

    assert summary.state == "unsupported" and summary.detail == "источник бэкапа не настроен"
    assert source.state == "unsupported" and incidents == ()
    assert probe_backup(Environment(hermes_home=tmp_path)).status == "unsupported"


def test_a_missing_status_file_is_no_data_with_the_reason_and_an_event(tmp_path: Path) -> None:
    env = Environment(hermes_home=tmp_path, backup_status=tmp_path / "daily-status.json")

    summary, source, incidents = backup.collect_backup(env, now=NOW)

    assert summary.state == "unknown" and summary.detail == "файл статуса отсутствует"
    assert source.state == "unavailable"
    assert [i.title for i in incidents] == ["Бэкап: файл статуса отсутствует"]
    assert probe_backup(env).status == "unsupported"


def test_an_unreadable_status_file_names_the_failure_class_not_its_text(tmp_path: Path) -> None:
    path = tmp_path / "daily-status.json"
    path.write_text("{not json", encoding="utf-8")
    env = Environment(hermes_home=tmp_path, backup_status=path)

    summary, source, incidents = backup.collect_backup(env, now=NOW)

    assert summary.state == "unknown" and summary.detail == "статус нечитаем: JSONDecodeError"
    assert source.state == "unavailable"
    assert len(incidents) == 1
    assert probe_backup(env).status == "supported"


def test_a_status_that_is_not_an_object_is_no_data(tmp_path: Path) -> None:
    path = tmp_path / "daily-status.json"
    path.write_text("[1, 2]", encoding="utf-8")

    summary, source, _ = backup.collect_backup(
        Environment(hermes_home=tmp_path, backup_status=path), now=NOW
    )

    assert summary.state == "unknown" and summary.detail == "статус не объект"
    assert source.state == "unavailable"


# ----------------------------------------------------------------------------- the screen


def _render(summary: BackupSummary | None, *, zone=UTC, now: datetime = NOW) -> str:
    snapshot = build_snapshot(
        now=now,
        gateway=None,
        drift=None,
        backup=summary,
        capacity=CapacitySummary(),
        sources=(),
        incidents=(),
        coverage=Coverage(expected_profiles=1, observed_profiles=1),
    )
    return render_dashboard(snapshot, now=now, zone=zone, period_seconds=300)


def test_the_line_carries_the_time_the_age_in_words_and_the_verdict() -> None:
    summary, _, _ = backup.summarize(_status(), now=NOW)

    text = _render(summary)

    assert "Бэкап: 21.09 15:56 UTC · 6 ч назад · integrity ok" in text.splitlines()


def test_the_line_is_in_the_reader_s_zone() -> None:
    summary, _, _ = backup.summarize(_status(), now=NOW)

    text = _render(summary, zone=ZoneInfo("Asia/Tokyo"))

    assert "Бэкап: 22.09 00:56 JST · 6 ч назад · integrity ok" in text.splitlines()


def test_a_stale_backup_is_marked_on_the_line_itself() -> None:
    old = NOW - timedelta(hours=30)
    summary, _, _ = backup.summarize(_status(finished_epoch=int(old.timestamp())), now=NOW)

    text = _render(summary)

    assert "Бэкап: 20.09 15:56 UTC · 30 ч назад · integrity ok ⚠️ старше 26 ч" in text.splitlines()


def test_a_failed_run_is_loud_on_the_line_and_names_the_phase_and_reason() -> None:
    summary, _, _ = backup.summarize(_failed(), now=NOW)

    text = _render(summary)

    assert (
        f"Бэкап: ⚠️ не состоялся 21.09 15:56 UTC · 6 ч назад · snapshot: {FAILED_REASON}"
        in text.splitlines()
    )


def test_no_data_and_not_observed_are_reasons_never_zero(tmp_path: Path) -> None:
    missing, _, _ = backup.collect_backup(
        Environment(hermes_home=tmp_path, backup_status=tmp_path / "none.json"), now=NOW
    )
    unconfigured, _, _ = backup.collect_backup(Environment(hermes_home=tmp_path), now=NOW)

    assert "Бэкап: нет данных (файл статуса отсутствует)" in _render(missing).splitlines()
    assert (
        "Бэкап: не наблюдается (источник бэкапа не настроен)" in _render(unconfigured).splitlines()
    )


def test_the_line_sits_in_the_top_block_beside_the_gateway_line() -> None:
    from telegram_dashboard.schema import GatewaySummary

    summary, _, _ = backup.summarize(_status(), now=NOW)
    snapshot = build_snapshot(
        now=NOW,
        gateway=GatewaySummary("running", "connected"),
        drift=None,
        backup=summary,
        capacity=CapacitySummary(),
        sources=(),
        incidents=(),
    )

    lines = render_dashboard(snapshot, now=NOW, zone=UTC, period_seconds=300).splitlines()

    gateway = next(i for i, line in enumerate(lines) if line.startswith("Gateway:"))
    assert lines[gateway + 1].startswith("Бэкап: ")


# ----------------------------------------------------------------------------- the tick


class Runner:
    def run(self, argv, *, timeout_seconds):
        return CommandResult(0, "", "")


def test_the_tick_reads_the_status_file_and_counts_the_source(tmp_path: Path) -> None:
    path = tmp_path / "daily-status.json"
    path.write_text(json.dumps(_status()), encoding="utf-8")
    env = Environment(hermes_home=tmp_path, limits_enabled=False, backup_status=path)

    snapshot = asyncio.run(collect_all_async(env, Runner(), now=NOW))

    assert snapshot.backup is not None and snapshot.backup.state == "ok"
    source = next(s for s in snapshot.sources if s.name == "backup")
    assert source.state == "fresh"
    text = render_dashboard(snapshot, now=NOW, zone=UTC, period_seconds=300)
    assert "Бэкап: 21.09 15:56 UTC · 6 ч назад · integrity ok" in text.splitlines()
    assert "бэкап" not in text.split("Не наблюдается:")[-1].split("\n")[0]


def test_without_a_configured_source_the_tick_still_names_it(tmp_path: Path) -> None:
    snapshot = asyncio.run(
        collect_all_async(
            Environment(hermes_home=tmp_path, limits_enabled=False), Runner(), now=NOW
        )
    )

    assert snapshot.backup is not None and snapshot.backup.state == "unsupported"
    text = render_dashboard(snapshot, now=NOW, zone=UTC, period_seconds=300)
    assert "бэкап (нет на этой установке)" in text


# ----------------------------------------------------------------------------- the plugin


def test_the_plugin_reads_the_status_path_from_its_settings_or_the_environment(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from probe_fakes import CHAT, FakeContext, load_plugin

    plugin = load_plugin()
    for name in ("HERMES_DASHBOARD_PROBE_CHAT", "HERMES_DASHBOARD_PROBE_BACKUP_STATUS"):
        monkeypatch.delenv(name, raising=False)
    base = {"chat_id": CHAT, "hermes_home": str(tmp_path), "limits_enabled": False}

    def status_path(**overrides: Any) -> Path | None:
        settings = plugin.read_settings(FakeContext({**base, **overrides}))
        assert settings is not None
        return settings.backup_status

    assert status_path() is None
    assert status_path(backup_status="/status/daily-status.json") == Path(
        "/status/daily-status.json"
    )
    monkeypatch.setenv("HERMES_DASHBOARD_PROBE_BACKUP_STATUS", "/env/daily-status.json")
    assert status_path() == Path("/env/daily-status.json")


def test_the_plugin_hands_the_status_path_to_the_collector(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from probe_fakes import CHAT, FakeContext, load_plugin

    plugin = load_plugin()
    monkeypatch.delenv("HERMES_DASHBOARD_PROBE_BACKUP_STATUS", raising=False)
    ctx = FakeContext(
        {
            "chat_id": CHAT,
            "hermes_home": str(tmp_path),
            "limits_enabled": False,
            "backup_status": str(tmp_path / "daily-status.json"),
        }
    )
    runtime = plugin.register(ctx)
    assert runtime is not None
    seen: list[Any] = []

    async def spy(env: Any, *args: Any, **kwargs: Any) -> Any:
        seen.append(env)
        return "snapshot"

    monkeypatch.setattr(runtime.dashboard.collect, "collect_all_async", spy)

    assert asyncio.run(runtime._collect(NOW)) == "snapshot"
    assert seen[0].backup_status == tmp_path / "daily-status.json"


def test_the_cron_config_carries_the_status_path_too(tmp_path: Path) -> None:
    from telegram_dashboard.__main__ import _environment

    env = _environment({"hermes_home": str(tmp_path), "backup_status": "~/status.json"})

    assert env.backup_status == Path("~/status.json").expanduser()
    assert _environment({"hermes_home": str(tmp_path)}).backup_status is None
