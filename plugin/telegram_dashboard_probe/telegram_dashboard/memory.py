"""Whether the agent's memory works: its two notebooks against their limits, the writes that wait
for approval and since when, when a write last landed, and a configured provider's errors.

Hermes keeps a profile's built-in memory in ``HERMES_HOME/memories``: ``MEMORY.md`` (the agent's
notes) and ``USER.md`` (what it knows of the user), entries joined by a line holding ``§``, each
notebook held to a character limit (``memory.memory_char_limit``, 2200; ``user_char_limit``,
1375) counted the engine's way: the entries stripped, empty and repeated ones dropped, joined
with the three-character delimiter. With ``memory.write_approval`` (``skills.write_approval``)
on, a write from the gateway, a cron job or the background review waits as one JSON file in
``HERMES_HOME/pending/memory`` (``pending/skills``) until ``/memory approve``; nothing tells the
operator, and the queue can grow for weeks while the agent believes it remembered. A background
review's replace or remove waits there even with the gate off. Approval applies the write and
deletes the file, and no journal is kept: the time a write last landed is the notebooks' own
modification time (a hand edit moves it too; for skills, the curator's ledger), and a pending
file's modification time is when it was queued (nothing rewrites one). A provider
(``memory.provider``) leaves no status, only the engine's warnings in ``logs/errors.log``.

Read: ``config.yaml`` (the ``memory`` limits, switches, gate and provider name, and
``skills.write_approval``) through the engine's own YAML parser; the two notebooks (up to 4 MB
each); the pending files' names and times, and for memory writes their ``action``,
``payload.target`` and the length of what an add would write (up to 2,000 files of 1 MB); the
tail of errors.log for the engine's memory-provider warnings (their time only). The same on
Hermes 0.21.3 and 0.21.6. Never shown, logged or kept: the text of a notebook, an entry, a
pending write or its summary, a log line, a path. Numbers, dates and flags leave; a failure is
the exception's class.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .compat import Environment
from .context_files import yaml_parser
from .gemini_log import read_tail, split_entries
from .policy import sanitize_public_text
from .schema import (
    Incident,
    MemoryNotebook,
    MemoryProvider,
    MemoryQueue,
    MemorySummary,
    SourceObservation,
    SourceState,
)

SOURCE_NAME = "memory"
CONFIG_FILE = "config.yaml"
MEMORY_DIR = "memories"
PENDING_DIR = "pending"
LEDGER = Path("skills") / ".curator_ledger.jsonl"
ERRORS_LOG = Path("logs") / "errors.log"
DELIMITER = "\n§\n"
# The notebook, the engine's target name, its limit key and default, its switch.
NOTEBOOKS = (
    ("MEMORY.md", "memory", "memory_char_limit", 2200, "memory_enabled"),
    ("USER.md", "user", "user_char_limit", 1375, "user_profile_enabled"),
)
SUBSYSTEMS = ("memory", "skills")
MAX_NOTEBOOK_BYTES = 4 * 1024 * 1024
MAX_PENDING_FILES = 2000
MAX_PENDING_BYTES = 1024 * 1024
LOG_TAIL_BYTES = 256 * 1024
# The gate's words (``tools/write_approval.py``) and the values that mean "no provider" (0.21.6
# ``agent/memory_provider.py``; 0.21.3 took any non-empty name as a provider).
GATE_WORDS = frozenset({"true", "on", "yes", "1", "approve", "enabled"})
NO_PROVIDER = frozenset({"", "default", "builtin", "built-in", "none"})
# A queue older than this with nothing applied since it began is stuck; a notebook this full
# refuses the next add of an ordinary entry; provider warnings this recent are its state.
STUCK_SECONDS = 3 * 86400
FULL_PERCENT = 90
PROVIDER_WINDOW_SECONDS = 86400

MemoryPart = tuple[MemorySummary, SourceObservation | None, tuple[Incident, ...]]


@dataclass(frozen=True, slots=True)
class _Config:
    limits: Mapping[str, int]
    enabled: Mapping[str, bool]
    gates: Mapping[str, bool]
    provider: str | None


def read_memory(env: Environment, *, now: datetime) -> MemoryPart:
    """The whole answer with its source; ``None`` as the source when the section is off. Never
    raises: a failure is one unavailable source with the exception's class, nothing logged."""
    if not env.memory_enabled:
        return MemorySummary("off"), None, ()
    home = env.hermes_home
    if not any((home / name).exists() for name in (CONFIG_FILE, MEMORY_DIR, PENDING_DIR)):
        detail = "no Hermes memory here"
        return (
            MemorySummary("unsupported", detail=detail),
            _source("unsupported", detail=detail),
            (),
        )
    try:
        config = read_config(home)
        notebooks = tuple(read_notebook(home, spec, config) for spec in NOTEBOOKS)
        queues = (
            read_queue(home, "memory", config, notebooks),
            read_queue(home, "skills", config, notebooks),
        )
        provider = read_provider(home, config, now)
    except Exception as exc:  # a file, a path, a parser: the class only, a message may carry text
        failed = f"memory: {type(exc).__name__}"
        return MemorySummary("unknown", detail=failed), _source("unavailable", detail=failed), ()
    unread = None if config is not None else "config.yaml not read"
    summary = MemorySummary("observed", notebooks, queues, provider, unread)
    return summary, _source("fresh", observed_at=now.isoformat()), incidents_for(summary, now)


