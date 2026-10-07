"""Whether the agent sees its rules: the project context files in the system prompt of the
latest session of every platform the gateway runs.

The engine builds a session's system prompt on its first turn (again after a compression or a
model switch) and keeps it in ``HERMES_HOME/state.db``: ``sessions.system_prompt_hash`` into
``system_prompts.prompt``, or the older inline ``sessions.system_prompt``. The context files it
found are sections ``## <file>`` under ``# Project Context``; a file over the budget is cut in
the middle with a ``[...truncated <file>: kept H+T of N chars`` marker, a file the engine's
injection scan refused is a ``[BLOCKED: <file> ...`` notice. Where the engine looked is the
``Current working directory:`` line of the prompt's runtime block: ``sessions.cwd`` stays empty
for gateway sessions, and the engine itself checks a stored prompt against that line.

The verdict compares the saved prompt with the files on disk now. A section whose file still
has exactly that text is ``loaded``; a different text is ``outdated`` (the prompt is a snapshot,
``/new`` rebuilds it). No section while a file is in the agent's directory, the gateway's
working directory or ``HERMES_HOME`` is ``none``; no file in any of them is ``no_files``, never
an alarm. A platform that skips context files in the engine's ``config.yaml`` is ``off``.

The database is the engine's and the gateway writes it: ``mode=ro`` with ``PRAGMA query_only``,
one query per platform, in a worker under the tick's deadline. The prompt and the files are
compared in memory here and dropped; the snapshot carries file names as the prompt labels them,
lengths, times and verdicts, never their text and never a path.
"""

from __future__ import annotations

import importlib
import json
import logging
import os
import re
import sqlite3
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

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
# at 500,000 characters); the verdict then cannot tell loaded from outdated and says loaded.
MAX_FILE_BYTES = 4 * 1024 * 1024
# The names the engine looks for in a directory, in its order (``.cursor/rules/*.mdc`` aside).
CONTEXT_NAMES = (
    ".hermes.md",
    "HERMES.md",
    "AGENTS.override.md",
    "AGENTS.md",
    "agents.md",
    "CLAUDE.md",
    "claude.md",
    ".cursorrules",
)
WARNING_VERDICTS = frozenset({"truncated", "outdated", "blocked", "none"})

_BLOCK_HEADER = "# Project Context\n\nThe following project context files have been loaded"
_RUNTIME_HEADING = "# Hermes runtime environment"
_CWD_RE = re.compile(r"^Current working directory: (.+)$", re.MULTILINE)
_SECTION_RE = re.compile(r"^## (\S[^\n]*)$", re.MULTILINE)
_TRUNCATED_RE = re.compile(r"\[\.\.\.truncated ([^:\n]+): kept (\d+)\+(\d+) of (\d+) chars")
_CHAIN_WARN_NAME = "AGENTS.md (directory chain)"

RulesPart = tuple[RulesSummary, SourceObservation | None, tuple[Incident, ...]]


@dataclass(frozen=True, slots=True)
class Cut:
    """One truncation marker: the head and tail the engine kept of ``total`` characters."""

    head: int
    tail: int
    total: int


@dataclass(frozen=True, slots=True)
class Places:
    """Where an operator may keep the rules: the agent's directory (from the prompt, else the
    gateway's), the gateway process's working directory, ``HERMES_HOME``."""

    agent_dir: Path | None
    gateway_dir: Path | None
    home: Path


def read_rules(env: Environment, *, now: datetime) -> RulesPart:
    """The whole answer with its source; ``None`` as the source when the line is off."""
    if not env.context_files_enabled:
        return RulesSummary("off"), None, ()
    path = env.hermes_home / STATE_FILE
    if not path.is_file():
        return _not_here("state.db not found")
    platforms = gateway_platforms(env.hermes_home)
    try:
        conn = open_readonly(path)
        try:
            query = prompt_query(conn)
            if query is None:
                return _not_here("state.db has no saved system prompts")
            latest = {name: row for name in platforms if (row := _latest(conn, query, name))}
        finally:
            conn.close()
    except sqlite3.Error as exc:
        detail = f"state.db: {type(exc).__name__}"
        return RulesSummary("unknown", detail=detail), _source("unavailable", detail=detail), ()
    skipped = skipping_platforms(env.hermes_home)
    judged = tuple(
        judge(name, started, prompt, env, skipped=name in skipped)
        for name, (started, prompt) in latest.items()
    )
    source = _source("fresh", observed_at=now.isoformat())
    return RulesSummary("observed", judged), source, incidents_for(judged)


