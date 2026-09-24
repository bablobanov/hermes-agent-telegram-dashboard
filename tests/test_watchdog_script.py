"""The cron watchdog for the plugin path, ``watchdog/dashboard_probe_check.py``.

Hermes delivers whatever it prints, so the tests pin stdout: empty when confirmed, an alert
otherwise, and the repeat policy (first tick past the threshold, then hourly; every tick for
``never``/``lost``). The last test runs the script in a subprocess against a directory holding
exactly the cut of the package that is shipped next to it; an import that creeps beyond the cut
fails here, not on the server.
"""

from __future__ import annotations

import importlib.util
import json
import shutil
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import ModuleType

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "watchdog" / "dashboard_probe_check.py"
NOW = datetime(2026, 9, 12, 12, 0, tzinfo=UTC)
PERIOD = 300
INTERVAL = 300


def _load_script() -> ModuleType:
    spec = importlib.util.spec_from_file_location("dashboard_probe_check", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # dataclasses resolve postponed annotations via sys.modules
    spec.loader.exec_module(module)
    return module


watchdog = _load_script()
THRESHOLD = PERIOD + 60  # confirmation_threshold_seconds(300): one period plus the slack


def _state(tmp_path: Path, **probe: object) -> Path:
    path = tmp_path / "plugin-data" / "telegram_dashboard_probe" / "state.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"probe": probe}), encoding="utf-8")
    return path


def _config(tmp_path: Path, state: Path, **extra: object) -> Path:
    cfg = tmp_path / "dashboard_probe_check.json"
    payload: dict[str, object] = {
        "plugin_state_path": str(state),
        "period_seconds": PERIOD,
        "check_interval_seconds": INTERVAL,
        **extra,
    }
    cfg.write_text(json.dumps(payload), encoding="utf-8")
    return cfg


def _record(seconds_ago: float, *, now: datetime = NOW, **extra: object) -> dict[str, object]:
    return {
        "message_id": "101",
        "chat_id": "-1001000000001",
        "last_status": "edited",
        "last_error": None,
        "ticks": 7,
        "last_confirmed_at": (now - timedelta(seconds=seconds_ago)).isoformat(),
        **extra,
    }


def _run(
    cfg: Path, capsys: pytest.CaptureFixture[str], *args: str, now: datetime = NOW
) -> tuple[int, str]:
    code = watchdog.main([str(cfg), *args], now=now)
    return code, capsys.readouterr().out


def test_confirmed_is_silence_with_exit_zero(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    cfg = _config(tmp_path, _state(tmp_path, **_record(60)))
    assert _run(cfg, capsys) == (0, "")

    code, out = _run(cfg, capsys, "--verbose")
    assert code == 0
    assert out.startswith("confirmed source=plugin-state:state.json message_id=101")


def test_first_tick_past_the_threshold_speaks_then_silence_until_the_hour(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    confirmed_at = NOW
    cfg = _config(tmp_path, _state(tmp_path, **_record(0, now=confirmed_at)))

    first_tick = confirmed_at + timedelta(seconds=THRESHOLD + 40)
    code, out = _run(cfg, capsys, now=first_tick)
    assert code == 0
    assert out.startswith("DASHBOARD PROBE ALERT: lagging\n🟡 Обновление запаздывает")
    assert "message_id=101" in out

    for ticks in range(1, 12):  # the rest of the hour: nothing, the message carries the state
        later = first_tick + timedelta(seconds=INTERVAL * ticks)
        assert _run(cfg, capsys, now=later) == (0, ""), ticks

    hour_later = first_tick + timedelta(seconds=INTERVAL * 12)
    code, out = _run(cfg, capsys, now=hour_later)
    assert out.startswith("DASHBOARD PROBE ALERT: stale\n🔴 ДАШБОРД УСТАРЕЛ")


def test_a_skipped_tick_swallows_that_hour_s_alert_by_construction() -> None:
    """The hourly window is one tick wide: if the scheduler skips the tick that falls into it,
    the hour passes silently and the next hour speaks again. Named so nobody hunts for it in the
    thresholds."""
    speak = watchdog.should_speak
    over_at = [40 + INTERVAL * k for k in range(25)]  # ticks 0..24 after crossing the threshold
    assert [k for k in range(25) if speak("stale", over_at[k], interval_seconds=INTERVAL)] == [
        0,
        12,
        24,
    ]
    assert (
        speak("stale", over_at[13], interval_seconds=INTERVAL) is False
    )  # tick 12 skipped: quiet hour


def test_never_and_lost_speak_every_tick(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    missing = tmp_path / "plugin-data" / "telegram_dashboard_probe" / "state.json"
    cfg = _config(tmp_path, missing)
    for tick in range(3):
        code, out = _run(cfg, capsys, now=NOW + timedelta(seconds=INTERVAL * tick))
        assert code == 0 and out.startswith("DASHBOARD PROBE ALERT: never\n"), tick

    lost = _record(
        30,
        message_id=None,
        lost_at=NOW.isoformat(),
        last_status="send_failed",
        last_error="Forbidden",
    )
    cfg = _config(tmp_path, _state(tmp_path, **lost))
    for tick in range(3):
        code, out = _run(cfg, capsys, now=NOW + timedelta(seconds=INTERVAL * tick))
        assert code == 0 and out.startswith(
            "DASHBOARD PROBE ALERT: lost\n🔴 Закреплённое сообщение пропало"
        ), tick

    # No usable stamp to count from either: a stale that is dated in the future speaks every tick.
    assert watchdog.should_speak("stale", None, interval_seconds=INTERVAL) is True


@pytest.mark.parametrize(
    ("config", "reason"),
    [
        (None, "config missing"),
        ({"period_seconds": 300}, "no plugin_state_path"),
        ({"plugin_state_path": "state.json", "period_seconds": "inf"}, "period_seconds"),
        (
            {"plugin_state_path": "state.json", "check_interval_seconds": -5},
            "check_interval_seconds",
        ),
    ],
)
def test_a_broken_watchdog_exits_non_zero_and_says_why(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    config: dict[str, object] | None,
    reason: str,
) -> None:
    cfg = tmp_path / "dashboard_probe_check.json"
    if config is not None:
        cfg.write_text(json.dumps(config), encoding="utf-8")
    assert watchdog.main([str(cfg)], now=NOW) == watchdog.EXIT_BROKEN
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err.startswith("dashboard probe watchdog broken: ") and reason in captured.err


def _cut(where: Path) -> Path:
    """Exactly what the deploy puts next to the script: the package modules named in the script."""
    package = where / "telegram_dashboard"
    package.mkdir()
    for name in watchdog.PACKAGE_MODULES:
        shutil.copy(ROOT / "telegram_dashboard" / name, package / name)
    return shutil.copy(SCRIPT, where / SCRIPT.name)


def test_the_script_runs_on_the_shipped_cut_of_the_package_alone(tmp_path: Path) -> None:
    deploy = tmp_path / "scripts"
    deploy.mkdir()
    script = _cut(deploy)
    cfg = _config(deploy, _state(tmp_path, **_record(60, now=datetime.now(UTC))))
    elsewhere = tmp_path / "cwd"
    elsewhere.mkdir()
    # -I: no PYTHONPATH, no script directory on sys.path; the script must find the cut itself.
    argv = [sys.executable, "-I", str(script), str(cfg), "--verbose"]

    done = subprocess.run(
        argv, cwd=elsewhere, capture_output=True, text=True, timeout=60, check=False
    )
    assert done.returncode == 0, done.stderr
    assert done.stdout.startswith("confirmed source=plugin-state:state.json message_id=101")

    (deploy / "telegram_dashboard" / "timeparse.py").unlink()  # the cut is what runs, nothing else
    broken = subprocess.run(
        argv, cwd=elsewhere, capture_output=True, text=True, timeout=60, check=False
    )
    assert broken.returncode == watchdog.EXIT_BROKEN
    assert "telegram_dashboard not importable" in broken.stderr
