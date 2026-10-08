"""The memory section: the two notebooks against their limits, the approval queue, a provider.

The cases of the task card (an empty queue, a queue stuck longer than the threshold, a notebook
past the threshold, a provider configured and failing, no memory at all), an installation that
shares none of our paths, the engine's own count of a notebook, the config the engine reads
with its own parser or not at all, and the promise that no text of a notebook, a pending write
or a log line ever leaves: not the screen, not the HTML, not the snapshot, not the log.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from telegram_dashboard import memory
from telegram_dashboard.collect import CommandResult, collect_all
from telegram_dashboard.compat import Environment
from telegram_dashboard.memory import DELIMITER, notebook_entries, read_memory
from telegram_dashboard.render import render_dashboard, to_telegram_html
from telegram_dashboard.schema import MemoryNotebook, MemorySummary

NOW = datetime(2026, 10, 8, 12, 0, tzinfo=UTC)
SECRET = "SECRET-memory-entry-7f3a"


class _Parser:
    """The engine's YAML parser as a test means it: the document, or its refusal."""

    def __init__(self, document: object = None, error: Exception | None = None) -> None:
        self.document, self.error = document, error

    def safe_load(self, text: str) -> object:
        if self.error is not None:
            raise self.error
        return self.document


class _Runner:
    def run(self, argv: Any, *, timeout_seconds: float) -> CommandResult:
        return CommandResult(0, "", "")


def _config(
    monkeypatch: pytest.MonkeyPatch,
    home: Path,
    memory_section: dict[str, Any] | None = None,
    skills_section: dict[str, Any] | None = None,
) -> None:
    """A config.yaml on disk, read through a stand-in for the engine's parser."""
    document = {"memory": memory_section or {}, "skills": skills_section or {}}
    (home / "config.yaml").write_text("# read by a stand-in parser\n", encoding="utf-8")
    monkeypatch.setattr(memory, "yaml_parser", lambda: _Parser(document))


def _at(path: Path, moment: datetime) -> None:
    os.utime(path, (moment.timestamp(), moment.timestamp()))


def _notebook(home: Path, name: str, entries: list[str], at: datetime) -> Path:
    path = home / "memories" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(DELIMITER.join(entries), encoding="utf-8")
    _at(path, at)
    return path


def _pending(
    home: Path, subsystem: str, number: int, at: datetime, record: dict[str, Any] | None = None
) -> Path:
    """One queued write the engine's way: ``pending/<subsystem>/<8 hex>.json``."""
    path = home / "pending" / subsystem / f"{number:08x}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    body = record or {
        "id": f"{number:08x}",
        "subsystem": subsystem,
        "action": "add",
        "origin": "background_review",
        "created_at": at.timestamp(),
        "summary": f"add: {SECRET}",
        "payload": {"target": "user", "content": f"{SECRET} the user likes tea"},
    }
    path.write_text(json.dumps(body, ensure_ascii=False, indent=2), encoding="utf-8")
    _at(path, at)
    return path


def _home(tmp_path: Path) -> Path:
    home = tmp_path / "srv" / "hermes" / "profiles" / "work"
    home.mkdir(parents=True)
    return home


def _read(home: Path) -> tuple[MemorySummary, Any, tuple[Any, ...]]:
    return read_memory(Environment(hermes_home=home), now=NOW)


def _screen(home: Path) -> list[str]:
    snapshot = collect_all(
        Environment(hermes_home=home), _Runner(), now=NOW, resolve_limits=lambda: None
    )
    return render_dashboard(snapshot, now=NOW, zone=UTC).splitlines()


def _main(lines: list[str]) -> list[str]:
    return [line for line in lines if not line.startswith(">")]


# ------------------------------------------------------------------ the cases of the task


