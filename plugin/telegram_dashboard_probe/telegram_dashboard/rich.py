"""The pinned message as a Rich Message (Bot API 10.1+, ``rich_message: {"html": ...}``).

The main screen is the text screen's own blocks (``render.render_parts``), a section each with a
thin line between two (1.0.0, the look Ilya accepted in a live probe): the status with the time
of the data, what needs attention, the ✓ row, what was not checked, the limits as a table of
every window and its reset, the Hermes version. Then two collapsed ``<details>``: the cron jobs
with their schedules, and one Details with a section per source (the plans and logins, the
agent's rules, its memory, the Telegram channel, backup and drift, the Hermes version, every
"no data" with its reason), the screen's own stamps and the dashboard's version closing it, as
in the text. The two tables are the only words the text does not carry, and they come from the
same snapshot.

Every word goes through the HTML form's escaping and its ``<code>`` rule (``render._html_line``),
so a file name or a ``/command`` is never a tap target. A message over Telegram's ceilings is not
built: ``None``, and the tick edits the HTML form instead.
"""

from __future__ import annotations

import html
import re
from collections.abc import Iterable, Sequence
from datetime import UTC, datetime, tzinfo

from .freshness import DeliveryRecord
from .policy import sanitize_public_text
from .render import (
    OK_MARK,
    WARN_MARK,
    ScreenParts,
    _code_spans,
    _Details,
    _html_line,
    _quota_rows,
    render_parts,
)
from .schema import CronJob, CronSummary, DashboardSnapshot, QuotaMetric
from .timeparse import format_day_time, format_in_zone, parse_timestamp, to_zone

# Telegram's ceilings for one rich message (Bot API 10.1, "Rich Messages"): 32,768 characters of
# text and 500 blocks, a table row, a paragraph, a thin line and a details block each one. The
# engine counts the source the same way (``RICH_MESSAGE_MAX_CHARS``, telegram adapter, v0.22.2).
RICH_MESSAGE_MAX_CHARS = 32_768
RICH_MESSAGE_MAX_BLOCKS = 500
# A long list is cut with a line that says how many are left out, never silently.
JOB_ROWS_LIMIT = 60
PAUSED_LIMIT = 30
DONE_LIMIT = 20
PAUSED_MARK = "⏸"
# The thin line between two sections (1.0.0): the probe's message 2, the one Ilya accepted.
_HR = "<hr/>"
# The titles of the cron fold and of the sections of Details. Details takes them in the order a
# reader acts on them (1.0.0, ``_DETAILS_ORDER``): what is spent, what the agent sees, then the
# plumbing; cron is a fold of its own before Details.
_TITLES = {
    "limits": "🧠 Limits",
    "cron": "🕒 Cron",
    "rules": "📜 Agent rules",
    "memory": "🗂 Memory",
    "telegram": "📡 Telegram channel",
    "backup": "💾 Backup · Drift",
    "hermes": "🤖 Hermes version",
}
_DETAILS_ORDER = ("limits", "rules", "memory", "telegram", "backup", "hermes")
_NO_DATA_TITLE = "❔ No data"
_BLOCK_TAG = re.compile(r"<(?:p|tr|table|details|blockquote|li|h[1-6]|hr)\b")
_RUN_FAILURES = ("run", "delivery", "blocked")


def to_telegram_rich(
    snapshot: DashboardSnapshot,
    *,
    now: datetime | None = None,
    delivery: DeliveryRecord | None = None,
    period_seconds: int | None = None,
    zone: tzinfo = UTC,
) -> str | None:
    """The rich message's HTML, or ``None`` when it would not fit Telegram's ceilings."""
    parts = render_parts(
        snapshot, now=now, delivery=delivery, period_seconds=period_seconds, zone=zone
    )
    body = _main(parts, snapshot, zone) + _cron_fold(parts, snapshot, zone)
    body += _details_fold(parts.details)
    if (
        len(body) > RICH_MESSAGE_MAX_CHARS
        or len(_BLOCK_TAG.findall(body)) > RICH_MESSAGE_MAX_BLOCKS
    ):
        return None
    return body


# ----------------------------------------------------------------------------- the frame


def _main(parts: ScreenParts, snapshot: DashboardSnapshot, zone: tzinfo) -> str:
    """The main screen: a section per block of the text form, ``## `` bold, a thin line between
    two and none around them; the limits block its heading and a table."""
    sections = []
    for block in parts.blocks:
        if block.key == "limits":
            table = _limits_table(snapshot.capacity.quotas, parts.reference, zone)
            sections.append(_text(block.lines[:1]) + table)
        else:
            sections.append(_text(block.lines))
    return _HR.join(sections)


