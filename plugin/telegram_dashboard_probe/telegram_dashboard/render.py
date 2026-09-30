"""Exception-first renderer for the pinned message: one phone screen.

The first line is the status with the data stamp (the pinned-message header shows that line),
then gateway, backup and drift as one short line each, up to five incidents, one line per
provider under "🧠 Limits used" with the time to every reset (plus a line for every model's own
limit), the Hermes version line after them,
and everything that explains a line (reasons, per-source stamps, coverage) in a details block
that Telegram shows collapsed.
Nothing is dropped, only moved: a line without a number still names its reason, in the
details. Work and automation blocks render only when the snapshot carries them; ``None`` means
the block is not observed on this installation and nothing is invented for it.

The text is transport-neutral: ``## `` marks a bold line, ``> `` marks a details line, and
``to_telegram_html``/``to_telegram_plain`` turn it into what a transport accepts.
"""

from __future__ import annotations

import html
import math
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, tzinfo

from .backup import STALE_SECONDS as BACKUP_STALE_SECONDS
from .backup import STALE_WORDS as BACKUP_STALE_WORDS
from .backup import describe_age
from .freshness import DeliveryRecord, classify_message_freshness, message_banner
from .gemini_log import hit_words, is_active
from .policy import sanitize_public_text
from .schema import (
    BackupSummary,
    DashboardSnapshot,
    DriftSummary,
    GatewaySummary,
    QuotaMetric,
    QuotaWindow,
    Refusal,
    VersionSummary,
)
from .timeparse import (
    age_seconds,
    format_day,
    format_day_time,
    format_in_zone,
    format_stamp,
    is_from_the_future,
    parse_timestamp,
)

TELEGRAM_TEXT_LIMIT = 4096
_SAFE_LIMIT = 3900

# A text check mark, not an emoji: the emoji forms render as a grey glyph or a green box on the
# phone (tried on 25.09), the plain glyph reads the same everywhere.
OK_MARK = "✓"
WARN_MARK = "⚠️"
# No bar (decision of 25.09): the shade glyphs came from a fallback font and read as noise, the
# solid ones sat below the letters or wrapped the line, and a bar by fifths added nothing to the
# number after it. Every percent on the screen is the spent share; the heading says so, and its
# brain names the block at a glance: bold alone hardly shows in Telegram Desktop (25.09).
LIMITS_HEADING = "🧠 Limits used"
# The Hermes version line after the limits: information only, no sign and no advice (decision of
# 25.09: updating Hermes is a process, not a restart).
VERSION_MARK = "🤖"
# A phone line; the version line keeps to it whatever the version string (a dev build, a local
# tag): a shorter form first, then the version cut with an ellipsis, whole in the details.
_LINE_COLUMNS = 32
_MINUTES_PER_DAY = 1440
# Windows carry no length label (decision of 25.09): the spent share and the time to its reset
# answer what the reader acts on, the plan (where the provider names it) is in the details, and
# a length the source does not state becomes a lie (the engine calls Codex's first window
# ``Session`` whatever its ``limit_window_seconds``; ``agent/account_usage.py``, 0.21.1 and
# 0.21.3). A label that narrows the scope stays: a limit for one model is not the account's limit.
_SCOPE_LABELS = {"Opus week": "Opus", "Sonnet week": "Sonnet"}
# A limit this far spent gets the warning mark on its line (decision of 25.09), and so does a
# window the provider itself calls warning or critical. Only the line: the overall status and the
# incidents come from the collectors, not from this number.
QUOTA_WARN_PERCENT = 90
_LOUD_SEVERITIES = ("warning", "critical")
# One-minute resolution on the screen: a stamp within a minute of the data time is the same.
_STAMP_SLACK_SECONDS = 60.0
_DETAILS_PREFIX = "> "

_STATUS_LABELS = {
    "normal": "🟢 Healthy",
    "warning": "🟡 Warning",
    "critical": "🔴 Critical",
    "unknown": "⚪ Unknown",
}
_SCHEDULER_LABELS = {
    "healthy": "normal",
    "degraded": "degraded",
    "unknown": "unknown",
}
_PROCESS_LABELS = {
    "running": OK_MARK,
    "stopped": "stopped",
    "unknown": "unknown",
    "unsupported": "not observed",
}
_PLATFORM_LABELS = {
    "connected": OK_MARK,
    "degraded": "error",
    "disconnected": "disconnected",
    "unknown": "unknown",
    "unsupported": "not observed",
}
_DRIFT_LABELS = {
    "unknown": "unknown",
    "unsupported": "not observed",
}
_SOURCE_LABELS = {
    "gateway_state": "gateway",
    "limits": "limits",
    "grok_quota": "Grok quota",
    "kimi_quota": "Kimi quota",
    "gemini_log": "Gemini log",
    "drift": "drift",
    "backup": "backup",
}


