"""Exception-first renderer for the pinned message.

MVP order: top line (status, data time, message time, coverage), limits, drift, up to five
incidents. Work and automation blocks render only when the snapshot carries them; ``None`` means
the block is not observed on this installation and nothing is invented for it.
"""

from __future__ import annotations

import html
from datetime import UTC, datetime, tzinfo

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
from .timeparse import age_seconds, format_in_zone, is_from_the_future, parse_timestamp

TELEGRAM_TEXT_LIMIT = 4096
_SAFE_LIMIT = 3900

_STATUS_LABELS = {
    "normal": "🟢 Норма",
    "warning": "🟡 Требует внимания",
    "critical": "🔴 Требует внимания",
    "unknown": "⚪ Состояние неизвестно",
}
_SCHEDULER_LABELS = {
    "healthy": "норма",
    "degraded": "деградация",
    "unknown": "неизвестно",
}
_PROCESS_LABELS = {
    "running": "работает",
    "stopped": "остановлен",
    "unknown": "неизвестно",
    "unsupported": "не наблюдается",
}
_PLATFORM_LABELS = {
    "connected": "подключён",
    "degraded": "ошибка",
    "disconnected": "не подключён",
    "unknown": "неизвестно",
    "unsupported": "не наблюдается",
}
_DRIFT_LABELS = {
    "unknown": "неизвестно",
    "unsupported": "не наблюдается",
}
_SOURCE_LABELS = {
    "gateway_state": "gateway",
    "limits": "лимиты",
    "grok_quota": "квота Grok",
    "kimi_quota": "квота Kimi",
    "drift": "дрейф",
    "backup": "бэкап",
}


def render_dashboard(
    snapshot: DashboardSnapshot,
    *,
    now: datetime | None = None,
    delivery: DeliveryRecord | None = None,
    period_seconds: int | None = None,
    zone: tzinfo = UTC,
) -> str:
    banner, freshness_lines = _freshness_lines(snapshot, now, delivery, period_seconds, zone)
    # The message-staleness banner is the first line: it must be visible above the status label.
    lines = [banner] if banner else []
    lines.extend(["# Hermes Dashboard", _STATUS_LABELS[snapshot.overall]])
    lines.extend(freshness_lines)
    lines.extend(_coverage_lines(snapshot))
    if snapshot.gateway is not None:
        lines.append(_gateway_line(snapshot.gateway))
    if snapshot.backup is not None:
        reference = now if now is not None else parse_timestamp(snapshot.observed_at)
        lines.append(_backup_line(snapshot.backup, reference, zone))
    if snapshot.incidents:
        lines.extend(["", "## Требует внимания"])
        lines.extend(
            f"- {sanitize_public_text(incident.title)}" for incident in snapshot.incidents[:5]
        )
    if snapshot.capacity.quotas:
        lines.extend(["", "## Лимиты"])
        for quota in snapshot.capacity.quotas:
            lines.extend(_quota_lines(quota, zone))
    if snapshot.drift is not None:
        lines.extend(["", "## Дрейф", _drift_line(snapshot.drift, zone)])
    if snapshot.work is not None:
        work = snapshot.work
        lines.extend(
            [
                "",
                "## Работа",
                (
                    f"- Выполняется: {work.executing} · "
                    f"В очереди: {work.queued} · "
                    f"Ждёт человека: {work.waiting_human}"
                ),
                f"- Ошибки: {work.failed} · Неизвестно: {work.unknown}",
            ]
        )
    if snapshot.automation is not None:
        automation = snapshot.automation
        lines.extend(
            [
                "",
                "## Автоматика",
                f"- Scheduler: {_SCHEDULER_LABELS[automation.scheduler]}",
                (
                    f"- Ошибки запусков: {automation.failed_runs} · "
                    f"Пропущено: {automation.missed_runs} · "
                    f"Ошибки доставки: {automation.delivery_failed}"
                ),
            ]
        )
    if now is not None and period_seconds:
        stamp = format_in_zone(now, zone, "%Y-%m-%d %H:%M %Z") or "время неизвестно"
        lines.extend(["", f"Обновлено: {stamp} · период {max(1, period_seconds // 60)} мин"])
    return bound_text("\n".join(lines))


