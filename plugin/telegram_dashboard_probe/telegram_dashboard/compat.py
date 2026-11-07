"""Capability probing and the compatibility matrix.

The dashboard never branches on a Hermes version number. Our own production carries engine
patches, so ``hermes --version`` does not describe what the installation can answer. Each source
is *probed*: does the file exist, does the facade import, does the command run. A probe that fails
makes the source ``unsupported``, which renders as reduced coverage.

The matrix is data (``compat_matrix.json``), not code: per source, how it is probed and on which
versions it was verified and how. A new Hermes release does not break the dashboard; it lowers
coverage and names what is missing until someone adds a row.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from importlib import resources
from pathlib import Path
from typing import Any, Literal

Capability = Literal["supported", "unsupported", "unknown"]

MATRIX_SCHEMA = 1
# The setting and the ``Environment`` field of the owner's cron labels (1.0.0). The plugin
# requires the name (``REQUIRED_API``): a copy without the field would reject the tick's
# ``Environment`` arguments every time.
CRON_NOTES = "cron_notes"
# The setting and the ``Environment`` field of the owner's list of built-in limit providers
# (issue #14). The plugin requires the name (``REQUIRED_API``), as for ``CRON_NOTES``.
LIMITS_PROVIDERS = "limits_providers"
# The ``Environment`` field that says the tick runs outside any profile scope on a gateway that
# multiplexes profiles (1.1.0, the launch profile's dashboard). The plugin requires the name
# (``REQUIRED_API``), as for ``CRON_NOTES``.
FACADE_UNSCOPED = "facade_unscoped"
# The providers the list may name: the facade's two and Gemini from the engine's log. An external
# source (``limits_sources``) is no built-in provider and is never subject to the list.
BUILT_IN_PROVIDERS = ("claude", "codex", "gemini")


def parse_cron_notes(
    raw: object, log: logging.Logger, prefix: str = ""
) -> tuple[tuple[str, str], ...]:
    """``cron_notes``, job id to label, as an immutable tuple of pairs: a mapping, an entry whose
    id or label is not a non-empty string skipped (YAML reads an unquoted all-digit id as a
    number). One warning per parse says how many were skipped, never which: a key or a value
    may carry text from the config. Anything but a mapping is "none configured", with its own
    warning. The plugin and the package's own tick both read the setting with this."""
    if not isinstance(raw, Mapping):
        if raw not in (None, ""):
            log.warning("%scron_notes must be a mapping; ignoring %r", prefix, type(raw))
        return ()
    pairs = tuple(
        (job_id.strip(), label.strip())
        for job_id, label in raw.items()
        if isinstance(job_id, str) and isinstance(label, str)
        if job_id.strip() and label.strip()
    )
    skipped = len(raw) - len(pairs)
    if skipped:
        log.warning(
            "%scron_notes: %d %s skipped (id and label must be non-empty strings; "
            "quote numeric ids)",
            prefix,
            skipped,
            "entry" if skipped == 1 else "entries",
        )
    return pairs


def parse_limits_providers(
    raw: object, log: logging.Logger, prefix: str = ""
) -> tuple[str, ...] | None:
    """``limits_providers``: the built-in limit providers to ask, by label in any case, in the
    order of ``BUILT_IN_PROVIDERS``. ``None`` when the setting is absent: every provider is
    asked, and one with no sign of an account has no line (issue #14). A list that names at
    least one: exactly the providers it names are asked, each always with its line, and one it
    leaves out is never asked. An empty list, written as such, asks none. An entry that names no
    built-in provider is skipped, one warning per parse giving the places, never the text; a
    list made only of such entries is "not set", not "ask none", so one typo cannot turn off
    every provider (B2 review M-b). Anything but a list is "not set", with its own warning. The
    plugin and the package's own tick both read the setting with this."""
    if raw is None or raw == "":
        return None
    if not isinstance(raw, list | tuple):
        log.warning("%slimits_providers must be a list; ignoring %r", prefix, type(raw))
        return None
    named: set[str] = set()
    skipped: list[str] = []
    for number, entry in enumerate(raw, start=1):
        name = entry.strip().lower() if isinstance(entry, str) else ""
        if name in BUILT_IN_PROVIDERS:
            named.add(name)
        else:
            skipped.append(str(number))
    if skipped:
        log.warning(
            "%slimits_providers: %s %s skipped (each must be claude, codex or gemini)%s",
            prefix,
            "entry" if len(skipped) == 1 else "entries",
            ", ".join(skipped),
            "" if named else "; none left, so the setting is ignored",
        )
    if skipped and not named:
        return None
    return tuple(name for name in BUILT_IN_PROVIDERS if name in named)


@dataclass(frozen=True, slots=True)
class ProbeResult:
    source: str
    status: Capability
    detail: str = ""


