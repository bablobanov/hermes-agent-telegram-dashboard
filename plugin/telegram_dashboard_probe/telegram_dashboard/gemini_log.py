"""Gemini's last HTTP 429 from the engine's own error log.

Google reports Gemini quota only to a project with billing enabled; a key on the free tier gets
no number from any surface. The one trace such an installation has is the 429 the engine logs
when Google refuses a call. This reader takes the newest such entry from the tail of
``HERMES_HOME/logs/errors.log`` (WARNING and above, rotated by the engine at 2 MB), or from the
whole file until the first successful read, and keeps four things from it: the time, and the
``limit``, the ``model`` and the seconds to retry when the message names them. The text of the
message itself never reaches the screen.

Only the engine's own calls are in that log. A script that calls Gemini on its own, such as a
skill's, is not seen; the details say so on every screen.

The record remembers the last refusal seen (``gemini_log_cache`` in the plugin's state), so a
429 that rotated out of the log is still "last". A per-minute refusal marks the line for an
hour; a daily one, told apart by a retry longer than a per-minute window, is an event until the
reset the provider named. Neither moves the overall status by itself.
"""

from __future__ import annotations

import math
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta, tzinfo
from pathlib import Path
from typing import Any, TypeGuard

from .backup import describe_age
from .compat import Environment
from .schema import Incident, QuotaMetric, Refusal, SourceObservation
from .timeparse import age_seconds, is_from_the_future, parse_timestamp

SOURCE_NAME = "gemini_log"
LABEL = "Gemini"
LOG_DIR = "logs"
LOG_FILE = "errors.log"
NO_QUOTA_REASON = "Google reports Gemini quota only with billing enabled"
# The engine rotates the file at 2 MB; a refusal with its traceback is a few KB, so this holds
# hours of ordinary warnings or dozens of refusals, and the record keeps what fell out.
TAIL_BYTES = 256 * 1024
# Until the first successful read (the record then holds ``last_429``, None when nothing was
# seen) the whole file is read (decision of 29.09): a 429 from before the plugin arrived is
# "last" at once, and "none in the log" means the file. The engine rotates at 2 MB; a file past
# this ceiling is not that rotation, and even its first read is the tail, as is a first read
# that cannot be decoded.
FIRST_READ_BYTES = 4 * 1024 * 1024
# The engine logs one refusal up to three times within a second (the tool's own ERROR with the
# whole message, the executor's preview cut at 200 characters, the voice reply's WARNING):
# entries this close to the newest one are one refusal, and the fields come from whichever
# entry still has them.
MERGE_SECONDS = 10.0
# A per-minute refusal is on the line this long: two ticks at the usual 30 min period.
LINE_MARK_SECONDS = 3600.0
# Gemini's quotas are per minute and per day. A per-minute window ends within a minute, so a
# retry longer than this is the daily quota, out until the reset the provider named.
DAY_RETRY_SECONDS = 120.0
INCIDENT_ID = "gemini:day_quota"
RECORD_KEY = "last_429"

GeminiPart = tuple[QuotaMetric, SourceObservation, tuple[Incident, ...]]

# ``hermes_logging._LOG_FORMAT``: the stamp, the level, an optional session tag in brackets,
# the logger's name and the message; the stamp in the host's local time without an offset.
_HEADER = re.compile(
    r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2},\d{3}) (DEBUG|INFO|WARNING|ERROR|CRITICAL)"
    r"(?: \[[^\]]*\])? (\S+): ",
    re.MULTILINE,
)
# The TTS provider's ``Gemini TTS API error (HTTP 429): …`` and the native adapter's
# ``Gemini HTTP 429 (RESOURCE_EXHAUSTED): …`` both pass; a 400 or a 403 does not.
_REFUSAL = re.compile(r"Gemini.*HTTP 429")
_LIMIT = re.compile(r"\blimit:\s*(\d{1,9})\b")
# A model name starts with a lowercase letter: a key (``AIza…``) can never be taken for one.
_MODEL = re.compile(r"\bmodel[:=]\s*([a-z][a-z0-9.\-]{0,39})")
_RETRY = re.compile(r"[Rr]etry in (\d+(?:\.\d+)?)\s*s\b")
_TRACEBACK = "\nTraceback (most recent call last):"
_STAMP_FORMAT = "%Y-%m-%d %H:%M:%S.%f"
_STAMP_ERRORS = (ValueError, OverflowError, OSError)