def _plural(count: int, one: str, many: str) -> str:
    """``1 key``, ``3 keys``, ``120,000 tokens``: the number with its noun."""
    return f"{count:,} {one if count == 1 else many}"


@dataclass
class _Details:
    """What the collapsed block says, grouped: the screen itself, backup and drift, reasons for
    every "no data"."""

    confirmed: str | None = None
    data: str | None = None
    period: str | None = None
    coverage: str | None = None
    state: list[str] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)

    def lines(self) -> list[str]:
        screen = [part for part in (self.confirmed, self.data, self.period, self.coverage) if part]
        groups = [
            screen,
            self.state,
            ["## No data", *self.missing] if self.missing else [],
        ]
        body: list[str] = []
        for group in groups:
            if not group:
                continue
            if body:
                body.append("")
            body.extend(group)
        if not body:
            return []
        return [
            "",
            *(_DETAILS_PREFIX + line if line else ">" for line in ["## Details", *body]),
        ]


def render_dashboard(
    snapshot: DashboardSnapshot,
    *,
    now: datetime | None = None,
    delivery: DeliveryRecord | None = None,
    period_seconds: int | None = None,
    zone: tzinfo = UTC,
) -> str:
    details = _Details()
    reference = now if now is not None else parse_timestamp(snapshot.observed_at)
    banner = _freshness(details, delivery, now, period_seconds, zone)
    # The message-staleness banner is the first line: it must be visible above the status label.
    lines = [banner] if banner else []
    data_stamp = format_stamp(snapshot.observed_at, zone) or "time unknown"
    lines.append(f"{_STATUS_LABELS[snapshot.overall]} · {data_stamp}")
    if snapshot.gateway is not None:
        lines.append(_gateway_line(snapshot.gateway))
    if snapshot.backup is not None:
        lines.append(_backup_line(snapshot.backup, reference, zone, details))
    if snapshot.drift is not None:
        lines.append(_drift_line(snapshot.drift, zone, details))
    lines.extend(_coverage_lines(snapshot, details))
    if snapshot.incidents:
        lines.extend(["", "## Needs attention"])
        lines.extend(
            f"- {sanitize_public_text(incident.title)}" for incident in snapshot.incidents[:5]
        )
    if snapshot.capacity.quotas:
        lines.extend(["", f"## {LIMITS_HEADING}"])
        for quota in snapshot.capacity.quotas:
            lines.extend(_quota_lines(quota, reference, details, zone))
        details.data = _data_stamps(snapshot, zone)
        details.state.extend(_account_words(snapshot.capacity.quotas, reference, zone))
        details.state.extend(_refusal_words(snapshot.capacity.quotas, zone))
    if snapshot.version is not None:
        lines.extend(["", _version_line(snapshot.version, details, zone)])
    if snapshot.work is not None:
        work = snapshot.work
        lines.extend(
            [
                "",
                "## Work",
                (
                    f"- Running: {work.executing} · "
                    f"Queued: {work.queued} · "
                    f"Waiting for a human: {work.waiting_human}"
                ),
                f"- Failed: {work.failed} · Unknown: {work.unknown}",
            ]
        )
    if snapshot.automation is not None:
        automation = snapshot.automation
        lines.extend(
            [
                "",
                "## Automation",
                f"- Scheduler: {_SCHEDULER_LABELS[automation.scheduler]}",
                (
                    f"- Failed runs: {automation.failed_runs} · "
                    f"Missed: {automation.missed_runs} · "
                    f"Delivery failed: {automation.delivery_failed}"
                ),
            ]
        )
    lines.extend(details.lines())
    return bound_text("\n".join(lines))


