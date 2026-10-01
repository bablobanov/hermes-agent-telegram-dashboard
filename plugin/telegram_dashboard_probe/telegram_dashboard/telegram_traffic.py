"""Whether messages move through a Telegram adapter that says it is connected.

``is_connected`` on the engine's adapter is ``self._running``: the adapter was started. It says
nothing about updates arriving or replies leaving (hermes-agent#102260, #111727, #104799). The
adapter keeps four counters of its own for that, and this module reads them off the live object
the plugin already holds, as attributes, never calling it: ``_updates_received_total`` (updates
Telegram handed to getUpdates in this polling generation), ``_polling_generation`` (reset on every
reconnect), ``_polling_last_progress_monotonic`` (the last successful getUpdates round-trip) and
``_send_path_degraded`` (``send()`` answers ``send_path_degraded`` while it is set; the dashboard's
own edit does not check it, so the screen can move while every reply fails). A private attribute
is a capability read off the object, never a promise: absent or of another type, the line says
no data and why (``compat_matrix.json``, row ``telegram_traffic``).

Two verdicts are definite: the send path degraded past the reconnect grace (``no_sends``), and no
polling progress for ``STALL_SECONDS`` (``stalled``). One is a suspicion: no update for longer than
this installation usually goes without one (``quiet``): twice the longest gap seen in the last
``GAP_DAYS`` days, at least ``QUIET_FLOOR_SECONDS``, at most ``QUIET_CEILING_SECONDS``, and never
before the first update was seen at all (decision of 01.10: an absolute threshold lies at night on
a one-person bot, a relative one without a floor shouts on a chatty group at lunch, one without a
ceiling never surfaces a dead bot on a quiet installation).

The counter is read once per tick, so "last update seen" is the tick that saw it grow, not the
update's own time; the details say so. Failed sends are the engine's own ERROR line in
``logs/errors.log`` (``Failed to send Telegram message``); successes are not logged at that level,
so the last successful send the plugin can vouch for is its own confirmed edit (``Confirmed`` in
the details). The record (``traffic_cache``) keeps the generation, the counter, the last sighting,
the daily gaps, the first sighting of a blocked send path and the last send error.
"""

from __future__ import annotations

import math
import re
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, TypeGuard

from .backup import describe_age
from .compat import Environment
from .gemini_log import TAIL_BYTES, Entry, log_path, read_tail, split_entries
from .schema import Incident, SourceObservation, SourceState, TrafficState, TrafficSummary
from .timeparse import age_seconds, parse_timestamp

SOURCE_NAME = "telegram_traffic"
ATTR_RECEIVED = "_updates_received_total"
ATTR_GENERATION = "_polling_generation"
ATTR_PROGRESS = "_polling_last_progress_monotonic"
ATTR_GENERATION_STARTED = "_polling_generation_started_monotonic"
ATTR_DEGRADED = "_send_path_degraded"
STALL_SECONDS = 300.0
RECONNECT_GRACE_SECONDS = 120.0
QUIET_FLOOR_SECONDS = 6 * 3600.0
QUIET_CEILING_SECONDS = 48 * 3600.0
QUIET_FACTOR = 2.0
GAP_DAYS = 7
SEND_ERRORS_HOUR = 3
_SEND_ERROR = re.compile(r"Failed to send Telegram message")
_DAY = "%Y-%m-%d"

TrafficPart = tuple[TrafficSummary, SourceObservation, tuple[Incident, ...]]


@dataclass(frozen=True, slots=True)
class Probe:
    """The adapter's counters as read at one moment; ``problem`` names why they could not be."""

    received_total: int | None = None
    generation: int | None = None
    progress_age_seconds: float | None = None
    generation_age_seconds: float | None = None
    send_path_degraded: bool | None = None
    problem: str | None = None


# ----------------------------------------------------------------------------- the probe


def probe_adapter(adapter: object, *, monotonic: float | None = None) -> Probe:
    """The counters as attributes of the live adapter. Nothing is called on it: a property that
    raises is a problem named here, not an exception in the tick."""
    clock = time.monotonic() if monotonic is None else monotonic
    names = (ATTR_RECEIVED, ATTR_GENERATION, ATTR_PROGRESS, ATTR_GENERATION_STARTED, ATTR_DEGRADED)
    values: dict[str, object] = {}
    for name in names:
        try:
            values[name] = getattr(adapter, name, None)
        except Exception as exc:
            return Probe(problem=f"adapter attribute {name} raises {exc.__class__.__name__}")
    if all(values[name] is None for name in (ATTR_RECEIVED, ATTR_GENERATION, ATTR_DEGRADED)):
        return Probe(problem="adapter has no traffic counters")
    received = values[ATTR_RECEIVED]
    if not isinstance(received, int) or isinstance(received, bool):
        return Probe(problem=f"adapter counter {ATTR_RECEIVED} is not a number")
    generation = values[ATTR_GENERATION]
    degraded = values[ATTR_DEGRADED]
    if not isinstance(generation, int) or isinstance(generation, bool):
        generation = None
    return Probe(
        received_total=received,
        generation=generation,
        progress_age_seconds=_age(values[ATTR_PROGRESS], clock),
        generation_age_seconds=_age(values[ATTR_GENERATION_STARTED], clock),
        send_path_degraded=degraded if isinstance(degraded, bool) else None,
    )


