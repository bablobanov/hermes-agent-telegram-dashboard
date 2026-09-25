"""Static verification states from section 11 of the hypotheses research, plus two of our own
for the pinned message itself and one for the Hermes version line. No real Hermes is touched:
every state is a snapshot literal.

Used by tests (``tests/test_states.py``) and by ``python -m telegram_dashboard --demo N`` so the
same text can be looked at in Telegram during the pilot.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from .freshness import DeliveryRecord
from .schema import (
    CapacitySummary,
    Coverage,
    DashboardSnapshot,
    DriftSummary,
    GatewaySummary,
    Incident,
    QuotaMetric,
    QuotaWindow,
    Severity,
    SourceObservation,
    SourceState,
    VersionSummary,
)

NOW = datetime(2026, 9, 9, 21, 0, tzinfo=UTC)
PERIOD_SECONDS = 300
_T = "2026-09-09T21:00:00+00:00"
_T_MINUS_2M = "2026-09-09T20:58:00+00:00"
_T_MINUS_20M = "2026-09-09T20:40:00+00:00"
_DRIFT_08 = "2026-09-09T08:00:00+00:00"
# Upstream's releases as of NOW (the real ones): 0.21.1 is Latest, 0.20.5 three releases below.
_V0_21_1 = "2026-09-07T22:17:01Z"
_V0_20_5 = "2026-08-21T12:16:39Z"
_RELEASES_KNOWN = 32


@dataclass(frozen=True, slots=True)
class State:
    number: int
    title: str
    snapshot: DashboardSnapshot
    delivery: DeliveryRecord
    expect_overall: str


def _limits_ok() -> CapacitySummary:
    return CapacitySummary(
        (
            QuotaMetric(
                "Claude",
                "official",
                windows=(
                    QuotaWindow("5h", 37.0, "2026-09-10T00:00:00+00:00"),
                    QuotaWindow("7d", 12.0, "2026-09-14T00:00:00+00:00"),
                ),
                detail="official",
            ),
            QuotaMetric(
                "Codex",
                "official",
                windows=(QuotaWindow("5h", 61.0, "2026-09-09T23:30:00+00:00"),),
                detail="official",
            ),
            QuotaMetric("Gemini", "unsupported", detail="source not confirmed"),
            QuotaMetric("Grok", "unsupported", detail="source not confirmed"),
        )
    )


def _sources(
    gateway: SourceState = "fresh", limits: SourceState = "fresh", drift: SourceState = "fresh"
) -> tuple[SourceObservation, ...]:
    return (
        SourceObservation("gateway_state", "official", gateway, observed_at=_T_MINUS_2M),
        SourceObservation("limits", "official", limits, observed_at=_T_MINUS_2M),
        SourceObservation("drift", "derived", drift, observed_at=_DRIFT_08),
    )


def _delivery_ok() -> DeliveryRecord:
    return DeliveryRecord(
        message_id=4242, last_confirmed_at=_T_MINUS_2M, last_attempt_at=_T_MINUS_2M
    )


def _snapshot(
    overall: Severity,
    *,
    incidents: tuple[Incident, ...] = (),
    sources: tuple[SourceObservation, ...] | None = None,
    drift: DriftSummary | None = None,
    gateway: GatewaySummary | None = None,
    capacity: CapacitySummary | None = None,
    version: VersionSummary | None = None,
) -> DashboardSnapshot:
    return DashboardSnapshot(
        overall=overall,
        observed_at=_T,
        coverage=Coverage(expected_profiles=1, observed_profiles=1),
        capacity=capacity or _limits_ok(),
        incidents=tuple(incidents),
        drift=drift or DriftSummary("clean", 0, 474, _DRIFT_08),
        gateway=gateway or GatewaySummary("running", "connected", _T_MINUS_2M),
        sources=sources or _sources(),
        version=version,
    )


def _version(running: str, published: str, behind: int) -> VersionSummary:
    return VersionSummary(
        running=running,
        latest="0.21.1",
        running_published_at=published,
        latest_published_at=_V0_21_1,
        behind=behind,
        list_size=_RELEASES_KNOWN,
        checked_at=_T,
    )


def all_states() -> tuple[State, ...]:
    return (
        State(
            1,
            "All normal",
            _snapshot("normal", version=_version("0.21.1", _V0_21_1, 0)),
            _delivery_ok(),
            "normal",
        ),
        State(
            2,
            "Gateway alive, Telegram polling down",
            _snapshot(
                "critical",
                incidents=(
                    Incident(
                        "telegram:polling",
                        "critical",
                        "Gateway running, but Telegram disconnected (conflict)",
                    ),
                ),
                gateway=GatewaySummary("running", "degraded", _T_MINUS_2M, detail="conflict"),
            ),
            _delivery_ok(),
            "critical",
        ),
        State(
            3,
            "Job done, delivery failed",
            _snapshot(
                "critical",
                incidents=(Incident("cron:delivery", "critical", "cron job result not delivered"),),
            ),
            _delivery_ok(),
            "critical",
        ),
        State(
            4,
            "One session waits for approval",
            _snapshot(
                "warning",
                incidents=(
                    Incident("session:approval", "warning", "One session waits for human approval"),
                ),
            ),
            _delivery_ok(),
            "warning",
        ),
        State(
            5,
            "One session above 80% context",
            _snapshot(
                "warning",
                incidents=(Incident("context:risk", "warning", "One session above 80% context"),),
            ),
            _delivery_ok(),
            "warning",
        ),
        State(
            6,
            "Session override plus an actual fallback",
            _snapshot(
                "warning",
                incidents=(
                    Incident(
                        "routing:fallback",
                        "warning",
                        "Model fallback fired under a session override",
                    ),
                ),
            ),
            _delivery_ok(),
            "warning",
        ),
        State(
            7,
            "Memory provider down, built-in took over",
            _snapshot(
                "warning",
                incidents=(
                    Incident(
                        "memory:provider",
                        "warning",
                        "External memory provider unavailable, the built-in one is working",
                    ),
                ),
            ),
            _delivery_ok(),
            "warning",
        ),
        State(
            8,
            "Corruption found in the state.db copy",
            _snapshot(
                "critical",
                incidents=(
                    Incident("state:integrity", "critical", "Corruption in the state.db copy"),
                ),
            ),
            _delivery_ok(),
            "critical",
        ),
        State(
            9,
            "Config differs from the approved baseline",
            _snapshot(
                "warning",
                incidents=(Incident("config:drift", "warning", "Config drift: 3 of 474 keys"),),
                drift=DriftSummary("drift", 3, 474, _DRIFT_08),
            ),
            _delivery_ok(),
            "warning",
        ),
        State(
            10,
            "One source not polled for a long time",
            _snapshot(
                "unknown",
                sources=_sources(limits="stale"),
            ),
            _delivery_ok(),
            "unknown",
        ),
        State(
            11,
            "Ours: message unconfirmed for over two periods",
            _snapshot("normal"),
            DeliveryRecord(
                message_id=4242,
                last_confirmed_at=_T_MINUS_20M,
                last_attempt_at=_T_MINUS_2M,
                last_error="transport",
            ),
            "normal",
        ),
        State(
            12,
            "Ours: the pinned message is lost",
            _snapshot("normal"),
            DeliveryRecord(
                message_id=4242,
                last_confirmed_at=_T_MINUS_20M,
                last_attempt_at=_T,
                last_error="lost",
                lost_at=_T,
            ),
            "normal",
        ),
        State(
            13,
            "Hermes three releases behind",
            _snapshot("normal", version=_version("0.20.5", _V0_20_5, 3)),
            _delivery_ok(),
            "normal",
        ),
    )