@dataclass(frozen=True, slots=True)
class Entry:
    """One log entry: its header line and every continuation line up to the next header."""

    at: datetime | None
    level: str
    logger: str
    text: str

    @property
    def head(self) -> str:
        return self.text.split("\n", 1)[0]

    @property
    def message(self) -> str:
        """The entry without its traceback: the fields are read from the message alone, and a
        traceback's source lines carry a ``model=`` of their own."""
        cut = self.text.find(_TRACEBACK)
        return self.text if cut < 0 else self.text[:cut]


# ----------------------------------------------------------------------------- the log


def logs_dir(env: Environment) -> Path:
    return env.hermes_home / LOG_DIR


def log_path(env: Environment) -> Path:
    return logs_dir(env) / LOG_FILE


def read_tail(path: Path, *, size: int = TAIL_BYTES, whole_up_to: int = 0) -> str:
    """The last ``size`` bytes of ``path`` from the first whole line to the last, as text; a
    file of at most ``whole_up_to`` bytes is read whole.

    The bytes after the last newline are a line the engine is still writing (a large entry is
    flushed in parts and may end inside a character): the next tick reads it whole. A missing
    file is empty text: the engine on Windows opens the log on the first warning, so no file
    and an empty file say the same thing. ``OSError`` and ``UnicodeDecodeError`` are the
    caller's to name.
    """
    try:
        with path.open("rb") as handle:
            handle.seek(0, 2)
            length = handle.tell()
            start = 0 if length <= whole_up_to else max(0, length - size)
            handle.seek(start)
            data = handle.read()
    except FileNotFoundError:
        return ""
    if start:
        cut = data.find(b"\n")
        data = data[cut + 1 :] if cut >= 0 else b""
    return data[: data.rfind(b"\n") + 1].decode("utf-8")


def _read_log(path: Path, tail_bytes: int, whole_up_to: int) -> str:
    """The whole file on a first read, the tail otherwise. A first read that cannot be decoded
    is the tail, as every later read would be: a stray byte before the tail (a crash in the
    middle of a character) must not blind the reader until the log rotates."""
    if whole_up_to:
        try:
            return read_tail(path, size=tail_bytes, whole_up_to=whole_up_to)
        except UnicodeDecodeError:
            pass  # the tail below is the answer; if it cannot be decoded either, that is named
    return read_tail(path, size=tail_bytes)


def split_entries(text: str, *, zone: tzinfo | None = None) -> list[Entry]:
    """The entries of ``text`` in file order; whatever precedes the first header (a tail read
    that began inside an entry) is dropped. ``zone`` is the zone the stamps are written in,
    the process's own when ``None``."""
    matches = list(_HEADER.finditer(text))
    entries: list[Entry] = []
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        body = text[match.start() : end].rstrip("\n")
        entries.append(Entry(_moment(match.group(1), zone), match.group(2), match.group(3), body))
    return entries


def _moment(stamp: str, zone: tzinfo | None) -> datetime | None:
    """The engine's naive stamp as an aware UTC moment. The log is written on this host by this
    process (the cron path runs on the same host), so the zone is the process's own unless the
    caller names one."""
    try:
        naive = datetime.strptime(stamp.replace(",", "."), _STAMP_FORMAT)
        local = naive.astimezone() if zone is None else naive.replace(tzinfo=zone)
        return local.astimezone(UTC)
    except _STAMP_ERRORS:
        return None


def newest_refusal(
    entries: Sequence[Entry], *, merge_seconds: float = MERGE_SECONDS
) -> Refusal | None:
    """The newest Gemini 429 among ``entries``, its fields borrowed from the entries of the same
    refusal (within ``merge_seconds`` before it) when its own text lacks them."""
    hits = [entry for entry in entries if entry.at is not None and _REFUSAL.search(entry.head)]
    if not hits:
        return None
    anchor = hits[-1]
    assert anchor.at is not None
    limit: int | None = None
    retry: float | None = None
    model: str | None = None
    for entry in reversed(hits):
        assert entry.at is not None
        if (anchor.at - entry.at).total_seconds() > merge_seconds:
            break
        message = entry.message
        if limit is None:
            limit = _int_in(_LIMIT, message)
        if retry is None:
            retry = _float_in(_RETRY, message)
        if model is None:
            model = _text_in(_MODEL, message)
    return Refusal(at=anchor.at.isoformat(), limit=limit, retry_seconds=retry, model=model)


