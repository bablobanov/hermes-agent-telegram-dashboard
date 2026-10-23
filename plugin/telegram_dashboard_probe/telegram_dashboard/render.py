"""Exception-first renderer for the pinned message: one phone screen.

The main screen in blocks (1.0.0, the look Ilya accepted in a live probe): the status with the
time of the data (the pinned-message header shows that line), what needs attention (up to five
incidents, then the line of everything that is not well), one row of what works (``✓ Gateway
✓ Telegram ✓ Backup ✓ Drift ✓ Cron``), one line of what could not be checked, one line per
provider under "🧠 Limits used" with the time to every reset (plus a line for every model's own
limit), the Hermes version line after them,
and everything that explains a line (ages, counts, reasons, per-source stamps, coverage) in a
details block that Telegram shows collapsed.
Nothing is dropped, only moved: a line without a number still names its reason, in the
details. Work and automation blocks render only when the snapshot carries them; ``None`` means
the block is not observed on this installation and nothing is invented for it.

The text is transport-neutral: ``## `` marks a bold line, ``#@`` the top line, ``> `` a details
line, a blank line parts two blocks, and ``to_telegram_html``/``to_telegram_plain`` turn it into
what a transport accepts. The rich form (``rich.py``) is built from the same parts
(``render_parts``), the blocks and the details kept by key.
"""

from __future__ import annotations

import html
import math
import re
import unicodedata
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, tzinfo

from . import __version__
from .backup import STALE_SECONDS as BACKUP_STALE_SECONDS
from .backup import STALE_WORDS as BACKUP_STALE_WORDS
from .backup import describe_age
from .context_files import platform_label
from .freshness import DeliveryRecord, classify_message_freshness, message_banner
from .gemini_log import hit_words, is_active
from .policy import sanitize_public_text
from .schema import (
    BackupSummary,
    CronFailure,
    CronSummary,
    DashboardSnapshot,
    DriftSummary,
    GatewaySummary,
    MemoryNotebook,
    MemoryProvider,
    MemoryQueue,
    MemorySummary,
    PlatformRules,
    QuotaMetric,
    QuotaWindow,
    Refusal,
    RulesSummary,
    SourceObservation,
    TrafficSummary,
    VersionSummary,
    WorkSummary,
)
from .timeparse import (
    age_seconds,
    format_day,
    format_day_time,
    format_in_zone,
    format_stamp,
    is_from_the_future,
    parse_timestamp,
)

TELEGRAM_TEXT_LIMIT = 4096
_SAFE_LIMIT = 3900

# A text check mark, not an emoji: the emoji forms render as a grey glyph or a green box on the
# phone (tried on 25.09), the plain glyph reads the same everywhere.
OK_MARK = "✓"
WARN_MARK = "⚠️"
# The ✓ row (1.0.0): a no-break space holds the mark to its name, so the row wraps between two
# items and never between a mark and the name it belongs to.
_ROW_SPACE = "\u00a0"
# What needs attention heads the main screen (1.0.0): the events, then the line of every item
# the ✓ row leaves out, so a broken item never leaves the screen.
ATTENTION_HEADING = "🛠 Needs attention"
# What the screen could not check, in one line (1.0.0): the sources not observed or stale, the
# profiles not seen, the sources that failed.
UNCHECKED_MARK = "❔"
# One coloured circle per screen, the top line's (1.0.0): the freshness banner's red or yellow
# becomes the warning mark on the dashboard. ``freshness.message_banner`` keeps its words, which
# are the watchdog's alert as well.
_BANNER_CIRCLES = ("🔴", "🟡")
# The top line (1.0.0): the status and the time of the data, bold. The neutral text carries it
# as a line of its own kind, ``#@<unix> <label> · updated <stamp>``, no number when the time is
# unreadable: the HTML and rich forms write the stamp as ``<tg-time format="r">``, which Telegram
# Desktop shows as "18 minutes ago" and Android as the words inside, so the words stay the full
# date with the zone; the plain form writes the words as they are, never in upper case.
_TOP_PREFIX = "#@"
_UPDATED = " · updated "
# No bar (decision of 25.09): the shade glyphs came from a fallback font and read as noise, the
# solid ones sat below the letters or wrapped the line, and a bar by fifths added nothing to the
# number after it. Every percent on the screen is the spent share; the heading says so, and its
# brain names the block at a glance: bold alone hardly shows in Telegram Desktop (25.09).
LIMITS_HEADING = "🧠 Limits used"
# The Hermes version line after the limits: information only, no sign and no advice (decision of
# 25.09: updating Hermes is a process, not a restart).
VERSION_MARK = "🤖"
# A release upstream published less than a day ago (0.11.0): the mark and its age on the version
# line, the time it came out in the details. Two columns wide on a phone, counted so.
NEW_MARK = "🆕"
NEW_RELEASE_SECONDS = 86_400
# A publication time this far ahead of the clock still reads "just out" (skew), not "no mark".
NEW_RELEASE_SKEW_SECONDS = 300
# A phone line; the version line keeps to it whatever the version string (a dev build, a local
# tag): a shorter form first, then the version cut with an ellipsis, whole in the details.
_LINE_COLUMNS = 32
_MINUTES_PER_DAY = 1440
# Windows carry no length label (decision of 25.09): the spent share and the time to its reset
# answer what the reader acts on, the plan (where the provider names it) is in the details, and
# a length the source does not state becomes a lie (the engine calls Codex's first window
# ``Session`` whatever its ``limit_window_seconds``; ``agent/account_usage.py``, 0.21.1 and
# 0.21.3). A label that narrows the scope stays: a limit for one model is not the account's limit.
_SCOPE_LABELS = {"Opus week": "Opus", "Sonnet week": "Sonnet"}
# A limit this far spent gets the warning mark on its line (decision of 25.09), and so does a
# window the provider itself calls warning or critical. Only the line: the overall status and the
# incidents come from the collectors, not from this number.
QUOTA_WARN_PERCENT = 90
_LOUD_SEVERITIES = ("warning", "critical")
# One-minute resolution on the screen: a stamp within a minute of the data time is the same.
_STAMP_SLACK_SECONDS = 60.0
_DETAILS_PREFIX = "> "

_STATUS_LABELS = {
    "normal": "🟢 Healthy",
    "warning": "🟡 Warning",
    "critical": "🔴 Critical",
    "unknown": "⚪ Unknown",
}
# The top line as the converters read it: only with one of the labels above, a number of digits
# alone for the tag, the stamp escaped and coded like every other word.
_TOP_LINE = re.compile(
    re.escape(_TOP_PREFIX)
    + r"(\d*) ("
    + "|".join(re.escape(label) for label in _STATUS_LABELS.values())
    + ")"
    + re.escape(_UPDATED)
    + "(.+)"
)
_PROCESS_LABELS = {
    "running": OK_MARK,
    "stopped": "stopped",
    "unknown": "unknown",
    "unsupported": "not observed",
}
_PLATFORM_LABELS = {
    "connected": OK_MARK,
    "degraded": "error",
    "disconnected": "disconnected",
    "unknown": "unknown",
    "unsupported": "not observed",
}
_DRIFT_LABELS = {
    "unknown": "unknown",
    "unsupported": "not observed",
}
_SOURCE_LABELS = {
    "gateway_state": "gateway",
    "limits": "limits",
    "grok_quota": "Grok quota",
    "kimi_quota": "Kimi quota",
    "gemini_log": "Gemini log",
    "drift": "drift",
    "backup": "backup",
    "cron": "cron",
    "cron_runs": "cron history",
    "telegram_traffic": "Telegram traffic",
    "context_files": "agent rules",
}
# The word on the Telegram line when the adapter is connected but the channel is deaf.
_TRAFFIC_WORDS = {"no_sends": "no sends", "stalled": "stalled", "quiet": "quiet"}
_FAILURE_WORDS = {
    "run": "run failed",
    "delivery": "not delivered",
    "blocked": "blocked",
    "overdue": "overdue since",
}


def _plural(count: int, one: str, many: str) -> str:
    """``1 key``, ``3 keys``, ``120,000 tokens``: the number with its noun."""
    return f"{count:,} {one if count == 1 else many}"