def _source(
    state: SourceState, *, detail: str | None = None, observed_at: str | None = None
) -> SourceObservation:
    return SourceObservation(SOURCE_NAME, "official", state, observed_at, detail)


# ------------------------------------------------------------------ the config


def read_config(home: Path) -> _Config | None:
    """The engine's memory settings; its defaults without a config file, ``None`` when the file
    is there and cannot be read (no YAML parser here, or the parser refuses it)."""
    path = home / CONFIG_FILE
    document: object = {}
    if path.is_file():
        parser = yaml_parser()
        if parser is None:
            return None
        try:
            document = parser.safe_load(path.read_text(encoding="utf-8-sig"))
        except Exception:  # the parser's own errors carry the file's text
            return None
    memory = _section(document, "memory")
    return _Config(
        limits={
            target: _limit(memory.get(key), default) for _, target, key, default, _ in NOTEBOOKS
        },
        enabled={target: memory.get(switch) is not False for _, target, _, _, switch in NOTEBOOKS},
        gates={"memory": _gate(memory), "skills": _gate(_section(document, "skills"))},
        provider=_provider(memory.get("provider")),
    )


def _section(document: object, name: str) -> Mapping[str, Any]:
    section = document.get(name) if isinstance(document, dict) else None
    return section if isinstance(section, dict) else {}


def _limit(value: object, default: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int | str):
        return default
    try:
        number = int(value)
    except ValueError:
        return default
    return number if number > 0 else default


def _gate(section: Mapping[str, Any]) -> bool:
    """``write_approval`` as the engine reads it; configs before v29 said
    ``write_mode: approve``."""
    value = section.get("write_approval")
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in GATE_WORDS
    return value is None and str(section.get("write_mode", "")).strip().lower() == "approve"


def _provider(value: object) -> str | None:
    if not isinstance(value, str) or value.strip().lower() in NO_PROVIDER:
        return None
    return sanitize_public_text(value.strip(), limit=24) or None


# ------------------------------------------------------------------ the notebooks


def read_notebook(
    home: Path, spec: tuple[str, str, str, int, str], config: _Config | None
) -> MemoryNotebook:
    """One notebook counted the engine's way; ``chars`` is ``None`` when there is no file."""
    name, target = spec[0], spec[1]
    enabled = config.enabled[target] if config is not None else None
    limit = config.limits[target] if config is not None else None
    path = home / MEMORY_DIR / name
    try:
        changed = path.stat().st_mtime
    except FileNotFoundError:
        return MemoryNotebook(name, enabled, None, limit)
    entries = notebook_entries(_read(path, MAX_NOTEBOOK_BYTES))
    lengths = [len(entry) for entry in entries]
    chars = sum(lengths) + len(DELIMITER) * max(len(entries) - 1, 0)
    return MemoryNotebook(
        name, enabled, chars, limit, len(entries), max(lengths, default=0), _iso(changed)
    )


