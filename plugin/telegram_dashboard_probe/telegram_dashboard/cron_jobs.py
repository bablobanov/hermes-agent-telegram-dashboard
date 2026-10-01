"""Cron jobs and the scheduler's ticker, as the engine keeps them under ``HERMES_HOME/cron/``.

``jobs.json`` holds every job with the outcome of its LAST run: ``last_status`` (a closed set the
engine documents as ``ok``, ``error``, ``delivery_failed``, ``blocked_config``, plus
``delivery_queued``), ``last_error``, ``last_delivery_error`` and ``failure_streak`` (agent
failures in a row; the engine leaves delivery failures out of it). The ticker stamps its liveness
beside it: ``ticker_heartbeat`` on every loop, ``ticker_last_success`` on every loop that raised
nothing, ``ticker_last_error`` with the reason. The thresholds are the engine's own (``hermes cron
status``): a heartbeat older than three ticks plus slack is a dead ticker, a ``next_run_at`` more
than fifteen minutes in the past is a job that is not firing.

Read only. Never read: ``prompt``, ``script``, ``deliver`` (chat ids), ``skills``, the model.
The engine's error texts (``last_error``, ``last_delivery_error``, ``ticker_last_error``) never
reach the screen or the record: each is reduced to the KIND of the error in a word
(``error_kind``), in the order the engine's own health projection uses (decision of 01.10); the
shared sanitizer is the second line behind that, not the first. The layered order (ticker alive,
record consistent, run recorded, delivery confirmed) follows itpartypattaya/hermes-cron's
cron-doctor (MIT); no code is taken from it.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

from .backup import describe_age
from .compat import Environment
from .policy import sanitize_public_text
from .schema import (
    CronFailure,
    CronFailureKind,
    CronState,
    CronSummary,
    Incident,
    SourceObservation,
    SourceState,
)
from .timeparse import age_seconds, is_from_the_future, parse_timestamp

SOURCE_NAME = "cron"
CRON_DIR = "cron"
JOBS_FILE = "jobs.json"
HEARTBEAT_FILE = "ticker_heartbeat"
SUCCESS_FILE = "ticker_last_success"
ERROR_FILE = "ticker_last_error"
# The engine's own: TICKER_INTERVAL_SECONDS * 3 + 20 (hermes_cli/cron.py, 0.21.3 and main).
TICKER_STALE_SECONDS = 200.0
# The engine's own grace before "next_run_at is overdue, the job is not firing".
OVERDUE_SECONDS = 15 * 60.0
# Statuses that are not a failed run: delivery outcomes are judged from their own field.
RUN_OK_STATUSES = frozenset({"ok", "delivery_failed", "delivery_queued"})
# A failure the engine erased with a later run stays in the details this many ticks.
HOLD_TICKS = 2
HELD_LIMIT = 50
MAX_EVENTS = 3
NAME_LIMIT = 24
# Eleven characters and an ellipsis: ``- Cron undelivered: dashboard-p…`` is 32 columns. A name
# with any character beyond ASCII is cut at eight (decision of 01.10, from Ilya's phone): Cyrillic
# glyphs are wider in a proportional font, and an event of the same count with a Cyrillic name
# wrapped where the Latin one fits.
EVENT_NAME_LIMIT = 12
EVENT_NAME_LIMIT_BEYOND_ASCII = 9
EVENT_TITLES: dict[CronFailureKind, str] = {
    "run": "Cron run failed",
    "delivery": "Cron undelivered",
    "blocked": "Cron blocked",
    "overdue": "Cron overdue",
}
# The kind of an engine error from its text, first match wins. The first four follow the order of
# the engine's own projection (agent/monitoring/cron_health.py:38-50 at v2026.9.14: auth before
# rate limit before timeout before network), then the Telegram delivery answers, then the two
# ticker failures ``hermes cron status`` names (Permission denied, fd exhaustion) and main's
# stale-code yield. The text itself goes nowhere.
_ERROR_KINDS: tuple[tuple[re.Pattern[str], str], ...] = (
    (
        re.compile(
            r"\b(?:authentication|authenticated|authorization|authorized|unauthorized|forbidden"
            r"|bearer|401|403)\b|\b(?:access|api|refresh) (?:token|key)\b",
            re.IGNORECASE,
        ),
        "auth",
    ),
    (re.compile(r"rate.?limit|\b429\b|\bquota\b", re.IGNORECASE), "rate limit"),
    (re.compile(r"timeout|timed out", re.IGNORECASE), "timeout"),
    (re.compile(r"not connected|send_path_degraded", re.IGNORECASE), "not connected"),
    (
        re.compile(
            r"chat not found|bot was (?:blocked|kicked)|user is deactivated|chat_id is empty",
            re.IGNORECASE,
        ),
        "chat unavailable",
    ),
    (re.compile(r"network|\bconnection\b|dns|socket|unreachable", re.IGNORECASE), "network"),
    (re.compile(r"permission ?(?:denied|error)|errno 13", re.IGNORECASE), "permission denied"),
    (re.compile(r"too many open files|emfile", re.IGNORECASE), "fd exhaustion"),
    (re.compile(r"stale code|\byield\b", re.IGNORECASE), "stale code"),
)
_LITERAL = re.compile(r"[a-z][a-z0-9_]{0,23}")

CronPart = tuple[CronSummary, SourceObservation, tuple[Incident, ...]]


class RunRecord(Protocol):
    """One failed run as the history reads it (``cron_runs.Run``)."""

    @property
    def job_id(self) -> str: ...

    @property
    def undelivered(self) -> bool: ...

    @property
    def claimed_at(self) -> str | None: ...

    @property
    def finished_at(self) -> str | None: ...


class RunHistory(Protocol):
    """What ``executions.db`` adds (``cron_runs.Runs``): the failures since the last check and
    the delivery streaks of the jobs the scan named. Structural, so the two modules never import
    each other at run time."""

    @property
    def recent_failures(self) -> Sequence[RunRecord]: ...

    @property
    def delivery_streaks(self) -> Mapping[str, int]: ...


@dataclass(frozen=True, slots=True)
class Job:
    job_id: str
    name: str
    enabled: bool
    state: str
    # The engine's pause stamp: with ``state`` one of its two pause markers (``enabled`` gates).
    paused_at: str | None
    next_run_at: str | None
    last_run_at: str | None
    last_status: str | None
    last_error: str | None
    delivery_error: str | None
    failure_streak: int | None

    @property
    def paused(self) -> bool:
        """As the ticker reads it (``cron/jobs.py`` ``_has_pause_marker`` and
        ``is_job_runnable``, v2026.9.14 and main): off, in the ``paused`` state, or stamped
        ``paused_at``. The engine does not fire an enabled record with the stamp alone (a
        half-pause, from a hand edit) and self-disables it on its next loop; until then
        ``hermes cron list`` shows it active, since ``effective_job_state`` trusts ``enabled``.
        The plugin follows what fires (0.9.1)."""
        return not self.enabled or self.state == "paused" or self.paused_at is not None

    @property
    def active(self) -> bool:
        """Not paused and scheduled or running, or enabled and parked by the engine in its
        terminal ``error`` state whatever its stamps (until the ticker rewrites such a record on
        its next loop): a job that should be running counts, so the line never reads ``1 of 0``;
        it is counted once, among the active, never among the paused as well."""
        if self.state == "error":
            return self.enabled
        return not self.paused and self.state in ("scheduled", "running")


@dataclass(frozen=True, slots=True)
class Ticker:
    heartbeat_at: str | None = None
    success_at: str | None = None
    # The kind of the last error in a word, never the engine's text.
    error: str | None = None
    detail: str | None = None


@dataclass(frozen=True, slots=True)
class Scan:
    """What one read of the files says, before the history and the record are consulted."""

    jobs: tuple[Job, ...] = ()
    ticker: Ticker = Ticker()
    state: CronState = "ok"
    failures: tuple[CronFailure, ...] = ()
    detail: str | None = None


# ----------------------------------------------------------------------------- the files


def read_jobs(path: Path) -> list[Job]:
    """The jobs of ``jobs.json``; ``OSError``/``ValueError`` are the caller's to name."""
    payload = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(payload, dict) or not isinstance(payload.get("jobs"), list):
        raise ValueError("jobs.json is not an object")
    return [job for job in (job_of(raw) for raw in payload["jobs"]) if job is not None]


