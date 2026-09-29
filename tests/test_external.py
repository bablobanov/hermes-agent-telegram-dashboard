"""External limit sources (``external.py``): contract 1 over loopback HTTP.

The contract is the public extension point of 0.8.0: any local process can answer it, and the
plugin knows nothing about who does. These tests pin the parsing of the contract, the guards on
the configured entry (loopback only, a key never from a bot token variable), and the HTTP read
itself against a real local server (no redirect, a size cap, the source's own timeout).
"""

from __future__ import annotations

import json
import re
import threading
import time
from collections.abc import Iterator
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import pytest
from probe_fakes import PLUGIN_DIR

from telegram_dashboard import external
from telegram_dashboard.external import Source, fetch_item, parse_contract, read_sources

NOW = datetime(2026, 9, 29, 7, 0, tzinfo=UTC)
URL = "http://127.0.0.1:18080/v1/usage"

# An answer captured from a real contract-1 source on 2026-09-28 (the author's own reference
# implementation, fed a fake usage API), verbatim but for the two values that depend on the moment
# of capture (``fetched_at``) and on the fake login (``login_expires_at``). The two sides of the
# contract cannot drift apart unnoticed.
REFERENCE_ANSWER: dict[str, Any] = {
    "contract": 1,
    "provider": "Claude",
    "state": "ok",
    "reason": None,
    "plan": "Max 5x",
    "login_expires_at": "2026-10-27T21:07:24Z",
    "fetched_at": "2026-09-29T06:59:30Z",
    "windows": [
        {
            "label": "session",
            "used_percent": 42,
            "resets_at": "2026-09-29T09:00:00Z",
            "scope": None,
            "severity": "normal",
        },
        {
            "label": "week",
            "used_percent": 86,
            "resets_at": "2026-09-29T23:00:00Z",
            "scope": None,
            "severity": "warning",
        },
        {
            "label": "week",
            "used_percent": 100,
            "resets_at": "2026-09-29T23:00:00Z",
            "scope": "Fable",
            "severity": "critical",
        },
    ],
}


def _source(**fields: Any) -> Source:
    (source,) = read_sources([{"url": URL, **fields}])
    return source


# ---------------------------------------------------------------- the contract


def test_the_reference_answer_parses_into_an_item_like_grok_s_and_kimi_s() -> None:
    item = parse_contract(REFERENCE_ANSWER, now=NOW)

    assert item["status"] == "available" and item["reason"] is None
    assert item["provider"] == "Claude" and item["plan"] == "Max 5x"
    assert item["login_expires_at"] == "2026-10-27T21:07:24Z"
    assert item["fetched_at"] == "2026-09-29T06:59:30Z"
    assert item["windows"] == [
        {
            "label": "session",
            "used_percent": 42.0,
            "reset_at": "2026-09-29T09:00:00Z",
            "scope": None,
            "severity": "normal",
        },
        {
            "label": "week",
            "used_percent": 86.0,
            "reset_at": "2026-09-29T23:00:00Z",
            "scope": None,
            "severity": "warning",
        },
        {
            "label": "week",
            "used_percent": 100.0,
            "reset_at": "2026-09-29T23:00:00Z",
            "scope": "Fable",
            "severity": "critical",
        },
    ]


def test_another_contract_version_is_no_data_with_the_reason() -> None:
    item = parse_contract({**REFERENCE_ANSWER, "contract": 2}, now=NOW)

    assert item["status"] == "unavailable" and item["reason"] == "contract 2 not supported"
    assert (
        parse_contract({**REFERENCE_ANSWER, "contract": True}, now=NOW)["status"] == "unavailable"
    )


def test_extra_keys_are_ignored_and_strings_are_sanitized() -> None:
    answer = {
        **REFERENCE_ANSWER,
        "future_field": {"anything": 1},
        "plan": "Max\x1b[31m 5x",
        "windows": [{"used_percent": 150, "scope": "Bearer abcdefghijklmnop", "extra": 1}],
    }

    item = parse_contract(answer, now=NOW)

    assert item["status"] == "available" and item["plan"] == "Max 5x"
    assert item["windows"] == [
        {
            "label": "window",
            "used_percent": None,
            "reset_at": None,
            "scope": "[secret]",
            "severity": None,
        }
    ]


