"""Gemini's last 429 from the engine's error log (``gemini_log.py``).

Google reports Gemini quota only to a project with billing enabled, so a free-tier installation
has one trace of its quota: the HTTP 429 the engine logs when a call is refused. These tests pin
the reading of that trace: the entry format of ``errors.log``, the fields taken from the message
(time, ``limit``, ``model``, the retry), the merge with the truncated duplicates the engine logs
in the same second, the record that outlives the log's rotation, and the two windows a refusal
gets on the screen (a per-minute one marks the line, a daily one is an event until the reset).
"""

from __future__ import annotations

import sys
import time
from dataclasses import asdict
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import pytest

from telegram_dashboard import gemini_log
from telegram_dashboard.compat import Environment
from telegram_dashboard.gemini_log import (
    DAY_RETRY_SECONDS,
    LINE_MARK_SECONDS,
    activate,
    collect_gemini,
    event_title,
    hit_words,
    incidents_for,
    is_active,
    newer,
    newest_refusal,
    read_tail,
    refusal_from_record,
    retry_words,
    split_entries,
)
from telegram_dashboard.schema import Refusal

NOW = datetime(2026, 9, 29, 14, 30, tzinfo=UTC)
# The stamps in the log are the host's local time without an offset; the tests name the host's
# zone explicitly so they read the same on every machine (the default is the process's zone).
ZONE = timezone(timedelta(hours=2))
STAMP = "2026-09-29 14:03:12,345"  # 12:03:12.345 UTC in ZONE
AT = "2026-09-29T12:03:12.345000+00:00"

# What Google says on a free-tier refusal: the first line as the engine's own tests carry it
# (``tests/agent/test_gemini_free_tier_gate.py`` at v2026.9.14), the rest as the message is
# presumed to read in full. The marker stands for the prose the screen must never quote.
PLANTED = "PLANTED-MESSAGE-TEXT"
GOOGLE_MESSAGE = (
    f"You exceeded your current quota, please check your plan and billing details. {PLANTED}\n"
    "* Quota exceeded for metric: generativelanguage.googleapis.com/"
    "generate_content_free_tier_requests, limit: 10, model: gemini-2.5-flash-preview-tts\n"
    "Please retry in 41.53s."
)
TTS_ERROR = f"TTS generation failed (gemini): Gemini TTS API error (HTTP 429): {GOOGLE_MESSAGE}"
# ``tools/tts_tool.py`` logs the failure with ``exc_info``: the traceback follows the message
# and repeats it on its last line. Its source lines carry a ``model=`` of their own.
TRACEBACK = (
    "Traceback (most recent call last):\n"
    '  File "tools/tts_tool.py", line 362, in _synthesize\n'
    "    audio = synthesize(text, model=model)\n"
    f"RuntimeError: Gemini TTS API error (HTTP 429): {GOOGLE_MESSAGE}"
)
# ``agent/tool_executor.py`` logs the same failure again, the preview cut at 200 characters.
TOOL_PREVIEW = (
    'Tool text_to_speech returned error (2.10s): {"error": "TTS chunk 1 failed (gemini): '
    f"{TTS_ERROR}"
)[:200]
LLM_WARNING = (
    "API call failed (attempt 1/3) error_type=GeminiAPIError thread=main provider=gemini "
    "base_url=https://generativelanguage.googleapis.com model=gemini-2.5-flash "
    "summary=Gemini HTTP 429 (RESOURCE_EXHAUSTED): Rate limited"
)


def _entry(
    stamp: str, level: str, logger: str, message: str, *, tag: str = "", trace: str = ""
) -> str:
    body = f"{stamp} {level}{tag} {logger}: {message}"
    if trace:
        body += "\n" + trace
    return body + "\n"


def _refusal_entry(stamp: str = STAMP) -> str:
    return _entry(stamp, "ERROR", "tools.tts_tool", TTS_ERROR, trace=TRACEBACK)


def _log(*entries: str) -> str:
    return "".join(entries)


def _env(tmp_path: Path) -> Environment:
    return Environment(hermes_home=tmp_path, limits_enabled=True)


def _write_log(tmp_path: Path, text: str | bytes) -> Path:
    logs = tmp_path / "logs"
    logs.mkdir(exist_ok=True)
    path = logs / "errors.log"
    if isinstance(text, bytes):
        path.write_bytes(text)
    else:
        path.write_text(text, encoding="utf-8", newline="\n")
    return path


