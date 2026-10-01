"""``telegram_traffic``: whether messages move through an adapter that says it is connected,
from the four counters the adapter keeps for itself, read off the live object as attributes."""

from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

from telegram_dashboard.compat import Environment
from telegram_dashboard.telegram_traffic import (
    QUIET_CEILING_SECONDS,
    QUIET_FLOOR_SECONDS,
    STALL_SECONDS,
    Probe,
    collect_traffic,
    probe_adapter,
    quiet_threshold,
)

NOW = datetime(2026, 9, 30, 12, 30, tzinfo=UTC)
SEND_ERROR = "ERROR plugins.platforms.telegram.adapter: [Telegram] Failed to send Telegram message: Timed out"


def _adapter(**over):
    fields = {
        "_updates_received_total": 12,
        "_polling_generation": 3,
        "_polling_last_progress_monotonic": 990.0,
        "_polling_generation_started_monotonic": 100.0,
        "_send_path_degraded": False,
    }
    fields.update(over)
    return SimpleNamespace(**fields)


def _env(tmp_path: Path) -> Environment:
    (tmp_path / "logs").mkdir(exist_ok=True)
    return Environment(hermes_home=tmp_path)


def _probe(**over) -> Probe:
    return probe_adapter(_adapter(**over), monotonic=1000.0)


# ----------------------------------------------------------------------------- the probe


def test_the_probe_reads_the_four_counters_as_attributes() -> None:
    probe = probe_adapter(_adapter(), monotonic=1000.0)

    assert (
        probe.received_total,
        probe.generation,
        probe.progress_age_seconds,
        probe.generation_age_seconds,
        probe.send_path_degraded,
    ) == (12, 3, 10.0, 900.0, False)
    assert probe.problem is None


def test_a_missing_or_mistyped_attribute_is_a_problem_not_a_number() -> None:
    bare = probe_adapter(SimpleNamespace(), monotonic=1.0)
    typed = probe_adapter(_adapter(_updates_received_total="12"), monotonic=1.0)

    assert bare.problem == "adapter has no traffic counters"
    assert typed.received_total is None
    assert typed.problem == "adapter counter _updates_received_total is not a number"


def test_an_adapter_property_that_raises_is_a_problem_never_an_exception() -> None:
    class Bad:
        @property
        def _updates_received_total(self):
            raise RuntimeError("no")

    assert "raises" in (probe_adapter(Bad(), monotonic=1.0).problem or "")


# ----------------------------------------------------------------------------- updates seen


def test_a_growing_counter_in_the_same_generation_is_an_update_seen_now(tmp_path: Path) -> None:
    cache = {
        "generation": 3,
        "received_total": 10,
        "last_update_seen_at": (NOW - timedelta(hours=1)).isoformat(),
        "gaps": {},
    }

    summary, source, incidents = collect_traffic(_env(tmp_path), _probe(), cache, now=NOW)

    assert summary.state == "ok" and summary.last_update_seen_at == NOW.isoformat()
    assert cache["gaps"] == {"2026-09-30": 3600.0}
    assert (source.name, source.state) == ("telegram_traffic", "fresh")
    assert incidents == ()


def test_a_new_polling_generation_never_reads_as_silence(tmp_path: Path) -> None:
    """Review focus 3: a reconnect starts a new generation with the counter at zero; a lower
    counter is a reset, not "no update since"; the first update of the new generation counts."""
    seen = (NOW - timedelta(hours=1)).isoformat()
    cache = {"generation": 2, "received_total": 50, "last_update_seen_at": seen, "gaps": {}}

    summary, _s, _i = collect_traffic(
        _env(tmp_path), _probe(_updates_received_total=0), cache, now=NOW
    )
    assert summary.last_update_seen_at == seen
    assert cache["generation"] == 3 and cache["received_total"] == 0

    later = NOW + timedelta(minutes=30)
    summary, _s, _i = collect_traffic(
        _env(tmp_path), _probe(_updates_received_total=2), cache, now=later
    )
    assert summary.last_update_seen_at == later.isoformat()


def test_no_update_ever_seen_is_never_quiet(tmp_path: Path) -> None:
    summary, _s, incidents = collect_traffic(
        _env(tmp_path), _probe(_updates_received_total=0), {}, now=NOW
    )

    assert summary.state == "ok" and summary.last_update_seen_at is None
    assert incidents == ()


def test_gaps_older_than_seven_days_are_dropped(tmp_path: Path) -> None:
    cache = {
        "generation": 3,
        "received_total": 10,
        "last_update_seen_at": (NOW - timedelta(minutes=5)).isoformat(),
        "gaps": {"2026-09-20": 20 * 3600.0, "2026-09-29": 3600.0},
    }

    collect_traffic(_env(tmp_path), _probe(), cache, now=NOW)

    assert set(cache["gaps"]) == {"2026-09-29", "2026-09-30"}