def notebook_entries(data: bytes) -> list[str]:
    """The entries the engine loads: split on the delimiter, stripped, empty and repeated ones
    dropped (``MemoryStore``)."""
    # The engine reads a notebook in text mode: a Windows or old-Mac line end is a newline.
    text = data.decode("utf-8-sig", errors="replace")
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    stripped = (entry.strip() for entry in text.split(DELIMITER))
    return list(dict.fromkeys(entry for entry in stripped if entry))


def _read(path: Path, limit: int) -> bytes:
    with path.open("rb") as handle:
        return handle.read(limit)


# ------------------------------------------------------------------ the queues


def read_queue(
    home: Path,
    subsystem: str,
    config: _Config | None,
    notebooks: tuple[MemoryNotebook, ...],
) -> MemoryQueue:
    """How many writes wait, the oldest and the newest, when a write last landed and, for
    memory, what the waiting adds would need in each notebook against its free room."""
    gate = config.gates[subsystem] if config is not None else None
    applied = _applied(home, subsystem, notebooks)
    files = _pending_files(home / PENDING_DIR / subsystem)
    times = [moment for _path, moment in files]
    if not times:
        return MemoryQueue(subsystem, gate, applied_at=applied)
    needs = _needs([path for path, _ in files], notebooks) if subsystem == "memory" else ()
    return MemoryQueue(
        subsystem, gate, len(times), _iso(min(times)), _iso(max(times)), applied, needs
    )


def _pending_files(directory: Path) -> list[tuple[Path, float]]:
    """The queued writes and when each was queued; a file approved away meanwhile is gone."""
    if not directory.is_dir():
        return []
    found: list[tuple[Path, float]] = []
    for path in sorted(directory.glob("*.json")):
        try:
            found.append((path, path.stat().st_mtime))
        except FileNotFoundError:
            continue
    return found


def _applied(home: Path, subsystem: str, notebooks: tuple[MemoryNotebook, ...]) -> str | None:
    if subsystem == "memory":
        changed = [notebook.changed_at for notebook in notebooks if notebook.changed_at]
        return max(changed) if changed else None
    try:
        return _iso((home / LEDGER).stat().st_mtime)
    except FileNotFoundError:
        return None


def _needs(
    paths: list[Path], notebooks: tuple[MemoryNotebook, ...]
) -> tuple[tuple[str, int, int], ...]:
    """Per notebook the characters the waiting adds would write (each with its delimiter) and
    the room left; only notebooks an add waits for. A file that cannot be read adds nothing."""
    wanted = {"memory": 0, "user": 0}
    for path in paths[:MAX_PENDING_FILES]:
        for target, length in _adds(path):
            if target in wanted:
                wanted[target] += length + len(DELIMITER)
    needs: list[tuple[str, int, int]] = []
    for spec, notebook in zip(NOTEBOOKS, notebooks, strict=True):
        name, target, room = spec[0], spec[1], _room(notebook)
        if wanted[target] and room is not None:
            needs.append((name, wanted[target], room))
    return tuple(needs)


def _room(notebook: MemoryNotebook) -> int | None:
    if notebook.limit is None:
        return None
    return max(notebook.limit - (notebook.chars or 0), 0)