def _freshness(
    details: _Details,
    delivery: DeliveryRecord | None,
    now: datetime | None,
    period_seconds: int | None,
    zone: tzinfo,
) -> str | None:
    if period_seconds:
        details.period = f"Period {max(1, period_seconds // 60)} min"
    if now is None or delivery is None or not period_seconds:
        return None
    freshness = classify_message_freshness(delivery, now=now, period_seconds=period_seconds)
    confirmed = format_in_zone(delivery.last_confirmed_at, zone, "%H:%M")
    details.confirmed = f"Confirmed {confirmed}" if confirmed else "Confirmed: not yet"
    return message_banner(freshness, delivery, now=now, period_seconds=period_seconds)


def _coverage_lines(snapshot: DashboardSnapshot, details: _Details) -> list[str]:
    """Exceptions on the screen, the full count in the details."""
    lines: list[str] = []
    parts: list[str] = []
    coverage = snapshot.coverage
    if coverage.expected_profiles:
        parts.append(f"profiles {coverage.observed_profiles}/{coverage.expected_profiles}")
        if coverage.observed_profiles != coverage.expected_profiles:
            lines.append(
                f"Profile coverage {coverage.observed_profiles}/{coverage.expected_profiles}"
            )
    if coverage.failed_sources:
        lines.append(f"Sources unavailable: {len(coverage.failed_sources)}")
    if snapshot.sources:
        seen = [source for source in snapshot.sources if source.state in ("fresh", "stale")]
        parts.append(f"sources {len(seen)}/{len(snapshot.sources)}")
        missing = [
            f"{_SOURCE_LABELS.get(source.name, source.name)} ({_source_reason(source.state)})"
            for source in snapshot.sources
            if source.state not in ("fresh", "stale")
        ]
        if missing:
            lines.append("Not observed: " + ", ".join(missing))
        stale = [
            _SOURCE_LABELS.get(source.name, source.name)
            for source in snapshot.sources
            if source.state == "stale"
        ]
        if stale:
            lines.append("Stale: " + ", ".join(stale))
    if parts:
        joined = " · ".join(parts)
        details.coverage = joined[0].upper() + joined[1:]
    return lines


def _source_reason(state: str) -> str:
    return "not on this installation" if state == "unsupported" else "unavailable"


def _gateway_line(gateway: GatewaySummary) -> str:
    process = _PROCESS_LABELS[gateway.process]
    telegram = _PLATFORM_LABELS[gateway.telegram]
    return f"Gateway {process} · Telegram {telegram}"


def _backup_line(
    backup: BackupSummary, reference: datetime | None, zone: tzinfo, details: _Details
) -> str:
    """The verdict and the age in words on the screen; the absolute time, the integrity verdict
    and any reason in the details. Shown in every state; a state without a number names why."""
    if backup.state == "unsupported":
        reason = sanitize_public_text(backup.detail or "source not configured", limit=60)
        details.state.append(f"Backup: {reason}")
        return "Backup: not observed"
    if backup.state == "unknown":
        reason = sanitize_public_text(backup.detail or "status not read", limit=60)
        details.state.append(f"Backup: {reason}")
        return "Backup: no data"
    stamp = format_day_time(backup.finished_at, zone) or "time unreadable"
    finished = parse_timestamp(backup.finished_at)
    age = None
    if finished is not None and reference is not None:
        age = age_seconds(finished, reference)
    if age is None or is_from_the_future(age):
        age_words = "age unknown"
    else:
        age_words = describe_age(max(age, 0.0))
    if backup.state == "failed":
        detail = sanitize_public_text(backup.detail or "reason not recorded", limit=120)
        details.state.append(f"Backup {stamp} · {detail}")
        return f"Backup {WARN_MARK} failed {age_words}"
    verdict = sanitize_public_text(backup.integrity or "unknown", limit=24)
    details.state.append(f"Backup {stamp} · integrity {verdict}")
    line = f"Backup {OK_MARK} {age_words}"
    if age is not None and age > BACKUP_STALE_SECONDS:
        line += f" {WARN_MARK} {BACKUP_STALE_WORDS}"
    return line


