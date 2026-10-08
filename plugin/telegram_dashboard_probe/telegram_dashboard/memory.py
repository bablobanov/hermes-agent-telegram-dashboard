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
review's replace or remove waits there even with the gate off. Approval and rejection both
delete the file, and no journal is kept: a pending file's modification time is when it was
queued (nothing rewrites one), and the time a write last landed is the notebooks' own
modification time (a hand edit moves it too; for skills, the curator's ledger). A provider
(``memory.provider``) leaves no status, only the engine's warnings in ``logs/errors.log``.

Read: ``config.yaml`` (the ``memory`` limits, switches, gate and provider name, and
``skills.write_approval``) through the engine's own YAML parser, each value as the engine takes
it (the managed overlay and ``${VAR}`` references are not applied); the two notebooks (up to
4 MB each), decoded as strictly as the engine decodes them; the pending files' names and times,
and for memory writes their ``action``, ``payload.target`` and the length of what an add would
write (up to 2,000 files of 256 KB); the tail of errors.log for the engine's memory-provider
warnings (their time, and whether one says the provider is unavailable). The same on Hermes
0.21.3 and 0.21.6. Never shown, logged or kept: the text of a notebook, an entry, a pending
write or its summary, a log line, a path. Numbers, dates and flags leave; a failure is the
exception's class.
"""

from __future__ import annotations

import codecs
import json
import math
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
# A pending memory write is a few entries of a notebook's size; a larger file adds nothing.
MAX_PENDING_BYTES = 256 * 1024
LOG_TAIL_BYTES = 256 * 1024
# The gate's words (``tools/write_approval.py``), the switches' words (``utils.TRUTHY_STRINGS``)
# and the values that mean "no provider" (0.21.6 ``agent/memory_provider.py``; 0.21.3 took any
# non-empty name as a provider). The same in 0.21.3 and 0.21.6.
GATE_WORDS = frozenset({"true", "on", "yes", "1", "approve", "enabled"})
TRUTHY_WORDS = frozenset({"1", "true", "yes", "on"})
NO_PROVIDER = frozenset({"", "default", "builtin", "built-in", "none"})
# What 0.21.6 logs once per gateway process when a configured provider is not usable.
UNAVAILABLE_WORDS = "reports unavailable"
# A pending write older than this is stuck; provider warnings this recent are its errors.
STUCK_SECONDS = 3 * 86400
PROVIDER_WINDOW_SECONDS = 86400
# An event is a line of the screen less its "- ".
EVENT_COLUMNS = 30

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
    try:
        return _observed(env, now)
    except Exception as exc:  # a file, a path, a parser: the class only, a message may carry text
        failed = f"memory: {type(exc).__name__}"
        return MemorySummary("unknown", detail=failed), _source("unavailable", detail=failed), ()


def _observed(env: Environment, now: datetime) -> MemoryPart:
    home = env.hermes_home
    if not any((home / name).exists() for name in (CONFIG_FILE, MEMORY_DIR, PENDING_DIR)):
        detail = "no Hermes memory here"
        return (
            MemorySummary("unsupported", detail=detail),
            _source("unsupported", detail=detail),
            (),
        )
    config = read_config(home)
    notebooks = tuple(read_notebook(home, spec, config) for spec in NOTEBOOKS)
    queues = (
        read_queue(home, "memory", config, notebooks),
        read_queue(home, "skills", config, notebooks),
    )
    provider = read_provider(home, config, now, started=env.process_started_at)
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
        enabled={
            target: _truthy(memory.get(switch), default=True)
            for _, target, _, _, switch in NOTEBOOKS
        },
        gates={"memory": _gate(memory), "skills": _gate(_section(document, "skills"))},
        provider=_provider(memory.get("provider")),
    )


def _section(document: object, name: str) -> Mapping[str, Any]:
    section = document.get(name) if isinstance(document, dict) else None
    return section if isinstance(section, dict) else {}


def _limit(value: object, default: int) -> int:
    """The limit the engine holds a notebook to: the value as configured, never converted
    (``agent_init`` hands it to ``MemoryStore`` as it is). ``0`` when no add can pass it: zero
    or below, or not a number (a quoted ``"5000"`` fails the engine's own comparison)."""
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, int | float):
        return 0
    if isinstance(value, float) and not math.isfinite(value):
        return 0
    return max(int(value), 0)


def _truthy(value: object, *, default: bool) -> bool:
    """``utils.is_truthy_value``: a notebook's switch is on unless set to something else."""
    if value is None:
        return default
    if isinstance(value, str):
        return value.strip().lower() in TRUTHY_WORDS
    return bool(value)


def _gate(section: Mapping[str, Any]) -> bool:
    """``write_approval`` as ``tools/write_approval.py`` reads it: a bool, or one of its words.
    Anything else is off, ``write_mode`` of configs before v29 too: only the config migration
    rewrites it, nothing reads it at run time."""
    value = section.get("write_approval")
    if isinstance(value, bool):
        return value
    return isinstance(value, str) and value.strip().lower() in GATE_WORDS


def _provider(value: object) -> str | None:
    if not isinstance(value, str) or value.strip().lower() in NO_PROVIDER:
        return None
    return sanitize_public_text(value.strip(), limit=24) or None


# ------------------------------------------------------------------ the notebooks


def read_notebook(
    home: Path, spec: tuple[str, str, str, int, str], config: _Config | None
) -> MemoryNotebook:
    """One notebook counted the engine's way; ``chars`` is ``None`` when there is no file, or
    when the engine cannot decode it (``readable`` False)."""
    name, target = spec[0], spec[1]
    enabled = config.enabled[target] if config is not None else None
    limit = config.limits[target] if config is not None else None
    path = home / MEMORY_DIR / name
    try:
        changed = path.stat().st_mtime
    except FileNotFoundError:
        return MemoryNotebook(name, enabled, None, limit)
    data = _read(path, MAX_NOTEBOOK_BYTES)
    entries = notebook_entries(data, whole=len(data) < MAX_NOTEBOOK_BYTES)
    if entries is None:
        return MemoryNotebook(name, enabled, None, limit, changed_at=_iso(changed), readable=False)
    lengths = [len(entry) for entry in entries]
    chars = sum(lengths) + len(DELIMITER) * max(len(entries) - 1, 0)
    return MemoryNotebook(
        name, enabled, chars, limit, len(entries), max(lengths, default=0), _iso(changed)
    )


def notebook_entries(data: bytes, *, whole: bool = True) -> list[str] | None:
    """The entries the engine loads: split on the delimiter, stripped, empty and repeated ones
    dropped (``MemoryStore``). ``None`` when the engine cannot decode the file: it reads it as
    strict UTF-8 and then loads nothing and refuses every write to it. ``whole`` is False when
    ``data`` is the file's head only: a character cut at its end is no error."""
    try:
        text = codecs.getincrementaldecoder("utf-8-sig")().decode(data, final=whole)
    except UnicodeDecodeError:
        return None
    # The engine reads a notebook in text mode: a Windows or old-Mac line end is a newline.
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
    """Per notebook the characters the waiting adds would write (each with its delimiter, but
    for the first entry of an empty notebook) and the room left; only notebooks the engine
    writes to and an add waits for. At most: an add the engine finds already there writes
    nothing. A file that cannot be read adds nothing."""
    wanted = {"memory": 0, "user": 0}
    for path in paths[:MAX_PENDING_FILES]:
        for target, length in _adds(path):
            if target in wanted:
                wanted[target] += length + len(DELIMITER)
    needs: list[tuple[str, int, int]] = []
    for spec, notebook in zip(NOTEBOOKS, notebooks, strict=True):
        name, target, room = spec[0], spec[1], _room(notebook)
        if not wanted[target] or room is None or not _writable(notebook):
            continue
        first = len(DELIMITER) if not notebook.entries else 0
        needs.append((name, wanted[target] - first, room))
    return tuple(needs)


def _writable(notebook: MemoryNotebook) -> bool:
    return notebook.enabled is not False and notebook.readable


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
    texts = (_add_text(op) for op in operations)
    return [(target, len(text.strip())) for text in texts if text is not None]


def _add_text(op: object) -> str | None:
    """What one operation adds: a batch takes ``content``, else ``new_text`` (``MemoryStore``)."""
    if not isinstance(op, dict) or op.get("action") != "add":
        return None
    text = op.get("content") or op.get("new_text")
    return text if isinstance(text, str) else None


# ------------------------------------------------------------------ the provider


def read_provider(
    home: Path, config: _Config | None, now: datetime, *, started: datetime | None = None
) -> MemoryProvider | None:
    """A configured provider, the engine's warnings about it in the errors log over the last
    day and the time of the last one, and whether the engine said since ``started`` (when the
    plugin came up in this gateway process) that the provider reports unavailable: 0.21.6 says
    so once per process, so that one warning stands until the next start. ``None`` without a
    provider. No warning is not proof it works: on 0.21.3 a provider that is not installed is
    logged at DEBUG only."""
    if config is None or config.provider is None:
        return None
    try:
        text = read_tail(home / ERRORS_LOG, size=LOG_TAIL_BYTES)
    except (OSError, UnicodeDecodeError):
        return MemoryProvider(config.provider, log_read=False)
    warnings = [
        (entry.at, UNAVAILABLE_WORDS in entry.head)
        for entry in split_entries(text)
        if entry.level in ("WARNING", "ERROR", "CRITICAL")
        and entry.at is not None
        and "memory provider" in entry.head.lower()
    ]
    window = PROVIDER_WINDOW_SECONDS
    recent = [at for at, _ in warnings if 0 <= (now - at).total_seconds() < window]
    down = [at for at, said in warnings if said and started is not None and at >= started]
    last = max((at for at, _ in warnings), default=None)
    return MemoryProvider(
        config.provider,
        len(recent),
        last.isoformat() if last else None,
        unavailable_at=max(down).isoformat() if down else None,
    )


# ------------------------------------------------------------------ events


def incidents_for(summary: MemorySummary, now: datetime) -> tuple[Incident, ...]:
    """A queue stuck, a notebook the engine cannot write to, a provider down or warning: one
    event each."""
    events = [_stuck(queue, now) for queue in summary.queues if is_stuck(queue, now)]
    for notebook in summary.notebooks:
        trouble = notebook_trouble(notebook)
        if trouble is not None:
            events.append(_trouble(notebook, trouble))
    provider = summary.provider
    if provider is not None and provider.unavailable_at:
        events.append(
            Incident("memory:provider:unavailable", "warning", "Memory provider: unavailable")
        )
    elif provider is not None and provider.errors:
        events.append(
            Incident(
                "memory:provider:errors",
                "warning",
                f"Memory provider: {provider.errors} error{'s' if provider.errors > 1 else ''}",
            )
        )
    return tuple(events)


def is_stuck(queue: MemoryQueue, now: datetime) -> bool:
    """A write waits longer than ``STUCK_SECONDS``. Approval and rejection both delete its
    file, so a write that old has had neither, whatever landed meanwhile (the agent's own adds,
    another write approved)."""
    oldest = _moment(queue.oldest_at)
    if not queue.count or oldest is None:
        return False
    return (now - oldest).total_seconds() >= STUCK_SECONDS


def notebook_trouble(notebook: MemoryNotebook) -> str | None:
    """What keeps the engine from writing to a notebook it has on: a file it cannot decode, a
    limit no add can pass, or more text than the limit (the engine's own warning on load:
    every add refused until the agent frees room). A notebook near its limit is the engine's
    ordinary state, no trouble: a refused add asks the model to consolidate in the same turn."""
    if notebook.enabled is False:
        return None
    if not notebook.readable:
        return "not UTF-8"
    if notebook.limit == 0:
        return "bad limit"
    over = notebook.limit is not None and (notebook.chars or 0) > notebook.limit
    return "over limit" if over else None


def _stuck(queue: MemoryQueue, now: datetime) -> Incident:
    oldest = _moment(queue.oldest_at)
    days = int((now - oldest).total_seconds() // 86400) if oldest is not None else 0
    label = "Memory" if queue.subsystem == "memory" else "Skills"
    writes = "write" if queue.count == 1 else "writes"
    title = f"{label}: {queue.count:,} {writes} stuck {days}d"
    if len(title) > EVENT_COLUMNS:
        title = f"{label}: {queue.count:,} stuck {days}d"
    return Incident(f"memory:{queue.subsystem}:stuck", "warning", title)


def _trouble(notebook: MemoryNotebook, trouble: str) -> Incident:
    kind = trouble.replace(" ", "_").replace("-", "").lower()
    return Incident(
        f"memory:{notebook.name}:{kind}", "warning", f"Memory: {notebook.name} {trouble}"
    )


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
