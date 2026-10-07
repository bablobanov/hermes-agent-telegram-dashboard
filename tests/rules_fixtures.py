"""The engine's saved system prompts (``state.db``) and the context files beside them, as
fixtures for the rules line.

The tables are those of ``hermes_state_common.py:323-389`` at v2026.9.14 cut to the columns that
matter here (the dashboard reads ``source``, ``started_at``, ``system_prompt_hash`` and the
prompt); the prompt is framed the way ``agent/prompt_builder.py`` and ``agent/system_prompt.py``
frame it at that tag: tiers joined by a blank line, the project-context block, ``## <label>``
sections, the truncation marker, the scan's ``[BLOCKED:`` notice, the runtime block last.
"""

from __future__ import annotations

import hashlib
import os
import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

# A phrase that exists only in the fixtures' rules and prompts: the screen, the snapshot and the
# log must never carry it.
SECRET_RULE = "Never push to master without the word aubergine-7f3a"
NOW = datetime(2026, 10, 7, 12, 40, tzinfo=UTC)
SESSION_AT = datetime(2026, 10, 7, 12, 32, 34, tzinfo=UTC).timestamp()
BEFORE_SESSION = SESSION_AT - 3600
AFTER_SESSION = SESSION_AT + 600

SCHEMA = """
CREATE TABLE system_prompts (hash TEXT PRIMARY KEY, prompt TEXT NOT NULL);
CREATE TABLE sessions (
    id TEXT PRIMARY KEY,
    source TEXT NOT NULL,
    model TEXT,
    system_prompt TEXT,
    system_prompt_hash TEXT,
    parent_session_id TEXT,
    started_at REAL NOT NULL,
    message_count INTEGER DEFAULT 0,
    cwd TEXT,
    profile_name TEXT
);
CREATE INDEX idx_sessions_source ON sessions(source);
CREATE INDEX idx_sessions_started ON sessions(started_at DESC);
"""
# Before v2026.8.3 the prompt lived in the row itself.
LEGACY_SCHEMA = """
CREATE TABLE sessions (
    id TEXT PRIMARY KEY, source TEXT NOT NULL, system_prompt TEXT, started_at REAL NOT NULL
);
"""

STABLE = "You are Hermes Agent, an intelligent AI assistant created by Nous Research."
BLOCK_HEADER = (
    "# Project Context\n\nThe following project context files have been loaded and should be "
    "followed:\n\n"
)
SKILLS = "## Skills\nNo skills are installed."
RUNTIME_HEADING = "# Hermes runtime environment"
RUNTIME_END = "<!-- End Hermes runtime environment -->"
# ``agent/prompt_builder.py``: CONTEXT_TRUNCATE_HEAD_RATIO, CONTEXT_TRUNCATE_TAIL_RATIO.
HEAD_RATIO = 0.7
TAIL_RATIO = 0.2


@dataclass(frozen=True)
class Session:
    source: str
    started_at: float
    prompt: str | None


def rules_text(extra: str = "") -> str:
    """A rules file of a realistic shape with the secret phrase in the middle."""
    head = "# Agent rules\n\n" + "\n".join(f"- rule {i}: keep the change small" for i in range(40))
    return f"{head}\n\n{SECRET_RULE}\n\n## Delivery\n\n- PDFs go through MEDIA:{extra}"


def section(label: str, text: str) -> str:
    return f"## {label}\n\n{text}"


def truncated(label: str, text: str, max_chars: int, warn_name: str | None = None) -> str:
    """``_truncate_content`` over a ``_context_section`` body, the way the engine cuts it."""
    body = section(label, text)
    if len(body) <= max_chars:
        return body
    head, tail = int(max_chars * HEAD_RATIO), int(max_chars * TAIL_RATIO)
    marker = (
        f"\n\n[...truncated {warn_name or label}: kept {head}+{tail} of {len(body)} chars. The "
        "middle is omitted — if you need the full instructions, read the complete file with "
        f"the read_file tool: /srv/agent/{label}]\n\n"
    )
    return body[:head] + marker + body[-tail:]


def blocked(label: str) -> str:
    return section(
        label,
        f"[BLOCKED: {label} contained potential prompt injection (ignore_previous). "
        "Content not loaded.]",
    )


def prompt(sections: Sequence[str], cwd: Path | str | None) -> str:
    """A whole system prompt: stable tier, the context block when there are sections, the
    volatile tier with the runtime block last (none for a remote backend: ``cwd`` None)."""
    tiers = [STABLE]
    if sections:
        tiers.append(BLOCK_HEADER + "\n".join(sections))
    volatile = [SKILLS, "Conversation started: Tuesday, October 07, 2026"]
    if cwd is not None:
        hints = (
            f"Host: Linux (6.1.0)\nUser home directory: /home/op\nCurrent working directory: {cwd}"
        )
        volatile.append(f"{RUNTIME_HEADING}\n\n{hints}\n\n{RUNTIME_END}")
    tiers.append("\n\n".join(volatile))
    return "\n\n".join(tiers)


def make_state_db(home: Path, sessions: Sequence[Session], *, legacy: bool = False) -> Path:
    """``home/state.db`` holding the sessions; the prompt deduplicated by its hash, as the
    engine stores it (``hermes_state.py:443-451``)."""
    home.mkdir(parents=True, exist_ok=True)
    path = home / "state.db"
    conn = sqlite3.connect(path)
    try:
        conn.executescript(LEGACY_SCHEMA if legacy else SCHEMA)
        for i, item in enumerate(sessions):
            _insert(conn, f"s{i}", item, legacy=legacy)
        conn.commit()
    finally:
        conn.close()
    return path


def _insert(conn: sqlite3.Connection, sid: str, item: Session, *, legacy: bool) -> None:
    if legacy:
        conn.execute(
            "INSERT INTO sessions VALUES (?,?,?,?)",
            (sid, item.source, item.prompt, item.started_at),
        )
        return
    digest = None
    if item.prompt is not None:
        digest = hashlib.sha256(item.prompt.encode("utf-8")).hexdigest()
        conn.execute("INSERT OR IGNORE INTO system_prompts VALUES (?,?)", (digest, item.prompt))
    conn.execute(
        "INSERT INTO sessions (id, source, system_prompt_hash, started_at, cwd)"
        " VALUES (?,?,?,?,NULL)",
        (sid, item.source, digest, item.started_at),
    )


def write_file(path: Path, text: str, *, at: float) -> Path:
    """A context file with its change time set (``at``, epoch seconds)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")
    os.utime(path, (at, at))
    return path


def gateway_state(home: Path, *platforms: str, stale: Sequence[str] = ()) -> None:
    """``gateway_state.json`` as ``gateway/status.py`` writes it: the record's own pid and start,
    each platform entry stamped by its writer; a ``stale`` platform by an earlier process."""
    import json

    home.mkdir(parents=True, exist_ok=True)
    pid, start = 4242, 1_790_000_000.0

    def entry(name: str) -> dict[str, object]:
        writer = (77, 1_780_000_000.0) if name in stale else (pid, start)
        return {"state": "connected", "writer_pid": writer[0], "writer_start_time": writer[1]}

    payload = {
        "pid": pid,
        "start_time": start,
        "gateway_state": "running",
        "updated_at": NOW.isoformat(),
        "platforms": {name: entry(name) for name in platforms},
    }
    (home / "gateway_state.json").write_text(json.dumps(payload), encoding="utf-8")