@dataclass
class _Group:
    """One source's lines in the details, under the key the rich form titles it by. ``joined``:
    the text form keeps it in the group before it, with no blank line between them."""

    key: str
    lines: list[str] = field(default_factory=list)
    joined: bool = False


@dataclass
class _Details:
    """What the collapsed block says, grouped (1.0.0, the order of the rich form's Details): one
    group per source, the reasons for every "no data", then the screen's own stamps and the
    dashboard's version last."""

    # The time of the top line, to the minute; the first of the screen's own stamps (1.0.0).
    screen: str | None = None
    confirmed: str | None = None
    data: str | None = None
    period: str | None = None
    coverage: str | None = None
    # The dashboard's own version (decision of 01.10): the installed copy, told from the message;
    # the last line of the block, a blank line before it (Ilya, 01.10, 0.9.2).
    version: str | None = None
    # A blank line between the sources (Ilya, 09.10, 0.11.0), in the order of the rich form's
    # Details (1.0.0): the plans, logins and refusals; the rules; the memory; Telegram; backup,
    # drift and cron; the Hermes version. A source writes into the last group.
    groups: list[_Group] = field(default_factory=lambda: [_Group("limits")])
    missing: list[str] = field(default_factory=list)

    @property
    def state(self) -> list[str]:
        return self.groups[-1].lines

    def section(self, key: str, *, joined: bool = False) -> None:
        """Start the next source's group; a source that wrote nothing leaves no blank line and no
        section: the next one takes its place, under its own key. A joined source joins the
        group it follows in the text; one that takes an empty place joins only what that place
        joined (cron after no backup and no drift starts a group, as it always did)."""
        current = self.groups[-1]
        if current.lines:
            self.groups.append(_Group(key, joined=joined))
        else:
            current.key = key
            current.joined = current.joined and joined

    def text_groups(self) -> list[list[str]]:
        """The groups as the text form separates them: a joined one stays in the one before."""
        merged: list[list[str]] = []
        for group in self.groups:
            if group.joined and merged:
                merged[-1] = [*merged[-1], *group.lines]
            else:
                merged.append(list(group.lines))
        return merged

    def stamps(self) -> list[str]:
        """The screen's own lines: its time, the confirmation, the data stamps, the period and
        the coverage."""
        parts = (self.screen, self.confirmed, self.data, self.period, self.coverage)
        return [part for part in parts if part]

    def lines(self) -> list[str]:
        groups = [
            *self.text_groups(),
            ["## No data", *self.missing] if self.missing else [],
            self.stamps(),
            [self.version] if self.version else [],
        ]
        body = _blank_separated(groups)
        if not body:
            return []
        return ["", *(_quoted(line) for line in ["## Details", *body])]

    def closing(self) -> str:
        """The end of the text a cut never takes (0.9.2, 1.0.0): the screen's stamps and the
        version, each after a blank line, exactly as ``lines`` ends."""
        tail = _blank_separated([self.stamps(), [self.version] if self.version else []])
        return "".join(f"\n{_quoted(line)}" for line in ["", *tail]) if tail else ""


def _blank_separated(groups: list[list[str]]) -> list[str]:
    """The lines of the groups that have any, a blank line between two."""
    out: list[str] = []
    for group in groups:
        if not group:
            continue
        if out:
            out.append("")
        out.extend(group)
    return out


def _quoted(line: str) -> str:
    """A details line of the neutral text: ``> text``, a bare ``>`` for a blank one."""
    return _DETAILS_PREFIX + line if line else _DETAILS_PREFIX.rstrip()


@dataclass(frozen=True)
class ScreenBlock:
    """One block of the main screen (1.0.0): the text form parts two with a blank line, the rich
    form makes each a section of its own; ``key`` names it (``limits`` is a table there)."""

    key: str
    lines: tuple[str, ...]


@dataclass(frozen=True)
class ScreenParts:
    """One screen before a transport form: the main screen's blocks in order (``## `` a bold
    line, ``#@`` the top line) and the details by source. ``reference`` is the time the ages are
    counted to."""

    blocks: tuple[ScreenBlock, ...]
    details: _Details
    reference: datetime | None

    @property
    def lines(self) -> tuple[str, ...]:
        """The main screen as the text form writes it."""
        return tuple(_blank_separated([list(block.lines) for block in self.blocks]))


@dataclass(frozen=True)
class _Item:
    """A thing the ✓ row names (1.0.0): its name there and its 0.12.0 line, which stands under
    "Needs attention" instead when it does not say the thing is well."""

    name: str
    line: str
    healthy: bool


def render_dashboard(
    snapshot: DashboardSnapshot,
    *,
    now: datetime | None = None,
    delivery: DeliveryRecord | None = None,
    period_seconds: int | None = None,
    zone: tzinfo = UTC,
) -> str:
    parts = render_parts(
        snapshot, now=now, delivery=delivery, period_seconds=period_seconds, zone=zone
    )
    details = parts.details
    text = "\n".join([*parts.lines, *details.lines()])
    # The screen's stamps and the version close the details (0.9.2, 1.0.0): a cut takes the
    # lines before them, never them.
    closing = details.closing()
    if len(text) <= _SAFE_LIMIT or not closing or not text.endswith(closing):
        return bound_text(text)
    # A cut at a blank line of the block leaves ``>…``, a line outside the quote that splits it
    # in two (review of 0.11.0): the ellipsis keeps the details prefix, in the one place kept.
    cut = bound_text(text.removesuffix(closing), _SAFE_LIMIT - len(closing) - 1)
    if cut.endswith("\n>…"):
        cut = cut.removesuffix(">…") + _DETAILS_PREFIX + "…"
    return cut + closing


def render_parts(
    snapshot: DashboardSnapshot,
    *,
    now: datetime | None = None,
    delivery: DeliveryRecord | None = None,
    period_seconds: int | None = None,
    zone: tzinfo = UTC,
) -> ScreenParts:
    """The blocks of the screen and its details, for any form: the text (``render_dashboard``)
    and the rich message (``rich.to_telegram_rich``) say the same. The blocks in order (1.0.0):
    the top line under the freshness banner, what needs attention, the ✓ row, what was not
    checked, the limits, the Hermes version, the work; an empty one is left out."""
    details = _Details()
    reference = now if now is not None else parse_timestamp(snapshot.observed_at)
    banner = _freshness(details, delivery, now, period_seconds, zone)
    # The message-staleness banner is the first line: it must be visible above the status label.
    head = [_dashboard_banner(banner)] if banner else []
    head.append(_top_line(snapshot, zone))
    details.screen = f"Screen {format_stamp(snapshot.observed_at, zone) or 'time unknown'}"
    limits = _limits_block(snapshot, reference, details, zone)
    _agent_in_details(snapshot, zone, details)
    items = _items(snapshot, reference, zone, details)
    details.section("hermes")
    hermes: tuple[str, ...] = ()
    if snapshot.version is not None:
        hermes = (_version_line(snapshot.version, details, zone, reference),)
    details.version = f"Dashboard {__version__}"
    unchecked = _unchecked_line(snapshot, details)
    blocks = (
        ScreenBlock("head", tuple(head)),
        ScreenBlock("attention", _attention(snapshot, items)),
        ScreenBlock("works", _works_row(items)),
        ScreenBlock("unchecked", (unchecked,) if unchecked else ()),
        ScreenBlock("limits", limits),
        ScreenBlock("hermes", hermes),
        ScreenBlock("work", _work_lines(snapshot.work)),
    )
    return ScreenParts(tuple(block for block in blocks if block.lines), details, reference)


def _dashboard_banner(banner: str) -> str:
    """The freshness banner with the warning mark for its red or yellow circle."""
    for circle in _BANNER_CIRCLES:
        if banner.startswith(circle):
            return WARN_MARK + banner.removeprefix(circle)
    return banner


def _top_line(snapshot: DashboardSnapshot, zone: tzinfo) -> str:
    """``#@1792781160 🟡 Warning · updated Oct 23 23:46 +05``: the status and the time of the
    data, the moment as a number for the forms that write it as a tag; ``updated time unknown``
    and no number when the time cannot be read or shown in the screen's zone."""
    label = _STATUS_LABELS[snapshot.overall]
    stamp = format_stamp(snapshot.observed_at, zone)
    moment = parse_timestamp(snapshot.observed_at)
    if stamp is None or moment is None:
        return f"{_TOP_PREFIX} {label}{_UPDATED}time unknown"
    return f"{_TOP_PREFIX}{int(moment.timestamp())} {label}{_UPDATED}{stamp}"