def _int_in(pattern: re.Pattern[str], text: str) -> int | None:
    match = pattern.search(text)
    return int(match.group(1)) if match else None


def _float_in(pattern: re.Pattern[str], text: str) -> float | None:
    match = pattern.search(text)
    if match is None:
        return None
    value = float(match.group(1))
    return value if math.isfinite(value) else None


def _text_in(pattern: re.Pattern[str], text: str) -> str | None:
    match = pattern.search(text)
    return match.group(1) if match else None


# ----------------------------------------------------------------------------- the record


def refusal_from_record(cache: Mapping[str, Any] | None) -> Refusal | None:
    """The refusal the record remembers; ``None`` for no record or one that is not well formed.
    The record is JSON in the plugin's state, so every field is checked, none trusted."""
    if not isinstance(cache, Mapping):
        return None
    last = cache.get(RECORD_KEY)
    if not isinstance(last, Mapping):
        return None
    at = last.get("at")
    if not isinstance(at, str) or parse_timestamp(at) is None:
        return None
    limit = last.get("limit")
    retry = last.get("retry_seconds")
    model = last.get("model")
    return Refusal(
        at=at,
        limit=limit if _is_number(limit) and isinstance(limit, int) and limit >= 0 else None,
        retry_seconds=float(retry) if _is_number(retry) and retry >= 0 else None,
        model=model if isinstance(model, str) and model else None,
    )


def _is_number(value: object) -> TypeGuard[int | float]:
    """A finite number; an integer too large for a float is none (``isfinite`` would raise)."""
    if not isinstance(value, int | float) or isinstance(value, bool):
        return False
    try:
        return math.isfinite(value)
    except OverflowError:
        return False


def remember(cache: dict[str, Any], refusal: Refusal) -> None:
    cache[RECORD_KEY] = {
        "at": refusal.at,
        "limit": refusal.limit,
        "retry_seconds": refusal.retry_seconds,
        "model": refusal.model,
    }


def newer(first: Refusal | None, second: Refusal | None) -> Refusal | None:
    """The one of two refusals with the later moment; one that saw nothing yields to the other."""
    if first is None or first.at is None:
        return second
    if second is None or second.at is None:
        return first
    moment_first, moment_second = parse_timestamp(first.at), parse_timestamp(second.at)
    if moment_second is not None and (moment_first is None or moment_second > moment_first):
        return second
    return first


# ----------------------------------------------------------------------------- the windows


def activate(refusal: Refusal) -> Refusal:
    """``daily`` and ``active_until`` from the moment and the retry the provider asked for: a
    daily refusal is active until its reset, any other for ``LINE_MARK_SECONDS``."""
    moment = parse_timestamp(refusal.at)
    if moment is None:
        return refusal
    daily = refusal.retry_seconds is not None and refusal.retry_seconds > DAY_RETRY_SECONDS
    span = refusal.retry_seconds if daily and refusal.retry_seconds else LINE_MARK_SECONDS
    try:
        until = moment + timedelta(seconds=span)
    except OverflowError:
        return replace(refusal, daily=daily)
    return replace(refusal, daily=daily, active_until=until.isoformat())


def is_active(refusal: Refusal | None, now: datetime) -> bool:
    """Whether the refusal is still on the screen at ``now``."""
    if refusal is None or refusal.active_until is None:
        return False
    until = parse_timestamp(refusal.active_until)
    left = age_seconds(now, until) if until is not None else None
    return left is not None and left > 0


def hit_words(age: float) -> str:
    """``just now``, ``5 min ago``, ``2 h ago``: the age of the refusal in the backup's words."""
    return "just now" if age < 60 else describe_age(age)