# ----------------------------------------------------------------------------- the verdicts


def test_sends_blocked_is_definite_after_the_reconnect_grace_only(tmp_path: Path) -> None:
    young = _probe(
        _send_path_degraded=True,
        _polling_generation_started_monotonic=950.0,
        _polling_last_progress_monotonic=None,
    )
    old = _probe(
        _send_path_degraded=True,
        _polling_generation_started_monotonic=100.0,
        _polling_last_progress_monotonic=None,
    )

    summary, _s, incidents = collect_traffic(_env(tmp_path), young, {}, now=NOW)
    assert summary.state == "reconnecting" and incidents == ()

    cache: dict = {}
    summary, _s, incidents = collect_traffic(_env(tmp_path), old, cache, now=NOW)
    assert summary.state == "no_sends" and summary.sends_blocked_since == NOW.isoformat()
    assert [(i.incident_id, i.severity, i.title) for i in incidents] == [
        ("telegram:no_sends", "critical", "Telegram: sends blocked")
    ]

    summary, _s, _i = collect_traffic(_env(tmp_path), old, cache, now=NOW + timedelta(minutes=30))
    assert summary.sends_blocked_since == NOW.isoformat()  # the first sighting stays


def test_polling_without_progress_for_five_minutes_is_stalled(tmp_path: Path) -> None:
    probe = _probe(_polling_last_progress_monotonic=1000.0 - STALL_SECONDS - 1)

    summary, _s, incidents = collect_traffic(_env(tmp_path), probe, {}, now=NOW)

    assert summary.state == "stalled"
    assert incidents[0].title == "Telegram polling stalled" and incidents[0].severity == "critical"


def test_quiet_is_relative_to_the_installation_with_a_floor_and_a_ceiling() -> None:
    assert quiet_threshold({}) == QUIET_FLOOR_SECONDS
    assert quiet_threshold({"2026-09-29": 4 * 3600.0}) == 8 * 3600.0
    assert quiet_threshold({"2026-09-29": 40 * 3600.0}) == QUIET_CEILING_SECONDS
    assert quiet_threshold({"2026-09-29": "junk", "2026-09-28": 2 * 3600.0}) == QUIET_FLOOR_SECONDS


def test_quiet_beyond_the_threshold_is_a_warning_with_the_usual_gap(tmp_path: Path) -> None:
    cache = {
        "generation": 3,
        "received_total": 12,
        "last_update_seen_at": (NOW - timedelta(hours=9)).isoformat(),
        "gaps": {"2026-09-29": 4 * 3600.0},
    }

    summary, _s, incidents = collect_traffic(_env(tmp_path), _probe(), cache, now=NOW)

    assert summary.state == "quiet"
    assert (summary.quiet_seconds, summary.threshold_seconds) == (9 * 3600.0, 8 * 3600.0)
    assert [(i.severity, i.title) for i in incidents] == [("warning", "Telegram quiet for 9 h")]


# ----------------------------------------------------------------------------- the log


def test_send_errors_come_from_the_engine_log_and_three_in_an_hour_are_an_event(
    tmp_path: Path,
) -> None:
    env = _env(tmp_path)
    moment = NOW - timedelta(minutes=10)
    stamp = moment.astimezone().strftime("%Y-%m-%d %H:%M:%S,000")
    lines = [f"{stamp} {SEND_ERROR}" for _ in range(3)]
    (tmp_path / "logs" / "errors.log").write_text("\n".join(lines) + "\n", encoding="utf-8")

    summary, _s, incidents = collect_traffic(env, _probe(), {}, now=NOW)

    assert summary.last_send_error_at == moment.isoformat()
    assert summary.send_errors_hour == 3
    assert [(i.severity, i.title) for i in incidents] == [
        ("warning", "Telegram: 3 sends failed in 1 h")
    ]


def test_no_adapter_is_the_cron_path_and_a_problem_is_no_data(tmp_path: Path) -> None:
    summary, source, _ = collect_traffic(_env(tmp_path), None, {}, now=NOW)
    assert (summary.state, source.state, summary.detail) == (
        "unsupported",
        "unsupported",
        "no adapter on the cron path",
    )

    problem = Probe(problem="adapter has no traffic counters")
    summary, source, _ = collect_traffic(_env(tmp_path), problem, {}, now=NOW)
    assert (summary.state, source.state, summary.detail) == (
        "unknown",
        "unavailable",
        "adapter has no traffic counters",
    )
