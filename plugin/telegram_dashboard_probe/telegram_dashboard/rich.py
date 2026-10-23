"""The pinned message as a Rich Message (Bot API 10.1+, ``rich_message: {"html": ...}``).

The main screen is the text screen's own blocks (``render.render_parts``), a section each with a
thin line between two (1.0.0, the look Ilya accepted in a live probe): the status with the time
of the data, what needs attention, the ✓ row, what was not checked, the limits as a table of
every window and its reset, the Hermes version. Then two collapsed ``<details>``: the cron jobs
as one table by how often they run, and one Details with a section per source (the plans and
logins, the agent's rules, its memory, the Telegram channel, backup and drift, the Hermes
version, every "no data" with its reason), the screen's own stamps and the dashboard's version
closing it, as in the text. The two tables are the only words the text does not carry, and
they come from the same snapshot.

Every word goes through the HTML form's escaping and its ``<code>`` rule (``render._html_line``),
so a file name or a ``/command`` is never a tap target. A message over Telegram's ceilings is not
built: ``None``, and the tick sends the fallback: the HTML form on an HTML message, the plain
words as one rich paragraph on a message that is rich already.
"""

from __future__ import annotations

import html
import re
from collections.abc import Callable, Iterable, Sequence
from datetime import UTC, datetime, tzinfo
from itertools import groupby

from .cron_frequency import OTHER
from .cron_jobs import NAME_LIMIT
from .freshness import DeliveryRecord
from .policy import sanitize_public_text
from .render import (
    WARN_MARK,
    ScreenParts,
    _code_spans,
    _Details,
    _html_line,
    _quota_rows,
    render_parts,
)
from .schema import CronJob, CronSummary, DashboardSnapshot, QuotaMetric
from .timeparse import format_day, format_short

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
# The header rows of the paused and the done jobs in the cron table (the owner, 24.10: the green
# mark beside the big pause sign, not the thin ✓); no count, the fold's title has it.
_PAUSED_HEADER = f"{PAUSED_MARK} Paused"
_DONE_HEADER = "✅ Done"
_FAILURE_NOTE = f"{WARN_MARK} before a job: its last run failed or it is overdue"
_PAUSED_NOTE = "A paused job does not fire: its date is the one set before the pause."
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
# A row of the cron table and whether it carries the failure mark before a name: the note under
# the table is read off the rows as built, never off a second cut of the list.
_Row = tuple[str, bool]
_BLOCK_TAG = re.compile(r"<(?:p|tr|table|details|blockquote|li|h[1-6]|hr)\b")


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
    # Over a ceiling there is no rich form, and that is the answer, not a fault: the tick falls
    # back (HTML on an HTML message, the plain words as one rich paragraph on a message that is
    # rich already). A long list of names full of ``&`` or ``"``, which escaping grows
    # five or six times, gets there (review of Task 3, 24.10).
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


def _cell(text: str, *, header: bool = False, span: int = 1, right: bool = False) -> str:
    tag = "th" if header else "td"
    wide = f' colspan="{span}"' if span > 1 else ""
    side = ' align="right"' if right else ""
    return f"<{tag}{wide}{side}>{_words(text)}</{tag}>"


def _cron_fold(parts: ScreenParts, snapshot: DashboardSnapshot, zone: tzinfo) -> str:
    """The cron jobs, collapsed, after the main screen: the cron group's details lines and the
    table of the jobs."""
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
    """The jobs as one table in the look of the limits table (1.0.0, the owner's pick of 24.10):
    the active under a shaded header row per frequency, the most frequent first, then the
    paused, then the done; the notes its rows call for under it."""
    active = _by_frequency([job for job in cron.jobs if job.state == "active"])
    paused = [job for job in cron.jobs if job.state == "paused"]
    done = [job for job in cron.jobs if job.state == "done"]
    rows = _active_rows(active, reference, zone)
    rows += _set_rows(_PAUSED_HEADER, paused, PAUSED_LIMIT, lambda job: job.next_run_at, zone)
    rows += _set_rows(_DONE_HEADER, done, DONE_LIMIT, lambda job: job.last_run_at, zone)
    if not rows:
        return ""
    table = "".join(markup for markup, _marked in rows)
    marked = any(marked for _markup, marked in rows)
    return f"<table compact bordered>{table}</table>" + _cron_notes(marked, paused)


def _by_frequency(jobs: Sequence[CronJob]) -> list[CronJob]:
    """The jobs by how often they run, ``Once`` first and ``Other`` last; one period with two
    labels (``Every 12 h``, ``Twice a day``) by the label. The sort is stable: inside a group
    the summary's order stays, what fails now first."""
    return sorted(jobs, key=lambda job: (job.frequency.period, job.frequency.label))


def _active_rows(jobs: Sequence[CronJob], reference: datetime | None, zone: tzinfo) -> list[_Row]:
    """A header row per frequency and its jobs with their next run; ``Other`` names each
    job's schedule, since its header does not say how often the job runs."""
    rows: list[_Row] = []
    for frequency, group in groupby(jobs[:JOB_ROWS_LIMIT], key=lambda job: job.frequency):
        rows.append(_header_row(frequency.label))
        rows.extend(
            _job_row(job, format_short(job.next_run_at, reference, zone), frequency == OTHER)
            for job in group
        )
    return rows + _more_rows(len(jobs) - JOB_ROWS_LIMIT)


def _set_rows(
    header: str,
    jobs: Sequence[CronJob],
    limit: int,
    stamp: Callable[[CronJob], str | None],
    zone: tzinfo,
) -> list[_Row]:
    """The paused or the done jobs under their header row, each with the day of ``stamp``."""
    if not jobs:
        return []
    rows = [_job_row(job, format_day(stamp(job), zone)) for job in jobs[:limit]]
    return [_header_row(header), *rows, *_more_rows(len(jobs) - limit)]


def _header_row(words: str) -> _Row:
    """The shaded header row across both columns; Telegram centres it, as the limits header."""
    return f"<tr>{_cell(words, header=True, span=2)}</tr>", False


def _more_rows(left_out: int) -> list[_Row]:
    """``… 5 more`` across both columns when a set was cut; nothing when it was not."""
    return [(f"<tr>{_cell(f'… {left_out} more', span=2)}</tr>", False)] if left_out > 0 else []


def _job_row(job: CronJob, date: str | None, with_schedule: bool = False) -> _Row:
    """The job's label, marked when it fails now, and its date on the right, ``—`` unread."""
    name = sanitize_public_text(job.label, limit=NAME_LIMIT)
    marked = job.failure is not None
    words = _words(f"{WARN_MARK} {name}" if marked else name)
    schedule = _schedule(job) if with_schedule else ""
    if schedule:
        words += f" · {schedule}"
    return f"<tr><td>{words}</td>{_cell(date or '—', right=True)}</tr>", marked


def _schedule(job: CronJob) -> str:
    """The schedule, up to 24 characters, in ``<code>``: ``*/30 * * * *`` is no command there,
    and reads as the expression it is."""
    if not job.schedule:
        return ""
    return f"<code>{html.escape(sanitize_public_text(job.schedule, limit=24))}</code>"


def _cron_notes(marked: bool, paused: Sequence[CronJob]) -> str:
    """One paragraph under the table with the lines that apply: what the mark before a name
    means when a row carries it, why a paused job's date can be behind (decision of 22.10)."""
    lines = []
    if marked:
        lines.append(html.escape(_FAILURE_NOTE))
    if any(job.paused_past_due for job in paused):
        lines.append(f"<i>{html.escape(_PAUSED_NOTE)}</i>")
    return _paragraph(lines)
