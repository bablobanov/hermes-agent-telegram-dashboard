"""Minimal Bot API transport on the standard library. The token never enters logs or results."""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any

from .delivery import TransportResult, classify_bot_api_error

_API_BASE = "https://api.telegram.org"


class BotApiTransport:
    def __init__(self, token: str, *, timeout_seconds: float = 10.0) -> None:
        if not token or any(char.isspace() for char in token):
            raise ValueError("bot token is empty or malformed")
        self._base = f"{_API_BASE}/bot{token}/"
        self._timeout = timeout_seconds

    def send_message(self, *, chat_id: str, text: str, thread_id: str | None) -> TransportResult:
        payload: dict[str, Any] = {
            "chat_id": chat_id,
            "text": text,
            "parse_mode": "HTML",
            "disable_notification": True,
            "link_preview_options": {"is_disabled": True},
        }
        if thread_id:
            payload["message_thread_id"] = int(thread_id)
        return self._call("sendMessage", payload)

    def edit_message_text(self, *, chat_id: str, message_id: int, text: str) -> TransportResult:
        payload = {
            "chat_id": chat_id,
            "message_id": message_id,
            "text": text,
            "parse_mode": "HTML",
            "link_preview_options": {"is_disabled": True},
        }
        return self._call("editMessageText", payload)

    def pin_chat_message(self, *, chat_id: str, message_id: int) -> TransportResult:
        payload = {"chat_id": chat_id, "message_id": message_id, "disable_notification": True}
        return self._call("pinChatMessage", payload)

    def _call(self, method: str, payload: dict[str, Any]) -> TransportResult:
        body = json.dumps(payload).encode("utf-8")
        request = urllib.request.Request(
            self._base + method,
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self._timeout) as response:
                raw = response.read()
                status = response.status
        except urllib.error.HTTPError as exc:
            raw = exc.read()
            status = exc.code
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            return TransportResult(False, error="transport", detail=type(exc).__name__)
        return parse_bot_api_response(status, raw)


def parse_bot_api_response(status: int, raw: bytes) -> TransportResult:
    try:
        data = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return TransportResult(False, error="transport", detail=f"non-json response {status}")
    if not isinstance(data, dict):
        return TransportResult(False, error="transport", detail=f"unexpected body {status}")
    if data.get("ok") is True:
        result = data.get("result")
        message_id = result.get("message_id") if isinstance(result, dict) else None
        valid = isinstance(message_id, int) and not isinstance(message_id, bool)
        return TransportResult(True, message_id=message_id if valid else None)
    description = str(data.get("description") or "")
    raw_parameters = data.get("parameters")
    parameters: dict[str, Any] = raw_parameters if isinstance(raw_parameters, dict) else {}
    retry_after = parameters.get("retry_after")
    return TransportResult(
        False,
        error=classify_bot_api_error(status, description),
        retry_after=retry_after if isinstance(retry_after, int) else None,
        detail=description[:120],
    )
