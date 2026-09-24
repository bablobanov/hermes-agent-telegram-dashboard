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
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from importlib import resources
from pathlib import Path
from typing import Any, Literal

Capability = Literal["supported", "unsupported", "unknown"]

MATRIX_SCHEMA = 1


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
        return "источник не описан в матрице"
    verified = entry.get("verified", {})
    if isinstance(verified, dict) and version in verified:
        return f"проверено на {version}: {verified[version]}"
    known = ", ".join(sorted(verified)) if isinstance(verified, dict) and verified else "нигде"
    return f"на {version} не проверено (проверено: {known})"