def job_of(raw: object) -> Job | None:
    if not isinstance(raw, Mapping) or not isinstance(raw.get("id"), str) or not raw["id"]:
        return None
    streak = raw.get("failure_streak")
    counted = isinstance(streak, int) and not isinstance(streak, bool) and streak >= 0
    return Job(
        job_id=raw["id"],
        name=_name(raw.get("name") or raw["id"]),
        enabled=raw.get("enabled", True) is not False,
        state=(_text(raw.get("state")) or "scheduled").lower(),
        paused_at=_text(raw.get("paused_at")),
        next_run_at=_text(raw.get("next_run_at")),
        last_run_at=_text(raw.get("last_run_at")),
        last_status=_text(raw.get("last_status")),
        last_error=_text(raw.get("last_error")),
        delivery_error=_text(raw.get("last_delivery_error")),
        failure_streak=streak if counted else None,
    )


def _text(value: object) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


def _name(value: object) -> str:
    """The job's name for the screen: the shared sanitizer (secrets, paths, chat ids and
    delivery targets), cut to ``NAME_LIMIT``."""
    return sanitize_public_text(str(value), limit=NAME_LIMIT) or "?"


def error_kind(text: object, *, default: str = "error") -> str:
    """The kind of an engine error in a word, from its text; the text itself goes nowhere."""
    if not isinstance(text, str) or not text.strip():
        return default
    for pattern, word in _ERROR_KINDS:
        if pattern.search(text):
            return word
    return default