def _adds(path: Path) -> Iterable[tuple[str, int]]:
    """``(target, length)`` of every add one pending memory write holds; the text is measured
    here and dropped."""
    try:
        record = json.loads(_read(path, MAX_PENDING_BYTES).decode("utf-8-sig"))
    except (OSError, ValueError, RecursionError):
        return ()
    payload = record.get("payload") if isinstance(record, dict) else None
    if not isinstance(payload, dict):
        return ()
    target = payload.get("target")
    if not isinstance(target, str):
        return ()
    if record.get("action") == "add":
        operations: object = [{"action": "add", "content": payload.get("content")}]
    else:
        operations = payload.get("operations")
    if not isinstance(operations, list):
        return ()
    return [
        (target, len(op["content"].strip()))
        for op in operations
        if isinstance(op, dict) and op.get("action") == "add" and isinstance(op.get("content"), str)
    ]


# ------------------------------------------------------------------ the provider


def read_provider(home: Path, config: _Config | None, now: datetime) -> MemoryProvider | None:
    """A configured provider and the engine's warnings about it in the errors log over the last
    day; ``None`` without one. No warning is not proof it works: on 0.21.3 a provider that is
    not installed is logged at DEBUG only."""
    if config is None or config.provider is None:
        return None
    try:
        text = read_tail(home / ERRORS_LOG, size=LOG_TAIL_BYTES)
    except (OSError, UnicodeDecodeError):
        return MemoryProvider(config.provider, log_read=False)
    times = [
        entry.at
        for entry in split_entries(text)
        if entry.level in ("WARNING", "ERROR", "CRITICAL")
        and "Memory provider" in entry.head
        and entry.at is not None
        and 0 <= (now - entry.at).total_seconds() < PROVIDER_WINDOW_SECONDS
    ]
    last = max(times).isoformat() if times else None
    return MemoryProvider(config.provider, len(times), last)


# ------------------------------------------------------------------ events


def incidents_for(summary: MemorySummary, now: datetime) -> tuple[Incident, ...]:
    """A queue stuck, a notebook nearly full, a provider that warns: one event each."""
    events = [_stuck(queue, now) for queue in summary.queues if is_stuck(queue, now)]
    events.extend(_full(notebook) for notebook in summary.notebooks if is_full(notebook))
    provider = summary.provider
    if provider is not None and provider.errors:
        events.append(
            Incident(
                "memory:provider:errors",
                "warning",
                f"Memory provider: {provider.errors} error{'s' if provider.errors > 1 else ''}",
            )
        )
    return tuple(events)


def is_stuck(queue: MemoryQueue, now: datetime) -> bool:
    """Writes wait longer than ``STUCK_SECONDS`` and none landed since the oldest was queued."""
    oldest = _moment(queue.oldest_at)
    if not queue.count or oldest is None:
        return False
    if (now - oldest).total_seconds() < STUCK_SECONDS:
        return False
    applied = _moment(queue.applied_at)
    return applied is None or applied < oldest


def fill_percent(notebook: MemoryNotebook) -> int | None:
    if notebook.chars is None or not notebook.limit:
        return None
    return int(notebook.chars * 100 // notebook.limit)


def is_full(notebook: MemoryNotebook) -> bool:
    """At ``FULL_PERCENT`` of its limit or past it; a notebook the engine switched off is not
    written to, so it is never full."""
    percent = fill_percent(notebook)
    return notebook.enabled is not False and percent is not None and percent >= FULL_PERCENT


def _stuck(queue: MemoryQueue, now: datetime) -> Incident:
    oldest = _moment(queue.oldest_at)
    days = int((now - oldest).total_seconds() // 86400) if oldest is not None else 0
    label = "Memory" if queue.subsystem == "memory" else "Skills"
    return Incident(
        f"memory:{queue.subsystem}:stuck",
        "warning",
        f"{label}: {queue.count:,} writes stuck {days}d",
    )


def _full(notebook: MemoryNotebook) -> Incident:
    percent = fill_percent(notebook) or 0
    words = "over limit" if percent > 100 else f"{percent}% full"
    return Incident(f"memory:{notebook.name}:full", "warning", f"Memory: {notebook.name} {words}")


def _moment(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        moment = datetime.fromisoformat(value)
    except ValueError:
        return None
    return moment if moment.tzinfo is not None else moment.replace(tzinfo=UTC)


def _iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, UTC).isoformat()