def _limits_block(
    snapshot: DashboardSnapshot, reference: datetime | None, details: _Details, zone: tzinfo
) -> tuple[str, ...]:
    """``🧠 Limits used`` and a line per provider; the plans, the logins and the refusals in the
    details, their first group."""
    quotas = snapshot.capacity.quotas
    if not quotas:
        return ()
    lines = [f"## {LIMITS_HEADING}"]
    for quota in quotas:
        lines.extend(_quota_lines(quota, reference, details, zone))
    details.data = _data_stamps(snapshot, zone)
    details.state.extend(_account_words(quotas, reference, zone))
    details.state.extend(_refusal_words(quotas, zone))
    return tuple(lines)


def _agent_in_details(snapshot: DashboardSnapshot, zone: tzinfo, details: _Details) -> None:
    """The agent's rules and its memory: a group each in the details, never a screen line."""
    details.section("rules")
    if snapshot.rules is not None:
        _rules_in_details(snapshot.rules, zone, details)
    details.section("memory")
    if snapshot.memory is not None:
        _memory_in_details(snapshot.memory, zone, details)


def _items(
    snapshot: DashboardSnapshot, reference: datetime | None, zone: tzinfo, details: _Details
) -> list[_Item]:
    """Gateway, Telegram, backup, drift and cron in the order of the ✓ row; a summary that is
    absent adds nothing. Their times, ages and counts go to the details: Telegram's group, then
    one with backup and drift, then cron's."""
    details.section("telegram")
    items = _gateway_items(snapshot.gateway, snapshot.traffic)
    if snapshot.traffic is not None:
        details.state.extend(_traffic_words(snapshot.traffic, zone))
    details.section("backup")
    if snapshot.backup is not None:
        items.append(_backup_item(snapshot.backup, reference, zone, details))
    if snapshot.drift is not None:
        items.append(_drift_item(snapshot.drift, zone, details))
    # A fold of its own in the rich form; one group with backup and drift in the text.
    details.section("cron", joined=True)
    if snapshot.cron is not None:
        line = _cron_line(snapshot.cron, reference, zone, details)
        items.append(_Item("Cron", line, snapshot.cron.state == "ok"))
    return items


def _attention(snapshot: DashboardSnapshot, items: list[_Item]) -> tuple[str, ...]:
    """``🛠 Needs attention``: the first five events in the collectors' order, then the line of
    every item the ✓ row leaves out, even one an event already names."""
    lines = [f"- {sanitize_public_text(incident.title)}" for incident in snapshot.incidents[:5]]
    lines.extend(item.line for item in items if not item.healthy)
    return (f"## {ATTENTION_HEADING}", *lines) if lines else ()


def _works_row(items: list[_Item]) -> tuple[str, ...]:
    """``✓ Gateway ✓ Telegram ✓ Backup ✓ Drift ✓ Cron``: every item that is well, no row
    without one."""
    names = [f"{OK_MARK}{_ROW_SPACE}{item.name}" for item in items if item.healthy]
    return (" ".join(names),) if names else ()


def _work_lines(work: WorkSummary | None) -> tuple[str, ...]:
    if work is None:
        return ()
    return (
        "## Work",
        (
            f"- Running: {work.executing} · "
            f"Queued: {work.queued} · "
            f"Waiting for a human: {work.waiting_human}"
        ),
        f"- Failed: {work.failed} · Unknown: {work.unknown}",
    )


def _freshness(
    details: _Details,
    delivery: DeliveryRecord | None,
    now: datetime | None,
    period_seconds: int | None,
    zone: tzinfo,
) -> str | None:
    if period_seconds:
        details.period = f"Period {max(1, period_seconds // 60)} min"
    if now is None or delivery is None or not period_seconds:
        return None
    freshness = classify_message_freshness(delivery, now=now, period_seconds=period_seconds)
    confirmed = format_in_zone(delivery.last_confirmed_at, zone, "%H:%M")
    details.confirmed = f"Confirmed {confirmed}" if confirmed else "Confirmed: not yet"
    return message_banner(freshness, delivery, now=now, period_seconds=period_seconds)


def _unchecked_line(snapshot: DashboardSnapshot, details: _Details) -> str | None:
    """``❔ Not checked: Gemini log (not on this installation), limits (stale), profiles 2/3``:
    every exception of the coverage in one line on the screen (1.0.0), the full count in the
    details."""
    coverage = snapshot.coverage
    missing = [source for source in snapshot.sources if source.state not in ("fresh", "stale")]
    stale = [source for source in snapshot.sources if source.state == "stale"]
    items = [f"{_source_label(source)} ({_source_reason(source.state)})" for source in missing]
    items.extend(f"{_source_label(source)} (stale)" for source in stale)
    if coverage.expected_profiles and coverage.observed_profiles != coverage.expected_profiles:
        items.append(f"profiles {coverage.observed_profiles}/{coverage.expected_profiles}")
    named = {source.name for source in (*missing, *stale)}
    failed = [name for name in coverage.failed_sources if name not in named]
    if failed:
        items.append(f"{_plural(len(failed), 'source', 'sources')} unavailable")
    details.coverage = _coverage_words(snapshot)
    return f"{UNCHECKED_MARK} Not checked: " + ", ".join(items) if items else None


def _coverage_words(snapshot: DashboardSnapshot) -> str | None:
    """``Profiles 1/1 · sources 10/10``: the full count, for the details."""
    parts: list[str] = []
    coverage = snapshot.coverage
    if coverage.expected_profiles:
        parts.append(f"profiles {coverage.observed_profiles}/{coverage.expected_profiles}")
    if snapshot.sources:
        seen = [source for source in snapshot.sources if source.state in ("fresh", "stale")]
        parts.append(f"sources {len(seen)}/{len(snapshot.sources)}")
    if not parts:
        return None
    joined = " · ".join(parts)
    return joined[0].upper() + joined[1:]


def _source_label(source: SourceObservation) -> str:
    return _SOURCE_LABELS.get(source.name, source.name)


def _source_reason(state: str) -> str:
    return "not on this installation" if state == "unsupported" else "unavailable"


def _gateway_items(gateway: GatewaySummary | None, traffic: TrafficSummary | None) -> list[_Item]:
    """``Gateway ✓`` and ``Telegram ✓``, the 0.12.0 line in two; when the adapter says connected
    and its own counters say the channel is deaf, the word says so instead. A disconnected
    adapter keeps its own word: the deaf verdict is only about a connected one."""
    if gateway is None:
        return []
    process = _PROCESS_LABELS[gateway.process]
    telegram = _PLATFORM_LABELS[gateway.telegram]
    if traffic is not None and gateway.telegram == "connected" and traffic.state in _TRAFFIC_WORDS:
        telegram = f"{WARN_MARK} {_TRAFFIC_WORDS[traffic.state]}"
    return [
        _Item("Gateway", f"Gateway {process}", gateway.process == "running"),
        _Item("Telegram", f"Telegram {telegram}", telegram == OK_MARK),
    ]


def _traffic_words(traffic: TrafficSummary, zone: tzinfo) -> list[str]:
    """The channel's times, always in the details: the last update seen, the last successful
    poll, since when the sends are blocked, how quiet it is against the usual, the last send
    error the engine logged. The first line is the channel's verdict, its mark in front (1.0.0):
    the warning mark when the counters say it is deaf."""
    if traffic.state in ("unsupported", "unknown"):
        reason = sanitize_public_text(traffic.detail or "not read", limit=60)
        return [f"Telegram traffic: {reason}"]
    seen = format_day_time(traffic.last_update_seen_at, zone) or "never"
    polling = format_in_zone(traffic.polling_at, zone, "%H:%M") or "unknown"
    mark = WARN_MARK if traffic.state in _TRAFFIC_WORDS else OK_MARK
    parts = [f"{mark} Telegram last update seen {seen} · polling ok {polling}"]
    since = format_in_zone(traffic.sends_blocked_since, zone, "%H:%M")
    if since:
        parts.append(f"sends blocked since {since}")
    if traffic.state == "quiet" and traffic.quiet_seconds is not None:
        words = f"quiet {describe_age(traffic.quiet_seconds).removesuffix(' ago')}"
        if traffic.usual_gap_seconds:
            usual = describe_age(traffic.usual_gap_seconds).removesuffix(" ago")
            words += f", usual gap up to {usual}"
        parts.append(words)
    last = format_day_time(traffic.last_send_error_at, zone)
    errors = f"last {last}" if last else "none in the log"
    return [" · ".join(parts), f"Telegram send errors: {errors}"]