def _literal(status: str) -> str:
    """A ``last_status`` literal the plugin does not know, as the word it is; anything that is
    not a plain literal reads ``error``."""
    return status if _LITERAL.fullmatch(status) else "error"


def read_ticker(cron_dir: Path) -> Ticker:
    heartbeat, problem_hb = _stamp(cron_dir / HEARTBEAT_FILE)
    success, problem_ok = _stamp(cron_dir / SUCCESS_FILE)
    error = _error_kind_of_file(cron_dir / ERROR_FILE)
    problems = [p for p in (problem_hb, problem_ok) if p]
    return Ticker(heartbeat, success, error, "; ".join(problems) or None)


def _stamp(path: Path) -> tuple[str | None, str | None]:
    """The epoch in the file's first token as an ISO moment; ``(None, reason)`` when unreadable,
    ``(None, None)`` when the file is not there (the ticker never ran, or another provider)."""
    try:
        tokens = path.read_text(encoding="utf-8-sig").split()
    except FileNotFoundError:
        return None, None
    except (OSError, UnicodeDecodeError) as exc:
        return None, f"{path.name} unreadable: {type(exc).__name__}"
    try:
        return datetime.fromtimestamp(float(tokens[0]), UTC).isoformat(), None
    except (IndexError, ValueError, OverflowError, OSError):
        return None, f"{path.name} is not a stamp"


def _error_kind_of_file(path: Path) -> str | None:
    """The kind of the ticker's last error; ``None`` without the file or without a message."""
    try:
        lines = path.read_text(encoding="utf-8-sig").splitlines()
    except (OSError, UnicodeDecodeError):
        return None
    message = "\n".join(lines[1:]).strip()
    return error_kind(message) if message else None


# ----------------------------------------------------------------------------- one job


def classify(job: Job, *, now: datetime, ticker_alive: bool) -> CronFailure | None:
    """What is wrong with ``job`` by the engine's own rules, or nothing. A paused job has no
    failures: its last run is history. A new status literal is named as it is and is never green."""
    if job.paused and job.state != "error":
        return None
    status = (job.last_status or "").lower()
    if job.state == "error" or (status and status not in RUN_OK_STATUSES):
        kind: CronFailureKind = "blocked" if status == "blocked_config" else "run"
        # The kind of the error from its text; a literal the plugin does not know is named as
        # the word it is (never green), a blocked run without a text reads ``config``.
        if kind == "blocked":
            fallback = "config"
        else:
            fallback = _literal(status) if status not in ("", "error") else "error"
        reason = error_kind(job.last_error, default=fallback)
        return CronFailure(
            job.job_id, job.name, kind, at=job.last_run_at, streak=job.failure_streak, reason=reason
        )
    if status == "delivery_failed" or job.delivery_error:
        reason = error_kind(job.delivery_error, default="delivery error")
        return CronFailure(
            job.job_id, job.name, "delivery", at=job.last_run_at, streak=None, reason=reason
        )
    if ticker_alive and job.active:
        due = parse_timestamp(job.next_run_at)
        late = age_seconds(due, now) if due is not None else None
        if late is not None and late > OVERDUE_SECONDS:
            return CronFailure(job.job_id, job.name, "overdue", at=job.next_run_at)
    return None


# ----------------------------------------------------------------------------- the scan


