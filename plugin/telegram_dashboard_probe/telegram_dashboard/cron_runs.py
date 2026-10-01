"""The scheduler's run history, ``HERMES_HOME/cron/executions.db``, read and nothing else.

``jobs.json`` knows the last run only; a failure the next successful run replaced between two
ticks of the dashboard lives here alone (hermes-agent#118354), and so does a streak of runs whose
result never reached its target (``delivery_outcome = 'failed'``; the engine's ``failure_streak``
leaves delivery out). The engine keeps the last 1000 terminal rows.

The database is the engine's and it is written by the gateway that runs this plugin: the
connection is ``mode=ro`` in the URI with ``PRAGMA query_only``, opened for one query set and
closed, with a short busy timeout; the columns read are ``job_id``, ``status``,
``delivery_outcome``, ``claimed_at`` and ``finished_at``, never ``error``. The whole read runs in
a worker under the tick's deadline (``collect._runs_guarded``); a database that cannot be opened
read-only is one unavailable source, and the cron line stands on ``jobs.json`` alone.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from .compat import Environment
from .schema import SourceObservation
from .timeparse import parse_timestamp

SOURCE_NAME = "cron_runs"
CRON_DIR = "cron"
EXECUTIONS_FILE = "executions.db"
CONNECT_TIMEOUT_SECONDS = 2.0
# The first read looks this far back; every later read from the record's ``runs_checked_at``.
LOOKBACK_SECONDS = 24 * 3600.0
ROW_LIMIT = 200
STREAK_ROWS = 20
STREAK_JOBS = 10
_TERMINAL = "('completed','failed','unknown')"


@dataclass(frozen=True, slots=True)
class Run:
    job_id: str
    status: str
    delivery_outcome: str | None
    claimed_at: str | None
    finished_at: str | None

    @property
    def undelivered(self) -> bool:
        return self.delivery_outcome == "failed"


@dataclass(frozen=True, slots=True)
class Runs:
    """The failures since the last check and the delivery streaks of the jobs asked for."""

    recent_failures: tuple[Run, ...]
    delivery_streaks: dict[str, int]


RunsPart = tuple[Runs | None, SourceObservation]


def open_readonly(path: Path) -> sqlite3.Connection:
    """A connection that can only read: ``mode=ro`` refuses to write at the file, ``query_only``
    refuses at the statement. The URI form is the only way to pass ``mode`` to sqlite3."""
    conn = sqlite3.connect(
        f"{path.resolve().as_uri()}?mode=ro", uri=True, timeout=CONNECT_TIMEOUT_SECONDS
    )
    conn.execute("PRAGMA query_only = 1")
    return conn


def recent_failures(
    conn: sqlite3.Connection, *, since: datetime, limit: int = ROW_LIMIT
) -> list[Run]:
    """Runs that failed or were not delivered, newest first, claimed at ``since`` or later. The
    engine stamps ``claimed_at`` in the profile's zone, so the moment is compared in Python."""
    rows = conn.execute(
        "SELECT job_id, status, delivery_outcome, claimed_at, finished_at FROM executions"
        " WHERE status = 'failed' OR delivery_outcome = 'failed'"
        " ORDER BY claimed_at DESC LIMIT ?",
        (limit,),
    ).fetchall()
    runs: list[Run] = []
    for job_id, status, outcome, claimed_at, finished_at in rows:
        moment = parse_timestamp(claimed_at)
        if moment is None or moment < since:
            continue
        runs.append(
            Run(
                str(job_id),
                str(status),
                outcome if isinstance(outcome, str) else None,
                claimed_at if isinstance(claimed_at, str) else None,
                finished_at if isinstance(finished_at, str) else None,
            )
        )
    return runs


def delivery_streak(conn: sqlite3.Connection, job_id: str, *, limit: int = STREAK_ROWS) -> int:
    """How many of the job's newest terminal runs in a row were not delivered."""
    rows = conn.execute(
        "SELECT delivery_outcome FROM executions WHERE job_id = ? AND status IN "
        + _TERMINAL
        + " ORDER BY claimed_at DESC, id DESC LIMIT ?",
        (job_id, limit),
    ).fetchall()
    streak = 0
    for (outcome,) in rows:
        if outcome != "failed":
            break
        streak += 1
    return streak


def read_runs(
    env: Environment, *, since: datetime, streak_for: Sequence[str], now: datetime
) -> RunsPart:
    """The whole answer with its source: ``None`` and why when the file is not there or cannot
    be read; the streaks for at most ``STREAK_JOBS`` of the ids asked for, in their order."""
    path = env.hermes_home / CRON_DIR / EXECUTIONS_FILE
    if not path.is_file():
        source = SourceObservation(
            SOURCE_NAME, "official", "unsupported", detail="executions.db not found"
        )
        return None, source
    try:
        conn = open_readonly(path)
        try:
            failures = recent_failures(conn, since=since)
            wanted = list(dict.fromkeys(streak_for))[:STREAK_JOBS]
            streaks = {job_id: delivery_streak(conn, job_id) for job_id in wanted}
        finally:
            conn.close()
    except sqlite3.Error as exc:
        source = SourceObservation(
            SOURCE_NAME, "official", "unavailable", detail=f"executions.db: {type(exc).__name__}"
        )
        return None, source
    source = SourceObservation(SOURCE_NAME, "official", "fresh", observed_at=now.isoformat())
    return Runs(tuple(failures), streaks), source