def _paragraph(lines: Iterable[str]) -> str:
    """Already escaped lines as one paragraph; nothing for none."""
    joined = "<br>".join(lines)
    return f"<p>{joined}</p>" if joined else ""


def _text(lines: Iterable[str]) -> str:
    """Lines of the text form, one paragraph."""
    return _paragraph(_html_line(line) for line in lines)


def _words(text: str) -> str:
    """Words that are no line of the text form (a job's name, a cell): escaped and coded, a
    leading ``#`` kept as it is, never read as a heading."""
    return _code_spans(html.escape(text))


def _details(summary: str, body: str) -> str:
    return f"<details><summary>{html.escape(summary)}</summary>{body}</details>" if body else ""


def _cell(text: str, *, header: bool = False, span: int = 1) -> str:
    tag = "th" if header else "td"
    wide = f' colspan="{span}"' if span > 1 else ""
    return f"<{tag}{wide}>{_words(text)}</{tag}>"


def _cron_fold(parts: ScreenParts, snapshot: DashboardSnapshot, zone: tzinfo) -> str:
    """The cron jobs, collapsed, after the main screen: the cron group's details lines and the
    jobs with their schedules."""
    if snapshot.cron is None:
        return ""
    lines = next((group.lines for group in parts.details.groups if group.key == "cron"), [])
    body = _text(lines) + _cron_body(snapshot.cron, parts.reference, zone)
    return _details(_cron_title(snapshot.cron), body)


def _details_fold(details: _Details) -> str:
    """One fold for everything that explains the screen (1.0.0): a section per source that wrote
    something, every "no data" with its reason, then the screen's own stamps and the dashboard's
    version, a thin line between two sections."""
    groups = {group.key: group.lines for group in details.groups}
    sections = [_section(_TITLES[key], groups.get(key, [])) for key in _DETAILS_ORDER]
    if details.missing:
        title = f"{_NO_DATA_TITLE} · {len(details.missing)}"
        sections.append(_section(title, details.missing))
    version = [details.version] if details.version else []
    sections.append(_text([*details.stamps(), *version]))
    return _details("Details", _HR.join(section for section in sections if section))


def _section(title: str, lines: Sequence[str]) -> str:
    """``<p><b>title</b><br>lines</p>``; nothing for no lines."""
    return _text([f"## {title}", *lines]) if lines else ""


# ----------------------------------------------------------------------------- the limits


def _limits_table(quotas: Sequence[QuotaMetric], reference: datetime | None, zone: tzinfo) -> str:
    """Every line of the limits block as a row (1.0.0): the provider, then a cell per window,
    the spent share and the time to its reset; a row with fewer cells than the widest spans its
    last one, a state in words spans them all."""
    rows = [row for quota in quotas for row in _quota_rows(quota, reference, zone)]
    if not rows:
        return ""
    width = max(len(cells) for _name, cells in rows)
    head = _cell("Provider", header=True) + _cell("Used · resets in", header=True, span=width)
    body = "".join(f"<tr>{_cell(name)}{_spread(cells, width)}</tr>" for name, cells in rows)
    return f"<table compact bordered><tr>{head}</tr>{body}</table>"


def _spread(cells: Sequence[str], width: int) -> str:
    """A row's cells, the last one spanning what the row leaves of ``width``."""
    last = len(cells) - 1
    return "".join(
        _cell(text, span=width - last if index == last else 1) for index, text in enumerate(cells)
    )


# ----------------------------------------------------------------------------- the cron jobs


def _cron_title(cron: CronSummary) -> str:
    """``🕒 Cron · 21 active · 2 paused · 4 done``: the counts the line and the list share."""
    if cron.state in ("unknown", "unsupported"):
        return _TITLES["cron"]
    done = sum(job.state == "done" for job in cron.jobs)
    words = [f"{cron.active or 0} active", f"{cron.paused or 0} paused"]
    if done:
        words.append(f"{done} done")
    return " · ".join([_TITLES["cron"], *words])


def _cron_body(cron: CronSummary, reference: datetime | None, zone: tzinfo) -> str:
    active = [job for job in cron.jobs if job.state == "active"]
    paused = [job for job in cron.jobs if job.state == "paused"]
    done = [job for job in cron.jobs if job.state == "done"]
    return (
        _jobs_table(active, reference, zone)
        + _paused_block(paused, reference, zone)
        + _done_block(done, reference, zone)
    )