def test_an_empty_queue_with_the_gate_on_is_no_event_and_one_line_of_details(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = _home(tmp_path)
    _config(monkeypatch, home, {"write_approval": True}, {"write_approval": True})
    _notebook(home, "MEMORY.md", ["notes one", "notes two"], NOW - timedelta(hours=3))
    _notebook(home, "USER.md", ["likes tea"], NOW - timedelta(days=2))

    summary, source, incidents = _read(home)

    assert (summary.state, source.state, incidents) == ("observed", "fresh", ())
    lines = _screen(home)
    assert "> Memory: MEMORY.md 21/2,200 (0%) · USER.md 9/1,375 (0%)" in lines
    assert "> Memory queue: empty · last write Oct 8 09:00 · approval on" in lines
    assert "> Skills queue: empty · approval on" in lines
    assert not [line for line in _main(lines) if "Memory" in line or "Skills" in line]


def test_a_queue_stuck_longer_than_three_days_with_nothing_applied_is_an_event(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = _home(tmp_path)
    _config(monkeypatch, home, {"write_approval": "approve"})
    _notebook(home, "USER.md", ["likes tea"], NOW - timedelta(days=40))
    for number, days in enumerate((10, 6, 1)):
        _pending(home, "memory", number, NOW - timedelta(days=days))

    summary, _source, incidents = _read(home)

    (queue, _skills) = summary.queues
    assert (queue.count, queue.gate) == (3, True)
    assert [incident.title for incident in incidents] == ["Memory: 3 writes stuck 10d"]
    lines = _screen(home)
    assert "- Memory: 3 writes stuck 10d" in lines
    assert "> Memory queue: 3 waiting since Sep 28 · last write Aug 29 12:00 · approval on" in lines
    assert len("- Memory: 3 writes stuck 10d") <= 32


@pytest.mark.parametrize(
    ("oldest_days", "landed_days"),
    [(10, 2), (2, 40)],
    ids=["a write landed after the oldest was queued", "the oldest is younger than 3 days"],
)
def test_a_queue_that_moves_or_is_young_is_no_event(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, oldest_days: int, landed_days: int
) -> None:
    home = _home(tmp_path)
    _config(monkeypatch, home, {"write_approval": True})
    _notebook(home, "USER.md", ["likes tea"], NOW - timedelta(days=landed_days))
    _pending(home, "memory", 1, NOW - timedelta(days=oldest_days))

    assert _read(home)[2] == ()


def test_a_skills_queue_is_stuck_against_the_curator_s_ledger(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = _home(tmp_path)
    _config(monkeypatch, home, {}, {"write_approval": "on"})
    ledger = home / "skills" / ".curator_ledger.jsonl"
    ledger.parent.mkdir(parents=True)
    ledger.write_text('{"id": "x"}\n', encoding="utf-8")
    _at(ledger, NOW - timedelta(days=30))
    for number in range(4):
        _pending(home, "skills", number, NOW - timedelta(days=29), {"action": "create"})

    titles = [incident.title for incident in _read(home)[2]]

    assert titles == ["Skills: 4 writes stuck 29d"]


@pytest.mark.parametrize(
    ("chars", "title"),
    [(1306, "Memory: USER.md 94% full"), (1400, "Memory: USER.md over limit"), (1200, None)],
)
def test_a_notebook_past_ninety_percent_of_its_limit_is_an_event(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, chars: int, title: str | None
) -> None:
    home = _home(tmp_path)
    _config(monkeypatch, home, {})
    _notebook(home, "USER.md", ["x" * chars], NOW - timedelta(days=1))

    titles = [incident.title for incident in _read(home)[2]]

    assert titles == ([title] if title else [])


def test_a_notebook_the_engine_switched_off_is_never_full(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = _home(tmp_path)
    _config(monkeypatch, home, {"user_profile_enabled": False})
    _notebook(home, "USER.md", ["x" * 1400], NOW - timedelta(days=1))

    _summary, _source, incidents = _read(home)

    assert incidents == ()
    assert "> Memory: MEMORY.md none yet · USER.md off" in _screen(home)


def _errors_log(home: Path, lines: list[str]) -> None:
    path = home / "logs" / "errors.log"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(f"{line}\n" for line in lines), encoding="utf-8")


def _stamp(moment: datetime) -> str:
    local = moment.astimezone()  # the engine writes its log in the host's local time
    return local.strftime("%Y-%m-%d %H:%M:%S,") + f"{local.microsecond // 1000:03d}"


def test_a_configured_provider_that_warns_in_the_log_is_an_event(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = _home(tmp_path)
    _config(monkeypatch, home, {"provider": "honcho"})
    recent, old = NOW - timedelta(hours=2), NOW - timedelta(days=3)
    _errors_log(
        home,
        [
            f"{_stamp(old)} WARNING agent.memory_manager: Memory provider 'honcho' sync_turn failed",
            f"{_stamp(recent)} INFO agent.agent_init: Memory provider 'honcho' activated",
            f"{_stamp(recent)} WARNING agent.memory_manager: Memory provider 'honcho' initialize"
            f" failed: {SECRET}",
            f"{_stamp(recent)} ERROR agent.memory_manager: Memory provider 'honcho'"
            f" handle_tool_call(search) failed: {SECRET}",
        ],
    )

    summary, _source, incidents = _read(home)

    assert summary.provider is not None
    assert (summary.provider.name, summary.provider.errors) == ("honcho", 2)
    assert [incident.title for incident in incidents] == ["Memory provider: 2 errors"]
    lines = _screen(home)
    assert "> Memory provider honcho: 2 errors in 24 h, last Oct 8 10:00" in lines


@pytest.mark.parametrize("name", ["", "none", "default", "builtin", "Built-In"])
def test_no_provider_is_no_provider_line(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, name: str
) -> None:
    home = _home(tmp_path)
    _config(monkeypatch, home, {"provider": name})

    assert _read(home)[0].provider is None


def test_a_provider_with_a_quiet_log_says_so_and_no_more(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = _home(tmp_path)
    _config(monkeypatch, home, {"provider": "holographic"})

    assert "> Memory provider holographic: no errors logged in 24 h" in _screen(home)


def test_no_memory_yet_is_no_event_and_an_empty_home_is_not_hermes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = _home(tmp_path)
    assert _read(home)[0].state == "unsupported"
    assert _read(home)[1].state == "unsupported"

    _config(monkeypatch, home, {})
    summary, source, incidents = _read(home)

    assert (summary.state, source.state, incidents) == ("observed", "fresh", ())
    assert "> Memory: MEMORY.md none yet · USER.md none yet" in _screen(home)


def test_the_dashboard_s_own_setting_turns_the_section_off(tmp_path: Path) -> None:
    home = _home(tmp_path)

    summary, source, incidents = read_memory(
        Environment(hermes_home=home, memory_enabled=False), now=NOW
    )

    assert (summary.state, source, incidents) == ("off", None, ())


# ------------------------------------------------------------------ another installation


def test_a_profile_home_with_its_own_limits_gets_its_own_numbers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """None of our paths: a named profile's home, limits of its own, a queue of adds."""
    home = _home(tmp_path)
    _config(
        monkeypatch,
        home,
        {"memory_char_limit": "5000", "user_char_limit": 600, "write_approval": "yes"},
    )
    _notebook(home, "MEMORY.md", ["a" * 1000, "b" * 3000], NOW - timedelta(days=1))
    _notebook(home, "USER.md", ["c" * 500], NOW - timedelta(days=1))
    _pending(home, "memory", 1, NOW - timedelta(hours=5))
    batch = {
        "action": "batch",
        "payload": {
            "target": "memory",
            "operations": [
                {"action": "add", "content": f"  {'d' * 97}  "},
                {"action": "replace", "old_text": SECRET, "content": "e" * 50},
                {"action": "add", "content": "f" * 47},
            ],
        },
    }
    _pending(home, "memory", 2, NOW - timedelta(hours=4), batch)

    summary, _source, incidents = _read(home)

    memory_book, user_book = summary.notebooks
    assert (memory_book.chars, memory_book.limit, memory_book.largest) == (4003, 5000, 3000)
    assert (user_book.chars, user_book.limit) == (500, 600)
    queue = summary.queues[0]
    user_add = len(f"{SECRET} the user likes tea") + 3
    assert queue.needs == (("MEMORY.md", 97 + 3 + 47 + 3, 997), ("USER.md", user_add, 100))
    assert incidents == ()
    lines = _screen(home)
    assert "> Memory: MEMORY.md 4,003/5,000 (80%, one entry 60%) · USER.md 500/600 (83%)" in lines
    assert (
        f"> Waiting adds: MEMORY.md 150 chars, 997 free; USER.md {user_add} chars, 100 free"
        in lines
    )


def test_adds_larger_than_the_room_left_say_they_do_not_fit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Our production on 08.10: 23,918 characters of adds waited for 157 free in USER.md."""
    home = _home(tmp_path)
    _config(monkeypatch, home, {"write_approval": True})
    _notebook(home, "USER.md", ["x" * 1300], NOW - timedelta(days=1))
    for number in range(3):
        _pending(home, "memory", number, NOW - timedelta(hours=number + 1))

    line = next(line for line in _screen(home) if line.startswith("> Waiting adds:"))

    assert line.endswith(": they do not fit")


# ------------------------------------------------------------------ the engine's count and config


def test_entries_are_counted_the_engine_s_way() -> None:
    data = "\ufeff  one  \n§\n\n§\ntwo\n§\none\n§\n   \n§\nthree §".encode()

    entries = notebook_entries(data)

    assert entries == ["one", "two", "three §"]
    assert len(DELIMITER.join(entries)) == 3 + 3 + 7 + 3 + 3


def test_the_count_is_the_engine_s_own_memory_store_count(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With the engine installed, its ``MemoryStore`` loads the same files and counts them."""
    store_module = pytest.importorskip("tools.memory_tool_store", reason="Hermes is not here")
    tool = pytest.importorskip("tools.memory_tool", reason="Hermes is not here")
    home = _home(tmp_path)
    _config(monkeypatch, home, {})
    _notebook(home, "MEMORY.md", ["  first  ", "второй, кириллица", "first", "", "x" * 300], NOW)
    _notebook(home, "USER.md", ["likes tea", "likes tea"], NOW)
    monkeypatch.setattr(tool, "get_memory_dir", lambda: home / "memories")
    store = store_module.MemoryStore()
    store.load_from_disk()

    summary, _source, _incidents = _read(home)

    ours = {book.name: book.chars for book in summary.notebooks}
    assert ours == {"MEMORY.md": store._char_count("memory"), "USER.md": store._char_count("user")}


@pytest.mark.parametrize(
    ("section", "gate"),
    [
        ({"write_approval": True}, True),
        ({"write_approval": "Approve"}, True),
        ({"write_approval": "enabled"}, True),
        ({"write_approval": "off"}, False),
        ({"write_approval": False}, False),
        ({"write_mode": "approve"}, True),
        ({"write_mode": "on"}, False),
        ({}, False),
    ],
)
def test_the_gate_reads_the_engine_s_words(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, section: dict[str, Any], gate: bool
) -> None:
    home = _home(tmp_path)
    _config(monkeypatch, home, section)

    assert _read(home)[0].queues[0].gate is gate


@pytest.mark.parametrize(
    "parser", [None, _Parser(error=ValueError("refused"))], ids=["no parser", "refused"]
)
def test_an_unread_config_leaves_limits_and_gate_unknown_never_guessed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, parser: _Parser | None
) -> None:
    home = _home(tmp_path)
    (home / "config.yaml").write_text("memory: {}\n", encoding="utf-8")
    monkeypatch.setattr(memory, "yaml_parser", lambda: parser)
    _notebook(home, "USER.md", ["x" * 1400], NOW - timedelta(days=1))

    summary, _source, incidents = _read(home)

    assert summary.detail == "config.yaml not read"
    assert summary.notebooks[1] == MemoryNotebook(
        "USER.md", None, 1400, None, 1, 1400, (NOW - timedelta(days=1)).isoformat()
    )
    assert summary.queues[0].gate is None
    assert incidents == ()
    assert "> Memory: MEMORY.md none yet · USER.md 1,400 chars · config.yaml not read" in _screen(
        home
    )


def test_the_real_yaml_parser_reads_the_engine_s_config(tmp_path: Path) -> None:
    """hermes_yaml on Hermes 0.21.6, PyYAML on 0.21.1-0.21.5."""
    if memory.yaml_parser() is None:
        pytest.skip("no YAML parser in this interpreter")
    home = _home(tmp_path)
    (home / "config.yaml").write_text(
        "memory:\n  memory_char_limit: 3000\n  write_approval: on\n  provider: honcho\n"
        "skills:\n  write_approval: true\n",
        encoding="utf-8",
    )

    summary = _read(home)[0]

    assert [book.limit for book in summary.notebooks] == [3000, 1375]
    assert [queue.gate for queue in summary.queues] == [True, True]
    assert summary.provider is not None and summary.provider.name == "honcho"


# ------------------------------------------------------------------ no text leaves


def test_no_text_of_a_notebook_a_pending_write_or_a_log_line_leaves(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    home = _home(tmp_path)
    _config(monkeypatch, home, {"write_approval": True, "provider": "honcho"})
    _notebook(home, "MEMORY.md", [f"{SECRET} note", "x" * 2100], NOW - timedelta(days=20))
    _notebook(home, "USER.md", [f"{SECRET} user"], NOW - timedelta(days=20))
    for number in range(3):
        _pending(home, "memory", number, NOW - timedelta(days=5 + number))
    _pending(home, "memory", 9, NOW, {"action": "add", "payload": {"target": SECRET, "content": 5}})
    (home / "pending" / "memory" / "broken.json").write_text(f"{{{SECRET}", encoding="utf-8")
    _pending(home, "skills", 1, NOW - timedelta(days=4), {"summary": SECRET, "name": SECRET})
    _errors_log(
        home,
        [
            f"{_stamp(NOW - timedelta(hours=1))} WARNING agent.memory_manager: Memory provider"
            f" 'honcho' prefetch timed out after 3.0s {SECRET}",
            f"Traceback (most recent call last):\n  File x, line 1\nValueError: {SECRET}",
        ],
    )
    caplog.set_level(logging.DEBUG)

    snapshot = collect_all(
        Environment(hermes_home=home), _Runner(), now=NOW, resolve_limits=lambda: None
    )
    text = render_dashboard(snapshot, now=NOW, zone=UTC)

    assert snapshot.memory is not None and snapshot.memory.state == "observed"
    assert len([i for i in snapshot.incidents if i.incident_id.startswith("memory:")]) == 4
    for where in (text, to_telegram_html(text), json.dumps(asdict(snapshot)), caplog.text):
        assert SECRET not in where
        assert str(home) not in where


def test_a_failure_reading_the_memory_is_its_class_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = _home(tmp_path)
    _config(monkeypatch, home, {})

    def crash(*_args: object, **_kwargs: object) -> object:
        raise PermissionError(f"[Errno 13] Permission denied: '{home}/memories/{SECRET}'")

    monkeypatch.setattr(memory, "read_notebook", crash)

    summary, source, incidents = _read(home)

    assert (summary.state, summary.detail) == ("unknown", "memory: PermissionError")
    assert (source.state, incidents) == ("unavailable", ())
    assert SECRET not in json.dumps(asdict(summary))
