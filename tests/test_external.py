"""External limit sources (``external.py``): contract 1 over loopback HTTP.

The contract is the public extension point of 0.8.0: any local process can answer it, and the
plugin knows nothing about who does. These tests pin the parsing of the contract, the guards on
the configured entry (loopback only, a key never from a bot token variable), and the HTTP read
itself against a real local server (no redirect, a size cap, the source's own timeout).
"""

from __future__ import annotations

import hashlib
import json
import re
import sys
import threading
import time
from collections.abc import Iterator
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from itertools import pairwise
from types import ModuleType
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


def test_the_reference_answer_parses_into_a_payload_item() -> None:
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


def test_another_contract_version_is_no_data_with_a_fixed_reason() -> None:
    """The reason never quotes the value it refuses (Codex re-check of 1.1.0): another number
    is an unsupported contract, anything else is the wrong shape."""
    item = parse_contract({**REFERENCE_ANSWER, "contract": 2}, now=NOW)

    assert item["status"] == "unavailable" and item["reason"] == "unsupported contract"
    for odd in (True, "1", "v2", 1.5, None, [1], {"version": 1}):
        refused = parse_contract({**REFERENCE_ANSWER, "contract": odd}, now=NOW)
        assert (refused["status"], refused["reason"]) == ("unavailable", "answer shape: contract")


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

    item = fetch_item(source, now=NOW, get=get, environ={"LIMITS_SOURCE_KEY": "k-123456"})

    assert item["status"] == "available"
    assert get.calls == [
        (URL, {"Accept": "application/json", "Authorization": "Bearer k-123456"}, 90.0)
    ]
    missing = fetch_item(source, now=NOW, get=get, environ={})
    assert missing["reason"] == "LIMITS_SOURCE_KEY not set" and len(get.calls) == 1


class UnscopedSecretError(RuntimeError):
    """Stands in for the engine's class of the same name (agent/secret_scope.py)."""


class _NarrowerUnscopedError(UnscopedSecretError):
    """A subclass the engine might raise one day: still an unscoped read."""


def _engine_scope(
    monkeypatch: pytest.MonkeyPatch, get_secret: Any, *, expose_class: bool = True
) -> None:
    """Install a fake ``agent.secret_scope``; ``import_module`` finds the leaf in sys.modules."""
    module = ModuleType("agent.secret_scope")
    if expose_class:
        module.UnscopedSecretError = UnscopedSecretError  # type: ignore[attr-defined]
    if get_secret is not None:
        module.get_secret = get_secret  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "agent.secret_scope", module)


def test_the_key_goes_through_the_engines_get_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    asked: list[tuple[str, str]] = []

    def get_secret(name: str, default: str = "") -> str:
        asked.append((name, default))
        return "scoped-key"

    _engine_scope(monkeypatch, get_secret)
    monkeypatch.setenv("LIMITS_SOURCE_KEY", "process-key")
    get = RecordingGet()

    item = fetch_item(_source(key_env="LIMITS_SOURCE_KEY"), now=NOW, get=get)

    assert item["status"] == "available"
    assert asked == [("LIMITS_SOURCE_KEY", "")]
    assert get.calls[0][1]["Authorization"] == "Bearer scoped-key"


def test_an_unscoped_read_is_no_data_without_a_request(monkeypatch: pytest.MonkeyPatch) -> None:
    def get_secret(name: str, default: str = "") -> str:
        raise UnscopedSecretError("restart the gateway with --secret-scope, please")

    _engine_scope(monkeypatch, get_secret)
    get = RecordingGet()

    item = fetch_item(_source(key_env="LIMITS_SOURCE_KEY"), now=NOW, get=get)

    assert get.calls == []
    assert item["reason"] == "key not readable outside a profile scope"
    assert "restart" not in str(item)


def test_a_subclass_of_the_engine_s_unscoped_error_is_an_unscoped_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Matched by the engine's own class, not by the name of the class raised (Codex review)."""

    def get_secret(name: str, default: str = "") -> str:
        raise _NarrowerUnscopedError("no scope")

    _engine_scope(monkeypatch, get_secret)
    get = RecordingGet()

    item = fetch_item(_source(key_env="LIMITS_SOURCE_KEY"), now=NOW, get=get)

    assert get.calls == []
    assert item["reason"] == "key not readable outside a profile scope"