def _drift_line(drift: DriftSummary, zone: tzinfo, details: _Details) -> str:
    checked = format_in_zone(drift.checked_at, zone, "%H:%M") if drift.checked_at else None
    checked_words = f"Drift checked {checked}" if checked else "Drift: check time unknown"
    if drift.state in ("clean", "drift") and drift.changed_keys is not None:
        if drift.total_keys is not None:
            count = f"{drift.changed_keys} of {drift.total_keys}"
        else:
            count = _plural(drift.changed_keys, "key", "keys")
        marker = OK_MARK if drift.state == "clean" else WARN_MARK
        details.state.append(checked_words)
        return f"Drift {marker} {count}"
    label = _DRIFT_LABELS.get(drift.state, drift.state)
    if drift.detail:
        # Why there is no number; a check that never finished has no check time to show.
        reason = sanitize_public_text(drift.detail, limit=80)
        details.state.append(f"Drift: {reason}" + (f" · checked {checked}" if checked else ""))
    else:
        details.state.append(checked_words)
    return f"Drift: {label}"


def _quota_lines(
    quota: QuotaMetric, reference: datetime | None, details: _Details, zone: tzinfo
) -> list[str]:
    """The provider's line with the account's windows, then a line of its own for every model's
    limit (decision of 29.09, replacing decision 4 of the subscription plan: the model's limit is
    wanted in sight, and next to the account's windows it does not fit a phone line). A model's
    line leaves out the time to its reset when the account's window of the same label resets at
    the same moment: the account's line already says it. An expired login is said as such: it is
    an answer, not "no data"."""
    provider = sanitize_public_text(quota.provider, limit=40)
    if quota.kind == "expired":
        return [f"{provider} · login expired"]
    if quota.kind != "official" or not quota.windows:
        return [_quota_line(quota, reference, details, zone)]
    own = tuple(window for window in quota.windows if window.scope is None)
    if not own:
        return [_windows_line(provider, quota.windows, reference)]
    lines = [_windows_line(provider, own, reference)]
    for window in quota.windows:
        if window.scope is None:
            continue
        shown = replace(window, reset_at=None) if _resets_with(window, own) else window
        lines.append(f"{_mark((window,))}{provider} {_window_words(shown, reference)}")
    return lines


def _resets_with(window: QuotaWindow, own: tuple[QuotaWindow, ...]) -> bool:
    """True when an account window of the same label resets within a minute of ``window``: the
    source rounds its stamps on its own (the Claude API differs in microseconds)."""
    moment = parse_timestamp(window.reset_at)
    if moment is None:
        return False
    for other in own:
        theirs = parse_timestamp(other.reset_at) if other.label == window.label else None
        if theirs is not None and abs((theirs - moment).total_seconds()) < 60:
            return True
    return False


def _account_words(
    quotas: tuple[QuotaMetric, ...], reference: datetime | None, zone: tzinfo
) -> list[str]:
    """``Plans: Claude Max 5x · Codex Prolite`` and ``Claude login until Oct 27``: only what a
    provider names; a plan nobody reports is not guessed."""
    words: list[str] = []
    plans = [
        f"{sanitize_public_text(q.provider, limit=40)} {sanitize_public_text(q.plan, limit=24)}"
        for q in quotas
        if q.plan
    ]
    if plans:
        words.append("Plans: " + " · ".join(plans))
    for quota in quotas:
        day = format_day(quota.login_expires_at, zone) if quota.login_expires_at else None
        if day is None:
            continue
        moment = parse_timestamp(quota.login_expires_at)
        ended = moment is not None and reference is not None and moment <= reference
        provider = sanitize_public_text(quota.provider, limit=40)
        words.append(f"{provider} login {'ended' if ended else 'until'} {day}")
    return words


def _quota_line(
    quota: QuotaMetric, reference: datetime | None, details: _Details, zone: tzinfo
) -> str:
    provider = sanitize_public_text(quota.provider, limit=40)
    if quota.kind == "local":
        details.missing.append(f"{provider}: remaining unknown, counted locally")
        return f"{provider} · locally {_plural(quota.used or 0, 'token', 'tokens')}"
    # "no data" always names its reason: the collector's own words, never a default that
    # reads as "we did not finish" when the truth is "the installation has no credential".
    if quota.kind == "unsupported":
        reason = sanitize_public_text(quota.detail or "source not found", limit=60)
        details.missing.append(f"{provider}: {reason}")
        return _no_data(provider, quota.refusal, reference, zone)
    if quota.kind == "unavailable":
        reason = sanitize_public_text(quota.detail or "source unavailable", limit=60)
        details.missing.append(f"{provider}: {reason}")
        return _no_data(provider, quota.refusal, reference, zone)
    if quota.windows:
        return _windows_line(provider, quota.windows, reference)
    if quota.used is not None and quota.limit:
        percent = round((quota.used / quota.limit) * 100)
        return f"{_warn(percent)}{provider} {percent}%{_reset_suffix(quota.reset_at, reference)}"
    details.missing.append(f"{provider}: windows not received")
    return _no_data(provider, quota.refusal, reference, zone)