def _cron_line(
    cron: CronSummary, reference: datetime | None, zone: tzinfo, details: _Details
) -> str:
    """``Cron ✓ 27 jobs``, the failures counted on the line, a dead or failing ticker named
    with its age; no data is never a zero."""
    if cron.state == "unsupported":
        reason = sanitize_public_text(cron.detail or "source not configured", limit=60)
        details.state.append(f"Cron: {reason}")
        return "Cron: not observed"
    if cron.state == "unknown":
        reason = sanitize_public_text(cron.detail or "not read", limit=60)
        details.state.append(f"Cron: {reason}")
        return "Cron: no data"
    details.state.extend(_cron_details(cron, zone))
    if cron.state in ("stalled", "ticks_failing"):
        stamp = cron.ticker_at if cron.state == "stalled" else cron.ticker_ok_at
        moment = parse_timestamp(stamp)
        age = age_seconds(moment, reference) if moment and reference else None
        what = "ticker silent" if cron.state == "stalled" else "ticks failing"
        if age is None:
            return f"Cron {WARN_MARK} {what}"
        words = describe_age(max(age, 0.0)).removesuffix(" ago")
        return f"Cron {WARN_MARK} {what} {words}"
    total = cron.active or 0
    if cron.state == "failing":
        return f"Cron {WARN_MARK} {len(cron.failing)} of {total} failing"
    return f"Cron {OK_MARK} {_plural(total, 'job', 'jobs')}"


def _cron_details(cron: CronSummary, zone: tzinfo) -> list[str]:
    ticker = format_in_zone(cron.ticker_at, zone, "%H:%M") or "no heartbeat"
    head = f"Cron {cron.active or 0} active · {cron.paused or 0} paused · ticker {ticker}"
    ok = format_in_zone(cron.ticker_ok_at, zone, "%H:%M")
    if ok and ok != ticker:
        head += f" · tick ok {ok}"
    if cron.detail:
        # A stamp the scan could not read, named beside the ticker it belongs to.
        head += f" ({sanitize_public_text(cron.detail, limit=60)})"
    words = [head]
    words.extend(_failure_words(failure, zone) for failure in (*cron.failing, *cron.held))
    if cron.ticker_error:
        words.append(f"Cron ticker error: {sanitize_public_text(cron.ticker_error, limit=24)}")
    return words


def _failure_words(failure: CronFailure, zone: tzinfo) -> str:
    name = sanitize_public_text(failure.name, limit=24)
    when = format_day_time(failure.at, zone) or "time unknown"
    text = f"Cron {name}: {_FAILURE_WORDS[failure.kind]} {when}"
    if failure.recovered_at:
        back = format_in_zone(failure.recovered_at, zone, "%H:%M") or "later"
        return f"{text}, ok since {back}"
    if failure.kind in ("run", "delivery"):
        text += f", {_streak_words(failure.streak)}"
    if failure.reason:
        # A kind in a word (``cron_jobs.error_kind``); sanitized once more, belt and braces.
        text += f" ({sanitize_public_text(failure.reason, limit=24)})"
    return text


def _streak_words(streak: int | None) -> str:
    """``3 in a row``, ``1 run``, ``streak unknown``: the history could not say."""
    if streak is None:
        return "streak unknown"
    return "1 run" if streak == 1 else f"{streak} in a row"


def _rules_in_details(rules: RulesSummary, zone: tzinfo, details: _Details) -> None:
    """The rules are a line per platform in the details, never a line of the main part: an
    agent that does not see its rules as they are is an event under "Needs attention"
    (``context_files.incidents_for``), which also makes the status a warning. A screen line
    beside the event said the same words twice (decision of 08.10, from the live window)."""
    if rules.state == "off":
        details.state.append("Rules: off in the dashboard settings")
        return
    if rules.state in ("unsupported", "unknown"):
        reason = sanitize_public_text(rules.detail or "not read", limit=60)
        details.state.append(f"Rules: {reason}")
        return
    if not rules.platforms:
        details.state.append("Rules: no platform session with a saved prompt yet")
        return
    details.state.extend(_rules_details(platform, zone) for platform in rules.platforms)


def _rules_details(rules: PlatformRules, zone: tzinfo) -> str:
    """One platform in the details: the files, the size, the session, what to do. Rules the
    agent sees as they are carry the mark in front (1.0.0)."""
    files = sanitize_public_text(", ".join(rules.files), limit=60) or "context file"
    session = format_day_time(rules.session_started_at, zone) or "time unknown"
    changed = format_day_time(rules.changed_at, zone)
    verdict = rules.verdict
    if verdict == "loaded":
        size = f" · {rules.chars:,} chars" if rules.chars else ""
        platform = platform_label(rules.platform)
        return f"{OK_MARK} Rules {platform}: {files}{size} · session {session}"
    if verdict == "truncated":
        kept = f"{rules.kept:,}" if rules.kept else "part"
        total = f" of {rules.chars:,} chars" if rules.chars else ""
        text = f"{files} cut, kept {kept}{total} · session {session}"
    elif verdict == "outdated" and rules.why == "gone":
        text = f"{files} in the prompt, no such file now · session {session}: /new"
    elif verdict == "outdated":
        when = f"changed {changed} after" if changed else "differ from the prompt of"
        text = f"{files} {when} session {session}: /new"
    elif verdict == "blocked":
        text = f"{files} refused by the engine's injection scan · session {session}"
    elif verdict == "none":
        text = _rules_none_words(rules, files, changed, session)
    elif verdict == "off":
        text = "context files off for it in the engine's config"
    elif verdict == "unknown" and rules.why == "config":
        text = f"{files} not in the prompt, config.yaml not read: skip unknown · session {session}"
    elif verdict == "unknown":
        text = f"rules in the prompt, the agent's directory not known here · session {session}"
    else:
        text = "no context file where the agent works"
    return f"Rules {platform_label(rules.platform)}: {text}"


def _memory_in_details(memory: MemorySummary, zone: tzinfo, details: _Details) -> None:
    """The memory has no line of its own on the screen, like the rules: a queue stuck, a
    notebook the engine cannot write to or a provider down or warning is an event under "Needs
    attention" (``memory.incidents_for``); the numbers are in the details. Never a word of the
    text."""
    if memory.state == "off":
        details.state.append("Memory: off in the dashboard settings")
        return
    if memory.state != "observed":
        reason = sanitize_public_text(memory.detail or "not read", limit=60)
        details.state.append(f"Memory: {reason}")
        return
    notebooks = " · ".join(_notebook_words(notebook) for notebook in memory.notebooks)
    unread = " · config.yaml not read" if memory.detail else ""
    details.state.append(f"Memory: {notebooks}{unread}")
    for queue in memory.queues:
        details.state.extend(_queue_words(queue, zone))
    if memory.provider is not None:
        details.state.append(_provider_words(memory.provider, zone))


def _notebook_words(notebook: MemoryNotebook) -> str:
    """``USER.md 1,218/1,375 (88%, one entry 76%)``: the share of one entry only when it holds
    half the limit or more."""
    name = notebook.name
    if notebook.enabled is False:
        return f"{name} off"
    if not notebook.readable:
        return f"{name} not UTF-8: the engine loads none of it and refuses every write"
    if notebook.limit == 0:
        return f"{name} no usable limit in config.yaml: every add refused"
    if notebook.chars is None:
        return f"{name} none yet"
    if not notebook.limit:
        return f"{name} {notebook.chars:,} chars"
    percent = notebook.chars * 100 // notebook.limit
    largest = notebook.largest * 100 // notebook.limit
    one = f", one entry {largest}%" if notebook.entries > 1 and largest >= 50 else ""
    return f"{name} {notebook.chars:,}/{notebook.limit:,} ({percent}%{one})"