# ----------------------------------------------------------------------------- entries


def test_split_entries_keeps_a_multiline_entry_whole_and_reads_the_local_stamp() -> None:
    text = _log(
        _entry("2026-09-29 14:00:00,001", "WARNING", "gateway.run", "something else"),
        _refusal_entry(),
    )

    entries = split_entries(text, zone=ZONE)

    assert [entry.level for entry in entries] == ["WARNING", "ERROR"]
    assert entries[1].logger == "tools.tts_tool"
    assert entries[1].at == datetime(2026, 9, 29, 14, 3, 12, 345000, tzinfo=ZONE)
    assert entries[1].at is not None and entries[1].at.isoformat() == AT
    assert entries[1].head.startswith(f"{STAMP} ERROR tools.tts_tool: TTS generation failed")
    assert "Traceback" in entries[1].text
    assert "Traceback" not in entries[1].message
    assert "Please retry in 41.53s." in entries[1].message


def test_text_before_the_first_header_is_dropped() -> None:
    """A tail read starts wherever the byte count falls: inside a traceback, as here."""
    text = "    audio = synthesize(text, model=model)\n" + TRACEBACK.splitlines()[-1] + "\n"
    text += _entry("2026-09-29 14:10:00,000", "INFO", "x", "later")

    entries = split_entries(text, zone=ZONE)

    assert [entry.logger for entry in entries] == ["x"]


def test_the_newest_refusal_carries_time_limit_model_and_retry() -> None:
    text = _log(
        _entry("2026-09-29 13:00:00,000", "INFO", "tools.tts_tool", "Generating speech"),
        _entry(
            "2026-09-29 13:30:00,000",
            "ERROR",
            "tools.tts_tool",
            "TTS generation failed (gemini): Gemini TTS API error (HTTP 403): key rejected",
        ),
        _refusal_entry(),
    )

    refusal = newest_refusal(split_entries(text, zone=ZONE))

    assert refusal == Refusal(
        at=AT, limit=10, retry_seconds=41.53, model="gemini-2.5-flash-preview-tts"
    )


def test_a_truncated_duplicate_logged_after_the_error_borrows_its_fields() -> None:
    """The tool executor's preview of the same failure is the newer entry and has no ``limit``;
    the anchor is its time, the fields come from the full entry of the same second."""
    text = _log(
        _refusal_entry(),
        _entry("2026-09-29 14:03:12,400", "WARNING", "agent.tool_executor", TOOL_PREVIEW),
    )

    refusal = newest_refusal(split_entries(text, zone=ZONE))

    assert refusal is not None
    assert refusal.at == "2026-09-29T12:03:12.400000+00:00"
    assert (refusal.limit, refusal.retry_seconds) == (10, 41.53)
    assert refusal.model == "gemini-2.5-flash-preview-tts"


def test_entries_more_than_ten_seconds_apart_do_not_merge() -> None:
    text = _log(
        _refusal_entry("2026-09-29 14:00:00,000"),
        _entry("2026-09-29 14:03:12,400", "WARNING", "agent.tool_executor", TOOL_PREVIEW),
    )

    refusal = newest_refusal(split_entries(text, zone=ZONE))

    assert refusal is not None
    assert refusal.at == "2026-09-29T12:03:12.400000+00:00"
    assert refusal.limit is None and refusal.retry_seconds is None


def test_a_session_tag_and_the_llm_wording_match_too() -> None:
    text = _entry(STAMP, "WARNING", "agent.conversation_loop", LLM_WARNING, tag=" [tg_4242]")

    refusal = newest_refusal(split_entries(text, zone=ZONE))

    assert refusal == Refusal(at=AT, model="gemini-2.5-flash")


def test_the_traceback_never_lends_its_model_to_a_message_without_one() -> None:
    message = "TTS generation failed (gemini): Gemini TTS API error (HTTP 429): Rate limited"
    text = _entry(STAMP, "ERROR", "tools.tts_tool", message, trace=TRACEBACK)

    refusal = newest_refusal(split_entries(text, zone=ZONE))

    # The fields come from the message alone: the traceback's source line ``model=model`` is
    # not the model, and its last line, which repeats a fuller message, is not read either.
    assert refusal == Refusal(at=AT)


