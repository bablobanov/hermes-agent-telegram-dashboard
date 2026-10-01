from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

Severity = Literal["normal", "warning", "critical", "unknown"]
# ``unsupported``: this installation cannot answer the question at all (no file, no facade).
# It renders exactly like ``unavailable`` (reduced coverage), never as zero or green.
SourceState = Literal["fresh", "stale", "unavailable", "unsupported"]
Authority = Literal["official", "local", "derived", "unknown"]
# ``expired``: the provider answered that the login behind the number has ended; the line says
# so instead of a number, and the owner signs in again.
QuotaKind = Literal["official", "local", "unavailable", "unsupported", "expired"]
DriftState = Literal["clean", "drift", "unknown", "unsupported"]
BackupState = Literal["ok", "failed", "unknown", "unsupported"]
GatewayProcessState = Literal["running", "stopped", "unknown", "unsupported"]
PlatformState = Literal["connected", "degraded", "disconnected", "unknown", "unsupported"]
# The cron ticker and its jobs as the engine's own records say (``cron_jobs.py``): ``failing``
# is at least one job that did not deliver what it should, ``stalled`` a heartbeat older than
# the engine's own threshold, ``ticks_failing`` a ticker that beats but whose every tick ends in
# an error. ``unknown``: the records could not be read; ``unsupported``: no cron directory.
CronState = Literal["ok", "failing", "stalled", "ticks_failing", "unknown", "unsupported"]
CronFailureKind = Literal["run", "delivery", "blocked", "overdue"]
# Whether messages move through a connected Telegram adapter (``telegram_traffic.py``):
# ``no_sends`` is the adapter's own send gate closed, ``stalled`` a poll without progress,
# ``quiet`` no update for longer than this installation usually waits, ``reconnecting`` a
# fresh polling generation that has not received anything yet.
TrafficState = Literal[
    "ok", "no_sends", "stalled", "quiet", "reconnecting", "unknown", "unsupported"
]


@dataclass(frozen=True, slots=True)
class SourceObservation:
    name: str
    authority: Authority
    state: SourceState
    observed_at: str | None = None
    detail: str | None = None


@dataclass(frozen=True, slots=True)
class Coverage:
    expected_profiles: int = 0
    observed_profiles: int = 0
    failed_sources: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class Incident:
    incident_id: str
    severity: Severity
    title: str
    scope: str = "installation"


@dataclass(frozen=True, slots=True)
class WorkSummary:
    executing: int = 0
    queued: int = 0
    waiting_human: int = 0
    failed: int = 0
    unknown: int = 0


@dataclass(frozen=True, slots=True)
class CronFailure:
    """One job that is not delivering what it should, as the engine's own records say."""

    job_id: str
    name: str
    kind: CronFailureKind
    # The failed run's end (``last_run_at``), or the overdue ``next_run_at``.
    at: str | None = None
    # Runs in a row; ``None`` when the history cannot say.
    streak: int | None = None
    # The KIND of the error in a word (``cron_jobs.error_kind``), never its text.
    reason: str | None = None
    # A later ok run replaced it in ``jobs.json``; held in the details for a while.
    recovered_at: str | None = None


@dataclass(frozen=True, slots=True)
class CronSummary:
    state: CronState = "unknown"
    # Enabled and scheduled (or running).
    active: int | None = None
    paused: int | None = None
    # Current: on the line and in the events.
    failing: tuple[CronFailure, ...] = ()
    # Erased by a later run: details only.
    held: tuple[CronFailure, ...] = ()
    # The last heartbeat.
    ticker_at: str | None = None
    # The last tick without an error.
    ticker_ok_at: str | None = None
    # The kind of the ticker's last error in a word, never its text.
    ticker_error: str | None = None
    detail: str | None = None


@dataclass(frozen=True, slots=True)
class TrafficSummary:
    """Whether messages move through the connected Telegram adapter, from its own counters."""

    state: TrafficState = "unknown"
    # The tick that saw the update counter grow.
    last_update_seen_at: str | None = None
    # The last successful getUpdates round-trip, wall clock.
    polling_at: str | None = None
    sends_blocked_since: str | None = None
    # From the engine's error log.
    last_send_error_at: str | None = None
    send_errors_hour: int = 0
    quiet_seconds: float | None = None
    threshold_seconds: float | None = None
    # The longest gap between updates seen lately, what the threshold was made from.
    usual_gap_seconds: float | None = None
    detail: str | None = None