def _not_here(detail: str) -> RulesPart:
    return RulesSummary("unsupported", detail=detail), _source("unsupported", detail=detail), ()


def _source(
    state: SourceState, *, detail: str | None = None, observed_at: str | None = None
) -> SourceObservation:
    return SourceObservation(SOURCE_NAME, "official", state, observed_at, detail)


def prompt_query(conn: sqlite3.Connection) -> str | None:
    """The newest saved prompt of one source, on the schema this database has: the deduplicated
    ``system_prompts`` table (Hermes v2026.8.3 and later), else the inline column; ``None``
    when neither is there."""
    tables = {name for (name,) in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if "sessions" not in tables:
        return None
    columns = {row[1] for row in conn.execute("PRAGMA table_info(sessions)")}
    if "system_prompts" in tables and "system_prompt_hash" in columns:
        inline = "s.system_prompt" if "system_prompt" in columns else "NULL"
        prompt = f"COALESCE(sp.prompt, {inline})"
        join = " LEFT JOIN system_prompts sp ON sp.hash = s.system_prompt_hash"
    elif "system_prompt" in columns:
        prompt, join = "s.system_prompt", ""
    else:
        return None
    return (
        f"SELECT s.started_at, {prompt} FROM sessions s{join}"
        f" WHERE s.source = ? AND {prompt} IS NOT NULL ORDER BY s.started_at DESC LIMIT 1"
    )


def _latest(conn: sqlite3.Connection, query: str, platform: str) -> tuple[float, str] | None:
    row = conn.execute(query, (platform,)).fetchone()
    if row is None or not isinstance(row[1], str) or not isinstance(row[0], int | float):
        return None
    return float(row[0]), row[1]


def gateway_platforms(home: Path) -> tuple[str, ...]:
    """The platforms of this profile as the gateway records them (``gateway_state.json``, the
    keys of ``platforms``; ``<profile>:<platform>`` keys belong to another profile's database),
    Telegram first; the dashboard's own platform when the record cannot say."""
    try:
        payload = json.loads((home / GATEWAY_STATE_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeDecodeError):
        return DEFAULT_PLATFORMS
    platforms = payload.get("platforms") if isinstance(payload, dict) else None
    if not isinstance(platforms, dict):
        return DEFAULT_PLATFORMS
    names = sorted(
        key for key in platforms if isinstance(key, str) and key.strip() and ":" not in key
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


def judge(
    platform: str, started: float, prompt: str, env: Environment, *, skipped: bool
) -> PlatformRules:
    """The verdict on one platform's latest saved prompt."""
    places = Places(agent_dir(prompt) or env.gateway_dir, env.gateway_dir, env.hermes_home)
    started_at = _iso(started)
    labels = section_labels(prompt)
    if labels:
        return _judge_sections(platform, started_at, prompt, labels, places.agent_dir)
    if skipped:
        return PlatformRules(platform, "off", started_at)
    return _judge_absent(platform, started, started_at, places)


def agent_dir(prompt: str) -> Path | None:
    """The agent's working directory as the prompt's runtime block names it (the last block:
    the engine keeps it after all prose so a quoted example cannot shadow it)."""
    start = prompt.rfind(_RUNTIME_HEADING)
    # A runtime block without the line is a remote backend: no host directory to name.
    found = _CWD_RE.findall(prompt, max(start, 0))
    if not found:
        return None
    value = found[-1].strip()
    return Path(value) if value else None


def section_labels(prompt: str) -> tuple[str, ...]:
    """The context files the prompt carries, as labelled: the ``## <label>`` lines of the
    project-context block whose name is one the engine loads."""
    start = prompt.find(_BLOCK_HEADER)
    if start < 0:
        return ()
    end = prompt.rfind(_RUNTIME_HEADING)
    region = prompt[start : end if end > start else len(prompt)]
    labels = (match.group(1).strip() for match in _SECTION_RE.finditer(region))
    return tuple(dict.fromkeys(label for label in labels if _is_context_label(label)))


def _is_context_label(label: str) -> bool:
    name = label.replace("\\", "/").rsplit("/", 1)[-1]
    return name in CONTEXT_NAMES or (name.endswith(".mdc") and ".cursor/rules/" in label)


def _judge_sections(
    platform: str, started_at: str, prompt: str, labels: tuple[str, ...], where: Path | None
) -> PlatformRules:
    cuts = truncations(prompt)
    results = [_section(prompt, label, where, cuts) for label in labels]
    order: tuple[RulesVerdict, ...] = ("blocked", "outdated", "truncated", "loaded")
    verdict = next(v for v in order if any(r[0] == v for r in results))
    worst = [r for r in results if r[0] == verdict]
    chars = sum(r[1] or 0 for r in worst) or None
    kept = sum(r[2] or 0 for r in worst) or None
    changed = max((r[3] for r in worst if r[3]), default=None)
    return PlatformRules(
        platform, verdict, started_at, labels, chars=chars, kept=kept, changed_at=changed
    )


Section = tuple[RulesVerdict, int | None, int | None, str | None]


def _section(prompt: str, label: str, where: Path | None, cuts: dict[str, Cut]) -> Section:
    """One section against its file: verdict, characters, kept, when the file changed."""
    if f"[BLOCKED: {label} contained potential prompt injection" in prompt:
        return "blocked", None, None, None
    own, shared = _cuts_of(label, cuts)
    cut = own or shared
    path = file_for(label, where)
    if path is None:  # nothing on disk to compare with: the prompt's own word stands
        if cut:
            return "truncated", cut.total, cut.head + cut.tail, None
        return "loaded", None, None, None
    text = read_context(path, label)
    if text is None:  # gone or unreadable since: the prompt carries rules the disk does not
        return "outdated", None, None, changed_at(path)
    body = f"## {label}\n\n{text}"
    if body in prompt:
        return "loaded", len(body), None, None
    if own and body[: own.head] in prompt and body[len(body) - own.tail :] in prompt:
        return "truncated", own.total, own.head + own.tail, None
    # A cut over several files (the cursor rules, an AGENTS.md chain): this file's start is
    # what can be checked.
    if shared and body[: min(len(body), shared.head, 1000)] in prompt:
        return "truncated", shared.total, shared.head + shared.tail, None
    return "outdated", len(body), None, changed_at(path)


def _cuts_of(label: str, cuts: dict[str, Cut]) -> tuple[Cut | None, Cut | None]:
    """The marker of this file alone, and one over a group it belongs to."""
    name = warn_name(label)
    if name == ".cursorrules":
        return None, cuts.get(name)
    shared = cuts.get(_CHAIN_WARN_NAME) if name not in (".hermes.md", "CLAUDE.md") else None
    return cuts.get(name), shared


def truncations(prompt: str) -> dict[str, Cut]:
    """The truncation markers by the name the engine warns with."""
    return {
        match.group(1): Cut(int(match.group(2)), int(match.group(3)), int(match.group(4)))
        for match in _TRUNCATED_RE.finditer(prompt)
    }


def warn_name(label: str) -> str:
    """The name the engine's truncation marker uses for a section with this label."""
    name = label.replace("\\", "/").rsplit("/", 1)[-1]
    if name in (".hermes.md", "HERMES.md"):
        return ".hermes.md"
    if name in ("CLAUDE.md", "claude.md"):
        return "CLAUDE.md"
    if name == ".cursorrules" or name.endswith(".mdc"):
        return ".cursorrules"
    return label


def file_for(label: str, where: Path | None) -> Path | None:
    """The file behind a label: relative to the agent's directory (a chain label is a relative
    path), a ``.hermes.md`` from there up (the engine walks to the git root). Only names the
    engine loads are ever opened."""
    if where is None or not _is_context_label(label):
        return None
    candidate = Path(os.path.normpath(where / label))
    if candidate.name not in (".hermes.md", "HERMES.md") or candidate.is_file():
        return candidate
    for directory in where.parents:
        if (directory / candidate.name).is_file():
            return directory / candidate.name
    return candidate


def read_context(path: Path, label: str) -> str | None:
    """The text as the engine puts it in the prompt: UTF-8, stripped, a leading BOM dropped, a
    ``.hermes.md`` without its YAML frontmatter; ``None`` when missing, too big or unreadable."""
    try:
        if not path.is_file() or path.stat().st_size > MAX_FILE_BYTES:
            return None
        text = path.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeDecodeError):
        return None
    if warn_name(label) == ".hermes.md":
        text = _strip_frontmatter(text)
    return text[1:] if text.startswith("﻿") else text


def _strip_frontmatter(content: str) -> str:
    content = content.lstrip("﻿")
    end = content.find("\n---", 3) if content.startswith("---") else -1
    return (content[end + 4 :].lstrip("\n") or content) if end != -1 else content


def _judge_absent(platform: str, started: float, started_at: str, places: Places) -> PlatformRules:
    """No section in the prompt: is a file there for it, and where."""
    agent = places.agent_dir
    if agent is not None and not looks_like_hermes_tree(agent):
        names, changed = present(agent)
        if names:
            why: RulesWhy = "after" if changed is not None and changed > started else "not_loaded"
            changed_iso = _iso(changed) if changed is not None else None
            return PlatformRules(
                platform, "none", started_at, names, changed_at=changed_iso, why=why
            )
    elsewhere: tuple[tuple[RulesWhy, Path | None], ...] = (
        ("gateway_dir", places.gateway_dir),
        ("home", places.home),
    )
    for why_here, directory in elsewhere:
        if directory is None or _same(directory, agent) or looks_like_hermes_tree(directory):
            continue
        names, _changed = present(directory)
        if names:
            return PlatformRules(platform, "none", started_at, names, why=why_here)
    return PlatformRules(platform, "no_files", started_at)


def present(directory: Path) -> tuple[tuple[str, ...], float | None]:
    """The non-empty context files in a directory and the newest change among them."""
    paths = [directory / name for name in CONTEXT_NAMES]
    rules_dir = directory / ".cursor" / "rules"
    try:
        if rules_dir.is_dir():
            paths.extend(sorted(rules_dir.glob("*.mdc")))
    except OSError:
        pass
    found = [(p, _mtime(p)) for p in paths if _non_empty(p)]
    # A case-insensitive disk answers for ``agents.md`` too: one name per file, the first.
    seen: dict[str, str] = {}
    for path, _moment in found:
        name = path.relative_to(directory).as_posix()
        seen.setdefault(name.casefold(), name)
    names = tuple(seen.values())
    changes = [moment for _, moment in found if moment is not None]
    return names, max(changes, default=None)


def _non_empty(path: Path) -> bool:
    try:
        return path.is_file() and path.stat().st_size > 0
    except OSError:
        return False


def _mtime(path: Path) -> float | None:
    """The latest of the file's change and its link's: a link made after the session counts."""
    try:
        return max(path.stat().st_mtime, path.lstat().st_mtime)
    except OSError:
        return None


def changed_at(path: Path) -> str | None:
    moment = _mtime(path)
    return _iso(moment) if moment is not None else None


def looks_like_hermes_tree(directory: Path) -> bool:
    """The engine's own source tree (or a directory inside it): its contributor ``AGENTS.md`` is
    deliberately never loaded for a messaging platform, so a file there is no rule of ours."""
    try:
        for candidate in (directory, *directory.parents):
            if (candidate / "hermes_cli" / "main.py").is_file() and (candidate / "agent").is_dir():
                return True
    except OSError:
        return False
    return False


def _same(left: Path, right: Path | None) -> bool:
    if right is None:
        return False
    try:
        return left.resolve() == right.resolve()
    except OSError:
        return left == right


def _iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, UTC).isoformat()


_INCIDENT_WORDS: dict[str, str] = {
    "none": "agent rules not loaded",
    "outdated": "agent rules outdated, /new",
    "truncated": "agent rules truncated",
    "blocked": "agent rules blocked",
}


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
}


def platform_label(platform: str) -> str:
    return _PLATFORM_LABELS.get(platform, platform[:1].upper() + platform[1:20])
