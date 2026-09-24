"""``--check`` is the external watchdog. The plugin path writes its record into the engine's
PluginState file (``<HERMES_HOME>/plugin-data/<namespace>/state.json``, one key ``probe``), not into
the cron path's ``dashboard-delivery.json``; a watchdog reading the wrong file would report
``never`` forever or, worse, a stale cron record while the plugin is fine."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

from telegram_dashboard.__main__ import main

NOW = datetime.now(UTC)


def _plugin_state(tmp_path: Path, **probe: object) -> Path:
    """Exactly the shape PluginState.set("probe", record) leaves on disk."""
    path = tmp_path / "plugin-data" / "telegram_dashboard_probe" / "state.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"probe": probe}, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def _config(tmp_path: Path, state: Path) -> str:
    cfg = tmp_path / "check.json"
    cfg.write_text(
        json.dumps({"plugin_state_path": str(state), "period_seconds": 300}), encoding="utf-8"
    )
    return str(cfg)


def _record(minutes_ago: float, **extra: object) -> dict[str, object]:
    stamp = (NOW - timedelta(minutes=minutes_ago)).isoformat()
    return {
        "message_id": "101",
        "chat_id": "-1001000000001",
        "last_status": "edited",
        "last_error": None,
        "ticks": 7,
        "last_confirmed_at": stamp,
        **extra,
    }


def test_check_reads_the_plugin_state_and_confirms_a_fresh_record(tmp_path, capsys) -> None:
    state = _plugin_state(tmp_path, **_record(2))

    assert main(["--config", _config(tmp_path, state), "--check"]) == 0
    out = capsys.readouterr().out
    assert out.startswith("confirmed") and "source=plugin-state" in out


def test_check_on_plugin_state_reports_lagging_and_stale(tmp_path) -> None:
    assert (
        main(["--config", _config(tmp_path, _plugin_state(tmp_path, **_record(7))), "--check"]) == 1
    )
    assert (
        main(["--config", _config(tmp_path, _plugin_state(tmp_path, **_record(11))), "--check"])
        == 2
    )


def test_check_on_plugin_state_sees_a_lost_message_that_was_not_recreated(tmp_path, capsys) -> None:
    lost = _record(
        1,
        message_id=None,
        lost_at=NOW.isoformat(),
        last_status="send_failed",
        last_error="Forbidden",
    )
    assert main(["--config", _config(tmp_path, _plugin_state(tmp_path, **lost)), "--check"]) == 2
    assert capsys.readouterr().out.startswith("lost")


def test_check_on_a_missing_or_foreign_plugin_state_is_never(tmp_path, capsys) -> None:
    missing = tmp_path / "plugin-data" / "telegram_dashboard_probe" / "state.json"
    assert main(["--config", _config(tmp_path, missing), "--check"]) == 2
    assert capsys.readouterr().out.startswith("never")

    other_plugin = _plugin_state(tmp_path)  # file exists, key "probe" holds nothing useful
    assert main(["--config", _config(tmp_path, other_plugin), "--check"]) == 2


def test_check_still_reads_the_cron_delivery_record_without_plugin_state_path(
    tmp_path, capsys
) -> None:
    delivery = tmp_path / "dashboard-delivery.json"
    delivery.write_text(
        json.dumps(
            {"message_id": 4242, "last_confirmed_at": (NOW - timedelta(minutes=1)).isoformat()}
        ),
        encoding="utf-8",
    )
    cfg = tmp_path / "cron.json"
    cfg.write_text(
        json.dumps({"state_path": str(delivery), "period_seconds": 300}), encoding="utf-8"
    )

    assert main(["--config", str(cfg), "--check"]) == 0
    assert "source=delivery-record" in capsys.readouterr().out
