"""Exception-first renderer for the pinned message: one phone screen.

The first line is the status with the data stamp (the pinned-message header shows that line),
then gateway, backup and drift as one short line each, up to five incidents, one line per
provider under "Limits", and everything that explains a line (reasons, reset times, per-source
stamps, coverage) in a details block that Telegram shows collapsed. Nothing is dropped, only
moved: a line without a number still names its reason, in the details. Work and automation
blocks render only when the snapshot carries them; ``None`` means the block is not observed on
this installation and nothing is invented for it.

The text is transport-neutral: ``## `` marks a bold line, ``> `` marks a details line, and
``to_telegram_html``/``to_telegram_plain`` turn it into what a transport accepts.
"""

from __future__ import annotations

import html
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta, tzinfo

from .backup import STALE_SECONDS as BACKUP_STALE_SECONDS
from .backup import STALE_WORDS as BACKUP_STALE_WORDS
from .backup import describe_age
from .freshness import DeliveryRecord, classify_message_freshness, message_banner
from .policy import sanitize_public_text
from .schema import (
    BackupSummary,
    DashboardSnapshot,
    DriftSummary,
    GatewaySummary,
    QuotaMetric,
    QuotaWindow,
)
from .timeparse import (
    age_seconds,
    day_words,
    format_day_time,
    format_in_zone,
    format_stamp,
    is_from_the_future,
    parse_timestamp,
    to_zone,
)

TELEGRAM_TEXT_LIMIT = 4096
_SAFE_LIMIT = 3900

# A text check mark, not an emoji: the emoji forms render as a grey glyph or a green box on the
# phone (tried on 25.09), the plain glyph reads the same everywhere.
OK_MARK = "✓"
WARN_MARK = "⚠️"
# Five cells, one per 20%: a wider bar wraps on a phone next to the provider and the number.
BAR_WIDTH = 5
BAR_FULL = "▓"
BAR_EMPTY = "░"
# A limit this far spent gets the warning mark on its line (decision of 25.09). Only the line:
# the overall status and the incidents come from the collectors, not from this number.
QUOTA_WARN_PERCENT = 90
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
    "drift": "drift",
    "backup": "backup",
}


def _plural(count: int, one: str, many: str) -> str:
    """``1 key``, ``3 keys``, ``120,000 tokens``: the number with its noun."""
    return f"{count:,} {one if count == 1 else many}"


@dataclass
class _Details:
    """What the collapsed block says, grouped: the screen itself, backup and drift, resets,
    reasons for every "no data"."""

    confirmed: str | None = None
    data: str | None = None
    period: str | None = None
    coverage: str | None = None
    state: list[str] = field(default_factory=list)
    resets: list[str] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)

    def lines(self) -> list[str]:
        screen = [part for part in (self.confirmed, self.data, self.period, self.coverage) if part]
        groups = [
            screen,
            self.state,
            ["## Resets", *self.resets] if self.resets else [],
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
        lines.extend(["", "## Limits"])
        for quota in snapshot.capacity.quotas:
            lines.append(_quota_line(quota, reference, zone, details))
        details.data = _data_stamps(snapshot, zone)
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


def _quota_line(
    quota: QuotaMetric, reference: datetime | None, zone: tzinfo, details: _Details
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
        return f"{provider} · no data"
    if quota.kind == "unavailable":
        reason = sanitize_public_text(quota.detail or "source unavailable", limit=60)
        details.missing.append(f"{provider}: {reason}")
        return f"{provider} · no data"
    if quota.windows:
        _note_resets(provider, quota.windows, reference, zone, details)
        return _windows_line(provider, quota.windows)
    if quota.used is not None and quota.limit:
        if quota.reset_at:
            details.resets.append(f"{provider} {_reset_words(quota.reset_at, reference, zone)}")
        return _bar_line(provider, round((quota.used / quota.limit) * 100))
    details.missing.append(f"{provider}: windows not received")
    return f"{provider} · no data"


def _windows_line(provider: str, windows: tuple[QuotaWindow, ...]) -> str:
    """The window with the most spent gets the bar; the others follow in words. The bar's own
    label is written only when there is more than one window to tell apart."""
    numbered = [window for window in windows if window.used_percent is not None]
    if not numbered:
        if len(windows) == 1:
            return f"{provider} · {_window_words(windows[0], with_label=False)}"
        return f"{provider} · " + " · ".join(_window_words(window) for window in windows)
    bar_window = max(numbered, key=lambda window: window.used_percent or 0.0)
    line = _bar_line(provider, bar_window.used_percent or 0.0)
    if len(windows) > 1:
        rest = [_window_words(window) for window in windows if window is not bar_window]
        line += f" {bar_window.label} · " + " · ".join(rest)
    return line


def _window_words(window: QuotaWindow, *, with_label: bool = True) -> str:
    """``label N%``; the provider's state in words when it gave no number; ``?`` otherwise."""
    if window.used_percent is not None:
        return f"{window.label} {window.used_percent:.0f}%"
    if window.note:
        return f"{window.label}: {window.note}" if with_label else window.note
    return f"{window.label} ?"


def _bar_line(provider: str, percent: float) -> str:
    """``⚠️ Codex ▓▓▓▓▓ 98%``: the mark from ``QUOTA_WARN_PERCENT`` on, the bar by fifths, the
    exact number after it. A state in words never becomes a bar; that is the caller's job."""
    cells = round(max(0.0, min(100.0, percent)) / (100 / BAR_WIDTH))
    bar = BAR_FULL * cells + BAR_EMPTY * (BAR_WIDTH - cells)
    warn = f"{WARN_MARK} " if percent >= QUOTA_WARN_PERCENT else ""
    return f"{warn}{provider} {bar} {percent:.0f}%"


def _note_resets(
    provider: str,
    windows: tuple[QuotaWindow, ...],
    reference: datetime | None,
    zone: tzinfo,
    details: _Details,
) -> None:
    with_reset = [
        (window, _reset_words(window.reset_at, reference, zone))
        for window in windows
        if window.reset_at
    ]
    if not with_reset:
        return
    if len(windows) == 1:
        details.resets.append(f"{provider} {with_reset[0][1]}")
        return
    details.resets.append(
        f"{provider} " + " · ".join(f"{window.label} {words}" for window, words in with_reset)
    )


def _reset_words(value: object, reference: datetime | None, zone: tzinfo) -> str:
    """A reset on the reference day is a time, the next day is ``tomorrow HH:MM``, anything
    else is a date: a phone line has no room for both when the reader can tell the day."""
    moment = parse_timestamp(value)
    local = to_zone(moment, zone) if moment is not None else None
    if local is None:
        return "date unreadable"
    reference_local = to_zone(reference, zone) if reference is not None else None
    if reference_local is None:
        return f"{day_words(local)} {local:%H:%M}"
    if local.date() == reference_local.date():
        return f"{local:%H:%M}"
    if local.date() == reference_local.date() + timedelta(days=1):
        return f"tomorrow {local:%H:%M}"
    return day_words(local)


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