def _raising_lookalike(name: str, default: str = "") -> str:
    """get_secret raising a class that is not the engine's but bears its name."""
    raise type("UnscopedSecretError", (RuntimeError,), {})("no scope")


def test_a_lookalike_of_the_engine_s_class_is_another_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Codex re-check: where the module exposes the class, only it (or a subclass) is an
    unscoped read; another class of the same name is a failed lookup named by its class."""
    _engine_scope(monkeypatch, _raising_lookalike)
    get = RecordingGet()

    item = fetch_item(_source(key_env="LIMITS_SOURCE_KEY"), now=NOW, get=get)

    assert get.calls == []
    assert item["reason"] == "key lookup failed: UnscopedSecretError"


def test_without_the_class_in_the_module_the_class_name_decides(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _engine_scope(monkeypatch, _raising_lookalike, expose_class=False)
    get = RecordingGet()

    item = fetch_item(_source(key_env="LIMITS_SOURCE_KEY"), now=NOW, get=get)

    assert get.calls == []
    assert item["reason"] == "key not readable outside a profile scope"


def test_the_real_engine_refuses_an_unscoped_read_and_reads_the_environment_otherwise(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Against the engine's own ``agent.secret_scope`` where it is importable (skipped without
    the engine, as in CI). Multiplexing on and no scope installed: the fixed reason and no
    request. Multiplexing off: the process environment reaches the header through get_secret."""
    scope = pytest.importorskip("agent.secret_scope")
    assert not scope.is_multiplex_active() and scope.current_secret_scope() is None
    monkeypatch.setenv("LIMITS_SOURCE_KEY", "process-key")
    get = RecordingGet()
    source = _source(key_env="LIMITS_SOURCE_KEY")

    scope.set_multiplex_active(True)
    try:
        refused = fetch_item(source, now=NOW, get=get)
    finally:
        scope.set_multiplex_active(False)
    read = fetch_item(source, now=NOW, get=get)

    assert refused["reason"] == "key not readable outside a profile scope"
    assert "process-key" not in str(refused)
    assert read["status"] == "available"
    assert [call[1]["Authorization"] for call in get.calls] == ["Bearer process-key"]


