"""Whether the agent sees its rules: the project context files in the system prompt of the
latest session of every platform the gateway runs.

The engine builds a session's system prompt on its first turn (again after a compression or a
model switch) and keeps it in ``HERMES_HOME/state.db``: ``sessions.system_prompt_hash`` into
``system_prompts.prompt``, or the older inline ``sessions.system_prompt``. Where the engine looked
for context files is the ``Current working directory:`` line of the prompt's runtime block
(``sessions.cwd`` stays empty for gateway sessions; the engine checks a stored prompt against
the same line).

The verdict never trusts headings found inside the prompt: the files are discovered on disk by
the engine's own rules (``.hermes.md`` up to the git root, the ``AGENTS.md`` chain from the git
root down, ``CLAUDE.md``, the cursor rules; the first kind found wins), rendered the way the
engine renders them, and compared with the prompt right after its ``# Project Context`` header.
The same text there is ``loaded``; the engine's cut of it ``truncated``; the scan's notice
``blocked``; anything else ``outdated``. When the prompt holds the current text, the end of the
block is proven by the engine's next part right after it (the skills index, the workspace
snapshot, the memory block, the timestamp line, the runtime block); without that proof a file
changed after the session started is ``outdated``, since a saved prompt cannot otherwise say
where an older, longer text ended. No block while a file is there for it is ``none``; no file
where the agent looks, nor an ``AGENTS.md`` or ``.hermes.md`` in the gateway's working directory
or in ``HERMES_HOME``, is ``no_files``, never an alarm. A platform that skips context files in
the engine's ``config.yaml`` is ``off``; a prompt whose directory cannot be known is ``unknown``.

The database is the engine's and the gateway writes it: ``mode=ro`` with ``PRAGMA query_only``,
two short statements per platform (the session row, then its one prompt), the connection closed
before any file is read, all in a worker under the tick's deadline. The prompt and the files are
compared in memory and dropped; the snapshot carries file names as the engine labels them,
lengths, times and verdicts, never their text and never a path.
"""

from __future__ import annotations

import importlib
import json
import logging
import os
import re
import sqlite3
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from .compat import Environment
from .cron_runs import open_readonly
from .schema import (
    Incident,
    PlatformRules,
    RulesSummary,
    RulesVerdict,
    RulesWhy,
    SourceObservation,
    SourceState,
)

logger = logging.getLogger(__name__)

SOURCE_NAME = "context_files"
STATE_FILE = "state.db"
GATEWAY_STATE_FILE = "gateway_state.json"
CONFIG_FILE = "config.yaml"
# Without a readable platform map the dashboard's own platform is the one judged.
DEFAULT_PLATFORMS = ("telegram",)
MAX_PLATFORMS = 12
# A context file larger than this is not read for the comparison (the engine's budget tops out
# at 500,000 characters, so such a file is always cut): the prompt's own marker then decides.
MAX_FILE_BYTES = 4 * 1024 * 1024
# A change time this far ahead of the clock is not a time (skew, an archive, a foreign disk).
FUTURE_SLACK_SECONDS = 60.0
HERMES_NAMES = (".hermes.md", "HERMES.md")
AGENTS_NAMES = ("AGENTS.override.md", "AGENTS.md", "agents.md")
CLAUDE_NAMES = ("CLAUDE.md", "claude.md")
CURSOR_NAME = ".cursorrules"
CONTEXT_NAMES = (*HERMES_NAMES, *AGENTS_NAMES, *CLAUDE_NAMES, CURSOR_NAME)
# The names that mean "for this agent" where the agent does not look (the gateway's directory,
# HERMES_HOME): a CLAUDE.md or .cursorrules there is most likely for another tool.
MEANT_FOR_HERMES = (*HERMES_NAMES, *AGENTS_NAMES)
WARNING_VERDICTS = frozenset({"truncated", "outdated", "blocked", "none"})