def _no_data(
    provider: str, refusal: Refusal | None, reference: datetime | None, zone: tzinfo
) -> str:
    """``Gemini · no data``; while the provider's last refusal is active (decision of 29.09), the
    line says that instead, in plain words (decision of 30.09): ``⚠️ Gemini paused till 23:00``
    until a daily quota's reset, in the screen's zone, and ``⚠️ Gemini hit limit 5 min ago`` for
    any other. Only the line: the status and the events come from the collectors, a daily
    refusal's event among them."""
    if refusal is None or reference is None or not is_active(refusal, reference):
        return f"{provider} · no data"
    back = format_in_zone(refusal.active_until, zone, "%H:%M") if refusal.daily else None
    if back is not None:
        return f"{WARN_MARK} {provider} paused till {back}"
    moment = parse_timestamp(refusal.at)
    age = age_seconds(moment, reference) if moment is not None else None
    if age is None:
        return f"{provider} · no data"
    return f"{WARN_MARK} {provider} hit limit {hit_words(max(0.0, age))}"


def _refusal_words(quotas: tuple[QuotaMetric, ...], zone: tzinfo) -> list[str]:
    """``Gemini refusals seen by Hermes: last Sep 29 14:03, per-minute limit, model …``: the
    last refusal the engine logged for a provider whose log is read, or that there is none. A
    script that calls the provider on its own is not in that log, hence "seen by Hermes"."""
    return [
        f"{sanitize_public_text(quota.provider, limit=40)} refusals seen by Hermes: "
        + _refusal_parts(quota.refusal, zone)
        for quota in quotas
        if quota.refusal is not None
    ]


def _refusal_parts(refusal: Refusal, zone: tzinfo) -> str:
    """Each part only when the log named it. The limit's number and the retry are not shown
    (decision of 30.09): without its unit the number says nothing, and a retry of seconds is
    long past when the screen is read."""
    if refusal.at is None:
        return "none"
    parts = [f"last {format_day_time(refusal.at, zone) or 'time unreadable'}"]
    kind = _limit_kind(refusal, zone)
    if kind is not None:
        parts.append(kind)
    if refusal.model:
        parts.append(f"model {sanitize_public_text(refusal.model, limit=40)}")
    return ", ".join(parts)


def _limit_kind(refusal: Refusal, zone: tzinfo) -> str | None:
    """``daily limit till Sep 26 23:00`` or ``per-minute limit``, as ``gemini_log.activate`` told
    them apart by the retry; ``daily limit`` alone when the reset cannot be shown (none was
    computed, or the screen's zone cannot represent it); nothing when the provider named no
    retry."""
    if refusal.daily:
        back = format_day_time(refusal.active_until, zone)
        return f"daily limit till {back}" if back is not None else "daily limit"
    return "per-minute limit" if refusal.retry_seconds is not None else None


def _windows_line(
    provider: str, windows: tuple[QuotaWindow, ...], reference: datetime | None
) -> str:
    """``Claude 37% (3h) · 12% (4d)``: every window in the provider's own order, each with the
    time to its reset. A line without a number says the provider's states in words."""
    numbered = [window.used_percent for window in windows if window.used_percent is not None]
    words = " · ".join(_window_words(window, reference) for window in windows)
    if not numbered:
        return f"{provider} · {words}"
    return f"{_mark(windows)}{provider} {words}"


def _mark(windows: tuple[QuotaWindow, ...]) -> str:
    """The warning mark for a line: its most spent window from ``QUOTA_WARN_PERCENT`` on, or any
    window the provider itself calls warning or critical."""
    spent = [window.used_percent for window in windows if window.used_percent is not None]
    loud = any(window.severity in _LOUD_SEVERITIES for window in windows)
    if loud or (spent and max(spent) >= QUOTA_WARN_PERCENT):
        return f"{WARN_MARK} "
    return ""


