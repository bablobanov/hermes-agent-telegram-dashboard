"""Staleness of the pinned MESSAGE, as opposed to staleness of the DATA inside it.

The two drift apart in both directions: a fresh snapshot nobody delivered, and a delivered message
whose collectors returned stale data. Data freshness lives in ``normalize.classify_freshness``;
this module only knows when Telegram last confirmed the message text.

A dead updater cannot edit the message to say it is dead. The message therefore always carries an
absolute confirmation time, and this classifier is exposed to an external check (``--check``) that
can alert through a different path.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from .timeparse import (
    CLOCK_SKEW_TOLERANCE_SECONDS,
    age_seconds,
    format_in_zone,
    is_from_the_future,
    parse_timestamp,
)

MessageFreshness = Literal["never", "confirmed", "lagging", "stale", "lost"]

_EXIT_CODES: dict[MessageFreshness, int] = {
    "confirmed": 0,
    "lagging": 1,
    "never": 2,
    "stale": 2,
    "lost": 2,
}


@dataclass(frozen=True, slots=True)
class DeliveryRecord:
    message_id: int | None = None
    last_confirmed_at: str | None = None
    last_attempt_at: str | None = None
    last_error: str | None = None
    text_hash: str | None = None
    lost_at: str | None = None
    recreated_at: str | None = None

    def confirmed(self, *, at: str, text_hash: str) -> DeliveryRecord:
        return replace(
            self,
            last_confirmed_at=at,
            last_attempt_at=at,
            last_error=None,
            text_hash=text_hash,
        )

    def failed(self, *, at: str, error: str) -> DeliveryRecord:
        return replace(self, last_attempt_at=at, last_error=error)


def classify_message_freshness(
    record: DeliveryRecord,
    *,
    now: datetime,
    period_seconds: int,
) -> MessageFreshness:
    if record.last_error == "lost":
        return "lost"
    if record.message_id is None or record.last_confirmed_at is None:
        return "never"
    age = message_age_seconds(record, now=now)
    if age is None or is_from_the_future(age):
        # Unreadable or dated ahead of now: a negative age would pass every threshold below.
        return "stale"
    if age <= confirmation_threshold_seconds(period_seconds):
        return "confirmed"
    if age <= 2 * period_seconds:
        return "lagging"
    return "stale"


def message_age_seconds(record: DeliveryRecord, *, now: datetime) -> float | None:
    """Seconds since Telegram last confirmed the text; ``None`` when the stamp is unreadable."""
    confirmed = parse_timestamp(record.last_confirmed_at)
    return age_seconds(confirmed, now) if confirmed is not None else None


def confirmation_threshold_seconds(period_seconds: int) -> float:
    """The age up to which a confirmation still counts as ``confirmed``; ``lagging`` starts past it.

    One number shared by the classifier and the watchdog's repeat policy, so "the first tick past
    the threshold" means the same moment in both.
    """
    return period_seconds + confirmation_slack_seconds(period_seconds)


def confirmation_slack_seconds(period_seconds: int) -> float:
    """How far past one period a confirmation still counts as ``confirmed``.

    An updater that edits once per period reads its own record at the start of the next tick,
    one period plus timer jitter after the last confirmation. Without slack every healthy tick
    would read as ``lagging`` and the banner would train the reader to ignore it. Bounded by the
    clock-skew tolerance, and by half a period so ``lagging`` keeps a window of its own.
    """
    return min(CLOCK_SKEW_TOLERANCE_SECONDS, period_seconds / 2)


PLUGIN_STATE_KEY = "probe"
_PLUGIN_FAILED_SUFFIX = "_failed"


def record_from_plugin_state(payload: object, *, key: str = PLUGIN_STATE_KEY) -> DeliveryRecord:
    """The probe plugin's record, as PluginState leaves it on disk, read as a ``DeliveryRecord``.

    The plugin keeps ``last_status``/``last_error`` and clears ``message_id`` when the message is
    lost; ``lost`` here therefore means lost and not recreated. ``never`` for a missing key or a
    foreign file: a watchdog pointed at the wrong file must fail, not pass.
    """
    if not isinstance(payload, dict):
        return DeliveryRecord()
    raw = payload.get(key)
    if not isinstance(raw, dict):
        return DeliveryRecord()
    message_id = _plugin_message_id(raw.get("message_id"))
    lost_at = _plugin_str(raw.get("lost_at"))
    status = _plugin_str(raw.get("last_status"))
    error = _plugin_str(raw.get("last_error"))
    if message_id is None and lost_at:
        last_error: str | None = "lost"
    elif status is not None and status.endswith(_PLUGIN_FAILED_SUFFIX):
        last_error = f"{status}: {error}" if error else status
    else:
        last_error = None
    return DeliveryRecord(
        message_id=message_id,
        last_confirmed_at=_plugin_str(raw.get("last_confirmed_at")),
        last_attempt_at=_plugin_str(raw.get("last_attempt_at")),
        last_error=last_error,
        lost_at=lost_at,
        recreated_at=_plugin_str(raw.get("recreated_at")),
    )


def load_plugin_record(path: Path) -> DeliveryRecord:
    """The probe plugin's record from its PluginState file; an empty record (``never``) when the
    file is missing or unreadable: a watchdog pointed at a wrong path must fail, not pass."""
    logger = logging.getLogger(__name__)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        logger.error("plugin state file missing: %s", path.name)
        return DeliveryRecord()
    except (OSError, ValueError, UnicodeDecodeError) as exc:
        logger.error("plugin state unreadable (%s): %s", type(exc).__name__, path.name)
        return DeliveryRecord()
    return record_from_plugin_state(payload)


def _plugin_message_id(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip().lstrip("-").isdigit():
        return int(value.strip())
    return None


def _plugin_str(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def check_exit_code(freshness: MessageFreshness) -> int:
    return _EXIT_CODES[freshness]


def message_banner(
    freshness: MessageFreshness,
    record: DeliveryRecord,
    *,
    now: datetime,
    period_seconds: int,
) -> str | None:
    """Loud one-line notice for the top of the message; ``None`` when nothing is wrong."""
    if freshness == "confirmed":
        return None
    if freshness == "never":
        return "⚠️ Сообщение ещё ни разу не подтверждено Telegram"
    if freshness == "lost":
        seen = format_in_zone(record.lost_at, now.tzinfo or UTC, "%H:%M %Z") or "время неизвестно"
        tail = "создано заново" if record.recreated_at else "НЕ восстановлено"
        return f"🔴 Закреплённое сообщение пропало ({seen}), {tail}"
    confirmed = parse_timestamp(record.last_confirmed_at)
    age = age_seconds(confirmed, now) if confirmed is not None else None
    if is_from_the_future(age):
        ahead = f"{int(-age // 60)} мин" if age is not None else "неизвестно на сколько"
        return f"🔴 ДАШБОРД УСТАРЕЛ: подтверждение датировано будущим ({ahead} вперёд)"
    minutes = "неизвестно сколько" if age is None else f"{int(age // 60)} мин"
    threshold = f"{(2 * period_seconds) // 60} мин"
    if freshness == "lagging":
        return f"🟡 Обновление запаздывает: последнее подтверждение {minutes} назад"
    return f"🔴 ДАШБОРД УСТАРЕЛ: последнее подтверждение {minutes} назад, порог {threshold}"
