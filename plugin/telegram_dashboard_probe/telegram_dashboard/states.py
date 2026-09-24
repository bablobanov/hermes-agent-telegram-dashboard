"""Static verification states from section 11 of the hypotheses research, plus two of our own
for the pinned message itself. No real Hermes is touched: every state is a snapshot literal.

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
)

NOW = datetime(2026, 9, 9, 21, 0, tzinfo=UTC)
PERIOD_SECONDS = 300
_T = "2026-09-09T21:00:00+00:00"
_T_MINUS_2M = "2026-09-09T20:58:00+00:00"
_T_MINUS_20M = "2026-09-09T20:40:00+00:00"
_DRIFT_08 = "2026-09-09T08:00:00+00:00"


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
                    QuotaWindow("5 ч", 37.0, "2026-09-10T00:00:00+00:00"),
                    QuotaWindow("7 дн", 12.0, "2026-09-14T00:00:00+00:00"),
                ),
                detail="official",
            ),
            QuotaMetric(
                "Codex",
                "official",
                windows=(QuotaWindow("5 ч", 61.0, "2026-09-09T23:30:00+00:00"),),
                detail="official",
            ),
            QuotaMetric("Gemini", "unsupported", detail="источник не подтверждён"),
            QuotaMetric("Grok", "unsupported", detail="источник не подтверждён"),
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
    )


def all_states() -> tuple[State, ...]:
    return (
        State(1, "Всё нормально", _snapshot("normal"), _delivery_ok(), "normal"),
        State(
            2,
            "Gateway жив, но Telegram polling не работает",
            _snapshot(
                "critical",
                incidents=(
                    Incident(
                        "telegram:polling",
                        "critical",
                        "Gateway работает, но Telegram не подключён (conflict)",
                    ),
                ),
                gateway=GatewaySummary("running", "degraded", _T_MINUS_2M, detail="conflict"),
            ),
            _delivery_ok(),
            "critical",
        ),
        State(
            3,
            "Job выполнен, delivery failed",
            _snapshot(
                "critical",
                incidents=(
                    Incident("cron:delivery", "critical", "Не доставлен результат cron-задачи"),
                ),
            ),
            _delivery_ok(),
            "critical",
        ),
        State(
            4,
            "Одна сессия ждёт согласования",
            _snapshot(
                "warning",
                incidents=(
                    Incident(
                        "session:approval", "warning", "Одна сессия ждёт согласования человека"
                    ),
                ),
            ),
            _delivery_ok(),
            "warning",
        ),
        State(
            5,
            "Одна сессия выше 80% контекста",
            _snapshot(
                "warning",
                incidents=(Incident("context:risk", "warning", "Одна сессия выше 80% контекста"),),
            ),
            _delivery_ok(),
            "warning",
        ),
        State(
            6,
            "Session override плюс фактический fallback",
            _snapshot(
                "warning",
                incidents=(
                    Incident(
                        "routing:fallback",
                        "warning",
                        "Сработал fallback модели при override сессии",
                    ),
                ),
            ),
            _delivery_ok(),
            "warning",
        ),
        State(
            7,
            "Memory provider недоступен, включился built-in",
            _snapshot(
                "warning",
                incidents=(
                    Incident(
                        "memory:provider",
                        "warning",
                        "Внешний memory provider недоступен, работает встроенный",
                    ),
                ),
            ),
            _delivery_ok(),
            "warning",
        ),
        State(
            8,
            "На копии state.db обнаружено повреждение",
            _snapshot(
                "critical",
                incidents=(
                    Incident("state:integrity", "critical", "Повреждение в копии state.db"),
                ),
            ),
            _delivery_ok(),
            "critical",
        ),
        State(
            9,
            "Config отличается от утверждённого baseline",
            _snapshot(
                "warning",
                incidents=(Incident("config:drift", "warning", "Дрейф конфига: 3 из 474 ключей"),),
                drift=DriftSummary("drift", 3, 474, _DRIFT_08),
            ),
            _delivery_ok(),
            "warning",
        ),
        State(
            10,
            "Один источник давно не опрашивался",
            _snapshot(
                "unknown",
                sources=_sources(limits="stale"),
            ),
            _delivery_ok(),
            "unknown",
        ),
        State(
            11,
            "Наше: сообщение не подтверждалось дольше двух периодов",
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
            "Наше: закреплённое сообщение пропало",
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
    )
