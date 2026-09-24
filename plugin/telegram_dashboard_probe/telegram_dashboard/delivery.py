"""Pinned-message mechanics: one message, edited in place, its id remembered on disk.

Decisions that are easy to get wrong:

* ``message is not modified`` is a confirmation, not a failure: Telegram compared our text with
  the live message, so the message exists and already says what we wanted.
* A missing message is loud: ``ERROR`` log, ``lost_at`` in the record, a banner in the recreated
  message. Silent recreation would hide the very failure class this product exists to expose.
* No retries here. The next cron tick is the retry; a retry loop inside the tick is how a
  forgotten process keeps a channel busy.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import os
import tempfile
from dataclasses import asdict, dataclass, replace
from datetime import datetime
from pathlib import Path
from typing import Literal, Protocol

from .freshness import DeliveryRecord

ErrorClass = Literal[
    "not_modified",
    "not_found",
    "forbidden",
    "rate_limited",
    "bad_request",
    "transport",
    "unknown",
]
DeliveryStatus = Literal["created", "edited", "unchanged", "recreated", "lost", "failed"]

_NOT_FOUND_MARKERS = (
    "message to edit not found",
    "message to pin not found",
    "message_id_invalid",
    "message can't be edited",
    "message identifier is not specified",
)


@dataclass(frozen=True, slots=True)
class TransportResult:
    ok: bool
    error: ErrorClass | None = None
    message_id: int | None = None
    retry_after: int | None = None
    detail: str = ""


class Transport(Protocol):
    def send_message(
        self, *, chat_id: str, text: str, thread_id: str | None
    ) -> TransportResult: ...

    def edit_message_text(self, *, chat_id: str, message_id: int, text: str) -> TransportResult: ...

    def pin_chat_message(self, *, chat_id: str, message_id: int) -> TransportResult: ...


@dataclass(frozen=True, slots=True)
class DeliveryOutcome:
    status: DeliveryStatus
    record: DeliveryRecord
    error: ErrorClass | None = None
    detail: str = ""


def classify_bot_api_error(status: int | None, description: str) -> ErrorClass:
    text = description.lower()
    if "message is not modified" in text:
        return "not_modified"
    if any(marker in text for marker in _NOT_FOUND_MARKERS):
        return "not_found"
    if status == 429 or "too many requests" in text:
        return "rate_limited"
    if status == 403 or "forbidden" in text or "bot was kicked" in text:
        return "forbidden"
    if status == 400:
        return "bad_request"
    return "unknown"


def text_digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class DeliveryStore:
    """JSON file with the delivery record. Atomic replace on save; a broken file reads as empty."""

    def __init__(self, path: Path) -> None:
        self._path = path

    @property
    def path(self) -> Path:
        return self._path

    def load(self) -> DeliveryRecord:
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return DeliveryRecord()
        except (OSError, ValueError, UnicodeDecodeError):
            logging.getLogger(__name__).error(
                "delivery record unreadable, treating as empty: %s", self._path.name
            )
            return DeliveryRecord()
        if not isinstance(raw, dict):
            return DeliveryRecord()
        return _record_from_dict(raw)

    def save(self, record: DeliveryRecord) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(asdict(record), ensure_ascii=False, indent=2)
        handle, tmp_name = tempfile.mkstemp(
            prefix=self._path.name, suffix=".tmp", dir=self._path.parent
        )
        try:
            with os.fdopen(handle, "w", encoding="utf-8") as tmp:
                tmp.write(payload)
            os.replace(tmp_name, self._path)
        except OSError:
            with contextlib.suppress(OSError):
                os.unlink(tmp_name)
            raise


def _record_from_dict(raw: dict[str, object]) -> DeliveryRecord:
    message_id = raw.get("message_id")
    valid_id = isinstance(message_id, int) and not isinstance(message_id, bool)
    return DeliveryRecord(
        message_id=message_id if valid_id else None,  # type: ignore[arg-type]
        last_confirmed_at=_optional_str(raw.get("last_confirmed_at")),
        last_attempt_at=_optional_str(raw.get("last_attempt_at")),
        last_error=_optional_str(raw.get("last_error")),
        text_hash=_optional_str(raw.get("text_hash")),
        lost_at=_optional_str(raw.get("lost_at")),
        recreated_at=_optional_str(raw.get("recreated_at")),
    )


def _optional_str(value: object) -> str | None:
    return value if isinstance(value, str) else None


def deliver(
    text: str,
    *,
    transport: Transport,
    record: DeliveryRecord,
    chat_id: str,
    thread_id: str | None,
    now: datetime,
    recreate_on_loss: bool = True,
    log: logging.Logger | None = None,
) -> DeliveryOutcome:
    """Edit the remembered message or create it. Never raises on transport outcomes."""
    logger = log or logging.getLogger(__name__)
    at = now.isoformat()
    digest = text_digest(text)
    if record.message_id is None:
        return _create(text, transport, record, chat_id, thread_id, at, digest, logger, "created")

    result = transport.edit_message_text(chat_id=chat_id, message_id=record.message_id, text=text)
    if result.ok:
        return DeliveryOutcome("edited", record.confirmed(at=at, text_hash=digest))
    if result.error == "not_modified":
        logger.info("pinned message %s unchanged; Telegram confirmed it exists", record.message_id)
        return DeliveryOutcome("unchanged", record.confirmed(at=at, text_hash=digest))
    if result.error == "not_found":
        logger.error("pinned dashboard message %s is MISSING: %s", record.message_id, result.detail)
        lost = replace(record, lost_at=at, last_attempt_at=at, last_error="lost")
        if not recreate_on_loss:
            return DeliveryOutcome("lost", lost, error="not_found", detail=result.detail)
        return _create(text, transport, lost, chat_id, thread_id, at, digest, logger, "recreated")
    logger.warning(
        "edit of pinned message %s failed: %s %s", record.message_id, result.error, result.detail
    )
    return DeliveryOutcome(
        "failed",
        record.failed(at=at, error=result.error or "unknown"),
        error=result.error,
        detail=result.detail,
    )


def _create(
    text: str,
    transport: Transport,
    record: DeliveryRecord,
    chat_id: str,
    thread_id: str | None,
    at: str,
    digest: str,
    logger: logging.Logger,
    status: DeliveryStatus,
) -> DeliveryOutcome:
    sent = transport.send_message(chat_id=chat_id, text=text, thread_id=thread_id)
    if not sent.ok or sent.message_id is None:
        error: ErrorClass = sent.error or "unknown"
        logger.error("could not create dashboard message: %s %s", error, sent.detail)
        keep_lost = record.last_error == "lost"
        failed = record.failed(at=at, error="lost" if keep_lost else error)
        return DeliveryOutcome("failed", failed, error=error, detail=sent.detail)
    created = replace(
        record.confirmed(at=at, text_hash=digest),
        message_id=sent.message_id,
        recreated_at=at if status == "recreated" else record.recreated_at,
    )
    pinned = transport.pin_chat_message(chat_id=chat_id, message_id=sent.message_id)
    if not pinned.ok:
        logger.error(
            "message %s created but NOT pinned: %s %s", sent.message_id, pinned.error, pinned.detail
        )
        return DeliveryOutcome(
            status,
            replace(created, last_error="pin_failed"),
            error=pinned.error,
            detail=f"pin failed: {pinned.detail}",
        )
    logger.info("dashboard message %s created and pinned (%s)", sent.message_id, status)
    return DeliveryOutcome(status, created)