def _window_words(window: QuotaWindow, reference: datetime | None) -> str:
    """``37% (3h)``, ``Opus 5% (4d)``, ``Fable 100% (4h)``; the provider's state in words when
    it gave no number; ``?`` otherwise. A state in words never becomes a number."""
    scope = window.scope or _SCOPE_LABELS.get(window.label)
    head = f"{scope} " if scope else ""
    if window.used_percent is not None:
        return f"{head}{window.used_percent:.0f}%{_reset_suffix(window.reset_at, reference)}"
    return f"{head}{window.note or '?'}"


def _warn(percent: float) -> str:
    """The mark from ``QUOTA_WARN_PERCENT`` on, for the most spent window of the line."""
    return f"{WARN_MARK} " if percent >= QUOTA_WARN_PERCENT else ""


def _reset_suffix(value: object, reference: datetime | None) -> str:
    """`` (3h)``: the time to the reset; `` (?)`` when it cannot be counted; nothing without a
    reset date at all."""
    if not value:
        return ""
    return f" ({_until(value, reference) or '?'})"


def _until(value: object, reference: datetime | None) -> str | None:
    """Whole minutes to ``value`` from ``reference``, rounded up, in ``_duration_words``; a
    reset already behind the data time is ``0m``. ``None`` when either side is unreadable."""
    moment = parse_timestamp(value)
    if moment is None or reference is None:
        return None
    ahead = age_seconds(reference, moment)
    if ahead is None:
        return None
    return _duration_words(max(0, math.ceil(ahead / 60)))


def _duration_words(minutes: int) -> str:
    """The status-line form: whole days from two days on (``4d``), ``1d5h`` or ``24h`` on the
    first day, then ``1h21m``, ``3h`` and ``45m``."""
    days, rest = divmod(minutes, _MINUTES_PER_DAY)
    hours, mins = divmod(rest, 60)
    if days >= 2:
        return f"{days}d"
    if days == 1:
        return f"1d{hours}h" if hours else "24h"
    if hours:
        return f"{hours}h{mins}m" if mins else f"{hours}h"
    return f"{mins}m"


def _version_line(version: VersionSummary, details: _Details, zone: tzinfo) -> str:
    """``🤖 Hermes 0.21.3 → 0.21.5``: the version the gateway runs, then the release upstream
    marks Latest. The dates, the count and the time of the check go to the details."""
    checked = _checked(version.checked_at, zone)
    running = sanitize_public_text(version.running, limit=24) if version.running else None
    if running is None:
        details.missing.append(f"Hermes version: {_reason(version.local_reason)}")
    if version.latest is None:
        details.missing.append(f"Hermes latest: {_reason(version.reason)}{checked}")
        if running is None:
            return _no_version()
        line = _fit(running, " · no data")
        if running not in line:
            details.state.append(f"Hermes version {running}")
        return line
    latest = sanitize_public_text(version.latest, limit=24)
    latest_of = f"{latest}{_of(version.latest_published_at, zone)}"
    if running is None:
        details.state.append(f"Hermes latest {latest_of}{checked}")
        return _no_version()
    details.state.extend(_version_details(version, running, latest_of, checked, zone))
    return _version_words(version.behind, running, latest)


def _version_words(behind: int | None, running: str, latest: str) -> str:
    if behind == 0:
        return _fit(running, f" {OK_MARK}")
    if behind is not None and behind > 0:
        return _fit(running, f" → {latest}")
    # Newer than Latest, or not on the list at all: no arrow, it would point the wrong way.
    shorter = f"{VERSION_MARK} Hermes {running}" + (" · newer" if behind is not None else "")
    return _fit(running, f" · latest {latest}", shorter)


def _fit(running: str, after: str, *shorter: str) -> str:
    """``🤖 Hermes <running><after>`` within a phone line: as is, else the first shorter form
    that fits, else the running version cut with an ellipsis so ``after`` stays whole."""
    head = f"{VERSION_MARK} Hermes "
    for line in (f"{head}{running}{after}", *shorter):
        if len(line) <= _LINE_COLUMNS:
            return line
    room = _LINE_COLUMNS - len(head) - len(after) - 1
    if room < 1:
        return f"{head}{running}"[: _LINE_COLUMNS - 1] + "…"
    return f"{head}{running[:room]}…{after}"


