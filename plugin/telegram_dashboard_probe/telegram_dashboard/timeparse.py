"""Timestamp handling that cannot crash the renderer.

A timestamp that is valid in UTC can be unrepresentable after a zone shift: ``datetime.max`` in
UTC moved to ``Europe/Moscow`` raises ``OverflowError``, and ``OverflowError`` is not a
``ValueError``. Every conversion here returns ``None`` instead of raising, so one bad date in one
field degrades that field to "unknown" and leaves the rest of the message intact.
"""

from __future__ import annotations

from datetime import UTC, datetime, tzinfo

_CONVERSION_ERRORS = (OverflowError, ValueError, OSError, TypeError)

# Two hosts a few seconds apart are normal. A stamp further ahead of ``now`` than this is not
# "very fresh": it is a clock that ran away or a corrupted record, and it cannot be proven.
CLOCK_SKEW_TOLERANCE_SECONDS = 60.0


def is_from_the_future(age: float | None) -> bool:
    """``True`` when an age is negative beyond the clock-skew tolerance."""
    return age is not None and age < -CLOCK_SKEW_TOLERANCE_SECONDS


def to_zone(moment: datetime, zone: tzinfo) -> datetime | None:
    """``moment`` in ``zone``; ``None`` when naive, offset-less or unrepresentable there."""
    try:
        if moment.utcoffset() is None:
            return None
        return moment.astimezone(zone)
    except _CONVERSION_ERRORS:
        return None


def parse_timestamp(value: object) -> datetime | None:
    """ISO-8601 text or ``datetime`` as an aware UTC datetime; ``None`` for anything else.

    Naive input is taken as UTC. A parseable date that cannot be shifted into UTC is ``None``.
    """
    if isinstance(value, datetime):
        candidate = value
    elif isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        if text[-1] in "Zz":
            text = text[:-1] + "+00:00"
        try:
            candidate = datetime.fromisoformat(text)
        except _CONVERSION_ERRORS:
            return None
    else:
        return None
    if candidate.tzinfo is None:
        candidate = candidate.replace(tzinfo=UTC)
    return to_zone(candidate, UTC)


def age_seconds(observed: datetime, now: datetime) -> float | None:
    """Seconds from ``observed`` to ``now``; ``None`` when either side is unrepresentable."""
    observed_utc = to_zone(observed, UTC)
    now_utc = to_zone(now, UTC)
    if observed_utc is None or now_utc is None:
        return None
    try:
        return (now_utc - observed_utc).total_seconds()
    except _CONVERSION_ERRORS:
        return None


def _localized(value: object, zone: tzinfo) -> datetime | None:
    """``value`` parsed and shifted into ``zone``; ``None`` at either step."""
    moment = parse_timestamp(value)
    return None if moment is None else to_zone(moment, zone)


def format_in_zone(value: object, zone: tzinfo, fmt: str = "%Y-%m-%d %H:%M %Z") -> str | None:
    """``value`` formatted in ``zone``; ``None`` when it cannot be parsed or represented there.

    ``fmt`` should hold numeric directives only: ``%b`` and ``%a`` follow the process locale,
    and the gateway's locale is not the reader's. Day-and-month words come from ``day_words``.
    """
    local = _localized(value, zone)
    return None if local is None else _strftime(local, fmt)


def _strftime(local: datetime, fmt: str) -> str | None:
    try:
        return local.strftime(fmt)
    except _CONVERSION_ERRORS:
        return None


# English month abbreviations by hand: ``strftime("%b")`` answers in the process locale, and
# the screen must read the same on every host.
MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


def day_words(local: datetime) -> str:
    """``Sep 5``: the month word and the day without a leading zero, for an already local moment."""
    return f"{MONTHS[local.month - 1]} {local.day}"


def format_day(value: object, zone: tzinfo) -> str | None:
    """``Sep 25`` in ``zone``; ``None`` when ``value`` cannot be parsed or represented there."""
    local = _localized(value, zone)
    return None if local is None else day_words(local)


def format_day_time(value: object, zone: tzinfo) -> str | None:
    """``Sep 25 16:18`` in ``zone``; ``None`` on the same conditions as ``format_day``."""
    return _day_and(value, zone, "%H:%M")


def format_stamp(value: object, zone: tzinfo) -> str | None:
    """``Sep 25 16:18 +05``: day, time and the zone abbreviation; ``None`` as ``format_day``."""
    return _day_and(value, zone, "%H:%M %Z")


def _day_and(value: object, zone: tzinfo, clock_fmt: str) -> str | None:
    local = _localized(value, zone)
    if local is None:
        return None
    clock = _strftime(local, clock_fmt)
    return None if clock is None else f"{day_words(local)} {clock}"
