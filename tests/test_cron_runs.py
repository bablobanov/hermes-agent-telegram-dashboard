"""``cron_runs``: the scheduler's run history, ``executions.db``, read and nothing else."""

import sqlite3
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import pytest
from cron_fixtures import make_db as _db

from telegram_dashboard.compat import Environment
from telegram_dashboard.cron_runs import delivery_streak, open_readonly, read_runs, recent_failures

NOW = datetime(2026, 9, 30, 12, 30, tzinfo=UTC)


def _at(minutes_ago: int, offset_hours: int = 0) -> str:
    """A ``claimed_at`` the engine would write: ISO text in the profile's zone."""
    moment = NOW - timedelta(minutes=minutes_ago)
    if offset_hours:
        moment = moment.astimezone(timezone(timedelta(hours=offset_hours)))
    return moment.isoformat()


def test_the_connection_is_read_only_and_query_only(tmp_path: Path) -> None:
    conn = open_readonly(_db(tmp_path, []))

    with pytest.raises(sqlite3.OperationalError):
        conn.execute(
            "INSERT INTO executions (id, job_id, source, process_id, pid, status, claimed_at)"
            " VALUES ('x','j','b','p',1,'failed','t')"
        )
    assert conn.execute("PRAGMA query_only").fetchone()[0] == 1
    conn.close()


def test_a_wal_database_held_open_by_its_writer_is_readable(tmp_path: Path) -> None:
    path = _db(tmp_path, [("b1", "failed", None, _at(5))], wal=True)

    conn = open_readonly(path)

    assert [r.job_id for r in recent_failures(conn, since=NOW - timedelta(hours=1))] == ["b1"]
    conn.close()


def test_recent_failures_compare_moments_not_strings(tmp_path: Path) -> None:
    """Review focus 2: the engine stamps ``claimed_at`` in the profile's zone, so a later
    moment may be a smaller string; the window is applied to the moment, in Python."""
    rows = [
        ("b1", "failed", None, _at(5, offset_hours=2)),
        ("b2", "completed", "failed", _at(50)),
        ("b3", "failed", None, _at(120)),
        ("b4", "completed", "delivered", _at(1)),
    ]
    conn = open_readonly(_db(tmp_path, rows))

    runs = recent_failures(conn, since=NOW - timedelta(hours=1))

    assert sorted(r.job_id for r in runs) == ["b1", "b2"]
    assert not any(hasattr(r, "error") for r in runs)
    conn.close()


def test_the_delivery_streak_counts_newest_undelivered_runs_only(tmp_path: Path) -> None:
    rows = [
        ("b1", "completed", "failed", _at(5)),
        ("b1", "completed", "failed", _at(35)),
        ("b1", "completed", "delivered", _at(65)),
        ("b1", "completed", "failed", _at(95)),
    ]
    conn = open_readonly(_db(tmp_path, rows))

    assert delivery_streak(conn, "b1") == 2
    assert delivery_streak(conn, "none") == 0
    conn.close()


def test_read_runs_is_the_whole_answer_with_a_source(tmp_path: Path) -> None:
    _db(tmp_path, [("b1", "completed", "failed", _at(5))])

    runs, source = read_runs(
        Environment(hermes_home=tmp_path),
        since=NOW - timedelta(hours=1),
        streak_for=["b1"],
        now=NOW,
    )

    assert runs is not None
    assert runs.delivery_streaks == {"b1": 1}
    assert [r.job_id for r in runs.recent_failures] == ["b1"]
    assert (source.name, source.state) == ("cron_runs", "fresh")


def test_no_database_is_not_on_this_installation(tmp_path: Path) -> None:
    runs, source = read_runs(Environment(hermes_home=tmp_path), since=NOW, streak_for=[], now=NOW)

    assert runs is None
    assert (source.state, source.detail) == ("unsupported", "executions.db not found")


def test_a_database_without_the_table_is_unavailable_with_the_class(tmp_path: Path) -> None:
    (tmp_path / "cron").mkdir()
    sqlite3.connect(tmp_path / "cron" / "executions.db").close()

    runs, source = read_runs(
        Environment(hermes_home=tmp_path), since=NOW, streak_for=["b1"], now=NOW
    )

    assert runs is None
    assert (source.state, source.detail) == ("unavailable", "executions.db: OperationalError")


def test_a_run_claimed_before_the_window_but_finished_inside_it_is_seen(tmp_path: Path) -> None:
    """Review, minor 8: the window is where the read left off; a run claimed before that moment
    and failed after it must not fall between two ticks."""
    rows = [("b1", "failed", None, _at(90), _at(30)), ("b2", "failed", None, _at(90), _at(80))]
    conn = open_readonly(_db(tmp_path, rows))

    runs = recent_failures(conn, since=NOW - timedelta(hours=1))

    assert [r.job_id for r in runs] == ["b1"]
    conn.close()
