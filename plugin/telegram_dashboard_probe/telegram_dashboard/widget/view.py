"""The widget's screen from one collection: the dashboard's own parts, words and thresholds.

``view()`` turns a ``DashboardSnapshot`` and its ``render.render_parts`` (the structured middle
both of the dashboard's text forms are drawn from) into a ``View`` free of the package's types:

- the status, the headline and the Status blocks are the topic's (``render_parts``)
- the limits: a card per provider the topic draws a line for (``render.drawn_quotas``); its
  columns are the account's windows in the source's order, under the source's own labels
  (``Session``, ``Weekly``, ``Week``: the owner's choice of 11.11); a model's window goes under
  the account window of its label, its reset said once when the dashboard calls it the same
  moment (``render._resets_with``); the shares, the reset words and the 90% mark are
  ``render``'s; a window the source did not send is not drawn
- the disk is the collector's (``DiskSummary``) in ``disk.space_words``; a low disk is the
  dashboard's own incident, on the screen with the others
- the cron jobs grouped by ``rich._by_frequency`` (``Once`` first, ``Other`` last), inside a
  group in the summary's order, the next run in ``timeparse.format_short`` words; ``Other``
  names each job's schedule, a paused job the day of its next run, a done one of its last
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, tzinfo
from itertools import groupby
from typing import Any

from .. import render, rich, timeparse
from ..cron_frequency import OTHER
from ..cron_jobs import NAME_LIMIT as JOB_NAME_LIMIT
from ..disk import space_words
from ..schema import CronFailure, CronJob, CronSummary, DashboardSnapshot, QuotaMetric, QuotaWindow

SEVERITIES = ("normal", "warning", "critical", "unknown")
UNAVAILABLE = "Dashboard data unavailable"
# The Status tab's blocks of ``render_parts`` in the dashboard's order; ``head`` gives the
# headline, the limits are cards of their own (``View.providers``).
STATUS_BLOCKS = ("attention", "works", "unchecked", "hermes", "work")
# Details groups shown elsewhere: the cron jobs are the Cron tab.
NOT_IN_DETAILS = ("cron",)
FAILURE_WORDS = {
    "run": "failed",
    "delivery": "not delivered",
    "blocked": "blocked",
    "overdue": "overdue",
}
TICKER_WORDS = {"stalled": " · stalled", "ticks_failing": " · ticks failing"}
NAME_LIMIT = 24
FAILING_NAME_LIMIT = 32
UNCHECKED_PREFIX = "Not checked: "

# A bar: [label, percent or words, severity 0/1/2] and, when its cell's bars do not share one,
# its own reset in words. A cell: [bars, reset words or None]. A provider: [name, cells] or
# [name, None, words] for one without numbers.
Bar = list[Any]
Cell = list[Any]
Provider = list[Any]


@dataclass(frozen=True)
class CronView:
    """The Cron tab. ``head`` the summary; ``failing`` ``[name, words, note]``; ``groups``
    ``(frequency, rows)`` from the most frequent, a row ``[name, minutes from the collection's
    minute to the next run or None, the next run in words]``; ``paused`` and ``done`` rows
    ``[name, day]``."""

    head: str
    failing: tuple[list[str], ...] = ()
    groups: tuple[tuple[str, tuple[list[Any], ...]], ...] = ()
    paused: tuple[list[str], ...] = ()
    done: tuple[list[str], ...] = ()


@dataclass(frozen=True)
class View:
    """One screen for the widget. ``disk`` is ``[words, % used, severity]``; ``settling``: the
    gateway is not running yet or Telegram not connected; ``zone``: the dashboard's display zone,
    the header's clock is in it."""

    status: str
    headline: str
    blocks: tuple[tuple[str, tuple[str, ...]], ...] = ()
    details: tuple[tuple[str, tuple[str, ...]], ...] = ()
    providers: tuple[Provider, ...] = ()
    disk: list[Any] | None = None
    cron: CronView | None = None
    settling: bool = False
    zone: tzinfo = field(default=UTC)


def unavailable(reason: str, zone: tzinfo = UTC) -> View:
    """A screen without the dashboard's data; ``zone`` keeps the header's clock in the owner's."""
    return View("unknown", UNAVAILABLE, (("note", (reason,)),), zone=zone)


# ----------------------------------------------------------------------------- the head


def headline(line: str) -> str:
    """``#@1790456400 🟢 Healthy · updated Sep 26 21:00 UTC`` → ``Healthy``."""
    words = line.split(" · ", 1)[0].split()
    while words and (words[0].startswith("#@") or not any(ch.isalnum() for ch in words[0])):
        words.pop(0)
    return " ".join(words) or "Status"