def test_another_failure_of_get_secret_names_only_the_class(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def get_secret(name: str, default: str = "") -> str:
        raise ValueError("secret detail that must not leak")

    get = RecordingGet()
    source = _source(key_env="LIMITS_SOURCE_KEY")
    _engine_scope(monkeypatch, get_secret)

    failed = fetch_item(source, now=NOW, get=get)
    _engine_scope(monkeypatch, None)
    absent = fetch_item(source, now=NOW, get=get)

    assert get.calls == []
    assert failed["reason"] == "key lookup failed: ValueError"
    assert absent["reason"] == "key lookup failed: AttributeError"
    assert "leak" not in str(failed)


def test_without_the_engine_module_the_process_environment_is_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setitem(sys.modules, "agent.secret_scope", None)
    monkeypatch.setenv("LIMITS_SOURCE_KEY", "process-key")
    get = RecordingGet()
    source = _source(key_env="LIMITS_SOURCE_KEY")

    item = fetch_item(source, now=NOW, get=get)

    assert item["status"] == "available"
    assert get.calls[0][1]["Authorization"] == "Bearer process-key"
    monkeypatch.delenv("LIMITS_SOURCE_KEY")
    assert fetch_item(source, now=NOW, get=get)["reason"] == "LIMITS_SOURCE_KEY not set"
    assert len(get.calls) == 1


def test_a_missing_module_inside_the_engine_does_not_fall_back_to_the_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The engine is there but one of its own imports is not (``hermes_constants``): a failed
    lookup, not the standalone CLI. The process environment is not read."""
    real_import = external.importlib.import_module

    def import_module(name: str, package: str | None = None) -> ModuleType:
        if name == "agent.secret_scope":
            raise ModuleNotFoundError("No module named 'hermes_constants'", name="hermes_constants")
        return real_import(name, package)

    monkeypatch.setattr(external.importlib, "import_module", import_module)
    monkeypatch.setenv("LIMITS_SOURCE_KEY", "process-key")
    get = RecordingGet()

    item = fetch_item(_source(key_env="LIMITS_SOURCE_KEY"), now=NOW, get=get)

    assert get.calls == []
    assert item["reason"] == "key lookup failed: ModuleNotFoundError"
    assert "process-key" not in str(item)


@pytest.mark.parametrize(
    "key", ["sk-0123\r4567", "sk-0123\n4567", "sk-0123 4567"], ids=["cr", "lf", "space"]
)
def test_a_key_a_header_cannot_carry_is_never_sent(
    monkeypatch: pytest.MonkeyPatch, key: str
) -> None:
    """http.client puts the whole header value, ``Bearer <key>``, into the text of the error it
    raises for such a key (Codex review, 1.1.0): the key is refused before any request, with a
    reason that names no part of it, whether it came from the environment or from get_secret."""
    get = RecordingGet()
    source = _source(key_env="LIMITS_SOURCE_KEY")

    given = fetch_item(source, now=NOW, get=get, environ={"LIMITS_SOURCE_KEY": key})
    _engine_scope(monkeypatch, lambda name, default="": key)
    scoped = fetch_item(source, now=NOW, get=get)

    assert get.calls == []
    for item in (given, scoped):
        assert item["reason"] == "key has characters a header cannot carry"
        assert "sk-0123" not in str(item) and "4567" not in str(item)


@pytest.mark.parametrize("key", ["k", "key", "1", "1234567"])
def test_a_key_shorter_than_8_characters_is_never_sent(
    monkeypatch: pytest.MonkeyPatch, key: str
) -> None:
    """Codex re-check 3: a key of fewer than 8 characters is refused before any request,
    whether it came from the environment or from get_secret. Every key that is sent has at
    least 8 characters, which the check for pieces of the key in displayed text relies on."""
    get = RecordingGet()
    source = _source(key_env="LIMITS_SOURCE_KEY")

    given = fetch_item(source, now=NOW, get=get, environ={"LIMITS_SOURCE_KEY": key})
    _engine_scope(monkeypatch, lambda name, default="": key)
    scoped = fetch_item(source, now=NOW, get=get)

    assert get.calls == []
    assert given["reason"] == scoped["reason"] == "key shorter than 8 characters"


def test_a_key_of_8_characters_is_sent() -> None:
    get = RecordingGet()

    item = fetch_item(
        _source(key_env="LIMITS_SOURCE_KEY"),
        now=NOW,
        get=get,
        environ={"LIMITS_SOURCE_KEY": "12345678"},
    )

    assert item["status"] == "available"
    assert get.calls[0][1]["Authorization"] == "Bearer 12345678"


def test_a_failed_request_with_a_key_names_only_the_exception_class() -> None:
    key = "sk-0123456789abcdef"

    def get(url: str, headers: dict[str, str], timeout: float) -> tuple[int, bytes]:
        raise ValueError(f"Invalid header value {headers['Authorization'].encode()!r}")

    item = fetch_item(
        _source(key_env="LIMITS_SOURCE_KEY"), now=NOW, get=get, environ={"LIMITS_SOURCE_KEY": key}
    )

    assert item["reason"] == "request failed: ValueError"
    assert "sk-0123" not in str(item) and "Bearer" not in str(item)


@pytest.mark.parametrize("bare", [False, True], ids=["header", "bare-key"])
def test_a_get_that_takes_the_header_away_still_gets_the_class_only(bare: bool) -> None:
    """Codex re-check: whether the request carried the key is decided before ``get`` runs, and
    ``get`` gets a copy of the headers. A ``get`` that pops the header and raises with its value
    cannot turn the reason back into the error's text."""
    key = "kq7Zr4p9XwLm2Vt8"
    passed: list[dict[str, str]] = []

    def get(url: str, headers: dict[str, str], timeout: float) -> tuple[int, bytes]:
        passed.append(headers)
        value = headers.pop("Authorization")
        raise ValueError(value.removeprefix("Bearer ") if bare else value)

    item = fetch_item(
        _source(key_env="LIMITS_SOURCE_KEY"), now=NOW, get=get, environ={"LIMITS_SOURCE_KEY": key}
    )

    assert item["reason"] == "request failed: ValueError"
    assert key[:6] not in str(item) and "Bearer" not in str(item)
    assert len(passed) == 1


def echoing_answers(key: str) -> dict[str, dict[str, Any]]:
    """Contract-1 answers from a source that echoes the key it was sent, in a reason, a plan,
    a window's label and scope."""
    return {
        "unavailable": {
            "contract": 1,
            "provider": "Claude",
            "state": "unavailable",
            "reason": f"denied for Bearer {key}",
            "plan": f"Pro {key}",
            "windows": [],
        },
        "ok": {
            "contract": 1,
            "provider": "Claude",
            "state": "ok",
            "plan": key,
            "windows": [{"label": f"w {key}", "used_percent": 5, "scope": "m" * 16 + key}],
        },
    }


def malformed_answers(key: str) -> dict[str, tuple[dict[str, Any], str | None]]:
    """Answers whose control fields hold the key, each with the fixed reason it gives (None: the
    answer stands, the field is dropped)."""
    base = {"contract": 1, "provider": "Claude", "state": "ok", "windows": []}
    return {
        "contract-object": ({**base, "contract": {key: 0}}, "answer shape: contract"),
        "contract-text": ({**base, "contract": key}, "answer shape: contract"),
        "state": ({**base, "state": key}, "answer shape: state"),
        "severity": ({**base, "windows": [{"used_percent": 5, "severity": key}]}, None),
    }


@pytest.mark.parametrize("name", list(malformed_answers("k")))
def test_a_control_field_holding_the_key_gives_a_fixed_reason(name: str) -> None:
    """Codex re-check: a reason about a malformed control field never quotes it, so a key in a
    contract object, a contract string, a state or a severity cannot reach the item."""
    key = "kq7Zr4p9XwLm2Vt8"
    answer, reason = malformed_answers(key)[name]

    item = fetch_item(
        _source(key_env="LIMITS_SOURCE_KEY"),
        now=NOW,
        get=RecordingGet(body=json.dumps(answer).encode("utf-8")),
        environ={"LIMITS_SOURCE_KEY": key},
    )

    assert item["reason"] == reason
    if reason is None:
        assert item["status"] == "available" and item["windows"][0]["severity"] is None
    assert key[:6] not in json.dumps(item)


def test_a_keyed_source_cannot_echo_its_key_into_the_item() -> None:
    """Codex re-check: a source that was sent the key can answer with it. Every occurrence of
    the key (and ``Bearer <key>``) is replaced by ``[key]`` before anything is cleaned or cut,
    so no cut leaves a prefix of it either. A source without a key is read as it answers."""
    key = "kq7Zr4p9XwLm2Vt8"
    source = _source(key_env="LIMITS_SOURCE_KEY")
    items = {
        name: fetch_item(
            source,
            now=NOW,
            get=RecordingGet(body=json.dumps(answer).encode("utf-8")),
            environ={"LIMITS_SOURCE_KEY": key},
        )
        for name, answer in echoing_answers(key).items()
    }

    for item in items.values():
        assert key[:6] not in json.dumps(item)
    assert items["unavailable"]["reason"] == "denied for [key]"
    assert items["unavailable"]["plan"] == "Pro [key]"
    assert items["ok"]["plan"] == "[key]"
    assert items["ok"]["windows"][0]["label"] == "w [key]"
    assert items["ok"]["windows"][0]["scope"] == "m" * 16 + "[key]"
    unkeyed = RecordingGet(body=json.dumps(echoing_answers(key)["ok"]).encode("utf-8"))
    assert fetch_item(_source(), now=NOW, get=unkeyed)["plan"] == key


# Protocol words long enough to be a key (a key has at least 8 characters, Codex re-check 3).
PROTOCOL_WORD_KEYS = ["critical", "unavailable", "login_expired"]


def _protocol_view(item: dict[str, Any]) -> tuple[Any, ...]:
    """What the control fields made of an answer: status, stamps, and per window the number,
    the reset and the severity."""
    windows = [(w["used_percent"], w["reset_at"], w["severity"]) for w in item["windows"]]
    return item["status"], item["fetched_at"], item["login_expires_at"], windows


def _shown_strings(item: dict[str, Any]) -> list[str]:
    windows = item["windows"]
    texts = [item["provider"], item["plan"], item["reason"]]
    texts += [w["label"] for w in windows] + [w["scope"] for w in windows]
    return [text for text in texts if isinstance(text, str)]


@pytest.mark.parametrize("key", PROTOCOL_WORD_KEYS)
def test_a_key_that_is_a_protocol_word_leaves_a_valid_answer_valid(key: str) -> None:
    """Codex re-check, a regression of wave 2: the control fields are checked as received and
    only displayed strings are kept free of the key. A key that is a severity or a state word
    leaves the state, the windows and the severities as they are without a key."""
    answers = [
        {**REFERENCE_ANSWER, "plan": f"Max {key}"},
        {**REFERENCE_ANSWER, "state": "unavailable", "reason": "maintenance", "plan": f"Max {key}"},
        {**REFERENCE_ANSWER, "state": "login_expired", "plan": f"Max {key}"},
    ]
    environ = {"LIMITS_SOURCE_KEY": key}
    statuses = []

    for answer in answers:
        body = json.dumps(answer).encode("utf-8")
        keyed = fetch_item(
            _source(key_env="LIMITS_SOURCE_KEY"),
            now=NOW,
            get=RecordingGet(body=body),
            environ=environ,
        )
        plain = fetch_item(_source(), now=NOW, get=RecordingGet(body=body))

        statuses.append(keyed["status"])
        assert _protocol_view(keyed) == _protocol_view(plain)
        assert keyed["plan"] == "Max [key]"
        shown = [text.casefold() for text in _shown_strings(keyed)]
        assert not [run for run in key_runs(key) for text in shown if run.casefold() in text]
    assert statuses == ["available", "unavailable", "expired"]


PIECE_KEY = "kq7Zr4p9XwLm2Vt8"
# Header-safe (no whitespace, no control character), with characters markup rewrites.
MARKUP_KEY = 'Ab<c&d"e12345678'
HIDDEN = "[key hidden]"


def _answer(**fields: Any) -> bytes:
    return json.dumps({"contract": 1, "provider": "Claude", **fields}).encode("utf-8")


def partial_echoes() -> dict[str, tuple[str, bytes]]:
    """``(key, body)`` of answers that echo a piece of the key, or the key in a form an exact
    match before cleaning cannot see, or not at all (``honest``)."""
    k = PIECE_KEY
    escaped = "".join(f"\\u{ord(ch):04x}" for ch in k)
    return {
        "prefix": (k, _answer(state="unavailable", reason=f"invalid key {k[:8]}...")),
        "fragment": (
            k,
            _answer(
                state="ok", windows=[{"label": k[4:16], "scope": f"m{k[2:12]}", "used_percent": 5}]
            ),
        ),
        "ansi-split": (k, _answer(state="unavailable", reason=f"denied {k[:5]}\x1b[0m{k[5:]}")),
        "markup-exact": (MARKUP_KEY, _answer(state="unavailable", reason=f"denied {MARKUP_KEY}")),
        "markup-piece": (
            MARKUP_KEY,
            _answer(state="unavailable", reason=f"denied {MARKUP_KEY[:9]}"),
        ),
        "json-escape": (
            k,
            b'{"contract": 1, "provider": "Claude", "state": "unavailable", "reason": "denied '
            + escaped.encode("ascii")
            + b'"}',
        ),
        "honest": (k, _answer(state="unavailable", reason="upstream busy, retry later")),
    }


def key_runs(key: str) -> set[str]:
    """Every 8 characters of the key in a row."""
    return {key[i : i + 8] for i in range(len(key) - 7)}


def strings_in(value: object) -> list[str]:
    """Every string in a decoded JSON value, keys included."""
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        return [s for k, v in value.items() for s in (*strings_in(k), *strings_in(v))]
    if isinstance(value, list):
        return [s for part in value for s in strings_in(part)]
    return []


_PIECE_EXPECTED = {
    "prefix": ("reason", HIDDEN),
    "fragment": ("window", (HIDDEN, HIDDEN)),
    "ansi-split": ("reason", HIDDEN),
    "markup-exact": ("reason", "denied [key]"),
    "markup-piece": ("reason", HIDDEN),
    "json-escape": ("reason", "denied [key]"),
    "honest": ("reason", "upstream busy, retry later"),
}


@pytest.mark.parametrize("name", list(_PIECE_EXPECTED))
def test_a_piece_of_the_key_in_a_displayed_string_hides_the_whole_field(name: str) -> None:
    """Codex re-check: honest services print part of a token, and cleaning can change a
    string. After cleaning and cutting, a displayed string that still holds 8 characters of the
    key in a row is replaced as a whole; an exact echo is ``[key]`` before that; an honest
    reason passes unchanged."""
    key, body = partial_echoes()[name]

    item = fetch_item(
        _source(key_env="LIMITS_SOURCE_KEY"),
        now=NOW,
        get=RecordingGet(body=body),
        environ={"LIMITS_SOURCE_KEY": key},
    )

    field, expected = _PIECE_EXPECTED[name]
    if field == "window":
        assert (item["windows"][0]["label"], item["windows"][0]["scope"]) == expected
    else:
        assert item["reason"] == expected
    assert not [run for run in key_runs(key) for text in strings_in(item) if run in text]


def test_an_answer_nested_too_deep_is_no_data_and_never_raises() -> None:
    """An answer nested deeper than the interpreter's json takes is "no data" with the reason,
    never an exception, keyed or not; a json that takes such a depth (newer interpreters guard
    the C stack instead of counting) sees a list, not an object."""
    body = b"[" * 30000 + b"]" * 30000
    environ = {"LIMITS_SOURCE_KEY": "kq7Zr4p9XwLm2Vt8"}
    get = RecordingGet(body=body)

    keyed = fetch_item(_source(key_env="LIMITS_SOURCE_KEY"), now=NOW, get=get, environ=environ)
    unkeyed = fetch_item(_source(), now=NOW, get=get)

    for item in (keyed, unkeyed):
        assert item["reason"] in ("answer shape: nested too deep", "answer shape: not an object")


def test_a_key_of_digits_never_reaches_a_reason_through_a_number() -> None:
    """The reason for another contract number is fixed text: a number that holds a key of
    digits does not reach it."""
    key = "48151623"
    body = json.dumps({"contract": 48151623, "provider": "Claude", "state": "ok"})

    item = fetch_item(
        _source(key_env="LIMITS_SOURCE_KEY"),
        now=NOW,
        get=RecordingGet(body=body.encode("utf-8")),
        environ={"LIMITS_SOURCE_KEY": key},
    )

    assert item["reason"] == "unsupported contract"
    assert "481516" not in json.dumps(item)


def test_a_failed_request_without_a_key_keeps_the_error_text() -> None:
    def get(url: str, headers: dict[str, str], timeout: float) -> tuple[int, bytes]:
        raise OSError("connection refused")

    item = fetch_item(_source(), now=NOW, get=get)

    assert item["reason"] == "request failed: connection refused"


def test_the_transport_s_own_error_for_a_key_names_only_its_class(server: _Server) -> None:
    """The real request path: http.client cannot encode a character outside latin-1 and names
    it in the error text. With a key in the request, only the class reaches the reason."""
    (source,) = read_sources([{"url": server.url(), "key_env": "LIMITS_SOURCE_KEY"}])

    item = fetch_item(source, now=NOW, environ={"LIMITS_SOURCE_KEY": "sk-0123й"})

    assert item["reason"] == "request failed: UnicodeEncodeError"
    assert "sk-0123" not in str(item) and "й" not in str(item) and "0439" not in str(item)
    assert server.seen == []  # type: ignore[attr-defined]


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

    item = fetch_item(source, now=NOW, environ={"LIMITS_SOURCE_KEY": "k-123456"})

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


# The names of the author's own source and its port, as SHA-256 of the lower-cased word: the
# check must not put the names into the public repository itself.
_UNNAMED = frozenset(
    {
        "b9ae76a82c4bdab84c8c00cbf327b8d7620410580700fe7fe80f6e139e1dea49",
        "87e8cc3ee4a9da18bfd2fd2ba8f44fc7ff24122c4849321e78cd5ff9b0023429",
        "adfdf777372e4df2c571cb656de68b7be4dfa47fd6220498807842390242ce97",
    }
)
_TEXT_SUFFIXES = (".py", ".json", ".yaml", ".yml", ".md", ".toml", ".txt", ".cfg")


def _unnamed_words(text: str) -> set[str]:
    """Every word and every pair of neighbours joined by ``-`` or ``_`` whose digest is listed."""
    words = re.findall(r"[a-z0-9]+", text.lower())
    pairs = {f"{a}{sep}{b}" for a, b in pairwise(words) for sep in "-_"}
    return {
        word
        for word in {*words, *pairs}
        if hashlib.sha256(word.encode("utf-8")).hexdigest() in _UNNAMED
    }


def test_the_plugin_knows_no_particular_source() -> None:
    """Decision of 29.09: the public repository carries nothing about the author's own source,
    in the plugin, the README, the tests or anywhere else in the tree."""
    root = PLUGIN_DIR.parents[1]
    files = [
        path
        for path in root.rglob("*")
        if path.is_file()
        and path.suffix in _TEXT_SUFFIXES
        and not {".git", "__pycache__", ".venv", ".mypy_cache", ".ruff_cache"} & set(path.parts)
    ]

    assert len(files) > 20, "the scan found too few files to mean anything"
    for path in files:
        found = _unnamed_words(path.read_text(encoding="utf-8"))
        assert not found, f"{path.relative_to(root)}: {len(found)} unnamed word(s)"


def test_only_the_external_module_reads_a_source_key() -> None:
    for path in PLUGIN_DIR.rglob("*.py"):
        if "__pycache__" in path.parts or path.name == "external.py":
            continue
        assert "key_env]" not in path.read_text(encoding="utf-8"), path.name
    assert "environ" in (PLUGIN_DIR / "telegram_dashboard" / "external.py").read_text(
        encoding="utf-8"
    )


# ---------------------------------------------------------------- the published contract

README = PLUGIN_DIR.parents[1] / "README.md"


def _readme_section(heading: str) -> str:
    body = README.read_text(encoding="utf-8")
    start = body.index(f"\n{heading}\n") + 1
    following = re.search(r"^#{1,3} ", body[start + len(heading) :], flags=re.M)
    end = start + len(heading) + following.start() if following else len(body)
    return body[start:end]


def _fenced(section: str, language: str) -> list[str]:
    return re.findall(rf"```{language}\n(.*?)```", section, flags=re.S)


def test_the_readme_example_is_an_answer_the_plugin_reads() -> None:
    """The contract is published in the README; its example answer must be one the parser takes
    whole, or the README teaches a shape the plugin refuses."""
    section = _readme_section("### External limit sources")
    answers = [json.loads(block) for block in _fenced(section, "json")]

    assert answers, "no JSON example in the section"
    item = parse_contract(answers[0], now=NOW)
    assert item["status"] == "available", item["reason"]
    assert item["provider"] and item["plan"] and item["login_expires_at"]
    assert any(window["scope"] for window in item["windows"])
    assert all(window["used_percent"] is not None for window in item["windows"])
    assert set(answers[0]) == set(REFERENCE_ANSWER)
    for window in answers[0]["windows"]:
        assert set(window) == set(REFERENCE_ANSWER["windows"][0])


def test_the_readme_example_setting_is_one_the_plugin_accepts() -> None:
    """The setting as ``config.yaml`` carries it. CI installs no YAML parser (the plugin needs
    none), so there this one skips; the contract test above runs everywhere."""
    yaml = pytest.importorskip("yaml")

    section = _readme_section("### External limit sources")
    setting = yaml.safe_load(_fenced(section, "yaml")[0])
    sources = read_sources(setting["limits_sources"])

    assert sources and all(source.problem is None for source in sources)


def test_the_readme_names_the_guards() -> None:
    """The refused key variables, the loopback rule and the tick's deadline are part of the
    contract a source author reads."""
    section = _readme_section("### External limit sources")

    for name in sorted(external.BOT_TOKEN_VARIABLES):
        assert f"`{name}`" in section
    assert f"`{external.BOT_TOKEN_SUFFIX}`" in section
    assert f"{external.TICK_TIMEOUT_SECONDS:.0f} s" in section
    assert f"{external.MAX_BODY_BYTES // 1024} KB" in section
    assert "`127.0.0.1`" in section and "`localhost`" in section and "`[::1]`" in section
    assert f"up to {external.MAX_SOURCES} sources" in section
    assert f"up to {external.MAX_WINDOWS}" in section


def test_the_settings_table_lists_limits_sources() -> None:
    rows = [line for line in README.read_text(encoding="utf-8").splitlines() if "| `" in line]

    assert any(line.lstrip().startswith("| `limits_sources` |") for line in rows)