def _freshness_lines(
    snapshot: DashboardSnapshot,
    now: datetime | None,
    delivery: DeliveryRecord | None,
    period_seconds: int | None,
    zone: tzinfo,
) -> tuple[str | None, list[str]]:
    data_stamp = format_in_zone(snapshot.observed_at, zone, "%H:%M %Z") or "время неизвестно"
    lines = [f"Данные: {data_stamp}"]
    if now is None or delivery is None or not period_seconds:
        return None, lines
    freshness = classify_message_freshness(delivery, now=now, period_seconds=period_seconds)
    banner = message_banner(freshness, delivery, now=now, period_seconds=period_seconds)
    confirmed = format_in_zone(delivery.last_confirmed_at, zone, "%H:%M %Z")
    lines.append(f"Сообщение подтверждено: {confirmed or 'ещё нет'}")
    return banner, lines


def _coverage_lines(snapshot: DashboardSnapshot) -> list[str]:
    lines: list[str] = []
    coverage = snapshot.coverage
    if coverage.expected_profiles:
        lines.append(f"Охват профилей: {coverage.observed_profiles}/{coverage.expected_profiles}")
    if coverage.failed_sources:
        lines.append(f"Недоступно источников: {len(coverage.failed_sources)}")
    if snapshot.sources:
        seen = [source for source in snapshot.sources if source.state in ("fresh", "stale")]
        lines.append(f"Охват источников: {len(seen)}/{len(snapshot.sources)}")
        missing = [
            f"{_SOURCE_LABELS.get(source.name, source.name)} ({_source_reason(source.state)})"
            for source in snapshot.sources
            if source.state not in ("fresh", "stale")
        ]
        if missing:
            lines.append("Не наблюдается: " + ", ".join(missing))
        stale = [
            _SOURCE_LABELS.get(source.name, source.name)
            for source in snapshot.sources
            if source.state == "stale"
        ]
        if stale:
            lines.append("Устарело: " + ", ".join(stale))
    return lines


def _source_reason(state: str) -> str:
    return "нет на этой установке" if state == "unsupported" else "недоступно"


def _gateway_line(gateway: GatewaySummary) -> str:
    process = _PROCESS_LABELS[gateway.process]
    telegram = _PLATFORM_LABELS[gateway.telegram]
    return f"Gateway: {process} · Telegram: {telegram}"


def _backup_line(backup: BackupSummary, reference: datetime | None, zone: tzinfo) -> str:
    """Value plus its own freshness: the absolute time, the age in words, the verdict. Shown in
    every state; a state without a number names its reason."""
    if backup.state == "unsupported":
        reason = sanitize_public_text(backup.detail or "источник не настроен", limit=60)
        return f"Бэкап: не наблюдается ({reason})"
    if backup.state == "unknown":
        reason = sanitize_public_text(backup.detail or "статус не прочитан", limit=60)
        return f"Бэкап: нет данных ({reason})"
    stamp = format_in_zone(backup.finished_at, zone, "%d.%m %H:%M %Z") or "время нечитаемо"
    finished = parse_timestamp(backup.finished_at)
    age = None
    if finished is not None and reference is not None:
        age = age_seconds(finished, reference)
    if age is None or is_from_the_future(age):
        age_words = "возраст неизвестен"
    else:
        age_words = describe_age(max(age, 0.0))
    if backup.state == "failed":
        detail = sanitize_public_text(backup.detail or "причина не записана", limit=120)
        return f"Бэкап: ⚠️ не состоялся {stamp} · {age_words} · {detail}"
    verdict = sanitize_public_text(backup.integrity or "неизвестно", limit=24)
    line = f"Бэкап: {stamp} · {age_words} · integrity {verdict}"
    if age is not None and age > BACKUP_STALE_SECONDS:
        line += f" ⚠️ {BACKUP_STALE_WORDS}"
    return line