def unchecked_items(line: str) -> list[str]:
    """``❔ Not checked: Gemini log (not on this installation), profiles 2/3`` → its items; a
    comma inside parentheses stays in its item. The card's title is the widget's."""
    _, found, rest = line.partition(UNCHECKED_PREFIX)
    text = rest if found else line
    items, depth, start = [], 0, 0
    for index, char in enumerate(text):
        depth += (char == "(") - (char == ")")
        if depth == 0 and text.startswith(", ", index):
            items.append(text[start:index])
            start = index + 2
    items.append(text[start:])
    return [item.strip() for item in items if item.strip()]


# ----------------------------------------------------------------------------- the limits


def severity(percent: int | None, said: str | None) -> int:
    """0 normal, 1 warning (the dashboard's 90% mark or the provider's word), 2 critical (100%
    or the provider's word)."""
    if said == "critical" or (percent is not None and percent >= 100):
        return 2
    warned = percent is not None and percent >= render.QUOTA_WARN_PERCENT
    return 1 if said in ("warning", "critical") or warned else 0


def title(label: str) -> str:
    """The source's label with its first letter raised: ``week`` → ``Week``, ``Weekly`` stays."""
    return (label[:1].upper() + label[1:])[:NAME_LIMIT] if label else "Limit"


def resets(words: str | None) -> str:
    return f"resets in {words}" if words else ""


def _bar(window: QuotaWindow, now: datetime) -> tuple[Bar, str]:
    """A window's bar (its label is the cell's business) and its reset in words."""
    share, reset = render._share_words(window, now, headed=False)
    percent = None if window.used_percent is None else round(window.used_percent)
    value: int | str = percent if percent is not None else share[:NAME_LIMIT]
    name = (window.scope or "")[:NAME_LIMIT]
    return [name, value, severity(percent, window.severity)], resets(reset)


def cells(quota: QuotaMetric, now: datetime) -> list[Cell]:
    """The account's windows in the source's order, a cell each; a model's window in the cell of
    the account window of its label (a cell of its own without one), with its own reset when
    the dashboard does not call it the same moment as that window's."""
    own = tuple(window for window in quota.windows if window.scope is None)
    found: list[tuple[str, list[Bar], str]] = []
    for window in own:
        bar, reset = _bar(window, now)
        found.append((window.label.lower(), [bar], reset))
    for window in quota.windows:
        if window.scope is None:
            continue
        bar, reset = _bar(window, now)
        home = next((cell for cell in found if cell[0] == window.label.lower()), None)
        if home is None:
            found.append((window.label.lower(), [bar], reset))
            continue
        if not render._resets_with(window, own) and reset:
            bar.append(reset)
        home[1].append(bar)
    out: list[Cell] = []
    for label, bars, reset in found:
        apart = any(len(bar) > 3 for bar in bars[1:])
        if apart and reset:
            bars[0] = [*bars[0], reset]  # every bar of the cell says its own reset
        out.append([title(label), bars, None if apart else reset])
    return out


def provider(quota: QuotaMetric, now: datetime, zone: tzinfo) -> Provider:
    """A provider as the card draws it, the words ready (the widget only places them):
    ``[name, cells(, line)]``, a cell ``[bars, reset]``, a bar ``[label, value, severity(,
    reset)]``; ``[name, None, words]`` for one without numbers.

    Two cells stand side by side, no head line: the window's title labels its account's bar,
    the models' bars their names; a cell of models only has the title in its reset line. One
    cell takes the full width and its title and reset go into the name's line (1A, 11.11):
    named bars (``All``, the models) under it; a single unnamed bar keeps the title as its
    label, the reset alone in the name's line."""
    name = quota.provider[:NAME_LIMIT]
    if quota.kind == "expired":
        return [name, None, render._LOGIN_EXPIRED]
    if quota.kind != "official" or not quota.windows:
        return [name, None, render._state_cell(quota, now, zone)[:48]]
    found = cells(quota, now)
    one = len(found) == 1
    out: list[Cell] = []
    line = ""
    for head, bars, reset in found:
        named = len(bars) > 1 or bool(bars[0][0])
        account = "All" if one and named else head
        drawn = [[bar[0] or account, *bar[1:]] for bar in bars]
        titled = one or any(not bar[0] for bar in bars)  # else no bar carries the title
        words = " · ".join(w for w in ("" if titled else head, reset or "") if w)
        out.append([drawn, "" if one else words])
        if one:
            line = " · ".join(w for w in (head if named else "", reset or "") if w)
    return [name, out, line] if one else [name, out]


# ----------------------------------------------------------------------------- the disk


def disk_row(snapshot: DashboardSnapshot) -> list[Any] | None:
    """``["24 GB free of 134", % used, severity]``; ``None`` when the collector read no disk."""
    disk = snapshot.disk
    if disk is None or not disk.total_bytes or disk.free_bytes is None:
        return None
    free, total = disk.free_bytes, disk.total_bytes
    used = round(100 - free / total * 100)
    return [space_words(free, total), max(0, min(used, 100)), int(disk.state == "low")]


# ----------------------------------------------------------------------------- the cron