@dataclass(frozen=True, slots=True)
class Environment:
    hermes_home: Path
    drift_command: tuple[str, ...] | None = None
    drift_report: Path | None = None
    limits_enabled: bool = True
    # The status file the backup timer writes; ``None`` means the line is not observed here.
    backup_status: Path | None = None
    # ``limits_sources`` from the config as given; ``external.read_sources`` checks each entry.
    limits_sources: tuple[Any, ...] = ()
    # The rules line (``context_files.py``); off by the dashboard's own setting only.
    context_files_enabled: bool = True
    # The memory section (``memory.py``); off by the dashboard's own setting only.
    memory_enabled: bool = True
    # The owner's label per cron job id (``cron_notes``) as pairs, nothing mutable; checked
    # entry by entry where the config is read, sanitized where a label is shown (``cron_jobs``).
    cron_notes: tuple[tuple[str, str], ...] = ()
    # The owner's ``limits_providers`` as ``parse_limits_providers`` reads it: ``None`` asks every
    # built-in provider and hides one with no sign of an account; a tuple (empty or not) asks
    # exactly those it names and always shows them (issue #14).
    limits_providers: tuple[str, ...] | None = None
    # The tick runs outside any profile scope on a gateway that multiplexes profiles (the plugin
    # path, the launch profile's dashboard): Hermes' secret lookup refuses there, the usage
    # facade answers empty whatever the account has, and its silence says nothing about this
    # installation, so it never makes a provider not configured (``limits._judged``).
    facade_unscoped: bool = False
    # The gateway process's working directory, where an operator may expect the agent's rules;
    # ``None`` outside the gateway unless the cron tick's config names it.
    gateway_dir: Path | None = None
    # The engine's ``TERMINAL_CWD`` as the gateway process holds it (the plugin path only):
    # where a sandbox backend's discovery runs when the prompt names no host directory.
    terminal_cwd: Path | None = None
    # When the plugin came up in the gateway process (the plugin path only): the engine says
    # some things once per process, and they stand until the next start (``memory.py``).
    process_started_at: datetime | None = None


def probe_gateway_state(env: Environment) -> ProbeResult:
    path = env.hermes_home / "gateway_state.json"
    if not path.is_file():
        return ProbeResult("gateway_state", "unsupported", "gateway_state.json not found")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeDecodeError) as exc:
        return ProbeResult("gateway_state", "unknown", f"unreadable: {type(exc).__name__}")
    if not isinstance(payload, dict) or "updated_at" not in payload:
        return ProbeResult("gateway_state", "unsupported", "no updated_at in gateway_state.json")
    if not isinstance(payload.get("platforms"), dict):
        return ProbeResult("gateway_state", "unsupported", "no platforms map in gateway_state.json")
    return ProbeResult("gateway_state", "supported")


def probe_drift(env: Environment) -> ProbeResult:
    if env.drift_report is not None:
        if env.drift_report.is_file():
            return ProbeResult("drift", "supported", "report file")
        return ProbeResult("drift", "unsupported", "drift report file not found")
    if env.drift_command:
        head = Path(env.drift_command[0])
        if head.is_absolute() and not head.exists():
            return ProbeResult("drift", "unsupported", "drift command executable not found")
        return ProbeResult("drift", "supported", "command")
    return ProbeResult("drift", "unsupported", "drift source not configured")


def probe_backup(env: Environment) -> ProbeResult:
    if env.backup_status is None:
        return ProbeResult("backup", "unsupported", "backup status not configured")
    if not env.backup_status.is_file():
        return ProbeResult("backup", "unsupported", "backup status file not found")
    return ProbeResult("backup", "supported", "status file")


def probe_limits(env: Environment, importable: Callable[[], ProbeResult]) -> ProbeResult:
    if not env.limits_enabled:
        return ProbeResult("limits", "unsupported", "disabled in config")
    return importable()


def load_matrix(path: Path | None = None) -> dict[str, Any]:
    if path is None:
        text = resources.files(__package__).joinpath("compat_matrix.json").read_text("utf-8")
    else:
        text = path.read_text(encoding="utf-8")
    payload = json.loads(text)
    if not isinstance(payload, dict) or payload.get("schema") != MATRIX_SCHEMA:
        raise ValueError("compat matrix: unsupported schema")
    sources = payload.get("sources")
    if not isinstance(sources, dict):
        raise ValueError("compat matrix: sources must be a map")
    return payload


def verification_for(matrix: Mapping[str, Any], source: str, version: str) -> str:
    """Human-readable verification status of ``source`` on ``version``; honest when unknown."""
    entry = matrix.get("sources", {}).get(source)
    if not isinstance(entry, dict):
        return "source not described in the matrix"
    verified = entry.get("verified", {})
    if isinstance(verified, dict) and version in verified:
        return f"verified on {version}: {verified[version]}"
    known = ", ".join(sorted(verified)) if isinstance(verified, dict) and verified else "nowhere"
    return f"not verified on {version} (verified: {known})"