def _queue_words(queue: MemoryQueue, zone: tzinfo) -> list[str]:
    """The approval queue in a line, and in a second what its adds would need: a queue of adds
    larger than the room left cannot be approved as it stands."""
    label = "Memory" if queue.subsystem == "memory" else "Skills"
    gate = {True: "on", False: "off", None: "unknown"}[queue.gate]
    landed = format_day_time(queue.applied_at, zone) if queue.applied_at else None
    last = f" · last write {landed or 'never'}" if queue.subsystem == "memory" else ""
    if not queue.count:
        if queue.subsystem == "skills" and not queue.gate:
            return []
        return [f"{label} queue: empty{last} · approval {gate}"]
    since = format_day(queue.oldest_at, zone) or "time unknown"
    lines = [f"{label} queue: {queue.count:,} waiting since {since}{last} · approval {gate}"]
    if queue.needs:
        needs = "; ".join(
            f"{name} {needed:,} chars, {free:,} free" for name, needed, free in queue.needs
        )
        tight = any(needed > free for _name, needed, free in queue.needs)
        lines.append(f"Waiting adds: {needs}" + (": they do not fit" if tight else ""))
    return lines


def _provider_words(provider: MemoryProvider, zone: tzinfo) -> str:
    name = sanitize_public_text(provider.name, limit=24)
    if not provider.log_read:
        return f"Memory provider {name}: errors.log not read"
    if provider.unavailable_at:
        warned = format_day_time(provider.unavailable_at, zone) or "time unknown"
        return f"Memory provider {name}: reports unavailable (said {warned}, once per start)"
    if not provider.errors:
        before = format_day_time(provider.last_error_at, zone) if provider.last_error_at else None
        return f"Memory provider {name}: no errors logged in 24 h" + (
            f", last {before}" if before else ""
        )
    last = format_day_time(provider.last_error_at, zone) or "time unknown"
    errors = _plural(provider.errors, "error", "errors")
    return f"Memory provider {name}: {errors} in 24 h, last {last}"


def _rules_none_words(rules: PlatformRules, files: str, changed: str | None, session: str) -> str:
    if rules.why == "after":
        return f"{files} changed {changed or 'later'}, after session {session}: /new"
    if rules.why == "not_utf8":
        return f"{files} not UTF-8, the engine skips it in silence: save it as UTF-8"
    if rules.why == "gateway_dir":
        return f"{files} only in the gateway's directory, the agent works in another"
    if rules.why == "home":
        return f"{files} only in HERMES_HOME, the agent works in another directory"
    return f"{files} in the agent's directory, not in the prompt of session {session}"


def _backup_item(
    backup: BackupSummary, reference: datetime | None, zone: tzinfo, details: _Details
) -> _Item:
    """The verdict and the age in words on the 0.12.0 line; the absolute time, the age, the
    integrity verdict and any reason in the details, the mark in front (1.0.0). Shown in every
    state; a state without a number names why. Well only with ✓ and no warning: an old backup is
    a line under "Needs attention"."""
    if backup.state == "unsupported":
        reason = sanitize_public_text(backup.detail or "source not configured", limit=60)
        details.state.append(f"Backup: {reason}")
        return _Item("Backup", "Backup: not observed", False)
    if backup.state == "unknown":
        reason = sanitize_public_text(backup.detail or "status not read", limit=60)
        details.state.append(f"Backup: {reason}")
        return _Item("Backup", "Backup: no data", False)
    stamp = format_day_time(backup.finished_at, zone) or "time unreadable"
    age, age_words = _backup_age(backup, reference)
    if backup.state == "failed":
        detail = sanitize_public_text(backup.detail or "reason not recorded", limit=120)
        details.state.append(f"{WARN_MARK} Backup {stamp} · {age_words} · {detail}")
        return _Item("Backup", f"Backup {WARN_MARK} failed {age_words}", False)
    verdict = sanitize_public_text(backup.integrity or "unknown", limit=24)
    stale = age is not None and age > BACKUP_STALE_SECONDS
    mark = WARN_MARK if stale else OK_MARK
    details.state.append(f"{mark} Backup {stamp} · {age_words} · integrity {verdict}")
    line = f"Backup {OK_MARK} {age_words}"
    if stale:
        line += f" {WARN_MARK} {BACKUP_STALE_WORDS}"
    return _Item("Backup", line, not stale)


def _backup_age(backup: BackupSummary, reference: datetime | None) -> tuple[float | None, str]:
    """The age of the run in seconds and in words; ``age unknown`` for a time that cannot be
    read or lies ahead of the clock."""
    finished = parse_timestamp(backup.finished_at)
    age = None
    if finished is not None and reference is not None:
        age = age_seconds(finished, reference)
    if age is None or is_from_the_future(age):
        return age, "age unknown"
    return age, describe_age(max(age, 0.0))


def _drift_item(drift: DriftSummary, zone: tzinfo, details: _Details) -> _Item:
    """``Drift ✓ 0 of 481`` on the 0.12.0 line; the count joins the check time in the details,
    the mark in front (1.0.0)."""
    checked = format_in_zone(drift.checked_at, zone, "%H:%M") if drift.checked_at else None
    if drift.state in ("clean", "drift") and drift.changed_keys is not None:
        if drift.total_keys is not None:
            count = f"{drift.changed_keys} of {drift.total_keys}"
        else:
            count = _plural(drift.changed_keys, "key", "keys")
        marker = OK_MARK if drift.state == "clean" else WARN_MARK
        when = f"checked {checked}" if checked else "check time unknown"
        details.state.append(f"{marker} Drift {count} · {when}")
        return _Item("Drift", f"Drift {marker} {count}", drift.state == "clean")
    label = _DRIFT_LABELS.get(drift.state, drift.state)
    if drift.detail:
        # Why there is no number; a check that never finished has no check time to show.
        reason = sanitize_public_text(drift.detail, limit=80)
        details.state.append(f"Drift: {reason}" + (f" · checked {checked}" if checked else ""))
    else:
        details.state.append(f"Drift checked {checked}" if checked else "Drift: check time unknown")
    return _Item("Drift", f"Drift: {label}", False)


def _quota_lines(
    quota: QuotaMetric, reference: datetime | None, details: _Details, zone: tzinfo
) -> list[str]:
    """The provider's line with the account's windows, then a line of its own for every model's
    limit (decision of 29.09, replacing decision 4 of the subscription plan: the model's limit is
    wanted in sight, and next to the account's windows it does not fit a phone line). A model's
    line leaves out the time to its reset when the account's window of the same label resets at
    the same moment: the account's line already says it. An expired login is said as such: it is
    an answer, not "no data"."""
    provider = sanitize_public_text(quota.provider, limit=40)
    if quota.kind == "expired":
        return [f"{provider} · login expired"]
    if quota.kind != "official" or not quota.windows:
        return [_quota_line(quota, reference, details, zone)]
    own = tuple(window for window in quota.windows if window.scope is None)
    if not own:
        return [_windows_line(provider, quota.windows, reference)]
    lines = [_windows_line(provider, own, reference)]
    for window in quota.windows:
        if window.scope is None:
            continue
        shown = replace(window, reset_at=None) if _resets_with(window, own) else window
        lines.append(f"{_mark((window,))}{provider} {_window_words(shown, reference)}")
    return lines


def _quota_rows(
    quota: QuotaMetric, reference: datetime | None, zone: tzinfo
) -> list[tuple[str, list[str]]]:
    """The rich table's rows for the lines ``_quota_lines`` writes (1.0.0): a row per line, the
    name as the line shows it, a cell per window (``37% · 3h``, the mark before a share the line
    marks loud) or the provider's state in words in one cell. The words come from the lines' own
    helpers, so the two never disagree; a model's limit spells its reset in its row, where its
    line leaves it to the account's line above. Nothing is written to the details."""
    provider = sanitize_public_text(quota.provider, limit=40)
    if quota.kind == "expired":
        return [(provider, ["login expired"])]
    if quota.kind != "official" or not quota.windows:
        return [(provider, [_state_cell(quota, reference, zone)])]
    own = tuple(window for window in quota.windows if window.scope is None)
    if not own:
        return [(provider, _line_cells(quota.windows, reference))]
    rows = [(provider, _line_cells(own, reference))]
    for window in quota.windows:
        if window.scope is not None:
            marked = bool(_mark((window,)))
            cells = _window_cells((window,), reference, marked=marked, headed=False)
            rows.append((f"{provider} {window.scope}", cells))
    return rows