@dataclass(frozen=True, slots=True)
class QuotaWindow:
    label: str
    used_percent: float | None = None
    reset_at: str | None = None
    # The provider's state in words when it gives no number (Grok's week before the first
    # request); shown instead of the percent, never turned into one.
    note: str | None = None
    # The model the limit applies to (``Fable``); ``None`` is the account's own limit.
    scope: str | None = None
    # The provider's own verdict on the window: ``normal``, ``warning`` or ``critical``.
    severity: str | None = None


@dataclass(frozen=True, slots=True)
class Refusal:
    """The provider's last HTTP 429 as the engine's own log recorded it (``gemini_log.py``).

    ``at`` is ``None`` when the log was read and holds none. Every other field is optional: the
    provider's message names a ``limit``, a ``model`` and the seconds to retry only sometimes.
    ``daily`` is a retry longer than a per-minute window, so the quota is out until a reset;
    ``active_until`` is how long the refusal stays on the screen (the line carries the mark, a
    daily one is an event). Both are derived on every tick, never stored."""

    at: str | None = None
    limit: int | None = None
    retry_seconds: float | None = None
    model: str | None = None
    daily: bool = False
    active_until: str | None = None


@dataclass(frozen=True, slots=True)
class QuotaMetric:
    provider: str
    kind: QuotaKind
    used: int | None = None
    limit: int | None = None
    reset_at: str | None = None
    windows: tuple[QuotaWindow, ...] = ()
    detail: str | None = None
    # When the number was read from the provider; a line older than the screen says so itself.
    fetched_at: str | None = None
    # The plan as the provider names it (``Max 5x``, ``Prolite``, ``SuperGrok``); details only.
    plan: str | None = None
    # When the login behind the number ends; the owner signs in again before that.
    login_expires_at: str | None = None
    # The last 429 the engine logged for the provider; ``None`` when nobody watches its log.
    refusal: Refusal | None = None


@dataclass(frozen=True, slots=True)
class CapacitySummary:
    quotas: tuple[QuotaMetric, ...] = ()


@dataclass(frozen=True, slots=True)
class DriftSummary:
    state: DriftState = "unknown"
    changed_keys: int | None = None
    total_keys: int | None = None
    checked_at: str | None = None
    detail: str | None = None


@dataclass(frozen=True, slots=True)
class BackupSummary:
    """The last backup run as its status file describes it. ``ok``/``failed`` is the run's
    verdict; ``unknown`` is a status that could not be read (``detail`` says why);
    ``unsupported`` is an installation with no status source configured."""

    state: BackupState = "unknown"
    finished_at: str | None = None
    integrity: str | None = None
    phase: str | None = None
    reason: str | None = None
    detail: str | None = None


@dataclass(frozen=True, slots=True)
class GatewaySummary:
    process: GatewayProcessState = "unknown"
    telegram: PlatformState = "unknown"
    updated_at: str | None = None
    detail: str | None = None


@dataclass(frozen=True, slots=True)
class VersionSummary:
    """The Hermes version the running gateway serves against the latest upstream release.

    Information only: no status, incident or coverage is derived from it. ``running`` is
    ``None`` when this installation cannot say (``local_reason``); ``latest`` is ``None`` when
    the upstream check has no answer (``reason``). ``behind`` is the position of ours on
    upstream's release list relative to Latest: above zero behind, zero the same release, below
    zero newer; ``None`` when ours is not among the ``list_size`` releases read."""

    running: str | None = None
    latest: str | None = None
    running_published_at: str | None = None
    latest_published_at: str | None = None
    behind: int | None = None
    list_size: int | None = None
    checked_at: str | None = None
    reason: str | None = None
    local_reason: str | None = None


@dataclass(frozen=True, slots=True)
class DashboardSnapshot:
    overall: Severity
    observed_at: str
    coverage: Coverage = field(default_factory=Coverage)
    # ``None`` means the block is not observed on this installation (outside MVP or unsupported).
    work: WorkSummary | None = None
    cron: CronSummary | None = None
    traffic: TrafficSummary | None = None
    capacity: CapacitySummary = field(default_factory=CapacitySummary)
    incidents: tuple[Incident, ...] = ()
    drift: DriftSummary | None = None
    gateway: GatewaySummary | None = None
    backup: BackupSummary | None = None
    sources: tuple[SourceObservation, ...] = ()
    # Not a source: the line informs and never moves the status or the coverage.
    version: VersionSummary | None = None