@pytest.mark.parametrize(
    "message",
    [
        "TTS generation failed (gemini): Gemini TTS API error (HTTP 400): bad request",
        "TTS generation failed (gemini): Gemini TTS API error (HTTP 403): key rejected",
        "API call failed error_type=APIError summary=Anthropic HTTP 429: rate limited",
        "TTS generation failed (elevenlabs): HTTP 429: too many requests",
    ],
)
def test_other_statuses_and_other_providers_do_not_match(message: str) -> None:
    assert newest_refusal(split_entries(_entry(STAMP, "ERROR", "x", message), zone=ZONE)) is None


def test_the_message_text_never_reaches_the_refusal() -> None:
    refusal = newest_refusal(split_entries(_refusal_entry(), zone=ZONE))

    assert refusal is not None
    assert PLANTED not in " ".join(str(value) for value in asdict(refusal).values())


def test_an_unreadable_stamp_skips_the_entry() -> None:
    text = _entry("2026-02-30 14:03:12,345", "ERROR", "tools.tts_tool", TTS_ERROR)

    assert newest_refusal(split_entries(text, zone=ZONE)) is None


# ----------------------------------------------------------------------------- the tail


def test_read_tail_starts_at_a_whole_line(tmp_path: Path) -> None:
    lines = [_entry(f"2026-09-29 14:{i:02d}:00,000", "INFO", "x", "y" * 50) for i in range(60)]
    path = _write_log(tmp_path, "".join(lines))

    tail = read_tail(path, size=500)

    assert tail.startswith("2026-09-29 14:")
    assert len(tail.encode("utf-8")) < 500
    assert tail.endswith("\n")


def test_read_tail_of_a_small_file_is_the_whole_file(tmp_path: Path) -> None:
    path = _write_log(tmp_path, _refusal_entry())

    assert read_tail(path, size=1 << 20) == _refusal_entry()


def test_a_line_still_being_written_is_left_for_the_next_tick(tmp_path: Path) -> None:
    """The engine may be mid-write when the tail is read, and a large entry flushed in parts can
    end inside a character. The bytes after the last newline are no line yet: they are not read,
    so a half-written character never turns the log unreadable."""
    partial = "2026-09-29 14:05:00,000 ERROR tools.tts_tool: Квота".encode()[:-1]
    path = _write_log(tmp_path, _refusal_entry().encode("utf-8") + partial)

    assert read_tail(path) == _refusal_entry()
    line, source, _events = collect_gemini(
        _env(tmp_path), {}, now=datetime(2026, 9, 29, 12, 10, tzinfo=UTC), zone=ZONE
    )
    assert source.state == "fresh"
    assert line.refusal is not None and line.refusal.at == AT


def test_a_record_with_a_number_too_large_for_a_float_keeps_the_moment_only() -> None:
    """The record is JSON in the plugin's state; an integer too large for a float is not a
    number to show, and reading it must not raise (``math.isfinite`` would)."""
    cache = {"last_429": {"at": AT, "limit": 10**400, "retry_seconds": 10**400}}

    assert refusal_from_record(cache) == Refusal(at=AT)


# ----------------------------------------------------------------------------- collect


def test_a_missing_file_in_a_living_logs_directory_is_none_in_the_log(tmp_path: Path) -> None:
    """The engine's Windows handler opens the file on the first WARNING: an empty log and no
    log say the same thing, nothing refused."""
    (tmp_path / "logs").mkdir()
    cache: dict[str, object] = {}

    metric, source, incidents = collect_gemini(_env(tmp_path), cache, now=NOW, zone=ZONE)

    assert (source.name, source.authority, source.state) == ("gemini_log", "local", "fresh")
    assert source.observed_at == NOW.isoformat()
    assert metric.provider == "Gemini" and metric.kind == "unsupported"
    assert metric.detail == "Google reports Gemini quota only with billing enabled"
    assert metric.refusal == Refusal()
    assert incidents == ()
    assert cache == {"checked_at": NOW.isoformat(), "last_429": None}


def test_no_logs_directory_is_unsupported(tmp_path: Path) -> None:
    metric, source, incidents = collect_gemini(_env(tmp_path), {}, now=NOW, zone=ZONE)

    assert source.state == "unsupported"
    assert source.detail == "no logs directory"
    assert metric.kind == "unsupported" and metric.refusal is None
    assert incidents == ()