# ``build_context_files_prompt``: the header and the line under it, then the sections.
_HEADER = (
    "# Project Context\n\nThe following project context files have been loaded and should be "
    "followed:\n\n"
)
_RUNTIME_HEADING = "# Hermes runtime environment"
_RUNTIME_END = "<!-- End Hermes runtime environment -->"
_CWD_PREFIX = "Current working directory:"
_HOME_PREFIX = "User home directory:"
# The parts the engine puts right after the context block (``build_system_prompt_parts``, the
# same on 0.21.1, 0.21.3, 0.21.5 and main): the skills index, the workspace snapshot, the
# memory block, the timestamp line, the runtime block. One of them after the current text
# proves the block ended there.
_NEXT_PARTS = (
    "## Skills\nBefore replying, scan the skills below",
    "Workspace (snapshot at session start",
    "═" * 46 + "\n",
    "Conversation started: ",
    f"{_RUNTIME_HEADING}\n\n",
)
_MARKER_RE = re.compile(
    r"\n\n\[\.\.\.truncated ([^:\n]{1,200}): kept (\d{1,9})\+(\d{1,9}) of (\d{1,9}) chars"
)
_CHAIN_WARN_NAME = "AGENTS.md (directory chain)"

RulesPart = tuple[RulesSummary, SourceObservation | None, tuple[Incident, ...]]
Kind = Literal["hermes", "agents", "claude", "cursor"]
ReadStatus = Literal["ok", "empty", "big", "undecodable"]


@dataclass(frozen=True, slots=True)
class Read:
    """A context file as the engine reads it: ``raw`` stripped (a BOM kept, as ``strip`` keeps
    it), ``text`` what the prompt carries (a leading BOM dropped, as the scan drops it);
    ``empty`` and ``undecodable`` files the engine skips, ``big`` ones it cuts."""

    raw: str | None
    text: str | None
    changed: float | None
    status: ReadStatus


@dataclass(frozen=True, slots=True)
class Found:
    """A context file the engine would load: its label as the prompt carries it, its text as the
    engine puts it in (``None`` when it cannot be read for the comparison), its newest change."""

    label: str
    text: str | None
    changed: float | None


@dataclass(frozen=True, slots=True)
class Expected:
    kind: Kind
    files: tuple[Found, ...]

    @property
    def labels(self) -> tuple[str, ...]:
        return tuple(found.label for found in self.files)

    @property
    def changed(self) -> float | None:
        return max((f.changed for f in self.files if f.changed is not None), default=None)


@dataclass(frozen=True, slots=True)
class Session:
    platform: str
    started: float
    prompt: str


@dataclass(frozen=True, slots=True)
class Match:
    verdict: RulesVerdict
    chars: int | None = None
    kept: int | None = None
    # The engine's next part follows the text: the block provably ends there.
    proven: bool = False


def read_rules(env: Environment, *, now: datetime) -> RulesPart:
    """The whole answer with its source; ``None`` as the source when the line is off. Never
    raises: a failure is one unavailable source with the exception's class, nothing logged."""
    if not env.context_files_enabled:
        return RulesSummary("off"), None, ()
    path = env.hermes_home / STATE_FILE
    try:
        if not path.is_file():
            return _not_here("state.db not found")
        sessions = latest_sessions(path, gateway_platforms(env.hermes_home))
        if sessions is None:
            return _not_here("state.db has no saved system prompts")
        skipped = skipping_platforms(env.hermes_home)
        judged = tuple(
            judge(s, env, skipped=s.platform in skipped, now=now.timestamp()) for s in sessions
        )
    except sqlite3.Error as exc:
        return _unavailable(f"state.db: {type(exc).__name__}")
    except Exception as exc:  # a file, a path, a parser: the class only, a message may carry a path
        return _unavailable(f"rules: {type(exc).__name__}")
    source = _source("fresh", observed_at=now.isoformat())
    return RulesSummary("observed", judged), source, incidents_for(judged)


def _not_here(detail: str) -> RulesPart:
    return RulesSummary("unsupported", detail=detail), _source("unsupported", detail=detail), ()


def _unavailable(detail: str) -> RulesPart:
    return RulesSummary("unknown", detail=detail), _source("unavailable", detail=detail), ()


def _source(
    state: SourceState, *, detail: str | None = None, observed_at: str | None = None
) -> SourceObservation:
    return SourceObservation(SOURCE_NAME, "official", state, observed_at, detail)


# ------------------------------------------------------------------ the database


def latest_sessions(path: Path, platforms: Iterable[str]) -> list[Session] | None:
    """The newest session with a saved prompt per platform; ``None`` when this database keeps no
    prompts. Two statements per platform, so no other session's prompt is ever read: the row
    (by the source index), then the one prompt."""
    conn = open_readonly(path)
    try:
        columns = prompt_columns(conn)
        if columns is None:
            return None
        found = [_latest(conn, columns, platform) for platform in platforms]
    finally:
        conn.close()
    return [session for session in found if session is not None]


