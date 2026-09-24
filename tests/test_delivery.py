import json
import logging
from datetime import UTC, datetime
from pathlib import Path

from telegram_dashboard.delivery import (
    DeliveryStore,
    TransportResult,
    classify_bot_api_error,
    deliver,
)
from telegram_dashboard.freshness import DeliveryRecord
from telegram_dashboard.telegram_api import parse_bot_api_response

NOW = datetime(2026, 9, 9, 21, 0, tzinfo=UTC)


class FakeTransport:
    def __init__(
        self, *, edit: TransportResult, send: TransportResult, pin: TransportResult
    ) -> None:
        self._edit, self._send, self._pin = edit, send, pin
        self.calls: list[str] = []

    def send_message(self, *, chat_id: str, text: str, thread_id: str | None) -> TransportResult:
        self.calls.append(f"send:{chat_id}:{thread_id}")
        return self._send

    def edit_message_text(self, *, chat_id: str, message_id: int, text: str) -> TransportResult:
        self.calls.append(f"edit:{message_id}")
        return self._edit

    def pin_chat_message(self, *, chat_id: str, message_id: int) -> TransportResult:
        self.calls.append(f"pin:{message_id}")
        return self._pin


OK = TransportResult(True)
SENT = TransportResult(True, message_id=77)
NOT_MODIFIED = TransportResult(
    False, error="not_modified", detail="Bad Request: message is not modified"
)
NOT_FOUND = TransportResult(
    False, error="not_found", detail="Bad Request: message to edit not found"
)


def test_first_delivery_creates_and_pins_and_remembers_id() -> None:
    transport = FakeTransport(edit=OK, send=SENT, pin=OK)

    outcome = deliver(
        "text", transport=transport, record=DeliveryRecord(), chat_id="c", thread_id="9", now=NOW
    )

    assert outcome.status == "created"
    assert outcome.record.message_id == 77
    assert outcome.record.last_confirmed_at == NOW.isoformat()
    assert transport.calls == ["send:c:9", "pin:77"]


def test_not_modified_is_a_confirmation_not_a_failure() -> None:
    transport = FakeTransport(edit=NOT_MODIFIED, send=SENT, pin=OK)
    record = DeliveryRecord(message_id=5, last_confirmed_at="2026-09-09T20:00:00+00:00")

    outcome = deliver(
        "same", transport=transport, record=record, chat_id="c", thread_id=None, now=NOW
    )

    assert outcome.status == "unchanged"
    assert outcome.record.last_confirmed_at == NOW.isoformat()
    assert outcome.record.last_error is None
    assert transport.calls == ["edit:5"]


def test_lost_message_is_logged_loudly_and_recreated_with_lost_marker(caplog) -> None:
    transport = FakeTransport(edit=NOT_FOUND, send=SENT, pin=OK)
    record = DeliveryRecord(message_id=5, last_confirmed_at="2026-09-09T20:00:00+00:00")

    with caplog.at_level(logging.ERROR):
        outcome = deliver(
            "t", transport=transport, record=record, chat_id="c", thread_id=None, now=NOW
        )

    assert outcome.status == "recreated"
    assert outcome.record.message_id == 77
    assert outcome.record.lost_at == NOW.isoformat()
    assert outcome.record.recreated_at == NOW.isoformat()
    assert any("MISSING" in message for message in caplog.messages)


def test_lost_message_without_recreate_keeps_loud_state() -> None:
    transport = FakeTransport(edit=NOT_FOUND, send=SENT, pin=OK)
    record = DeliveryRecord(message_id=5, last_confirmed_at="2026-09-09T20:00:00+00:00")

    outcome = deliver(
        "t",
        transport=transport,
        record=record,
        chat_id="c",
        thread_id=None,
        now=NOW,
        recreate_on_loss=False,
    )

    assert outcome.status == "lost"
    assert outcome.record.last_error == "lost"
    assert outcome.record.message_id == 5
    assert transport.calls == ["edit:5"]


def test_transient_failure_keeps_confirmation_time_and_does_not_retry() -> None:
    transport = FakeTransport(
        edit=TransportResult(False, error="rate_limited", retry_after=30), send=SENT, pin=OK
    )
    record = DeliveryRecord(message_id=5, last_confirmed_at="2026-09-09T20:00:00+00:00")

    outcome = deliver("t", transport=transport, record=record, chat_id="c", thread_id=None, now=NOW)

    assert outcome.status == "failed"
    assert outcome.record.last_confirmed_at == "2026-09-09T20:00:00+00:00"
    assert outcome.record.last_error == "rate_limited"
    assert transport.calls == ["edit:5"]


def test_created_but_unpinned_is_reported() -> None:
    transport = FakeTransport(edit=OK, send=SENT, pin=TransportResult(False, error="forbidden"))

    outcome = deliver(
        "t", transport=transport, record=DeliveryRecord(), chat_id="c", thread_id=None, now=NOW
    )

    assert outcome.status == "created"
    assert outcome.record.message_id == 77
    assert outcome.record.last_error == "pin_failed"


def test_bot_api_error_classes() -> None:
    assert classify_bot_api_error(400, "Bad Request: message is not modified") == "not_modified"
    assert classify_bot_api_error(400, "Bad Request: message to edit not found") == "not_found"
    assert classify_bot_api_error(429, "Too Many Requests: retry after 5") == "rate_limited"
    assert classify_bot_api_error(403, "Forbidden: bot was kicked") == "forbidden"
    assert classify_bot_api_error(400, "Bad Request: chat not found") == "bad_request"
    assert classify_bot_api_error(502, "") == "unknown"


def test_bot_api_response_parsing_never_raises() -> None:
    ok = parse_bot_api_response(200, b'{"ok": true, "result": {"message_id": 12}}')
    assert ok.ok and ok.message_id == 12
    limited = parse_bot_api_response(
        429, b'{"ok": false, "description": "Too Many Requests", "parameters": {"retry_after": 7}}'
    )
    assert limited.error == "rate_limited" and limited.retry_after == 7
    assert parse_bot_api_response(502, b"<html>bad gateway</html>").error == "transport"
    assert parse_bot_api_response(200, b'{"ok": true, "result": true}').message_id is None


def test_store_round_trip_and_corrupt_file(tmp_path: Path) -> None:
    store = DeliveryStore(tmp_path / "state" / "delivery.json")
    assert store.load() == DeliveryRecord()
    record = DeliveryRecord(message_id=3, last_confirmed_at=NOW.isoformat(), text_hash="abc")
    store.save(record)
    assert store.load() == record
    assert json.loads(store.path.read_text(encoding="utf-8"))["message_id"] == 3

    store.path.write_text("{not json", encoding="utf-8")
    assert store.load() == DeliveryRecord()
    store.path.write_text(json.dumps({"message_id": "12"}), encoding="utf-8")
    assert store.load().message_id is None