def test_states_login_expired_and_unavailable() -> None:
    expired = parse_contract({**REFERENCE_ANSWER, "state": "login_expired", "windows": []}, now=NOW)
    down = parse_contract(
        {**REFERENCE_ANSWER, "state": "unavailable", "reason": "runner busy", "windows": []},
        now=NOW,
    )
    odd = parse_contract({**REFERENCE_ANSWER, "state": "sleeping"}, now=NOW)

    assert (expired["status"], expired["plan"]) == ("expired", "Max 5x")
    assert expired["fetched_at"] == "2026-09-29T06:59:30Z"
    assert (down["status"], down["reason"]) == ("unavailable", "runner busy")
    assert down["login_expires_at"] == "2026-10-27T21:07:24Z", "a date survives a bad tick"
    assert (odd["status"], odd["reason"]) == ("unavailable", "answer shape: state")


def test_windows_are_capped_at_six_and_the_provider_is_required() -> None:
    many = {**REFERENCE_ANSWER, "windows": [{"used_percent": n} for n in range(9)]}

    assert len(parse_contract(many, now=NOW)["windows"]) == 6
    nameless = parse_contract({**REFERENCE_ANSWER, "provider": " "}, now=NOW)
    assert nameless["reason"] == "answer without provider"
    long_name = parse_contract({**REFERENCE_ANSWER, "provider": "P" * 40}, now=NOW)
    assert len(long_name["provider"]) <= 24


def test_a_stamp_from_the_future_is_replaced_by_the_time_of_reading() -> None:
    item = parse_contract({**REFERENCE_ANSWER, "fetched_at": "2026-09-29T09:00:00Z"}, now=NOW)

    assert item["fetched_at"] == NOW.isoformat()


# ---------------------------------------------------------------- the configured entry


@pytest.mark.parametrize(
    "url",
    ["http://127.0.0.1:18080/v1/usage", "http://localhost:9000/x", "http://[::1]:9000/"],
)
def test_loopback_http_urls_are_accepted(url: str) -> None:
    (source,) = read_sources([{"url": url}])

    assert source.problem is None and source.timeout_seconds == 20.0


@pytest.mark.parametrize(
    ("url", "problem"),
    [
        ("https://127.0.0.1:18080/", "url must be http on loopback"),
        ("http://10.0.0.5:18080/", "url must be on loopback"),
        ("http://example.com/usage", "url must be on loopback"),
        ("http://user:pw@127.0.0.1:18080/", "url must not carry credentials"),
        ("http://127.0.0.1:99999/", "url unreadable"),
        ("", "url missing"),
    ],
)
def test_anything_but_loopback_http_is_refused(url: str, problem: str) -> None:
    (source,) = read_sources([{"url": url}])

    assert source.problem == problem


def test_the_setting_itself_and_its_limits() -> None:
    assert read_sources(None) == () and read_sources([]) == ()
    (whole,) = read_sources({"url": URL})
    assert whole.problem == "limits_sources must be a list"
    sources = read_sources([{"url": f"http://127.0.0.1:{9000 + n}/"} for n in range(6)])
    assert [s.problem for s in sources[4:]] == ["more than 4 sources"] * 2
    assert len({s.key for s in sources}) == 6, "one cache key per url"
    assert _source(timeout_seconds=500).timeout_seconds == 120.0
    assert _source(timeout_seconds=0).timeout_seconds == 1.0
    assert _source(timeout_seconds="soon").timeout_seconds == 20.0
    assert _source(key_env="MY KEY").problem == "key_env is not a variable name"


# ---------------------------------------------------------------- one attempt


class RecordingGet:
    def __init__(self, status: int = 200, body: bytes = b"") -> None:
        self.status = status
        self.body = body or json.dumps(REFERENCE_ANSWER).encode("utf-8")
        self.calls: list[tuple[str, dict[str, str], float]] = []

    def __call__(self, url: str, headers: dict[str, str], timeout: float) -> tuple[int, bytes]:
        self.calls.append((url, dict(headers), timeout))
        return self.status, self.body