def scan_cron(env: Environment, *, now: datetime) -> Scan:
    cron_dir = env.hermes_home / CRON_DIR
    if not cron_dir.is_dir():
        return Scan(state="unsupported", detail="no cron directory")
    try:
        jobs = tuple(read_jobs(cron_dir / JOBS_FILE))
    except FileNotFoundError:
        return Scan(state="unsupported", detail="jobs.json not found")
    except ValueError as exc:
        named = str(exc).startswith("jobs.json")
        detail = str(exc) if named else f"jobs.json unreadable: {type(exc).__name__}"
        return Scan(state="unknown", detail=detail)
    except OSError as exc:
        return Scan(state="unknown", detail=f"jobs.json unreadable: {type(exc).__name__}")
    ticker = read_ticker(cron_dir)
    heartbeat_age = _age(ticker.heartbeat_at, now)
    success_age = _age(ticker.success_at, now)
    if heartbeat_age is not None and heartbeat_age > TICKER_STALE_SECONDS:
        return Scan(jobs, ticker, "stalled")
    if heartbeat_age is not None and success_age is not None and success_age > TICKER_STALE_SECONDS:
        return Scan(jobs, ticker, "ticks_failing")
    alive = heartbeat_age is not None
    verdicts = (classify(job, now=now, ticker_alive=alive) for job in jobs)
    failures = tuple(failure for failure in verdicts if failure is not None)
    return Scan(jobs, ticker, "failing" if failures else "ok", failures, ticker.detail)


def _age(stamp: str | None, now: datetime) -> float | None:
    moment = parse_timestamp(stamp)
    age = age_seconds(moment, now) if moment is not None else None
    return None if age is None or is_from_the_future(age) else age


# ----------------------------------------------------------------------------- the record


def hold(
    cache: dict[str, Any],
    current: Sequence[CronFailure],
    jobs: Mapping[str, Job],
    *,
    now: datetime,
) -> tuple[CronFailure, ...]:
    """Failures the engine no longer shows, kept ``HOLD_TICKS`` ticks with the run that replaced
    them. The record is JSON in the plugin's state: every entry is checked, none trusted."""
    raw = cache.get("held")
    held: dict[str, Any] = raw if isinstance(raw, dict) else {}
    cache["held"] = held
    present = {failure.job_id for failure in current}
    for failure in current:
        entry = held.get(failure.job_id)
        same = (
            isinstance(entry, dict)
            and entry.get("at") == failure.at
            and entry.get("kind") == failure.kind
        )
        if not same:
            held.pop(failure.job_id, None)  # re-recorded: the newest position
            held[failure.job_id] = {
                "kind": failure.kind,
                "at": failure.at,
                "name": failure.name,
                "reason": failure.reason,
                "streak": failure.streak,
                "shown": 0,
            }
    kept: list[CronFailure] = []
    for job_id, entry in list(held.items()):
        if not isinstance(entry, dict) or entry.get("kind") not in EVENT_TITLES:
            del held[job_id]
            continue
        raw_shown = entry.get("shown")
        shown = raw_shown if isinstance(raw_shown, int) and not isinstance(raw_shown, bool) else 0
        if job_id in present:
            entry["shown"] = 0  # still current: the hold starts when the engine erases it
            continue
        if shown >= HOLD_TICKS:
            del held[job_id]
            continue
        entry["shown"] = shown + 1
        job = jobs.get(job_id)
        streak = entry.get("streak")
        counted = isinstance(streak, int) and not isinstance(streak, bool)
        kept.append(
            CronFailure(
                job_id,
                str(entry.get("name") or job_id),
                entry["kind"],
                at=_text(entry.get("at")),
                streak=streak if counted else None,
                reason=_text(entry.get("reason")),
                recovered_at=(job.last_run_at if job is not None else None) or now.isoformat(),
            )
        )
    for oldest in list(held)[: max(0, len(held) - HELD_LIMIT)]:
        del held[oldest]
    return tuple(kept[-HELD_LIMIT:])


# ----------------------------------------------------------------------------- the part


