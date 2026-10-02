"""The GET Grok and Kimi share, against real local servers.

Both requests carry the provider's bearer token. urllib's redirect handler copies every header but
the body's onto the next request, ``Authorization`` included, whatever host ``Location`` names. So
a redirect is not followed: the attempt is "no data" with ``HTTP 302`` and the host the redirect
names never sees the token (fix of 0.9.3; before it, both readers followed).
"""

from __future__ import annotations

import json
import threading
from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import pytest

from telegram_dashboard import grok, kimi

NOW = datetime(2026, 10, 2, 12, 0, tzinfo=UTC)
TOKEN = "planted-bearer-for-this-test-only"
BODY = b'{"ok": true}'


class _Server:
    """A loopback server that answers every GET alike and records its path and bearer."""

    def __init__(self, status: int, location: str | None = None) -> None:
        self.seen: list[tuple[str, str | None]] = []
        seen = self.seen

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args: object) -> None:
                pass

            def do_GET(self) -> None:
                seen.append((self.path, self.headers.get("Authorization")))
                body = BODY if status == 200 else b""
                self.send_response(status)
                if location:
                    self.send_header("Location", location)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def url(self, path: str) -> str:
        return f"http://127.0.0.1:{self.httpd.server_address[1]}{path}"

    def close(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()


@pytest.fixture(autouse=True)
def _no_proxy_for_loopback(monkeypatch: pytest.MonkeyPatch) -> None:
    """The shared GET keeps the environment's proxies; these servers are local."""
    for name in ("http_proxy", "HTTP_PROXY", "all_proxy", "ALL_PROXY"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("no_proxy", "*")


@pytest.fixture
def servers() -> Iterator[tuple[_Server, _Server]]:
    """The second answers 200; the first redirects there, to another port: another host."""
    second = _Server(200)
    first = _Server(302, location=second.url("/v1/usages"))
    yield first, second
    first.close()
    second.close()


def _grok_attempt(url: str, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    monkeypatch.setattr(grok, "BILLING_URL", url)
    return grok.fetch_item(now=NOW, resolve=lambda: TOKEN)


def _kimi_attempt(url: str, _monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    return kimi.fetch_item(now=NOW, resolve=lambda: (TOKEN, url.removesuffix(kimi.USAGES_PATH)))


Attempt = Callable[[str, pytest.MonkeyPatch], dict[str, Any]]


@pytest.mark.parametrize("attempt", [_grok_attempt, _kimi_attempt], ids=["grok", "kimi"])
def test_a_redirect_is_no_data_and_the_host_it_names_never_sees_the_token(
    attempt: Attempt, servers: tuple[_Server, _Server], monkeypatch: pytest.MonkeyPatch
) -> None:
    first, second = servers

    item = attempt(first.url("/v1/usages"), monkeypatch)

    assert second.seen == [], "the token must not travel on to the host the redirect names"
    assert first.seen == [("/v1/usages", f"Bearer {TOKEN}")]
    assert item["status"] == "unavailable" and item["reason"] == "HTTP 302"
    assert TOKEN not in json.dumps(item)


@pytest.mark.parametrize("get", [grok.http_get, kimi.http_get], ids=["grok", "kimi"])
def test_an_answer_without_a_redirect_is_read_as_before(
    get: Callable[[str, dict[str, str]], tuple[int, str]], servers: tuple[_Server, _Server]
) -> None:
    _first, second = servers

    assert get(second.url("/v1/usages"), {"Authorization": f"Bearer {TOKEN}"}) == (
        200,
        BODY.decode(),
    )
    assert second.seen == [("/v1/usages", f"Bearer {TOKEN}")]
