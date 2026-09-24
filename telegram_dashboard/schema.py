from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

Severity = Literal["normal", "warning", "critical", "unknown"]
# ``unsupported``: this installation cannot answer the question at all (no file, no facade).
# It renders exactly like ``unavailable`` (reduced coverage), never as zero or green.
SourceState = Literal["fresh", "stale", "unavailable", "unsupported"]
Authority = Literal["official", "local", "derived", "unknown"]
QuotaKind = Literal["official", "local", "unavailable", "unsupported"]
DriftState = Literal["clean", "drift", "unknown", "unsupported"]
BackupState = Literal["ok", "failed", "unknown", "unsupported"]
GatewayProcessState = Literal["running", "stopped", "unknown", "unsupported"]
PlatformState = Literal["connected", "degraded", "disconnected", "unknown", "unsupported"]


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
class AutomationSummary:
    scheduler: Literal["healthy", "degraded", "unknown"] = "unknown"
    failed_runs: int = 0
    missed_runs: int = 0
    delivery_failed: int = 0


@dataclass(frozen=True, slots=True)
class QuotaWindow:
    label: str
    used_percent: float | None = None
    reset_at: str | None = None
    # The provider's state in words when it gives no number (Grok's week before the first
    # request); shown instead of the percent, never turned into one.
    note: str | None = None


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
class DashboardSnapshot:
    overall: Severity
    observed_at: str
    coverage: Coverage = field(default_factory=Coverage)
    # ``None`` means the block is not observed on this installation (outside MVP or unsupported).
    work: WorkSummary | None = None
    automation: AutomationSummary | None = None
    capacity: CapacitySummary = field(default_factory=CapacitySummary)
    incidents: tuple[Incident, ...] = ()
    drift: DriftSummary | None = None
    gateway: GatewaySummary | None = None
    backup: BackupSummary | None = None
    sources: tuple[SourceObservation, ...] = ()