def _quota_lines(quota: QuotaMetric, zone: tzinfo) -> list[str]:
    provider = sanitize_public_text(quota.provider, limit=40)
    if quota.kind == "local":
        used = f"{quota.used or 0:,}".replace(",", " ")
        return [f"- {provider}: Локально учтено: {used} токенов", "- Остаток: неизвестно"]
    # "нет данных" always names its reason: the collector's own words, never a default that
    # reads as "we did not finish" when the truth is "the installation has no credential".
    if quota.kind == "unsupported":
        reason = sanitize_public_text(quota.detail or "источник не подтверждён", limit=60)
        return [f"- {provider}: нет данных ({reason})"]
    if quota.kind == "unavailable":
        reason = sanitize_public_text(quota.detail or "источник недоступен", limit=60)
        return [f"- {provider}: нет данных ({reason})"]
    if quota.windows:
        line = f"- {provider}: " + _windows_text(quota, zone)
        # Each limit line carries its own freshness: a number read on its own cadence must not
        # borrow the screen's "Обновлено".
        if quota.fetched_at:
            fetched = format_in_zone(quota.fetched_at, zone, "%H:%M")
            line += f" · данные {fetched}" if fetched else " · данные: время нечитаемо"
        return [line]
    if quota.used is not None and quota.limit:
        remaining = round(((quota.limit - quota.used) / quota.limit) * 100)
        lines = [f"- {provider}: Остаток {remaining}%"]
        if quota.reset_at:
            reset = format_in_zone(quota.reset_at, zone, "%Y-%m-%d %H:%M %Z")
            lines.append(f"- Reset: {reset or 'дата нечитаема'}")
        return lines
    return [f"- {provider}: нет данных (окна не получены)"]


def _windows_text(quota: QuotaMetric, zone: tzinfo) -> str:
    """One window: ``label N% · сброс <date>``. Several windows: each carries its own reset in
    brackets, because one date after two percentages says nothing about which window it ends."""
    if len(quota.windows) == 1:
        window = quota.windows[0]
        text = _window_head(window)
        if window.reset_at:
            reset = format_in_zone(window.reset_at, zone, "%d.%m %H:%M %Z")
            text += f" · сброс {reset}" if reset else " · сброс: дата нечитаема"
        return text
    parts = []
    for window in quota.windows:
        part = _window_head(window)
        if window.reset_at:
            reset = format_in_zone(window.reset_at, zone, "%d.%m %H:%M")
            part += f" (сброс {reset})" if reset else " (сброс: дата нечитаема)"
        parts.append(part)
    return " · ".join(parts)


def _window_head(window: QuotaWindow) -> str:
    """``label N%``; the provider's state in words when it gave no number; ``?`` otherwise."""
    if window.used_percent is not None:
        return f"{window.label} {window.used_percent:.0f}%"
    if window.note:
        return f"{window.label}: {window.note}"
    return f"{window.label} ?"


def _drift_line(drift: DriftSummary, zone: tzinfo) -> str:
    checked = format_in_zone(drift.checked_at, zone, "%H:%M %Z") if drift.checked_at else None
    suffix = f" · проверено {checked}" if checked else " · время проверки неизвестно"
    if drift.state in ("clean", "drift") and drift.changed_keys is not None:
        total = f" из {drift.total_keys}" if drift.total_keys is not None else ""
        marker = "✅" if drift.state == "clean" else "⚠️"
        return f"- {marker} {drift.changed_keys}{total} ключей расходятся{suffix}"
    label = _DRIFT_LABELS.get(drift.state, drift.state)
    if drift.detail:
        # Why there is no number; a check that never finished has no check time to show.
        reason = sanitize_public_text(drift.detail, limit=80)
        return f"- {label}: {reason}{suffix if checked else ''}"
    return f"- {label}{suffix}"


def bound_text(text: str, limit: int = _SAFE_LIMIT) -> str:
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


def to_telegram_plain(text: str) -> str:
    """The screen for a transport that sends plain text: ``#``/``##`` headings become upper-case
    lines, every other line is passed through unchanged.

    The engine's ``TelegramAdapter.edit_message`` without ``finalize`` sets no parse mode, so a
    heading marker would reach the chat as a literal ``#``. Its ``finalize=True`` path converts to
    MarkdownV2 but splits an over-long payload into NEW continuation messages, which a pinned
    dashboard must never do. Upper case survives any transport.
    """
    return "\n".join(
        line.lstrip("#").strip().upper() if line.startswith("#") else line
        for line in text.split("\n")
    )


def to_telegram_html(text: str) -> str:
    """Escape for ``parse_mode=HTML`` and turn ``#``/``##`` headings into bold lines."""
    out: list[str] = []
    for line in text.split("\n"):
        stripped = line.lstrip("#").lstrip() if line.startswith("#") else None
        if stripped is not None:
            out.append(f"<b>{html.escape(stripped)}</b>")
        else:
            out.append(html.escape(line))
    return "\n".join(out)
