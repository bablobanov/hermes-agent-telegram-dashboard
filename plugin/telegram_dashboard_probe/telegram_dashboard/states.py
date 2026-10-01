"""Static verification states from section 11 of the hypotheses research, plus two of our own
for the pinned message itself, one for the Hermes version line, three showcase states for the
catalog screenshots, two for an external limits source, two for Gemini's 429 from the
engine's log, and three for the cron line and the deaf channel. No real Hermes is touched:
every state is a snapshot literal.

Used by tests (``tests/test_states.py``) and by ``python -m telegram_dashboard --demo N`` so the
same text can be looked at in Telegram during the pilot.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import UTC, datetime

from .freshness import DeliveryRecord
from .schema import (
    BackupSummary,
    CapacitySummary,
    Coverage,
    CronFailure,
    CronSummary,
    DashboardSnapshot,
    DriftSummary,
    GatewaySummary,
    Incident,
    QuotaMetric,
    QuotaWindow,
    Refusal,
    Severity,
    SourceObservation,
    SourceState,
    TrafficSummary,
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
    cron: CronSummary | None = None,
    traffic: TrafficSummary | None = None,
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
        cron=cron,
        traffic=traffic,
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


# The showcase states (14-16): one healthy installation with all seven sources on 2026-09-26, the
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
_S_LAST_UPDATE = "2026-09-26T20:30:00+00:00"
_V0_21_3 = "2026-09-14T16:04:14Z"
_V0_21_5 = "2026-09-24T10:09:38Z"
# The Gemini line as the engine's log makes it (``gemini_log.py``): no number without billing.
_GEMINI_REASON = "Google reports Gemini quota only with billing enabled"


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
            QuotaMetric("Gemini", "unsupported", detail=_GEMINI_REASON, refusal=Refusal()),
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
    overall: Severity,
    *,
    incidents: tuple[Incident, ...] = (),
    drift: DriftSummary | None = None,
    capacity: CapacitySummary | None = None,
    extra_sources: tuple[SourceObservation, ...] = (),
    cron: CronSummary | None = None,
    traffic: TrafficSummary | None = None,
) -> DashboardSnapshot:
    return DashboardSnapshot(
        overall=overall,
        observed_at=_S,
        coverage=Coverage(expected_profiles=1, observed_profiles=1),
        capacity=capacity or _showcase_limits(),
        incidents=incidents,
        drift=drift or DriftSummary("clean", 0, 481, _S_DRIFT_08),
        gateway=GatewaySummary("running", "connected", _S_MINUS_2M),
        backup=BackupSummary("ok", _S_BACKUP, integrity="ok"),
        sources=(
            *_sources(at=_S_MINUS_2M, drift_at=_S_DRIFT_08),
            SourceObservation("backup", "official", "fresh", observed_at=_S_BACKUP),
            SourceObservation("grok_quota", "official", "fresh", observed_at=_S_MINUS_2M),
            SourceObservation("kimi_quota", "official", "fresh", observed_at=_S_MINUS_2M),
            SourceObservation("gemini_log", "local", "fresh", observed_at=_S),
            SourceObservation("cron", "official", "fresh", observed_at=_S_MINUS_2M),
            SourceObservation("cron_runs", "official", "fresh", observed_at=_S),
            SourceObservation("telegram_traffic", "local", "fresh", observed_at=_S),
            *extra_sources,
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
        cron=cron
        or CronSummary("ok", active=27, paused=3, ticker_at=_S_MINUS_2M, ticker_ok_at=_S_MINUS_2M),
        traffic=traffic
        or TrafficSummary("ok", last_update_seen_at=_S_LAST_UPDATE, polling_at=_S_MINUS_2M),
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


# The external source states (17-18): the showcase installation with Claude answered by a local
# process in contract 1 (README, "External limit sources"), as the tick builds it from the answer:
# the account's windows, a model's own limits, the plan and the login date; Codex and Grok with
# the plans their sources name. The login dates are counted from SHOWCASE_NOW.
_EXTERNAL_SOURCE = SourceObservation("Claude limits", "official", "fresh", observed_at=_S)
_LOGIN_ENDING = "2026-09-28T19:00:00+00:00"  # 46 hours after SHOWCASE_NOW
_LOGIN_ENDED = "2026-09-26T19:00:00+00:00"  # two hours before it
_S_WEEK_RESET = "2026-09-29T21:00:00+00:00"


def _external_limits(claude: QuotaMetric) -> CapacitySummary:
    plans = {"Codex": "Prolite", "Grok": "SuperGrok"}
    quotas = []
    for quota in _showcase_limits().quotas:
        if quota.provider == "Claude":
            quotas.append(claude)
        else:
            quotas.append(replace(quota, plan=plans.get(quota.provider)))
    return CapacitySummary(tuple(quotas))


def _external_claude() -> QuotaMetric:
    return QuotaMetric(
        "Claude",
        "official",
        windows=(
            QuotaWindow("session", 42.0, "2026-09-26T23:10:00+00:00", severity="normal"),
            QuotaWindow("week", 67.0, _S_WEEK_RESET, severity="normal"),
            QuotaWindow("week", 100.0, _S_WEEK_RESET, scope="Fable", severity="critical"),
            QuotaWindow("week", 20.0, _S_WEEK_RESET, scope="Sonnet", severity="normal"),
        ),
        fetched_at=_S,
        plan="Max 5x",
        login_expires_at=_LOGIN_ENDING,
    )


def _external_states() -> tuple[State, ...]:
    expired = QuotaMetric(
        "Claude", "expired", fetched_at=_S, plan="Max 5x", login_expires_at=_LOGIN_ENDED
    )
    return (
        State(
            17,
            "External source: a model limit, plans and an ending login",
            _showcase_snapshot(
                "warning",
                incidents=(
                    Incident("claude:login_expiring", "warning", "Claude login expires in 2 days"),
                ),
                capacity=_external_limits(_external_claude()),
                extra_sources=(_EXTERNAL_SOURCE,),
            ),
            _delivery_ok(_S_MINUS_2M),
            "warning",
            now=SHOWCASE_NOW,
        ),
        State(
            18,
            "External source: the login expired",
            _showcase_snapshot(
                "warning",
                incidents=(Incident("claude:login_expired", "warning", "Claude login expired"),),
                capacity=_external_limits(expired),
                extra_sources=(_EXTERNAL_SOURCE,),
            ),
            _delivery_ok(_S_MINUS_2M),
            "warning",
            now=SHOWCASE_NOW,
        ),
    )


# The Gemini states (19-20): the showcase installation after Google refused the engine's call.
# A per-minute 429 is the mark on the Gemini line for an hour, the status untouched; a daily one
# (a retry longer than a per-minute window) is an event until the reset Google named (decision of
# 29.09). The refusals are what ``gemini_log.activate`` builds from such a log entry, the event
# what ``incidents_for`` says; the test holds both to it.
_TTS = "gemini-2.5-flash-preview-tts"


def _gemini_limits(refusal: Refusal) -> CapacitySummary:
    return CapacitySummary(
        tuple(
            replace(quota, refusal=refusal) if quota.provider == "Gemini" else quota
            for quota in _showcase_limits().quotas
        )
    )


def _gemini_states() -> tuple[State, ...]:
    minute = Refusal(
        at=_S_MINUS_20M,
        limit=3,
        retry_seconds=41.53,
        model=_TTS,
        active_until="2026-09-26T21:40:00+00:00",
    )
    day = Refusal(
        at="2026-09-26T18:57:00+00:00",
        limit=15,
        retry_seconds=14580.0,
        model=_TTS,
        daily=True,
        active_until="2026-09-26T23:00:00+00:00",
    )
    return (
        State(
            19,
            "Gemini minute quota hit",
            _showcase_snapshot("normal", capacity=_gemini_limits(minute)),
            _delivery_ok(_S_MINUS_2M),
            "normal",
            now=SHOWCASE_NOW,
        ),
        State(
            20,
            "Gemini day quota hit",
            _showcase_snapshot(
                "warning",
                incidents=(Incident("gemini:day_quota", "warning", "Gemini daily limit used up"),),
                capacity=_gemini_limits(day),
            ),
            _delivery_ok(_S_MINUS_2M),
            "warning",
            now=SHOWCASE_NOW,
        ),
    )


# The cron and traffic states (21-23): the showcase installation when a run fails three times in
# a row (a warning, the job on the Cron line and in the events), when the cron ticker goes silent
# (critical: nothing scheduled runs), and when the adapter is connected but its own counters say
# the channel is deaf (critical: the screen itself cannot reach anyone). The events are literals;
# ``test_states`` holds them to what ``cron_jobs.incidents_for`` and ``telegram_traffic``
# build for the same records.
_S_TICKER_SILENT = "2026-09-26T20:48:00+00:00"


def _cron_states() -> tuple[State, ...]:
    failed_run = CronFailure(
        "b1",
        "daily-digest",
        "run",
        at="2026-09-26T20:05:00+00:00",
        streak=3,
        reason="rate limit",
    )
    return (
        State(
            21,
            "Cron: a run failed three times in a row",
            _showcase_snapshot(
                "warning",
                incidents=(Incident("cron:run:b1", "warning", "Cron run failed: daily-digest"),),
                cron=CronSummary(
                    "failing",
                    active=27,
                    paused=3,
                    failing=(failed_run,),
                    ticker_at=_S_MINUS_2M,
                    ticker_ok_at=_S_MINUS_2M,
                ),
            ),
            _delivery_ok(_S_MINUS_2M),
            "warning",
            now=SHOWCASE_NOW,
        ),
        State(
            22,
            "Cron: the ticker went silent",
            _showcase_snapshot(
                "critical",
                incidents=(Incident("cron:ticker", "critical", "Cron ticker silent 12 min"),),
                cron=CronSummary(
                    "stalled",
                    active=27,
                    paused=3,
                    ticker_at=_S_TICKER_SILENT,
                    ticker_ok_at=_S_TICKER_SILENT,
                ),
            ),
            _delivery_ok(_S_MINUS_2M),
            "critical",
            now=SHOWCASE_NOW,
        ),
        State(
            23,
            "Telegram connected but deaf: sends blocked",
            _showcase_snapshot(
                "critical",
                incidents=(Incident("telegram:no_sends", "critical", "Telegram: sends blocked"),),
                traffic=TrafficSummary(
                    "no_sends",
                    last_update_seen_at=_S_LAST_UPDATE,
                    polling_at=_S_MINUS_2M,
                    sends_blocked_since="2026-09-26T20:40:00+00:00",
                ),
            ),
            _delivery_ok(_S_MINUS_2M),
            "critical",
            now=SHOWCASE_NOW,
        ),
    )


# The narrow phone state (24, 0.9.1): the showcase installation with the two lines that wrapped
# on a phone on 01.10. An event with a name beyond ASCII is cut at eight; Claude's marked line
# with two windows keeps its exact times since 0.9.2 (Ilya, 01.10: accuracy over width) and wraps
# on the narrow phone. The mark on the Claude line is the provider's own ``warning`` on the week,
# as it was on that phone; the week resets exactly four days away. The event is a literal;
# ``test_states`` holds it to what ``cron_jobs.incidents_for`` builds for the record.
_S_IN_3H34M = "2026-09-27T00:34:00+00:00"
_S_IN_4D = "2026-09-30T21:00:00+00:00"


def _phone_states() -> tuple[State, ...]:
    undelivered = CronFailure(
        "b2",
        "Утренняя сводка проекта",
        "delivery",
        at="2026-09-26T20:05:00+00:00",
        streak=1,
        reason="chat unavailable",
    )
    claude = QuotaMetric(
        "Claude",
        "official",
        windows=(
            QuotaWindow("5h", 29.0, _S_IN_3H34M),
            QuotaWindow("7d", 87.0, _S_IN_4D, severity="warning"),
        ),
        detail="official",
    )
    return (
        State(
            24,
            "The narrow phone: a marked limits line and a Cyrillic job name",
            _showcase_snapshot(
                "warning",
                incidents=(Incident("cron:delivery:b2", "warning", "Cron undelivered: Утренняя…"),),
                capacity=_external_limits(claude),
                cron=CronSummary(
                    "failing",
                    active=27,
                    paused=3,
                    failing=(undelivered,),
                    ticker_at=_S_MINUS_2M,
                    ticker_ok_at=_S_MINUS_2M,
                ),
            ),
            _delivery_ok(_S_MINUS_2M),
            "warning",
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
                "warning",
                incidents=(
                    Incident("cron:delivery:b1", "warning", "Cron undelivered: daily-digest"),
                ),
                cron=CronSummary(
                    "failing",
                    active=9,
                    paused=0,
                    failing=(
                        CronFailure(
                            "b1",
                            "daily-digest",
                            "delivery",
                            at=_T_MINUS_20M,
                            streak=2,
                            reason="not connected",
                        ),
                    ),
                    ticker_at=_T_MINUS_2M,
                    ticker_ok_at=_T_MINUS_2M,
                ),
            ),
            _delivery_ok(),
            "warning",
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
        *_external_states(),
        *_gemini_states(),
        *_cron_states(),
        *_phone_states(),
    )
