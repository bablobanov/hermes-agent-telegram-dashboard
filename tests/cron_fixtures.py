"""The engine's ``executions`` table (``cron/executions.py:51-88`` at v2026.9.14) as a fixture,
shared by ``test_cron_runs.py`` and ``test_collect_async.py``. The ``error`` column is filled
with a sentinel so a test can prove it is never read."""

import sqlite3
from pathlib import Path

SCHEMA = """CREATE TABLE executions (id TEXT PRIMARY KEY, job_id TEXT NOT NULL, source TEXT NOT NULL,
  process_id TEXT NOT NULL, pid INTEGER NOT NULL, process_started_at INTEGER, status TEXT NOT NULL,
  handoff_pending INTEGER NOT NULL DEFAULT 0, handoff_started_at REAL, claimed_at TEXT NOT NULL,
  started_at TEXT, finished_at TEXT, error TEXT, delivery_outcome TEXT, scheduled_instant TEXT)"""
ERROR_SENTINEL = "secret error text never read"
# Writers held open for the session, the way the gateway holds the file (``wal=True``).
_WRITERS: list[sqlite3.Connection] = []


def make_db(tmp_path: Path, rows, *, wal: bool = False) -> Path:
    """``rows`` are ``(job_id, status, delivery_outcome, claimed_at[, finished_at])``; without a
    ``finished_at`` the run finished when it was claimed. With ``wal`` the writer's connection
    stays open, the way the gateway holds the file."""
    (tmp_path / "cron").mkdir(exist_ok=True)
    path = tmp_path / "cron" / "executions.db"
    conn = sqlite3.connect(path)
    if wal:
        conn.execute("PRAGMA journal_mode=WAL")
    conn.execute(SCHEMA)
    for i, row in enumerate(rows):
        job_id, status, outcome, claimed_at = row[:4]
        finished_at = row[4] if len(row) > 4 else claimed_at
        conn.execute(
            "INSERT INTO executions VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                f"e{i}",
                job_id,
                "builtin",
                "p",
                1,
                None,
                status,
                0,
                None,
                claimed_at,
                claimed_at,
                finished_at,
                ERROR_SENTINEL,
                outcome,
                None,
            ),
        )
    conn.commit()
    if wal:
        _WRITERS.append(conn)
    else:
        conn.close()
    return path
