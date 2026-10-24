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
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import pytest

from telegram_dashboard import grok, kimi
from telegram_dashboard.quota_cache import tick

NOW = datetime(2026, 10, 2, 12, 0, tzinfo=UTC)
TOKEN = "planted-bearer-for-this-test-only"
BODY = b'{"ok": true}'


class _Server:
    """A loopback server that answers every GET alike and records its path and bearer."""

    def __init__(self, status: int, location: str | None = None) -> None:
        self.status = status
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
        # A short poll: ``shutdown`` waits for one, and every case starts two servers.
        threading.Thread(target=self.httpd.serve_forever, args=(0.05,), daemon=True).start()

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


@pytest.fixture(params=[301, 302, 303, 307, 308])
def servers(request: pytest.FixtureRequest) -> Iterator[tuple[_Server, _Server]]:
    """The second answers 200; the first redirects there, to another port: another host. Every
    status urllib would follow, so a handler that stops only some of them fails here."""
    second = _Server(200)
    first = _Server(request.param, location=second.url("/v1/usages"))
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
    assert item["status"] == "unavailable" and item["reason"] == f"HTTP {first.status}"
    assert TOKEN not in json.dumps(item)


@pytest.mark.parametrize("get", [grok.http_get, kimi.http_get], ids=["grok", "kimi"])
def test_an_answer_without_a_redirect_is_read_as_before(
    get: Callable[[str, dict[str, str]], tuple[int, str]],
) -> None:
    server = _Server(200)
    try:
        answer = get(server.url("/v1/usages"), {"Authorization": f"Bearer {TOKEN}"})
    finally:
        server.close()

    assert answer == (200, BODY.decode())
    assert server.seen == [("/v1/usages", f"Bearer {TOKEN}")]


# ----------------------------------------------------------------------------- the policy
#
# ``tick`` keeps every external limit source (``collect.external_tick``) to one attempt per
# interval. These are the policy's own tests.


class CountingFetch:
    def __init__(self, item: dict[str, Any]) -> None:
        self.item = item
        self.calls = 0

    def __call__(self, *, now: datetime) -> dict[str, Any]:
        self.calls += 1
        return {
            **self.item,
            "fetched_at": now.isoformat() if self.item["status"] == "available" else None,
        }


AVAILABLE = {
    "provider": "x",
    "status": "available",
    "reason": None,
    "source": "external",
    "fetched_at": None,
    "windows": [{"label": "7d", "used_percent": 27.0, "reset_at": "2026-10-07T19:25:30+00:00"}],
}
FAILED = {**AVAILABLE, "status": "unavailable", "reason": "HTTP 503", "windows": []}


def test_the_source_is_asked_once_per_interval_and_served_from_the_cache_between() -> None:
    fetch = CountingFetch(AVAILABLE)
    cache: dict[str, Any] = {}

    first = tick(cache, now=NOW, interval_seconds=900, fetch=fetch)
    second = tick(cache, now=NOW + timedelta(minutes=5), interval_seconds=900, fetch=fetch)
    third = tick(cache, now=NOW + timedelta(minutes=14), interval_seconds=900, fetch=fetch)
    fourth = tick(cache, now=NOW + timedelta(minutes=15), interval_seconds=900, fetch=fetch)

    assert fetch.calls == 2
    assert first["fetched_at"] == second["fetched_at"] == third["fetched_at"] == NOW.isoformat()
    assert fourth["fetched_at"] == (NOW + timedelta(minutes=15)).isoformat()
    assert cache["attempted_at"] == fourth["fetched_at"] and cache["item"] == fourth


def test_a_failed_attempt_stays_no_data_until_the_next_interval_not_the_next_tick() -> None:
    fetch = CountingFetch(FAILED)
    cache: dict[str, Any] = {}

    items = [
        tick(cache, now=NOW + timedelta(minutes=m), interval_seconds=900, fetch=fetch)
        for m in (0, 5, 10, 15)
    ]

    assert fetch.calls == 2  # minute 0 and minute 15, not every tick
    assert all(item["status"] == "unavailable" and item["reason"] == "HTTP 503" for item in items)


def test_a_cached_number_older_than_the_interval_is_not_a_number() -> None:
    """The guard behind the policy: whatever put an old number into the cache (a clock jump, a
    hand-edited state file), the screen does not show it under a fresh stamp."""
    old = {**AVAILABLE, "fetched_at": (NOW - timedelta(hours=2)).isoformat()}
    cache = {"attempted_at": NOW.isoformat(), "item": old}
    fetch = CountingFetch(AVAILABLE)

    item = tick(cache, now=NOW + timedelta(minutes=1), interval_seconds=900, fetch=fetch)

    assert fetch.calls == 0
    assert item["status"] == "unavailable" and item["reason"] == "cached number too old"
    assert item["windows"] == []