def retry_words(seconds: float) -> str:
    """``42 s``, ``15 min``, ``4 h``: the retry the provider asked for; seconds rounded up, the
    coarser units to the nearest."""
    if seconds < 60:
        return f"{math.ceil(seconds)} s"
    if seconds < 3600:
        return f"{max(1, round(seconds / 60))} min"
    return f"{max(1, round(seconds / 3600))} h"


def event_title(age: float) -> str:
    """``Gemini out of quota 2 h ago``: 32 columns with the list dash at the longest age."""
    return f"{LABEL} out of quota {hit_words(max(0.0, age))}"


def incidents_for(refusal: Refusal | None, now: datetime) -> tuple[Incident, ...]:
    """A daily refusal is an event while it is active; a per-minute one is never an event."""
    if refusal is None or not refusal.daily or not is_active(refusal, now):
        return ()
    moment = parse_timestamp(refusal.at)
    age = age_seconds(moment, now) if moment is not None else None
    if age is None:
        return ()
    return (Incident(INCIDENT_ID, "warning", event_title(age)),)


# ----------------------------------------------------------------------------- the collector


def collect_gemini(
    env: Environment,
    cache: dict[str, Any] | None,
    *,
    now: datetime,
    zone: tzinfo | None = None,
    tail_bytes: int = TAIL_BYTES,
    first_read_bytes: int = FIRST_READ_BYTES,
) -> GeminiPart:
    """The Gemini line, the log as a source and the events, synchronously: the cron path calls
    this directly, the tick from a worker thread under its deadline.

    No ``logs/`` directory is ``unsupported`` (the home is not an engine's). A directory without
    the file, or an empty file, is ``fresh`` with nothing seen. A file that cannot be read is
    ``unavailable``, and the line still says what the record remembers.

    Until the first successful read, while the record holds no ``last_429``, a file of at most
    ``first_read_bytes`` is read whole and a larger one by its tail; after that the tail. The
    cron path keeps no record, so each of its runs is a first read.
    """
    store = cache if cache is not None else {}
    whole_up_to = first_read_bytes if RECORD_KEY not in store else 0
    if not logs_dir(env).is_dir():
        source = SourceObservation(SOURCE_NAME, "local", "unsupported", detail="no logs directory")
        return remembered_part(store, source, now=now)
    try:
        text = _read_log(log_path(env), tail_bytes, whole_up_to)
    except (OSError, UnicodeDecodeError) as exc:
        detail = f"log unreadable: {type(exc).__name__}"
        source = SourceObservation(SOURCE_NAME, "local", "unavailable", detail=detail)
        return remembered_part(store, source, now=now)
    remembered = _current(refusal_from_record(store), now)
    found = _current(newest_refusal(split_entries(text, zone=zone)), now)
    latest = newer(remembered, found)
    if latest is not None and latest is found:
        remember(store, found)
    store.setdefault(RECORD_KEY, None)
    store["checked_at"] = now.isoformat()
    source = SourceObservation(SOURCE_NAME, "local", "fresh", observed_at=now.isoformat())
    return _part(latest or Refusal(), source, now)


def remembered_part(
    cache: Mapping[str, Any] | None, source: SourceObservation, *, now: datetime
) -> GeminiPart:
    """The line from what the record remembers, beside ``source``: the log was not read this
    time (no directory, an unreadable file, the tick's deadline), so nothing is written."""
    return _part(_current(refusal_from_record(cache), now), source, now)


def _current(refusal: Refusal | None, now: datetime) -> Refusal | None:
    """A refusal dated ahead of ``now`` beyond the clock-skew tolerance is a clock that ran
    away, not a refusal to show."""
    if refusal is None or refusal.at is None:
        return refusal
    moment = parse_timestamp(refusal.at)
    age = age_seconds(moment, now) if moment is not None else None
    return None if age is None or is_from_the_future(age) else refusal


def _part(refusal: Refusal | None, source: SourceObservation, now: datetime) -> GeminiPart:
    active = activate(refusal) if refusal is not None else None
    metric = QuotaMetric(LABEL, "unsupported", detail=NO_QUOTA_REASON, refusal=active)
    return metric, source, incidents_for(active, now)