def prompt_columns(conn: sqlite3.Connection) -> tuple[bool, bool] | None:
    """Which prompt storage this schema has: the deduplicated ``system_prompts`` table
    (Hermes v2026.8.3 and later), the inline column, both, or neither (``None``)."""
    tables = {name for (name,) in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if "sessions" not in tables:
        return None
    names = {row[1] for row in conn.execute("PRAGMA table_info(sessions)")}
    by_hash = "system_prompts" in tables and "system_prompt_hash" in names
    inline = "system_prompt" in names
    return (by_hash, inline) if by_hash or inline else None


def _latest(conn: sqlite3.Connection, columns: tuple[bool, bool], platform: str) -> Session | None:
    by_hash, inline = columns
    hash_col = "system_prompt_hash" if by_hash else "NULL"
    saved = " OR ".join(
        part
        for part, present in (
            ("system_prompt_hash IS NOT NULL", by_hash),
            ("system_prompt IS NOT NULL", inline),
        )
        if present
    )
    row = conn.execute(
        f"SELECT rowid, started_at, {hash_col} FROM sessions"
        f" WHERE source = ? AND ({saved}) ORDER BY started_at DESC LIMIT 1",
        (platform,),
    ).fetchone()
    if row is None or not isinstance(row[1], int | float):
        return None
    prompt = None
    if by_hash and row[2] is not None:
        hit = conn.execute("SELECT prompt FROM system_prompts WHERE hash = ?", (row[2],)).fetchone()
        prompt = hit[0] if hit else None
    if prompt is None and inline:
        hit = conn.execute(
            "SELECT system_prompt FROM sessions WHERE rowid = ?", (row[0],)
        ).fetchone()
        prompt = hit[0] if hit else None
    return Session(platform, float(row[1]), prompt) if isinstance(prompt, str) else None


# ------------------------------------------------------------------ the engine's own files


def gateway_platforms(home: Path) -> tuple[str, ...]:
    """The platforms of this profile the running gateway has written (``gateway_state.json``:
    the ``platforms`` entries whose writer is the record's own pid and start, the way the
    engine's status tells live from preserved; ``<profile>:`` keys belong to another profile's
    database). Right after a start, with none written yet, Telegram; without writer stamps
    (an older engine) every key; without a record, Telegram."""
    try:
        payload = json.loads((home / GATEWAY_STATE_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeDecodeError):
        return DEFAULT_PLATFORMS
    platforms = payload.get("platforms") if isinstance(payload, dict) else None
    if not isinstance(platforms, dict):
        return DEFAULT_PLATFORMS
    keys = [k for k in platforms if isinstance(k, str) and k.strip() and ":" not in k]
    writer = (payload.get("pid"), payload.get("start_time"))
    if None in writer:
        names = sorted(keys)
    else:
        names = sorted(
            key
            for key in keys
            if isinstance(entry := platforms[key], dict)
            and (entry.get("writer_pid"), entry.get("writer_start_time")) == writer
        )
    names.sort(key=lambda name: name != "telegram")
    return tuple(names[:MAX_PLATFORMS]) or DEFAULT_PLATFORMS


def skipping_platforms(home: Path) -> frozenset[str]:
    """``gateway.platforms.<name>.skip_context_files`` from the engine's ``config.yaml``, the one
    key read there, with the engine's own YAML parser when the interpreter has it; empty when it
    cannot be read (no platform is then taken as skipping)."""
    path = home / CONFIG_FILE
    if not path.is_file():
        return frozenset()
    try:
        yaml: Any = importlib.import_module("yaml")
    except ImportError:
        return frozenset()
    try:
        config = yaml.safe_load(path.read_text(encoding="utf-8"))
    except Exception as exc:  # OSError, a decode error, the parser's own errors
        logger.debug("rules: config.yaml not read (%s)", type(exc).__name__)
        return frozenset()
    gateway = config.get("gateway") if isinstance(config, dict) else None
    platforms = gateway.get("platforms") if isinstance(gateway, dict) else None
    if not isinstance(platforms, dict):
        return frozenset()
    return frozenset(
        str(name)
        for name, entry in platforms.items()
        if isinstance(entry, dict) and bool(entry.get("skip_context_files"))
    )


# ------------------------------------------------------------------ the verdict


def judge(session: Session, env: Environment, *, skipped: bool, now: float) -> PlatformRules:
    """The verdict on one platform's latest saved prompt."""
    prompt, started_at = session.prompt, _iso(session.started)
    # Where discovery ran: the prompt's own line; for a sandbox backend the engine's TERMINAL_CWD
    # when it exists on the host, else the process's directory (``resolve_context_cwd``).
    where = agent_dir(prompt) or _usable(env.terminal_cwd) or env.gateway_dir
    start = prompt.find(_HEADER)
    if start < 0:
        if skipped:
            return PlatformRules(session.platform, "off", started_at)
        return _absent(session, started_at, where, env)
    region = prompt[start + len(_HEADER) :]
    if where is None:  # rules in the prompt, and no directory to compare them with
        return PlatformRules(session.platform, "unknown", started_at)
    expected = discover(where)
    if expected is None:  # rules in the prompt, none on disk now
        label = _first_label(region)
        files = (label,) if label else ()
        return PlatformRules(session.platform, "outdated", started_at, files, why="gone")
    return _compare(session, started_at, region, expected, now)


def _usable(path: Path | None) -> Path | None:
    try:
        return path if path is not None and path.is_dir() else None
    except OSError:
        return None


def agent_dir(prompt: str) -> Path | None:
    """The agent's working directory as the engine itself reads it back from a stored prompt
    (``_stored_prompt_matches_runtime``): in the last runtime block when the prompt ends with
    one, the line within three after ``User home directory:``; ``None`` for a sandbox backend,
    which names no host directory."""
    if prompt.endswith(_RUNTIME_END):
        runtime = prompt.rpartition(f"\n\n{_RUNTIME_HEADING}\n\n")[2]
        lines = runtime.split("\n\n", 1)[0].splitlines()
    else:
        lines = prompt.splitlines()
    for index, line in enumerate(lines):
        if not line.startswith(_HOME_PREFIX):
            continue
        for candidate in lines[index + 1 : index + 4]:
            if candidate.startswith(_CWD_PREFIX):
                value = candidate[len(_CWD_PREFIX) :].strip()
                return Path(value) if value else None
    return None


def _first_label(region: str) -> str | None:
    """The engine's own heading right after the header (never a heading inside a file)."""
    first = region.split("\n", 1)[0]
    label = first[3:].strip() if first.startswith("## ") else ""
    name = label.replace("\\", "/").rsplit("/", 1)[-1]
    known = name in CONTEXT_NAMES or name.endswith(".mdc")
    return label if label and len(label) <= 80 and known else None


def _compare(
    session: Session, started_at: str, region: str, expected: Expected, now: float
) -> PlatformRules:
    match = _match(region, expected)
    verdict, kept = match.verdict, match.kept
    changed = expected.changed
    after = changed is not None and session.started < changed <= now + FUTURE_SLACK_SECONDS
    if verdict in ("loaded", "truncated") and after and not match.proven:
        # Changed after the session started, and nothing after the text proves the block
        # ended there: the prompt may hold an older, longer text.
        verdict, kept = "outdated", None
    changed_at = _iso(changed) if verdict == "outdated" and after and changed else None
    return PlatformRules(
        session.platform, verdict, started_at, expected.labels, match.chars, kept, changed_at
    )


def _match(region: str, expected: Expected) -> Match:
    """The prompt's block against the files as the engine renders them."""
    if any(_blocked(region, label) for label in expected.labels):
        return Match("blocked")
    if any(found.text is None for found in expected.files):
        return _by_marker(region, expected)
    full = render(expected)
    if region.startswith(full):
        return Match("loaded", len(full), proven=_ends_here(region, len(full)))
    cut = _marker_at(region, expected)
    if cut is not None:
        return cut
    return Match("outdated", len(full))


def _ends_here(region: str, end: int) -> bool:
    rest = region[end:]
    return rest == "" or (rest.startswith("\n\n") and rest[2:].startswith(_NEXT_PARTS))


def _blocked(region: str, label: str) -> bool:
    notice = f"## {label}\n\n[BLOCKED: {label} contained potential prompt injection"
    return region.startswith(notice) or f"\n{notice}" in region[:200_000]


def _by_marker(region: str, expected: Expected) -> Match:
    """A file too big to read here: the engine always cut it, its marker says by how much."""
    first = expected.labels[0]
    marker = _MARKER_RE.search(region, 0, 600_000)
    if region.startswith(f"## {first}\n\n") and marker:
        total, kept = int(marker.group(4)), int(marker.group(2)) + int(marker.group(3))
        # Its text unread, the end of the block is unproven: a change after the start is outdated.
        return Match("truncated", total, kept)
    return Match("outdated")


def render(expected: Expected) -> str:
    """The block's body as the engine writes it before any cut (``_context_section``,
    ``_load_agents_md``, ``_load_cursorrules``), trailing whitespace stripped as the tier is."""
    return _bundle(expected).rstrip() if expected.kind == "cursor" else _joined(expected)


def _joined(expected: Expected) -> str:
    return "\n\n".join(_section_body(found) for found in expected.files)


def _bundle(expected: Expected) -> str:
    """The cursor rules as the engine cuts them: every section with its blank line after it."""
    return "".join(f"{_section_body(found)}\n\n" for found in expected.files)


def _section_body(found: Found) -> str:
    return f"## {found.label}\n\n{found.text}"


def _marker_at(region: str, expected: Expected) -> Match | None:
    """The engine's cut of this very text: the head it kept, the marker with this text's own
    length, the tail it kept; ``None`` when the prompt is not that cut."""
    if expected.kind == "cursor":
        return _cut(region, _bundle(expected), CURSOR_NAME)
    if len(expected.files) == 1:
        return _cut(region, _section_body(expected.files[0]), _warn_name(expected))
    # An AGENTS.md chain: cut as a whole, or each section on its own and every other one whole.
    whole = _cut(region, _joined(expected), _CHAIN_WARN_NAME)
    if whole is not None:
        return whole
    total = kept = 0
    for found in expected.files:
        body = _section_body(found)
        cut = _cut(region, body, found.label)
        if cut is None and body not in region:
            return _chain_cut_twice(region, expected)
        if cut is None:
            total, kept = total + len(body), kept + len(body)
        else:
            total, kept = total + (cut.chars or 0), kept + (cut.kept or 0)
    return Match("truncated", total, kept) if total != kept else None


def _chain_cut_twice(region: str, expected: Expected) -> Match | None:
    """A chain whose sections were cut and then the whole again: the first file's start and the
    chain's own marker are what can be checked."""
    chain = next((m for m in _MARKER_RE.finditer(region) if m.group(1) == _CHAIN_WARN_NAME), None)
    first = _section_body(expected.files[0])
    if chain is None or not region.startswith(first[: min(len(first), 1000)]):
        return None
    return Match("truncated", int(chain.group(4)), int(chain.group(2)) + int(chain.group(3)))


def _cut(region: str, body: str, warn: str) -> Match | None:
    for marker in _MARKER_RE.finditer(region):
        head, tail, total = int(marker.group(2)), int(marker.group(3)), int(marker.group(4))
        if marker.group(1) != warn or total != len(body):
            continue
        end = _cut_end(region, body, marker.start(), head, tail)
        if end is not None:
            return Match("truncated", total, head + tail, proven=_ends_here(region, end))
    return None


def _cut_end(region: str, body: str, at: int, head: int, tail: int) -> int | None:
    """Where the cut ends in the region: ``body[:head]`` right before the marker,
    ``body[-tail:]`` right after its closing ``]`` (its trailing blank line stripped by the
    tier when it closes the block); ``None`` when the prompt is not this cut."""
    if region[max(0, at - head) : at] != body[:head]:
        return None
    close = region.find("]\n\n", at)
    kept_tail = body[len(body) - tail :].rstrip()
    start = close + 3
    if close < 0 or region[start : start + len(kept_tail)] != kept_tail:
        return None
    return start + len(kept_tail)


def _warn_name(expected: Expected) -> str:
    """The name the engine's truncation marker uses for a one-file kind."""
    if expected.kind == "hermes":
        return ".hermes.md"
    if expected.kind == "claude":
        return "CLAUDE.md"
    return expected.files[0].label


def _absent(
    session: Session, started_at: str, where: Path | None, env: Environment
) -> PlatformRules:
    """No block in the prompt: is a file there for it, and where."""
    if where is not None and not looks_like_hermes_tree(where):
        expected = discover(where)
        if expected is not None:
            changed = expected.changed
            after = changed is not None and changed > session.started
            return PlatformRules(
                session.platform,
                "none",
                started_at,
                expected.labels,
                changed_at=_iso(changed) if changed is not None else None,
                why="after" if after else "not_loaded",
            )
        unreadable = _undecodable(where, CONTEXT_NAMES)
        if unreadable:
            return PlatformRules(session.platform, "none", started_at, unreadable, why="not_utf8")
    elsewhere: tuple[tuple[RulesWhy, Path | None], ...] = (
        ("gateway_dir", env.gateway_dir),
        ("home", env.hermes_home),
    )
    for why_here, directory in elsewhere:
        if directory is None or _same(directory, where) or looks_like_hermes_tree(directory):
            continue
        names = meant_for_hermes(directory)
        if names:
            return PlatformRules(session.platform, "none", started_at, names, why=why_here)
    return PlatformRules(session.platform, "no_files", started_at)


def meant_for_hermes(directory: Path) -> tuple[str, ...]:
    """The ``.hermes.md`` and ``AGENTS.md`` files with content in a directory the agent does not
    look in, one name per file on a case-insensitive disk."""
    seen: dict[str, str] = {}
    for name in MEANT_FOR_HERMES:
        found = _read(directory / name)
        if found is not None and found.status != "empty":
            seen.setdefault(name.casefold(), name)
    return tuple(seen.values())


def _undecodable(directory: Path, names: Iterable[str]) -> tuple[str, ...]:
    """Context files the engine skips in silence because they are not UTF-8."""
    seen: dict[str, str] = {}
    for name in names:
        found = _read(directory / name)
        if found is not None and found.status == "undecodable":
            seen.setdefault(name.casefold(), name)
    return tuple(seen.values())


# ------------------------------------------------------------------ discovery, as the engine does


def discover(where: Path) -> Expected | None:
    """The context files the engine would load from this directory now, first kind found
    (``build_context_files_prompt``); ``None`` when there is none."""
    try:
        cwd = where.resolve()
    except (OSError, RuntimeError):
        return None
    root = _git_root(cwd)
    finders: tuple[tuple[Kind, Callable[[Path, Path | None], list[Found]]], ...] = (
        ("hermes", _hermes),
        ("agents", _agents),
        ("claude", _claude),
        ("cursor", _cursor),
    )
    for kind, finder in finders:
        files = finder(cwd, root)
        if files:
            return Expected(kind, tuple(files))
    return None


def _git_root(cwd: Path) -> Path | None:
    for directory in (cwd, *cwd.parents):
        try:
            if (directory / ".git").exists():
                return directory
        except OSError:
            continue
    return None


def _loadable(found: Read | None) -> bool:
    return found is not None and found.status in ("ok", "big")


def _hermes(cwd: Path, root: Path | None) -> list[Found]:
    """The nearest ``.hermes.md`` / ``HERMES.md`` from the directory up to the git root (the
    directory only without one), frontmatter dropped; labelled by its name."""
    for directory in [cwd, *cwd.parents] if root else [cwd]:
        path = next((directory / n for n in HERMES_NAMES if _is_file(directory / n)), None)
        if path is not None:
            found = _read(path)
            if found is None or not _loadable(found):
                return []
            text = None if found.text is None else _strip_frontmatter(found.text)
            return [Found(path.name, text, found.changed)]
        if directory == root:
            return []
    return []


def _agents(cwd: Path, root: Path | None) -> list[Found]:
    """The ``AGENTS.md`` chain from the git root down to the directory, the first loadable of
    ``AGENTS.override.md`` / ``AGENTS.md`` / ``agents.md`` per directory, a repeated text once."""
    chain = [cwd]
    if root is not None and root != cwd and cwd.is_relative_to(root):
        parts = cwd.relative_to(root).parts
        chain = [root] + [root.joinpath(*parts[: i + 1]) for i in range(len(parts))]
    files: list[Found] = []
    seen: set[str] = set()
    for directory in chain:
        for name in AGENTS_NAMES:
            found = _read(directory / name)
            if found is None or not _loadable(found):
                continue
            if found.raw is None or found.raw not in seen:
                if found.raw is not None:
                    seen.add(found.raw)
                label = name if directory == cwd else os.path.relpath(directory / name, cwd)
                files.append(Found(label, found.text, found.changed))
            break
    return files


def _claude(cwd: Path, _root: Path | None) -> list[Found]:
    for name in CLAUDE_NAMES:
        found = _read(cwd / name)
        if found is not None and _loadable(found):
            return [Found(name, found.text, found.changed)]
    return []


def _cursor(cwd: Path, _root: Path | None) -> list[Found]:
    candidates = [(cwd / CURSOR_NAME, CURSOR_NAME)]
    rules = cwd / ".cursor" / "rules"
    try:
        if rules.is_dir():
            candidates += [(p, f".cursor/rules/{p.name}") for p in sorted(rules.glob("*.mdc"))]
    except OSError:
        pass
    files = []
    for path, label in candidates:
        found = _read(path)
        if found is not None and _loadable(found):
            files.append(Found(label, found.text, found.changed))
    return files


def _read(path: Path) -> Read | None:
    """A context file as the engine reads it (UTF-8, stripped) and the newest change to it, a
    link to it, or, on POSIX, its inode (a copy that kept an old time still changed it);
    ``None`` when there is no such regular file or it cannot be opened."""
    if not _is_file(path):
        return None
    try:
        stat, lstat = path.stat(), path.lstat()
        times = [stat.st_mtime, lstat.st_mtime]
        if os.name == "posix":
            times.append(stat.st_ctime)
        changed = max(times)
        if stat.st_size > MAX_FILE_BYTES:
            return Read(None, None, changed, "big")
        data = path.read_bytes()
    except (OSError, ValueError):
        return None
    try:
        raw = data.decode("utf-8").replace("\r\n", "\n").replace("\r", "\n").strip()
    except UnicodeDecodeError:
        return Read(None, None, changed, "undecodable" if data.strip() else "empty")
    if not raw:
        return Read(raw, raw, changed, "empty")
    return Read(raw, raw[1:] if raw.startswith("\ufeff") else raw, changed, "ok")


def _is_file(path: Path) -> bool:
    try:
        return path.is_file()
    except (OSError, ValueError):
        return False


def _strip_frontmatter(content: str) -> str:
    """``_strip_yaml_frontmatter`` of the engine, byte for byte."""
    content = content.lstrip("\ufeff")
    end = content.find("\n---", 3) if content.startswith("---") else -1
    return (content[end + 4 :].lstrip("\n") or content) if end != -1 else content


def looks_like_hermes_tree(directory: Path) -> bool:
    """The engine's own source tree (or a directory inside it): its contributor ``AGENTS.md`` is
    deliberately never loaded for a messaging platform, so a file there is no rule of ours."""
    try:
        for candidate in (directory, *directory.parents):
            if (candidate / "hermes_cli" / "main.py").is_file() and (candidate / "agent").is_dir():
                return True
    except (OSError, ValueError):
        return False
    return False


def _same(left: Path, right: Path | None) -> bool:
    if right is None:
        return False
    try:
        return left.resolve() == right.resolve()
    except (OSError, RuntimeError):
        return left == right


def _iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, UTC).isoformat()


# ------------------------------------------------------------------ events and words

# Short enough for "- <label>: <words>" to keep 32 columns with a label of up to 12; the
# details carry the /new.
_INCIDENT_WORDS: dict[str, str] = {
    "none": "rules not loaded",
    "outdated": "rules outdated",
    "truncated": "rules truncated",
    "blocked": "rules blocked",
}
PLATFORM_LABEL_LIMIT = 12


def incidents_for(judged: Iterable[PlatformRules]) -> tuple[Incident, ...]:
    """One event per platform whose agent does not see its rules as they are."""
    return tuple(
        Incident(
            f"rules:{rules.platform}:{rules.verdict}",
            "warning",
            f"{platform_label(rules.platform)}: {_INCIDENT_WORDS[rules.verdict]}",
        )
        for rules in judged
        if rules.verdict in WARNING_VERDICTS
    )


_PLATFORM_LABELS = {
    "telegram": "Telegram",
    "discord": "Discord",
    "slack": "Slack",
    "whatsapp": "WhatsApp",
    "whatsapp_cloud": "WhatsApp",
    "signal": "Signal",
    "matrix": "Matrix",
    "mattermost": "Mattermost",
    "email": "Email",
    "sms": "SMS",
    "api_server": "API",
    "homeassistant": "HomeAssist",
    "bluebubbles": "BlueBubbles",
    "msgraph_webhook": "MS Graph",
    "wecom_callback": "WeCom",
    "wecom": "WeCom",
    "weixin": "Weixin",
    "dingtalk": "DingTalk",
    "feishu": "Feishu",
}


def platform_label(platform: str) -> str:
    label = _PLATFORM_LABELS.get(platform) or platform[:1].upper() + platform[1:]
    return label[:PLATFORM_LABEL_LIMIT]