def minute(moment: datetime) -> int:
    """The moment's minute since the epoch: the clock minute it falls in."""
    return math.floor(moment.timestamp() / 60)


def run_in(value: object, now: datetime) -> int | None:
    """Clock minutes from the collection's minute to a run's; ``None`` when unreadable."""
    moment = timeparse.parse_timestamp(value)
    return None if moment is None else minute(moment) - minute(now)


def failure_note(failure: CronFailure, zone: tzinfo) -> str:
    words = [timeparse.format_day_time(failure.at, zone) or "time unknown"]
    if failure.kind in ("run", "delivery") and failure.streak:
        words.append("1 run" if failure.streak == 1 else f"{failure.streak} in a row")
    if failure.reason:
        words.append(str(failure.reason)[:NAME_LIMIT])
    return " · ".join(words)


def _row(job: CronJob, now: datetime, zone: tzinfo, other: bool) -> list[Any]:
    name = job.label[:JOB_NAME_LIMIT]
    if other and job.schedule:
        name = f"{name} · {job.schedule[:24]}"
    when = timeparse.format_short(job.next_run_at, now, zone) or "—"
    return [name, run_in(job.next_run_at, now), when]


def _failing(cron: CronSummary, zone: tzinfo) -> tuple[list[str], ...]:
    failures = {failure.job_id: failure for failure in cron.failing}
    listed = {job.job_id for job in cron.jobs}
    rows = [
        [
            job.label[:FAILING_NAME_LIMIT],
            FAILURE_WORDS.get(job.failure, job.failure),
            failure_note(failures[job.job_id], zone) if job.job_id in failures else "",
        ]
        for job in cron.jobs
        if job.failure is not None
    ]
    rows += [
        [
            failure.name[:FAILING_NAME_LIMIT],
            FAILURE_WORDS.get(failure.kind, failure.kind),
            failure_note(failure, zone),
        ]
        for failure in cron.failing
        if failure.job_id not in listed
    ]
    return tuple(rows)


def cron_view(cron: CronSummary | None, now: datetime, zone: tzinfo) -> CronView:
    """The failing jobs (a failure whose job is not listed too), then the active jobs that do not
    fail by frequency (``rich._by_frequency``), then the paused and the done."""
    if cron is None or cron.state in ("unknown", "unsupported"):
        return CronView("No data" if cron is None or cron.state == "unknown" else "Not observed")
    ticker = timeparse.format_in_zone(cron.ticker_at, zone, "%H:%M")
    head = f"{cron.active or 0} active · " + (f"ticker {ticker}" if ticker else "no heartbeat")
    active = rich._by_frequency(
        [job for job in cron.jobs if job.state == "active" and job.failure is None]
    )
    groups = tuple(
        (frequency.label, tuple(_row(job, now, zone, frequency == OTHER) for job in jobs))
        for frequency, jobs in groupby(active, key=lambda job: job.frequency)
    )
    return CronView(
        head=head + TICKER_WORDS.get(cron.state, ""),
        failing=_failing(cron, zone),
        groups=groups,
        paused=_set_rows(cron.jobs, "paused", lambda job: job.next_run_at, zone),
        done=_set_rows(cron.jobs, "done", lambda job: job.last_run_at, zone),
    )


def _set_rows(
    jobs: Sequence[CronJob], state: str, when: Any, zone: tzinfo
) -> tuple[list[str], ...]:
    return tuple(
        [job.label[:JOB_NAME_LIMIT], timeparse.format_day(when(job), zone) or ""]
        for job in jobs
        if job.state == state
    )


# ----------------------------------------------------------------------------- the view


def settling(snapshot: DashboardSnapshot) -> bool:
    """The gateway still coming up: its process is not ``running`` or Telegram not ``connected``."""
    gateway = snapshot.gateway
    return gateway is not None and (gateway.process != "running" or gateway.telegram != "connected")


def view(
    snapshot: DashboardSnapshot, parts: render.ScreenParts, now: datetime, zone: tzinfo
) -> View:
    blocks = {block.key: tuple(block.lines) for block in parts.blocks}
    head = blocks.get("head", ())
    top = next((line for line in head if line.startswith("#@")), head[-1] if head else "")
    details = parts.details
    groups = [
        (group.key, tuple(group.lines))
        for group in details.groups
        if group.lines and group.key not in NOT_IN_DETAILS
    ]
    if details.missing:
        groups.append(("no data", tuple(details.missing)))
    status = snapshot.overall if snapshot.overall in SEVERITIES else "unknown"
    quotas = render.drawn_quotas(snapshot.capacity.quotas, now)
    return View(
        status=status,
        headline=headline(top),
        blocks=tuple((key, blocks[key]) for key in STATUS_BLOCKS if blocks.get(key)),
        details=tuple(groups),
        providers=tuple(provider(quota, now, zone) for quota in quotas),
        disk=disk_row(snapshot),
        cron=cron_view(snapshot.cron, now, zone),
        settling=settling(snapshot),
        zone=zone,
    )