def _line_cells(windows: tuple[QuotaWindow, ...], reference: datetime | None) -> list[str]:
    """The cells of a line ``_windows_line`` writes, marked where that line is."""
    numbered = any(window.used_percent is not None for window in windows)
    return _window_cells(windows, reference, marked=numbered and bool(_mark(windows)))


def _window_cells(
    windows: tuple[QuotaWindow, ...],
    reference: datetime | None,
    *,
    marked: bool,
    headed: bool = True,
) -> list[str]:
    """A cell per window, ``⚠️ 93% · 1h20m``: the mark only on a line the text marks, before the
    share that makes it loud. A line of words alone is one cell, the words as the line has them
    after its ``·``."""
    shares = [_share_words(window, reference, headed=headed) for window in windows]
    if all(window.used_percent is None for window in windows):
        words = " · ".join(share for share, _reset in shares)
        return [f"{WARN_MARK} {words}" if marked else words]
    return [
        (f"{WARN_MARK} " if marked and _mark((window,)) else "")
        + (f"{share} · {reset}" if reset else share)
        for window, (share, reset) in zip(windows, shares, strict=True)
    ]


def _state_cell(quota: QuotaMetric, reference: datetime | None, zone: tzinfo) -> str:
    """The cell of a provider without windows: the words ``_quota_line`` writes after its name."""
    if quota.kind == "local":
        return f"locally {_plural(quota.used or 0, 'token', 'tokens')}"
    if quota.kind == "official" and quota.used is not None and quota.limit:
        percent = round((quota.used / quota.limit) * 100)
        reset = _reset_words(quota.reset_at, reference)
        return f"{_warn(percent)}{percent}%" + (f" · {reset}" if reset else "")
    words, loud = _no_data_words(quota.refusal, reference, zone)
    return f"{WARN_MARK} {words}" if loud else words


def _resets_with(window: QuotaWindow, own: tuple[QuotaWindow, ...]) -> bool:
    """True when an account window of the same label resets within a minute of ``window``: the
    source rounds its stamps on its own (the Claude API differs in microseconds)."""
    moment = parse_timestamp(window.reset_at)
    if moment is None:
        return False
    for other in own:
        theirs = parse_timestamp(other.reset_at) if other.label == window.label else None
        if theirs is not None and abs((theirs - moment).total_seconds()) < 60:
            return True
    return False


def _account_words(
    quotas: tuple[QuotaMetric, ...], reference: datetime | None, zone: tzinfo
) -> list[str]:
    """``Plans: Claude Max 5x · Codex Prolite`` and ``Claude login until Oct 27``: only what a
    provider names; a plan nobody reports is not guessed."""
    words: list[str] = []
    plans = [
        f"{sanitize_public_text(q.provider, limit=40)} {sanitize_public_text(q.plan, limit=24)}"
        for q in quotas
        if q.plan
    ]
    if plans:
        words.append("Plans: " + " · ".join(plans))
    for quota in quotas:
        day = format_day(quota.login_expires_at, zone) if quota.login_expires_at else None
        if day is None:
            continue
        moment = parse_timestamp(quota.login_expires_at)
        ended = moment is not None and reference is not None and moment <= reference
        provider = sanitize_public_text(quota.provider, limit=40)
        words.append(f"{provider} login {'ended' if ended else 'until'} {day}")
    return words


def _quota_line(
    quota: QuotaMetric, reference: datetime | None, details: _Details, zone: tzinfo
) -> str:
    provider = sanitize_public_text(quota.provider, limit=40)
    if quota.kind == "local":
        details.missing.append(f"{provider}: remaining unknown, counted locally")
        return f"{provider} · locally {_plural(quota.used or 0, 'token', 'tokens')}"
    # "no data" always names its reason: the collector's own words, never a default that
    # reads as "we did not finish" when the truth is "the installation has no credential".
    if quota.kind == "unsupported":
        reason = sanitize_public_text(quota.detail or "source not found", limit=60)
        details.missing.append(f"{provider}: {reason}")
        return _no_data(provider, quota.refusal, reference, zone)
    if quota.kind == "unavailable":
        reason = sanitize_public_text(quota.detail or "source unavailable", limit=60)
        details.missing.append(f"{provider}: {reason}")
        return _no_data(provider, quota.refusal, reference, zone)
    if quota.windows:
        return _windows_line(provider, quota.windows, reference)
    if quota.used is not None and quota.limit:
        percent = round((quota.used / quota.limit) * 100)
        return f"{_warn(percent)}{provider} {percent}%{_reset_suffix(quota.reset_at, reference)}"
    details.missing.append(f"{provider}: windows not received")
    return _no_data(provider, quota.refusal, reference, zone)


def _no_data(
    provider: str, refusal: Refusal | None, reference: datetime | None, zone: tzinfo
) -> str:
    """``Gemini · no data``; while the provider's last refusal is active (decision of 29.09), the
    line says that instead, in plain words (decision of 30.09): ``⚠️ Gemini paused till 23:00``
    until a daily quota's reset, in the screen's zone, and ``⚠️ Gemini hit limit 5 min ago`` for
    any other. Only the line: the status and the events come from the collectors, a daily
    refusal's event among them."""
    words, loud = _no_data_words(refusal, reference, zone)
    return f"{WARN_MARK} {provider} {words}" if loud else f"{provider} · {words}"


def _no_data_words(
    refusal: Refusal | None, reference: datetime | None, zone: tzinfo
) -> tuple[str, bool]:
    """``("paused till 23:00", True)``: what ``_no_data`` says after the provider's name, and
    whether the line carries the mark; the rich table's cell says the same."""
    if refusal is None or reference is None or not is_active(refusal, reference):
        return "no data", False
    back = format_in_zone(refusal.active_until, zone, "%H:%M") if refusal.daily else None
    if back is not None:
        return f"paused till {back}", True
    moment = parse_timestamp(refusal.at)
    age = age_seconds(moment, reference) if moment is not None else None
    if age is None:
        return "no data", False
    return f"hit limit {hit_words(max(0.0, age))}", True


def _refusal_words(quotas: tuple[QuotaMetric, ...], zone: tzinfo) -> list[str]:
    """``Gemini refusals seen by Hermes: last Sep 29 14:03, per-minute limit, model …``: the
    last refusal the engine logged for a provider whose log is read, or that there is none. A
    script that calls the provider on its own is not in that log, hence "seen by Hermes"."""
    return [
        f"{sanitize_public_text(quota.provider, limit=40)} refusals seen by Hermes: "
        + _refusal_parts(quota.refusal, zone)
        for quota in quotas
        if quota.refusal is not None
    ]


def _refusal_parts(refusal: Refusal, zone: tzinfo) -> str:
    """Each part only when the log named it. The limit's number and the retry are not shown
    (decision of 30.09): without its unit the number says nothing, and a retry of seconds is
    long past when the screen is read."""
    if refusal.at is None:
        return "none"
    parts = [f"last {format_day_time(refusal.at, zone) or 'time unreadable'}"]
    kind = _limit_kind(refusal, zone)
    if kind is not None:
        parts.append(kind)
    if refusal.model:
        parts.append(f"model {sanitize_public_text(refusal.model, limit=40)}")
    return ", ".join(parts)


def _limit_kind(refusal: Refusal, zone: tzinfo) -> str | None:
    """``daily limit till Sep 26 23:00`` or ``per-minute limit``, as ``gemini_log.activate`` told
    them apart by the retry; ``daily limit`` alone when the reset cannot be shown (none was
    computed, or the screen's zone cannot represent it); nothing when the provider named no
    retry."""
    if refusal.daily:
        back = format_day_time(refusal.active_until, zone)
        return f"daily limit till {back}" if back is not None else "daily limit"
    return "per-minute limit" if refusal.retry_seconds is not None else None