def assemble_cron(
    scan: Scan,
    cache: dict[str, Any] | None,
    *,
    now: datetime,
    runs: RunHistory | None = None,
) -> CronPart:
    """The block, the source and the events from a scan, the history (``cron_runs.Runs`` or
    ``None``) and the record. The history fills the delivery streaks and adds failures the engine
    erased between two ticks; the record holds them."""
    store = cache if cache is not None else {}
    store["checked_at"] = now.isoformat()
    if scan.state in ("unsupported", "unknown"):
        summary = CronSummary(scan.state, detail=scan.detail)
        source_state: SourceState = "unsupported" if scan.state == "unsupported" else "unavailable"
        source = SourceObservation(SOURCE_NAME, "official", source_state, detail=scan.detail)
        incidents: tuple[Incident, ...] = ()
        if scan.state == "unknown":
            incidents = (Incident("cron:unreadable", "warning", f"Cron: {scan.detail}"),)
        return summary, source, incidents
    jobs = {job.job_id: job for job in scan.jobs}
    failures = _with_history(scan.failures, jobs, runs, now=now)
    held = hold(store, failures, jobs, now=now)
    current = tuple(f for f in failures if f.recovered_at is None)
    summary = CronSummary(
        scan.state,
        active=sum(job.active for job in scan.jobs),
        paused=sum(job.paused and not job.active and job.state != "completed" for job in scan.jobs),
        failing=current,
        held=(*(f for f in failures if f.recovered_at is not None), *held),
        ticker_at=scan.ticker.heartbeat_at,
        ticker_ok_at=scan.ticker.success_at,
        ticker_error=scan.ticker.error,
        detail=scan.detail,
    )
    source = SourceObservation(
        SOURCE_NAME, "official", "fresh", observed_at=scan.ticker.heartbeat_at or now.isoformat()
    )
    return summary, source, incidents_for(summary, now=now)


def _with_history(
    failures: Sequence[CronFailure],
    jobs: Mapping[str, Job],
    runs: RunHistory | None,
    *,
    now: datetime,
) -> tuple[CronFailure, ...]:
    """Delivery streaks from the history; failures the history saw since the last check whose job
    now reads ok are failures too, already recovered."""
    if runs is None:
        return tuple(failures)
    out = [
        replace(f, streak=runs.delivery_streaks.get(f.job_id)) if f.kind == "delivery" else f
        for f in failures
    ]
    seen = {f.job_id for f in out}
    for run in runs.recent_failures:
        job = jobs.get(run.job_id)
        if job is None or run.job_id in seen or job.paused:
            continue
        seen.add(run.job_id)
        kind: CronFailureKind = "delivery" if run.undelivered else "run"
        out.append(
            CronFailure(
                run.job_id,
                job.name,
                kind,
                at=run.finished_at or run.claimed_at,
                recovered_at=job.last_run_at or now.isoformat(),
            )
        )
    return tuple(out)


def _event_name(name: str) -> str:
    """The job's name for an event line: whole up to ``EVENT_NAME_LIMIT``, else cut with an
    ellipsis; ``EVENT_NAME_LIMIT_BEYOND_ASCII`` when any character of it is beyond ASCII, the
    ellipsis of the sanitizer's own cut at ``NAME_LIMIT`` aside."""
    beyond = not name.replace("…", "").isascii()
    limit = EVENT_NAME_LIMIT_BEYOND_ASCII if beyond else EVENT_NAME_LIMIT
    if len(name) <= limit:
        return name
    return name[: limit - 1].rstrip() + "…"


def incidents_for(summary: CronSummary, *, now: datetime) -> tuple[Incident, ...]:
    if summary.state in ("stalled", "ticks_failing"):
        stamp = summary.ticker_at if summary.state == "stalled" else summary.ticker_ok_at
        age = _age(stamp, now)
        what = "ticker silent" if summary.state == "stalled" else "ticks failing"
        title = f"Cron {what}"
        if age is not None:
            title += f" {describe_age(age).removesuffix(' ago')}"
        return (Incident("cron:ticker", "critical", title),)
    events: list[Incident] = []
    for failure in summary.failing[:MAX_EVENTS]:
        name = _event_name(sanitize_public_text(failure.name, limit=NAME_LIMIT))
        title = f"{EVENT_TITLES[failure.kind]}: {name}"
        events.append(Incident(f"cron:{failure.kind}:{failure.job_id}", "warning", title))
    return tuple(events)


def collect_cron(
    env: Environment,
    cache: dict[str, Any] | None,
    *,
    now: datetime,
    runs: RunHistory | None = None,
) -> CronPart:
    """The synchronous whole: the cron path calls this; the tick scans first, reads the history
    in a worker with the ids the scan named, then assembles (``collect._cron_guarded``)."""
    return assemble_cron(scan_cron(env, now=now), cache, now=now, runs=runs)