def _jobs_table(jobs: Sequence[CronJob], reference: datetime | None, zone: tzinfo) -> str:
    """The active jobs, what fails now first: the name, the schedule in the engine's words, the
    next run (marked when overdue) and the last one (marked when it failed)."""
    if not jobs:
        return ""
    head = "".join(_cell(word, header=True) for word in ("Job", "Schedule", "Next", "Last"))
    rows = [_job_row(job, reference, zone) for job in jobs[:JOB_ROWS_LIMIT]]
    if len(jobs) > JOB_ROWS_LIMIT:
        rows.append(f"<tr>{_cell(f'… {len(jobs) - JOB_ROWS_LIMIT} more', span=4)}</tr>")
    return f"<table compact striped><tr>{head}</tr>{''.join(rows)}</table>"


def _job_row(job: CronJob, reference: datetime | None, zone: tzinfo) -> str:
    name = sanitize_public_text(job.name, limit=24)
    if job.failure is not None:
        name = f"{WARN_MARK} {name}"
    upcoming = _when(job.next_run_at, reference, zone) or "—"
    if job.failure == "overdue":
        upcoming = f"{WARN_MARK} {upcoming}"
    last = _when(job.last_run_at, reference, zone)
    if last is not None:
        last = f"{WARN_MARK if job.failure in _RUN_FAILURES else OK_MARK} {last}"
    return f"<tr>{_cell(name)}<td>{_schedule(job)}</td>{_cell(upcoming)}{_cell(last or '—')}</tr>"


def _schedule(job: CronJob) -> str:
    """The schedule whole in ``<code>``: ``*/30 * * * *`` is no command there, and reads as the
    expression it is."""
    if not job.schedule:
        return ""
    return f"<code>{html.escape(sanitize_public_text(job.schedule, limit=24))}</code>"


def _paused_block(jobs: Sequence[CronJob], reference: datetime | None, zone: tzinfo) -> str:
    """The paused jobs under their mark; one whose next run is behind says when it was due, and
    a line says why that is no alarm (decision of 22.10)."""
    if not jobs:
        return ""
    lines = [f"<b>{PAUSED_MARK} Paused · {len(jobs)}</b>"]
    lines.extend(_paused_line(job, reference, zone) for job in jobs[:PAUSED_LIMIT])
    if len(jobs) > PAUSED_LIMIT:
        lines.append(html.escape(f"… {len(jobs) - PAUSED_LIMIT} more"))
    if any(job.paused_past_due for job in jobs):
        why = "A paused job does not fire: its date is the one set before the pause."
        lines.append(f"<i>{html.escape(why)}</i>")
    return _paragraph(lines)


def _paused_line(job: CronJob, reference: datetime | None, zone: tzinfo) -> str:
    words = _words(sanitize_public_text(job.name, limit=24))
    schedule = _schedule(job)
    if schedule:
        words += f" · {schedule}"
    due = _when(job.next_run_at, reference, zone) if job.paused_past_due else None
    return f"{words} · was due {html.escape(due)}" if due else words


def _done_block(jobs: Sequence[CronJob], reference: datetime | None, zone: tzinfo) -> str:
    """The completed jobs, collapsed once more: the latest run first."""
    lines = []
    for job in jobs[:DONE_LIMIT]:
        name = sanitize_public_text(job.name, limit=24)
        last = _when(job.last_run_at, reference, zone)
        lines.append(_words(f"{name} · last {last}" if last else name))
    if len(jobs) > DONE_LIMIT:
        lines.append(_words(f"… {len(jobs) - DONE_LIMIT} more"))
    return _details(f"{OK_MARK} Done · {len(jobs)}", _paragraph(lines)) if jobs else ""


def _when(stamp: str | None, reference: datetime | None, zone: tzinfo) -> str | None:
    """``13:00`` on the screen's day, ``Oct 23 08:00`` on another; ``None`` when unreadable."""
    moment = parse_timestamp(stamp)
    local = to_zone(moment, zone) if moment is not None else None
    if moment is None or local is None:
        return None
    today = to_zone(reference, zone) if reference is not None else None
    if today is not None and local.date() == today.date():
        return format_in_zone(moment, zone, "%H:%M")
    return format_day_time(moment, zone)