def _windows_line(
    provider: str, windows: tuple[QuotaWindow, ...], reference: datetime | None
) -> str:
    """``Claude 37% (3h) · 12% (4d3h)``: every window in the provider's own order, each with the
    time to its reset. A line without a number says the provider's states in words. The line is
    never shortened to keep to the phone line (Ilya, 01.10, 0.9.2: accuracy over width): a long
    one wraps, its times stay exact and a number stays whole."""
    numbered = [window.used_percent for window in windows if window.used_percent is not None]
    words = " · ".join(_window_words(window, reference) for window in windows)
    if not numbered:
        return f"{provider} · {words}"
    return f"{_mark(windows)}{provider} {words}"


def _mark(windows: tuple[QuotaWindow, ...]) -> str:
    """The warning mark for a line: its most spent window from ``QUOTA_WARN_PERCENT`` on, or any
    window the provider itself calls warning or critical."""
    spent = [window.used_percent for window in windows if window.used_percent is not None]
    loud = any(window.severity in _LOUD_SEVERITIES for window in windows)
    if loud or (spent and max(spent) >= QUOTA_WARN_PERCENT):
        return f"{WARN_MARK} "
    return ""


def _window_words(window: QuotaWindow, reference: datetime | None) -> str:
    """``37% (3h)``, ``Opus 5% (4d)``, ``Fable 100% (4h)``; the provider's state in words when
    it gave no number; ``?`` otherwise. A state in words never becomes a number."""
    share, reset = _share_words(window, reference)
    return f"{share} ({reset})" if reset else share


def _share_words(
    window: QuotaWindow, reference: datetime | None, *, headed: bool = True
) -> tuple[str, str | None]:
    """``("Opus 37%", "3h")``: the spent share with the scope that narrows it (left out when the
    rich table's row names it: not ``headed``), and the time to its reset (``_reset_words``);
    the provider's state in words and no time when it gave no number."""
    scope = (window.scope or _SCOPE_LABELS.get(window.label)) if headed else None
    head = f"{scope} " if scope else ""
    if window.used_percent is not None:
        return f"{head}{window.used_percent:.0f}%", _reset_words(window.reset_at, reference)
    return f"{head}{window.note or '?'}", None


def _warn(percent: float) -> str:
    """The mark from ``QUOTA_WARN_PERCENT`` on, for the most spent window of the line."""
    return f"{WARN_MARK} " if percent >= QUOTA_WARN_PERCENT else ""


def _reset_suffix(value: object, reference: datetime | None) -> str:
    """`` (3h)``: the time to the reset; `` (?)`` when it cannot be counted; nothing without a
    reset date at all."""
    words = _reset_words(value, reference)
    return f" ({words})" if words else ""


def _reset_words(value: object, reference: datetime | None) -> str | None:
    """``3h``: the time to the reset; ``?`` when it cannot be counted; ``None`` without a reset
    date at all."""
    if not value:
        return None
    minutes = _minutes_until(value, reference)
    return _duration_words(minutes) if minutes is not None else "?"


def _minutes_until(value: object, reference: datetime | None) -> int | None:
    """Whole minutes to ``value`` from ``reference``, rounded up; a reset already behind the data
    time is 0. ``None`` when either side is unreadable."""
    moment = parse_timestamp(value)
    if moment is None or reference is None:
        return None
    ahead = age_seconds(reference, moment)
    if ahead is None:
        return None
    return max(0, math.ceil(ahead / 60))


def _duration_words(minutes: int) -> str:
    """The time to a reset, never earlier than the real one (Ilya, 01.10, 0.9.2): the time is
    when the quota is back. Under a day to the minute (``3h34m``, ``45m``, the minutes already
    rounded up by ``_minutes_until``); from a day on to the hour, the hour rounded up
    (``4d2h37m`` → ``4d3h``, ``1d23h59m`` → ``2d``). A zero second unit is not written (``3h``,
    ``4d``)."""
    if minutes >= _MINUTES_PER_DAY:
        days, hours = divmod(math.ceil(minutes / 60), 24)
        return f"{days}d{hours}h" if hours else f"{days}d"
    hours, mins = divmod(minutes, 60)
    if hours:
        return f"{hours}h{mins}m" if mins else f"{hours}h"
    return f"{mins}m"


def _version_line(
    version: VersionSummary, details: _Details, zone: tzinfo, reference: datetime | None
) -> str:
    """``🤖 Hermes 0.21.3 → 0.21.5``: the version the gateway runs, then the release upstream
    marks Latest; for a day after Latest came out, ``🆕`` and its age (``🤖 Hermes 0.21.3 →
    0.21.6 🆕 3h``). The dates, the count, the time it came out and the time of the check go to
    the details; a check that failed keeps the last answer, and the details say so."""
    checked = _checked(version.checked_at, zone)
    running = sanitize_public_text(version.running, limit=24) if version.running else None
    if running is None:
        details.missing.append(f"Hermes version: {_reason(version.local_reason)}")
    if version.latest is None:
        details.missing.append(f"Hermes latest: {_reason(version.reason)}{checked}")
        if running is None:
            return _no_version()
        line = _fit(running, " · no data")
        if running not in line:
            details.state.append(f"Hermes version {running}")
        return line
    latest = sanitize_public_text(version.latest, limit=24)
    latest_of = f"{latest}{_of(version.latest_published_at, zone)}"
    new = _new_release_age(version.latest_published_at, reference)
    if new is not None:
        out = format_day_time(version.latest_published_at, zone) or "time unknown"
        details.state.append(f"New release: Hermes {latest} out {out}")
    if version.reason:
        confirmed = format_day_time(version.confirmed_at, zone) or "time unknown"
        reason = sanitize_public_text(version.reason, limit=60)
        details.state.append(f"Hermes latest as of {confirmed}: {reason}{checked}")
        checked = ""
    if running is None:
        details.state.append(f"Hermes latest {latest_of}{checked}")
        return _no_version()
    details.state.extend(_version_details(version, running, latest_of, checked, zone))
    mark = f" {NEW_MARK} {new}" if new is not None else ""
    return _version_words(version.behind, running, latest, mark)