def test_an_empty_file_is_fresh_with_nothing_seen(tmp_path: Path) -> None:
    _write_log(tmp_path, "")

    metric, source, _incidents = collect_gemini(_env(tmp_path), {}, now=NOW, zone=ZONE)

    assert source.state == "fresh"
    assert metric.refusal == Refusal()


def test_a_refusal_in_the_tail_is_the_line_the_record_and_no_event(tmp_path: Path) -> None:
    _write_log(
        tmp_path, _log(_entry("2026-09-29 14:00:00,000", "INFO", "x", "y"), _refusal_entry())
    )
    cache: dict[str, object] = {}
    now = datetime(2026, 9, 29, 12, 8, tzinfo=UTC)  # five minutes after the refusal

    metric, source, incidents = collect_gemini(_env(tmp_path), cache, now=now, zone=ZONE)

    assert source.state == "fresh"
    assert metric.refusal == Refusal(
        at=AT,
        limit=10,
        retry_seconds=41.53,
        model="gemini-2.5-flash-preview-tts",
        daily=False,
        active_until="2026-09-29T13:03:12.345000+00:00",
    )
    assert is_active(metric.refusal, now)
    assert incidents == ()
    assert cache["last_429"] == {
        "at": AT,
        "limit": 10,
        "retry_seconds": 41.53,
        "model": "gemini-2.5-flash-preview-tts",
    }
    assert cache["checked_at"] == now.isoformat()


def test_undecodable_bytes_are_unavailable_and_the_record_survives(tmp_path: Path) -> None:
    _write_log(tmp_path, b"\xff\xfe not text\n")
    cache = {"last_429": {"at": AT, "limit": 10, "retry_seconds": 41.53, "model": None}}
    before = dict(cache)

    metric, source, _incidents = collect_gemini(_env(tmp_path), cache, now=NOW, zone=ZONE)

    assert source.state == "unavailable"
    assert source.detail == "log unreadable: UnicodeDecodeError"
    assert metric.refusal is not None and metric.refusal.at == AT
    assert cache == before


def test_the_record_wins_over_an_empty_tail_and_a_newer_tail_rewrites_it(tmp_path: Path) -> None:
    older = "2026-09-28T12:00:00+00:00"
    cache: dict[str, object] = {"last_429": {"at": older, "limit": 3}}
    _write_log(tmp_path, _entry("2026-09-29 14:00:00,000", "INFO", "x", "y"))

    metric, _source, _incidents = collect_gemini(_env(tmp_path), cache, now=NOW, zone=ZONE)
    assert metric.refusal is not None and metric.refusal.at == older
    assert cache["last_429"] == {"at": older, "limit": 3}

    _write_log(tmp_path, _refusal_entry())
    metric, _source, _incidents = collect_gemini(_env(tmp_path), cache, now=NOW, zone=ZONE)
    assert metric.refusal is not None and metric.refusal.at == AT
    assert cache["last_429"] == {
        "at": AT,
        "limit": 10,
        "retry_seconds": 41.53,
        "model": "gemini-2.5-flash-preview-tts",
    }


def test_a_refusal_older_than_its_window_keeps_the_details_without_mark_or_event(
    tmp_path: Path,
) -> None:
    _write_log(tmp_path, _refusal_entry())
    now = NOW + timedelta(days=2)

    metric, _source, incidents = collect_gemini(_env(tmp_path), {}, now=now, zone=ZONE)

    assert metric.refusal is not None and metric.refusal.at == AT
    assert not is_active(metric.refusal, now)
    assert incidents == ()


def test_a_refusal_dated_in_the_future_is_not_shown(tmp_path: Path) -> None:
    _write_log(tmp_path, _refusal_entry())
    now = datetime(2026, 9, 29, 11, 0, tzinfo=UTC)  # an hour before the stamp

    metric, source, incidents = collect_gemini(_env(tmp_path), {}, now=now, zone=ZONE)

    assert source.state == "fresh"
    assert metric.refusal == Refusal()
    assert incidents == ()


@pytest.mark.skipif(sys.platform == "win32", reason="time.tzset is POSIX only")
def test_naive_stamps_follow_the_host_zone_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TZ", "Europe/Berlin")
    time.tzset()  # type: ignore[attr-defined]
    try:
        entries = split_entries(_refusal_entry())
    finally:
        monkeypatch.delenv("TZ")
        time.tzset()  # type: ignore[attr-defined]

    assert entries[0].at is not None and entries[0].at.isoformat() == AT