def test_the_key_comes_from_the_named_variable_and_nowhere_else() -> None:
    get = RecordingGet()
    source = _source(key_env="LIMITS_SOURCE_KEY", timeout_seconds=90)

    item = fetch_item(source, now=NOW, get=get, environ={"LIMITS_SOURCE_KEY": "k-123"})

    assert item["status"] == "available"
    assert get.calls == [
        (URL, {"Accept": "application/json", "Authorization": "Bearer k-123"}, 90.0)
    ]
    missing = fetch_item(source, now=NOW, get=get, environ={})
    assert missing["reason"] == "LIMITS_SOURCE_KEY not set" and len(get.calls) == 1


def test_http_codes_size_and_json_shape() -> None:
    source = _source()

    assert fetch_item(source, now=NOW, get=RecordingGet(503))["reason"] == "HTTP 503"
    big = RecordingGet(body=b"{" + b" " * (64 * 1024) + b"}")
    assert fetch_item(source, now=NOW, get=big)["reason"] == "answer over 64 KB"
    assert fetch_item(source, now=NOW, get=RecordingGet(body=b"<html>"))["reason"] == (
        "answer shape: not JSON"
    )


def test_a_refused_entry_is_never_asked() -> None:
    get = RecordingGet()
    (source,) = read_sources([{"url": "http://10.0.0.5:1/"}])

    item = fetch_item(source, now=NOW, get=get)

    assert get.calls == [] and item["reason"] == "url must be on loopback"


# ---------------------------------------------------------------- the HTTP read itself


class _Server:
    def __init__(self, handler: type[BaseHTTPRequestHandler]) -> None:
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def url(self, path: str = "/v1/usage") -> str:
        return f"http://127.0.0.1:{self.httpd.server_address[1]}{path}"

    def close(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()


@pytest.fixture
def server() -> Iterator[_Server]:
    seen: list[str] = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args: object) -> None:
            pass

        def do_GET(self) -> None:
            seen.append(self.path)
            if self.path == "/moved":
                self.send_response(302)
                self.send_header("Location", "/v1/usage")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            if self.path == "/slow":
                time.sleep(1.0)
            body = json.dumps(REFERENCE_ANSWER).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    running = _Server(Handler)
    running.seen = seen  # type: ignore[attr-defined]
    yield running
    running.close()


def test_a_real_answer_over_loopback(server: _Server) -> None:
    (source,) = read_sources([{"url": server.url()}])

    item = fetch_item(source, now=NOW)

    assert item["status"] == "available" and item["provider"] == "Claude"


def test_a_redirect_is_not_followed(server: _Server) -> None:
    (source,) = read_sources([{"url": server.url("/moved"), "key_env": "LIMITS_SOURCE_KEY"}])

    item = fetch_item(source, now=NOW, environ={"LIMITS_SOURCE_KEY": "k-123"})

    assert item["reason"] == "HTTP 302"
    assert server.seen == ["/moved"], "the key must not travel on to another path"  # type: ignore[attr-defined]


def test_the_source_s_own_timeout_bounds_the_request(server: _Server) -> None:
    (source,) = read_sources([{"url": server.url("/slow"), "timeout_seconds": 1}])
    started = time.monotonic()

    item = fetch_item(
        source, now=NOW, get=lambda url, headers, _t: external.http_get(url, headers, 0.2)
    )

    assert time.monotonic() - started < 0.9
    assert item["status"] == "unavailable" and item["reason"].startswith("request failed")


# ---------------------------------------------------------------- nothing about one source


def test_the_plugin_knows_no_particular_source() -> None:
    """Decision of 29.09: the public plugin carries nothing about the author's own source."""
    files = [*PLUGIN_DIR.rglob("*.py"), PLUGIN_DIR.parents[1] / "README.md"]
    for path in files:
        if "__pycache__" in path.parts:
            continue
        text = path.read_text(encoding="utf-8")
        assert not re.search(r"claude-runner|claude_runner|\b8328\b", text), path.name


def test_only_the_external_module_reads_a_source_key() -> None:
    for path in PLUGIN_DIR.rglob("*.py"):
        if "__pycache__" in path.parts or path.name == "external.py":
            continue
        assert "key_env]" not in path.read_text(encoding="utf-8"), path.name
    assert "environ" in (PLUGIN_DIR / "telegram_dashboard" / "external.py").read_text(
        encoding="utf-8"
    )