def _age(value: object, clock: float) -> float | None:
    if _number(value):
        return max(0.0, clock - float(value))
    return None


def _number(value: object) -> TypeGuard[int | float]:
    return isinstance(value, int | float) and not isinstance(value, bool) and math.isfinite(value)


def _text(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


# ----------------------------------------------------------------------------- the rules


def quiet_threshold(gaps: Mapping[str, object]) -> float:
    """Twice the longest gap between updates this installation has seen lately, within the
    floor and the ceiling; the floor alone when nothing is known."""
    longest = max((float(gap) for gap in gaps.values() if _number(gap)), default=0.0)
    return min(QUIET_CEILING_SECONDS, max(QUIET_FLOOR_SECONDS, QUIET_FACTOR * longest))


def send_errors(entries: Sequence[Entry], *, now: datetime) -> tuple[str | None, int]:
    """The last failed send the engine logged and how many in the last hour."""
    hits = [entry for entry in entries if entry.at is not None and _SEND_ERROR.search(entry.head)]
    last_at = hits[-1].at if hits else None
    last = last_at.isoformat() if last_at is not None else None
    hour = 0
    for entry in hits:
        age = age_seconds(entry.at, now) if entry.at is not None else None
        if age is not None and 0 <= age <= 3600:
            hour += 1
    return last, hour


def _verdict(
    probe: Probe, store: Mapping[str, Any], now: datetime
) -> tuple[TrafficState, float | None, float]:
    """The state from the probe and the record, with how quiet the channel is and the threshold
    that applies; the definite verdicts first, the suspicion after them."""
    seen = parse_timestamp(store.get("last_update_seen_at"))
    quiet = age_seconds(seen, now) if seen is not None else None
    raw_gaps = store.get("gaps")
    threshold = quiet_threshold(raw_gaps if isinstance(raw_gaps, Mapping) else {})
    generation_age = probe.generation_age_seconds or 0.0
    # No progress since the last round-trip, or none at all since the generation started.
    progress_age = probe.progress_age_seconds
    idle = progress_age if progress_age is not None else generation_age
    state: TrafficState = "ok"
    if probe.send_path_degraded and generation_age > RECONNECT_GRACE_SECONDS:
        state = "no_sends"
    elif idle > STALL_SECONDS:
        state = "stalled"
    elif quiet is not None and quiet > threshold:
        state = "quiet"
    elif probe.send_path_degraded:
        state = "reconnecting"
    return state, quiet, threshold


def _polling_at(probe: Probe, now: datetime) -> str | None:
    if probe.progress_age_seconds is None:
        return None
    return (now - timedelta(seconds=probe.progress_age_seconds)).isoformat()


# ----------------------------------------------------------------------------- the record


def _note_updates(store: dict[str, Any], probe: Probe, now: datetime) -> None:
    """The counter grew in the same generation, or a new generation already carries updates: an
    update was seen by this tick. A new generation with a lower counter is a reset, not silence."""
    received = probe.received_total
    same = store.get("generation") == probe.generation
    previous = store.get("received_total")
    if not isinstance(previous, int) or isinstance(previous, bool):
        previous = None
    grew = received is not None and (
        (same and previous is not None and received > previous) or (not same and received > 0)
    )
    if grew:
        seen = parse_timestamp(store.get("last_update_seen_at"))
        gap = age_seconds(seen, now) if seen is not None else None
        cutoff = (now - timedelta(days=GAP_DAYS)).strftime(_DAY)
        raw = store.get("gaps")
        items = raw.items() if isinstance(raw, Mapping) else ()
        gaps: dict[str, float] = {
            day: float(value)
            for day, value in items
            if isinstance(day, str) and day >= cutoff and _number(value)
        }
        if gap is not None and gap >= 0:
            day = now.strftime(_DAY)
            gaps[day] = max(gaps.get(day, 0.0), gap)
        store["gaps"] = gaps
        store["last_update_seen_at"] = now.isoformat()
    store["generation"] = probe.generation
    store["received_total"] = received


def _note_degraded(store: dict[str, Any], probe: Probe, now: datetime) -> str | None:
    """The first sighting of a blocked send path, kept while it stays blocked."""
    if probe.send_path_degraded:
        since = _text(store.get("degraded_since")) or now.isoformat()
        store["degraded_since"] = since
        return since
    store.pop("degraded_since", None)
    return None


def _log_errors(
    env: Environment, store: dict[str, Any], now: datetime, tail_bytes: int
) -> tuple[str | None, int]:
    """The last failed send and the hour's count from the engine's log; a log that cannot be
    read leaves the record's last one and no count."""
    remembered = _text(store.get("last_send_error_at"))
    try:
        text = read_tail(log_path(env), size=tail_bytes)
    except (OSError, UnicodeDecodeError):
        return remembered, 0
    last, hour = send_errors(split_entries(text), now=now)
    last = last or remembered
    if last is not None:
        store["last_send_error_at"] = last
    return last, hour


# ----------------------------------------------------------------------------- the part


def collect_traffic(
    env: Environment,
    probe: Probe | None,
    cache: dict[str, Any] | None,
    *,
    now: datetime,
    tail_bytes: int = TAIL_BYTES,
) -> TrafficPart:
    """The block, the source and the events from the probe, the record and the log's tail."""
    store = cache if cache is not None else {}
    if probe is None:
        return _off("unsupported", "no adapter on the cron path")
    if probe.problem:
        return _off("unknown", probe.problem)
    _note_updates(store, probe, now)
    last_error, hour = _log_errors(env, store, now, tail_bytes)
    blocked_since = _note_degraded(store, probe, now)
    state, quiet, threshold = _verdict(probe, store, now)
    summary = TrafficSummary(
        state,
        last_update_seen_at=_text(store.get("last_update_seen_at")),
        polling_at=_polling_at(probe, now),
        sends_blocked_since=blocked_since,
        last_send_error_at=last_error,
        send_errors_hour=hour,
        quiet_seconds=quiet if state == "quiet" else None,
        threshold_seconds=threshold if state == "quiet" else None,
    )
    store["checked_at"] = now.isoformat()
    source = SourceObservation(SOURCE_NAME, "local", "fresh", observed_at=now.isoformat())
    return summary, source, incidents_for(summary)


def remembered_traffic(
    store: Mapping[str, Any], probe: Probe | None, *, now: datetime, detail: str
) -> TrafficPart:
    """What the probe and the record say without the log, when the log read missed its deadline
    or crashed: the same verdicts, the record's last send error, no hour count. Writes nothing."""
    if probe is None:
        return _off("unsupported", "no adapter on the cron path")
    if probe.problem:
        return _off("unknown", probe.problem)
    state, quiet, threshold = _verdict(probe, store, now)
    blocked_since = _text(store.get("degraded_since")) if probe.send_path_degraded else None
    summary = TrafficSummary(
        state,
        last_update_seen_at=_text(store.get("last_update_seen_at")),
        polling_at=_polling_at(probe, now),
        sends_blocked_since=blocked_since,
        last_send_error_at=_text(store.get("last_send_error_at")),
        send_errors_hour=0,
        quiet_seconds=quiet if state == "quiet" else None,
        threshold_seconds=threshold if state == "quiet" else None,
    )
    source = SourceObservation(SOURCE_NAME, "local", "unavailable", detail=detail)
    return summary, source, incidents_for(summary)


def _off(state: TrafficState, detail: str) -> TrafficPart:
    source_state: SourceState = "unsupported" if state == "unsupported" else "unavailable"
    summary = TrafficSummary(state, detail=detail)
    return summary, SourceObservation(SOURCE_NAME, "local", source_state, detail=detail), ()


def incidents_for(summary: TrafficSummary) -> tuple[Incident, ...]:
    events: list[Incident] = []
    if summary.state == "no_sends":
        events.append(Incident("telegram:no_sends", "critical", "Telegram: sends blocked"))
    elif summary.state == "stalled":
        events.append(Incident("telegram:stalled", "critical", "Telegram polling stalled"))
    elif summary.state == "quiet" and summary.quiet_seconds is not None:
        words = describe_age(summary.quiet_seconds).removesuffix(" ago")
        events.append(Incident("telegram:quiet", "warning", f"Telegram quiet for {words}"))
    if summary.send_errors_hour >= SEND_ERRORS_HOUR:
        title = f"Telegram: {summary.send_errors_hour} sends failed in 1 h"
        events.append(Incident("telegram:send_errors", "warning", title))
    return tuple(events)
