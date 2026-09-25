"""The last ``state.db`` backup: age and verdict from the status file the backup timer writes.

The source is a JSON status the timer's root step writes on every run, success or failure
(``ok``, ``phase``, ``reason``, ``integrity``, ``finished_epoch``, ``finished_at``, ...). The
dashboard reads it; it never opens the database or the copy. The line is shown always, not only
when something broke: a screen silent about the norm makes silence indistinguishable from
confirmation. A missing or unreadable status is a reason on the line and an event, never a zero.

The staleness threshold is the watchdog's: a daily timer plus slack (26 h).
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

from .compat import Environment
from .policy import sanitize_public_text
from .schema import BackupSummary, Incident, SourceObservation, SourceState
from .timeparse import age_seconds, is_from_the_future, parse_timestamp

SOURCE_NAME = "backup"
STALE_SECONDS = 26 * 3600
STALE_WORDS = f"older than {STALE_SECONDS // 3600} h"

BackupPart = tuple[BackupSummary, SourceObservation, tuple[Incident, ...]]


def describe_age(seconds: float) -> str:
    """An age in words the reader does not have to compute."""
    if seconds < 60:
        return "less than a minute ago"
    if seconds < 3600:
        return f"{int(seconds // 60)} min ago"
    if seconds < 48 * 3600:
        return f"{int(seconds // 3600)} h ago"
    return f"{int(seconds // 86400)} d ago"


def finished_at_of(payload: dict[str, object]) -> str | None:
    """When the run finished: ``finished_epoch`` first (what the watchdog reads), else
    ``finished_at``; ``None`` when neither is readable."""
    epoch = payload.get("finished_epoch")
    if isinstance(epoch, int | float) and not isinstance(epoch, bool):
        try:
            return datetime.fromtimestamp(float(epoch), UTC).isoformat()
        except (OverflowError, OSError, ValueError):
            pass
    moment = parse_timestamp(payload.get("finished_at"))
    return moment.isoformat() if moment is not None else None


def summarize(payload: object, *, now: datetime) -> BackupPart:
    """The status as a block, a source observation and the events it warrants."""
    if not isinstance(payload, dict):
        return _no_data("status is not an object", incident=True)
    finished = finished_at_of(payload)
    if finished is None:
        return _no_data("status has no time", incident=False)
    moment = parse_timestamp(finished)
    age = age_seconds(moment, now) if moment is not None else None
    if age is None or is_from_the_future(age):
        return _no_data("status dated in the future", incident=False)

    ok = payload.get("ok") is True
    integrity = payload.get("integrity")
    phase = sanitize_public_text(str(payload.get("phase") or ""), limit=24) or None
    reason = sanitize_public_text(str(payload.get("reason") or ""), limit=100) or None
    incidents: list[Incident] = []
    if ok:
        summary = BackupSummary(
            "ok",
            finished_at=finished,
            integrity=sanitize_public_text(str(integrity), limit=24) if integrity else None,
        )
    else:
        detail = ": ".join(part for part in (phase, reason) if part) or "reason not recorded"
        summary = BackupSummary(
            "failed", finished_at=finished, phase=phase, reason=reason, detail=detail
        )
        incidents.append(Incident("backup:failed", "warning", f"Backup failed: {detail}"))
    state: SourceState = "stale" if age > STALE_SECONDS else "fresh"
    if state == "stale":
        incidents.append(
            Incident(
                "backup:stale",
                "warning",
                f"Backup {STALE_WORDS}: last run {describe_age(age)}",
            )
        )
    source = SourceObservation(SOURCE_NAME, "derived", state, observed_at=finished)
    return summary, source, tuple(incidents)


def collect_backup(env: Environment, *, now: datetime) -> BackupPart:
    """Read the configured status file; every failure to read is named, none raises."""
    path = env.backup_status
    if path is None:
        summary = BackupSummary("unsupported", detail="backup source not configured")
        source = SourceObservation(SOURCE_NAME, "derived", "unsupported", detail=summary.detail)
        return summary, source, ()
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return _no_data("status file missing", incident=True)
    except (OSError, ValueError, UnicodeDecodeError) as exc:
        return _no_data(f"status unreadable: {type(exc).__name__}", incident=True)
    return summarize(payload, now=now)


def _no_data(detail: str, *, incident: bool) -> BackupPart:
    summary = BackupSummary("unknown", detail=detail)
    source = SourceObservation(SOURCE_NAME, "derived", "unavailable", detail=detail)
    incidents = (Incident("backup:unreadable", "warning", f"Backup: {detail}"),) if incident else ()
    return summary, source, incidents