def _new_release_age(published: str | None, reference: datetime | None) -> str | None:
    """How long ago Latest came out, while that is less than a day: ``45m``, ``3h``."""
    stamp = parse_timestamp(published) if published else None
    if stamp is None or reference is None:
        return None
    age = (reference - stamp).total_seconds()
    if not -NEW_RELEASE_SKEW_SECONDS <= age < NEW_RELEASE_SECONDS:
        return None
    minutes = max(int(age // 60), 1)
    return f"{minutes}m" if minutes < 60 else f"{minutes // 60}h"


def _version_words(behind: int | None, running: str, latest: str, mark: str = "") -> str:
    if behind == 0:
        return _fit(running, f" {OK_MARK}{mark}")
    if behind is not None and behind > 0:
        return _fit(running, f" → {latest}{mark}", f"{VERSION_MARK} Hermes → {latest}{mark}")
    # Newer than Latest, or not on the list at all: no arrow, it would point the wrong way. The
    # mark is Latest's: a form without Latest goes without it, or it would date ours.
    shorter = f"{VERSION_MARK} Hermes {running}" + (" · newer" if behind is not None else "")
    return _fit(running, f" · latest {latest}{mark}", shorter)


def _width(line: str) -> int:
    """Columns on a phone: an emoji (the version and new-release marks) takes two, the text
    check mark and the arrow one."""
    return sum(2 if unicodedata.east_asian_width(char) in "WF" else 1 for char in line)


def _fit(running: str, after: str, *shorter: str) -> str:
    """``🤖 Hermes <running><after>`` within a phone line: as is, else the first shorter form
    that fits, else the running version cut with an ellipsis so ``after`` stays whole."""
    head = f"{VERSION_MARK} Hermes "
    for line in (f"{head}{running}{after}", *shorter):
        if _width(line) <= _LINE_COLUMNS:
            return line
    room = _LINE_COLUMNS - _width(head) - _width(after) - 1
    if room < 1:
        return f"{head}{running[: _LINE_COLUMNS - _width(head) - 1]}…"
    return f"{head}{running[:room]}…{after}"


def _version_details(
    version: VersionSummary, running: str, latest_of: str, checked: str, zone: tzinfo
) -> list[str]:
    ours = f"Hermes {running}{_of(version.running_published_at, zone)}"
    behind = version.behind
    if behind == 0:
        return [f"{ours} is the latest{checked}"]
    if behind is None:
        return [
            f"Hermes {running} not among the last {version.list_size} releases, "
            f"latest {latest_of}{checked}"
        ]
    if behind < 0:
        return [f"{ours} is newer than the latest {latest_of}{checked}"]
    return [
        f"{ours}, latest {latest_of}",
        f"{_plural(behind, 'release', 'releases')} behind{checked}",
    ]


def _no_version() -> str:
    return f"{VERSION_MARK} Hermes · no data"


def _of(published: str | None, zone: tzinfo) -> str:
    day = format_day(published, zone) if published else None
    return f" of {day}" if day else ""


def _checked(checked_at: str | None, zone: tzinfo) -> str:
    stamp = format_day_time(checked_at, zone) if checked_at else None
    return f" · checked {stamp}" if stamp else ""


def _reason(reason: str | None) -> str:
    return sanitize_public_text(reason, limit=60) if reason else "unknown"


def _data_stamps(snapshot: DashboardSnapshot, zone: tzinfo) -> str | None:
    """``Data HH:MM · Kimi HH:MM``: the screen's data time and every number read at another
    minute. A number read on its own cadence must not borrow the screen's stamp; when every
    stamp matches, the first line already says it and the details stay silent."""
    observed = parse_timestamp(snapshot.observed_at)
    exceptions: list[str] = []
    for quota in snapshot.capacity.quotas:
        has_number = bool(quota.windows) or (quota.used is not None and bool(quota.limit))
        if not has_number or not quota.fetched_at:
            continue
        provider = sanitize_public_text(quota.provider, limit=40)
        fetched = parse_timestamp(quota.fetched_at)
        if fetched is None:
            exceptions.append(f"{provider} time unreadable")
            continue
        gap = age_seconds(fetched, observed) if observed is not None else None
        if gap is None or abs(gap) >= _STAMP_SLACK_SECONDS:
            exceptions.append(f"{provider} {format_in_zone(fetched, zone, '%H:%M')}")
    if not exceptions:
        return None
    screen = format_in_zone(snapshot.observed_at, zone, "%H:%M") or "time unknown"
    return f"Data {screen} · " + " · ".join(exceptions)


def bound_text(text: str, limit: int = _SAFE_LIMIT) -> str:
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


def _split_details(line: str) -> tuple[bool, str]:
    """``(is_details, content)`` for a neutral line: ``> text`` and a bare ``>`` are details."""
    if line.startswith(_DETAILS_PREFIX):
        return True, line[len(_DETAILS_PREFIX) :]
    if line == _DETAILS_PREFIX.rstrip():
        return True, ""
    return False, line


def to_telegram_plain(text: str) -> str:
    """The screen for a transport that sends plain text: ``#``/``##`` headings become upper-case
    lines, the top line its words as they are (1.0.0), details lines lose their marker and stay
    in place, every other line is unchanged.

    The engine's ``TelegramAdapter.edit_message`` without ``finalize`` sets no parse mode, so a
    heading marker would reach the chat as a literal ``#``. Its ``finalize=True`` path converts
    to MarkdownV2 but splits an over-long payload into NEW continuation messages, which a pinned
    dashboard must never do. Upper case survives any transport; the details are simply shown.
    """
    out: list[str] = []
    for line in text.split("\n"):
        _, content = _split_details(line)
        top = _TOP_LINE.fullmatch(content)
        if top is not None:
            out.append(top.group(2) + _UPDATED + top.group(3))
        elif content.startswith("#"):
            out.append(content.lstrip("#").strip().upper())
        else:
            out.append(content)
    return "\n".join(out)


def to_telegram_html(text: str) -> str:
    """Escape for ``parse_mode=HTML``: headings become bold lines, the top line too with its
    stamp a ``<tg-time>`` (``_top_html``), a run of details lines becomes one ``<blockquote
    expandable>``, which Telegram shows collapsed, and a file name or a ``/command`` becomes
    ``<code>`` (``_code_spans``)."""
    out: list[str] = []
    quote: list[str] = []

    def flush() -> None:
        if quote:
            out.append("<blockquote expandable>" + "\n".join(quote) + "</blockquote>")
            quote.clear()

    for line in text.split("\n"):
        is_details, content = _split_details(line)
        if is_details:
            quote.append(_html_line(content))
        else:
            flush()
            out.append(_html_line(content))
    flush()
    return "\n".join(out)


def _html_line(line: str) -> str:
    top = _TOP_LINE.fullmatch(line)
    if top is not None:
        return _top_html(top)
    if line.startswith("#"):
        return f"<b>{_code_spans(html.escape(line.lstrip('#').lstrip()))}</b>"
    return _code_spans(html.escape(line))


def _top_html(top: re.Match[str]) -> str:
    """The top line bold, its stamp a ``<tg-time format="r">`` when the time was read: the one
    tag a line of the screen makes besides ``<b>`` and ``<code>``, its value digits alone, the
    words escaped and coded like every other word."""
    unix, label, stamp = top.groups()
    words = _code_spans(html.escape(stamp))
    if unix:
        words = f'<tg-time unix="{unix}" format="r">{words}</tg-time>'
    return f"<b>{_code_spans(html.escape(label + _UPDATED))}{words}</b>"


# A run of the characters a file name, a path, a command, a mention, a hashtag or a cashtag is
# made of.
_WORD = re.compile(r"[\w./@#$-]+")
# A name with a letter in it, or nothing (a dotfile), then a dot and an extension of two or more
# characters that starts with a letter: ``AGENTS.md``, ``.hermes.md``, ``.cursorrules``,
# ``state.db``, a config key ``display.platforms.max.cleanup_progress``. A version (``0.21.3``,
# ``kimi-k2.5``, ``gemini-2.5-pro``) has a digit there and stays plain.
_FILE_LIKE = re.compile(r"(?:[\w./-]*[^\W\d][\w./-]*)?\.[^\W\d][\w-]+")
# What Telegram finds inside a word: a command where a slash opens the word or follows a dot or a
# dash (``/new``, ``/2fa``, ``./new``, ``/new-session``; a fraction ``1/1`` is not one), and a
# domain before the word's end or a slash (``x.com/y``, ``2.ai``, ``AGENTS.md/``).
_ENTITY_INSIDE = re.compile(r"(?:^|[.-])/\w|\.[^\W\d_]{2,}(?=$|/)")
# A mention, a hashtag or a cashtag where Telegram finds one (0.12.0: the rich screen lists every
# cron job by name, and a name is whatever the owner typed): ``@``, ``#`` or ``$`` after no word
# character (``#weekly``, ``report-#weekly``, ``deploy/@ops``, ``$BTC``, ``$@user``). ``C#`` and
# ``a@b`` are none of them.
_MARK_START = re.compile(r"(?<!\w)(?:[@#]\w|\$[^\W\d_])")


def _code_spans(escaped: str) -> str:
    """Put every word that looks like a file name or a ``/command`` in ``<code>``, by its form,
    not by a list. Telegram makes a link of any word ending in a top-level domain (``.md``,
    ``.py``, ``.sh``) and a command of any ``/word``, ``parse_mode=HTML`` or not: a tap on
    ``AGENTS.md`` opened a site, a tap on ``/new`` would have reset the session of the chat the
    dashboard lives in (window of 07.10). Inside ``<code>`` it finds neither, and a tap copies the
    word. ``escaped`` is already HTML-escaped: an entity (``&#x27;``) has no dot and no slash."""
    return _WORD.sub(_code_word, escaped)


def _code_word(match: re.Match[str]) -> str:
    word = match.group()
    if match.start() and match.string[match.start() - 1] == "&":
        return word  # the inside of an entity (``&#x27;``), never a hashtag
    core = word.rstrip(".")
    if _FILE_LIKE.fullmatch(core) or _ENTITY_INSIDE.search(core) or _MARK_START.search(core):
        return f"<code>{core}</code>{word[len(core) :]}"
    return word