def _version_details(
    version: VersionSummary, running: str, latest_of: str, checked: str, zone: tzinfo
) -> list[str]:
    ours = f"Hermes {running}{_of(version.running_published_at, zone)}"
    behind = version.behind
    if behind == 0:
        return [f"{ours} is the latest{checked}"]
    if behind is None:
        return [
            f"Hermes {running} not among the last {version.list_size} releases, "
            f"latest {latest_of}{checked}"
        ]
    if behind < 0:
        return [f"{ours} is newer than the latest {latest_of}{checked}"]
    return [
        f"{ours}, latest {latest_of}",
        f"{_plural(behind, 'release', 'releases')} behind{checked}",
    ]


def _no_version() -> str:
    return f"{VERSION_MARK} Hermes · no data"


def _of(published: str | None, zone: tzinfo) -> str:
    day = format_day(published, zone) if published else None
    return f" of {day}" if day else ""


def _checked(checked_at: str | None, zone: tzinfo) -> str:
    stamp = format_day_time(checked_at, zone) if checked_at else None
    return f" · checked {stamp}" if stamp else ""


def _reason(reason: str | None) -> str:
    return sanitize_public_text(reason, limit=60) if reason else "unknown"


def _data_stamps(snapshot: DashboardSnapshot, zone: tzinfo) -> str | None:
    """``Data HH:MM · Kimi HH:MM``: the screen's data time and every number read at another
    minute. A number read on its own cadence must not borrow the screen's stamp; when every
    stamp matches, the first line already says it and the details stay silent."""
    observed = parse_timestamp(snapshot.observed_at)
    exceptions: list[str] = []
    for quota in snapshot.capacity.quotas:
        has_number = bool(quota.windows) or (quota.used is not None and bool(quota.limit))
        if not has_number or not quota.fetched_at:
            continue
        provider = sanitize_public_text(quota.provider, limit=40)
        fetched = parse_timestamp(quota.fetched_at)
        if fetched is None:
            exceptions.append(f"{provider} time unreadable")
            continue
        gap = age_seconds(fetched, observed) if observed is not None else None
        if gap is None or abs(gap) >= _STAMP_SLACK_SECONDS:
            exceptions.append(f"{provider} {format_in_zone(fetched, zone, '%H:%M')}")
    if not exceptions:
        return None
    screen = format_in_zone(snapshot.observed_at, zone, "%H:%M") or "time unknown"
    return f"Data {screen} · " + " · ".join(exceptions)


def bound_text(text: str, limit: int = _SAFE_LIMIT) -> str:
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


def _split_details(line: str) -> tuple[bool, str]:
    """``(is_details, content)`` for a neutral line: ``> text`` and a bare ``>`` are details."""
    if line.startswith(_DETAILS_PREFIX):
        return True, line[len(_DETAILS_PREFIX) :]
    if line == _DETAILS_PREFIX.rstrip():
        return True, ""
    return False, line


def to_telegram_plain(text: str) -> str:
    """The screen for a transport that sends plain text: ``#``/``##`` headings become upper-case
    lines, details lines lose their marker and stay in place, every other line is unchanged.

    The engine's ``TelegramAdapter.edit_message`` without ``finalize`` sets no parse mode, so a
    heading marker would reach the chat as a literal ``#``. Its ``finalize=True`` path converts
    to MarkdownV2 but splits an over-long payload into NEW continuation messages, which a pinned
    dashboard must never do. Upper case survives any transport; the details are simply shown.
    """
    out: list[str] = []
    for line in text.split("\n"):
        _, content = _split_details(line)
        out.append(content.lstrip("#").strip().upper() if content.startswith("#") else content)
    return "\n".join(out)


def to_telegram_html(text: str) -> str:
    """Escape for ``parse_mode=HTML``: headings become bold lines, a run of details lines
    becomes one ``<blockquote expandable>``, which Telegram shows collapsed."""
    out: list[str] = []
    quote: list[str] = []

    def flush() -> None:
        if quote:
            out.append("<blockquote expandable>" + "\n".join(quote) + "</blockquote>")
            quote.clear()

    for line in text.split("\n"):
        is_details, content = _split_details(line)
        if is_details:
            quote.append(_html_line(content))
        else:
            flush()
            out.append(_html_line(content))
    flush()
    return "\n".join(out)


def _html_line(line: str) -> str:
    if line.startswith("#"):
        return f"<b>{html.escape(line.lstrip('#').lstrip())}</b>"
    return html.escape(line)