# ----------------------------------------------------------------------------- windows


def test_minute_and_day_are_told_apart_by_the_retry() -> None:
    minute = activate(Refusal(at=AT, retry_seconds=41.53))
    unknown = activate(Refusal(at=AT))
    day = activate(Refusal(at=AT, retry_seconds=14580.0))

    assert (minute.daily, unknown.daily, day.daily) == (False, False, True)
    assert minute.active_until == "2026-09-29T13:03:12.345000+00:00"
    assert unknown.active_until == minute.active_until
    assert day.active_until == "2026-09-29T16:06:12.345000+00:00"
    assert DAY_RETRY_SECONDS == 120 and LINE_MARK_SECONDS == 3600
    assert activate(Refusal()) == Refusal()


def test_a_day_refusal_is_an_event_until_the_reset_a_minute_one_never() -> None:
    day = activate(Refusal(at=AT, limit=15, retry_seconds=14580.0))
    minute = activate(Refusal(at=AT, limit=3, retry_seconds=41.53))
    two_hours_on = datetime(2026, 9, 29, 14, 3, 13, tzinfo=UTC)

    assert incidents_for(day, two_hours_on) == (
        gemini_log.Incident("gemini:day_quota", "warning", "Gemini out of quota 2 h ago"),
    )
    assert incidents_for(day, datetime(2026, 9, 29, 16, 7, tzinfo=UTC)) == ()
    assert incidents_for(minute, two_hours_on) == ()
    assert incidents_for(minute, datetime(2026, 9, 29, 12, 4, tzinfo=UTC)) == ()
    assert incidents_for(None, two_hours_on) == ()


def test_is_active_follows_active_until() -> None:
    minute = activate(Refusal(at=AT, retry_seconds=41.53))

    assert is_active(minute, datetime(2026, 9, 29, 13, 3, tzinfo=UTC))
    assert not is_active(minute, datetime(2026, 9, 29, 13, 4, tzinfo=UTC))
    assert not is_active(Refusal(), NOW)
    assert not is_active(None, NOW)


@pytest.mark.parametrize(
    ("age", "words"),
    [(0, "just now"), (59, "just now"), (60, "1 min ago"), (3599, "59 min ago"), (7200, "2 h ago")],
)
def test_hit_words(age: float, words: str) -> None:
    assert hit_words(age) == words


def test_the_event_title_fits_a_phone_line_at_every_age() -> None:
    for age in (0, 59, 60, 59 * 60, 3600, 23 * 3600, 47 * 3600, 5 * 86400):
        title = event_title(age)
        assert len("- " + title) <= 32, title
    assert event_title(59 * 60) == "Gemini out of quota 59 min ago"


@pytest.mark.parametrize(
    ("seconds", "words"),
    [
        (41.53, "42 s"),
        (59, "59 s"),
        (60, "1 min"),
        (90, "2 min"),
        (900, "15 min"),
        (14580, "4 h"),
        (86400, "24 h"),
    ],
)
def test_retry_words(seconds: float, words: str) -> None:
    assert retry_words(seconds) == words


# ----------------------------------------------------------------------------- the record


def test_refusal_from_record_reads_only_a_well_formed_entry() -> None:
    assert refusal_from_record(None) is None
    assert refusal_from_record({}) is None
    assert refusal_from_record({"last_429": None}) is None
    assert refusal_from_record({"last_429": {"at": "yesterday"}}) is None
    assert refusal_from_record({"last_429": {"at": AT, "limit": "10"}}) == Refusal(at=AT)
    assert refusal_from_record({"last_429": {"at": AT, "limit": True}}) == Refusal(at=AT)
    assert refusal_from_record(
        {"last_429": {"at": AT, "limit": 10, "retry_seconds": 41, "model": "gemini-2.5-flash"}}
    ) == Refusal(at=AT, limit=10, retry_seconds=41.0, model="gemini-2.5-flash")


def test_newer_prefers_the_later_moment_and_tolerates_nothing_seen() -> None:
    early = Refusal(at="2026-09-28T12:00:00+00:00")
    late = Refusal(at=AT)

    assert newer(early, late) is late
    assert newer(late, early) is late
    assert newer(None, late) is late
    assert newer(late, None) is late
    assert newer(Refusal(), late) is late
    assert newer(None, None) is None
