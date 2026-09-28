"""Static verification states from section 11 of the hypotheses research, plus two of our own
for the pinned message itself, one for the Hermes version line and three showcase states for
the catalog screenshots. No real Hermes is touched: every state is a snapshot literal.

Used by tests (``tests/test_states.py``) and by ``python -m telegram_dashboard --demo N`` so the
same text can be looked at in Telegram during the pilot.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from .freshness import DeliveryRecord
from .schema import (
    BackupSummary,
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
    now: datetime = NOW  # the moment the state is rendered at; the showcase has its own day


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
    gateway: SourceState = "fresh",
    limits: SourceState = "fresh",
    drift: SourceState = "fresh",
    *,
    at: str = _T_MINUS_2M,
    drift_at: str = _DRIFT_08,
) -> tuple[SourceObservation, ...]:
    return (
        SourceObservation("gateway_state", "official", gateway, observed_at=at),
        SourceObservation("limits", "official", limits, observed_at=at),
        SourceObservation("drift", "derived", drift, observed_at=drift_at),
    )


def _delivery_ok(at: str = _T_MINUS_2M) -> DeliveryRecord:
    return DeliveryRecord(message_id=4242, last_confirmed_at=at, last_attempt_at=at)


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


# The showcase states (14-16): one healthy installation with all six sources on 2026-09-26, the
# catalog screenshots: every line the screen can show, then the same screen with a drift incident,
# then the same screen under the stale banner. The release dates are upstream's real ones (0.21.3
# is v2026.9.14, 0.21.5 is v2026.9.24, 0.21.4 between them); every other number is made up. The
# verification states above keep their golden texts untouched.
SHOWCASE_NOW = datetime(2026, 9, 26, 21, 0, tzinfo=UTC)
_S = "2026-09-26T21:00:00+00:00"
_S_MINUS_2M = "2026-09-26T20:58:00+00:00"
_S_MINUS_20M = "2026-09-26T20:40:00+00:00"
_S_DRIFT_08 = "2026-09-26T08:00:00+00:00"
_S_BACKUP = "2026-09-26T11:00:00+00:00"
_V0_21_3 = "2026-09-14T16:04:14Z"
_V0_21_5 = "2026-09-24T10:09:38Z"


def _showcase_limits() -> CapacitySummary:
    return CapacitySummary(
        (
            QuotaMetric(
                "Claude",
                "official",
                windows=(
                    QuotaWindow("5h", 42.0, "2026-09-26T23:10:00+00:00"),
                    QuotaWindow("7d", 67.0, "2026-09-29T21:00:00+00:00"),
                ),
                detail="official",
            ),
            QuotaMetric(
                "Codex",
                "official",
                windows=(
                    QuotaWindow("5h", 93.0, "2026-09-26T22:20:00+00:00"),
                    QuotaWindow("7d", 58.0, "2026-10-01T21:00:00+00:00"),
                ),
                detail="official",
            ),
            QuotaMetric("Gemini", "unsupported", detail="source not confirmed"),
            QuotaMetric(
                "Grok",
                "official",
                windows=(QuotaWindow("7d", 31.0, "2026-10-02T21:00:00+00:00"),),
            ),
            QuotaMetric(
                "Kimi",
                "official",
                windows=(
                    QuotaWindow("5h", 8.0, "2026-09-27T00:40:00+00:00"),
                    QuotaWindow("month", 46.0, "2026-10-17T21:00:00+00:00"),
                ),
            ),
        )
    )


def _showcase_snapshot(
    overall: Severity, *, incidents: tuple[Incident, ...] = (), drift: DriftSummary | None = None
) -> DashboardSnapshot:
    return DashboardSnapshot(
        overall=overall,
        observed_at=_S,
        coverage=Coverage(expected_profiles=1, observed_profiles=1),
        capacity=_showcase_limits(),
        incidents=incidents,
        drift=drift or DriftSummary("clean", 0, 481, _S_DRIFT_08),
        gateway=GatewaySummary("running", "connected", _S_MINUS_2M),
        backup=BackupSummary("ok", _S_BACKUP, integrity="ok"),
        sources=(
            *_sources(at=_S_MINUS_2M, drift_at=_S_DRIFT_08),
            SourceObservation("backup", "official", "fresh", observed_at=_S_BACKUP),
            SourceObservation("grok_quota", "official", "fresh", observed_at=_S_MINUS_2M),
            SourceObservation("kimi_quota", "official", "fresh", observed_at=_S_MINUS_2M),
        ),
        version=VersionSummary(
            running="0.21.3",
            latest="0.21.5",
            running_published_at=_V0_21_3,
            latest_published_at=_V0_21_5,
            behind=2,
            list_size=36,
            checked_at=_S,
        ),
    )


def _showcase_states() -> tuple[State, ...]:
    return (
        State(
            14,
            "Showcase: every line of a healthy screen",
            _showcase_snapshot("normal"),
            _delivery_ok(_S_MINUS_2M),
            "normal",
            now=SHOWCASE_NOW,
        ),
        State(
            15,
            "Showcase: the healthy screen with config drift",
            _showcase_snapshot(
                "warning",
                incidents=(Incident("config:drift", "warning", "Config drift: 3 of 481 keys"),),
                drift=DriftSummary("drift", 3, 481, _S_DRIFT_08),
            ),
            _delivery_ok(_S_MINUS_2M),
            "warning",
            now=SHOWCASE_NOW,
        ),
        State(
            16,
            "Showcase: the healthy screen under the stale banner",
            _showcase_snapshot("normal"),
            DeliveryRecord(
                message_id=4242,
                last_confirmed_at=_S_MINUS_20M,
                last_attempt_at=_S_MINUS_2M,
                last_error="transport",
            ),
            "normal",
            now=SHOWCASE_NOW,
        ),
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
        *_showcase_states(),
    )
