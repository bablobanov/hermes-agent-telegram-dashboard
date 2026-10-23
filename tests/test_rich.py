"""``rich``: the pinned message as a Rich Message, built from the text screen's own parts.

The text form is held by ``test_render`` and ``test_states``; here the rich one says the same
blocks, a section each with a thin line between two, the limits as a table, the cron fold, one
Details fold with a section per source that wrote something, and stays within Telegram's
ceilings.
"""

import re
from dataclasses import replace
from datetime import timedelta
from html.parser import HTMLParser
from zoneinfo import ZoneInfo

import pytest
from cron_fixtures import FRAGMENTS, PLANTED

from telegram_dashboard import __version__, rich
from telegram_dashboard.cron_frequency import OTHER
from telegram_dashboard.render import _html_line, render_dashboard, render_parts
from telegram_dashboard.rich import to_telegram_rich
from telegram_dashboard.schema import (
    CapacitySummary,
    CronFailure,
    CronJob,
    CronSummary,
    Frequency,
    QuotaMetric,
    QuotaWindow,
)
from telegram_dashboard.states import PERIOD_SECONDS, SHOWCASE_NOW, all_states

_VOID = {"br", "hr"}


class _Balance(HTMLParser):
    """Every tag closed in the order it was opened; the tags seen."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.stack: list[str] = []
        self.seen: set[str] = set()
        self.errors: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.seen.add(tag)
        if tag not in _VOID:
            self.stack.append(tag)

    def handle_endtag(self, tag: str) -> None:
        if tag in _VOID:
            return  # ``<hr/>`` is a start and an end at once
        if not self.stack or self.stack.pop() != tag:
            self.errors.append(tag)


def _state(number: int):
    return next(state for state in all_states() if state.number == number)


def _rich(number: int) -> str:
    state = _state(number)
    out = to_telegram_rich(
        state.snapshot, now=state.now, delivery=state.delivery, period_seconds=PERIOD_SECONDS
    )
    assert out is not None
    return out


def _summaries(markup: str) -> list[str]:
    return [part.split("</summary>")[0] for part in markup.split("<summary>")[1:]]


def _details_body(markup: str) -> str:
    return markup.split("<summary>Details</summary>")[1].removesuffix("</details>")


def _cron_fold(markup: str) -> str:
    """The cron fold after its title, up to the Details fold."""
    return markup.split("<summary>🕒 Cron")[1].split("</summary>", 1)[1].split("<details>")[0]


def _table(markup: str) -> str:
    """The rows of the cron table."""
    return _cron_fold(markup).split("<table compact bordered>")[1].split("</table>")[0]


def _headers(markup: str) -> list[str]:
    return re.findall(r'<tr><th colspan="2">(.*?)</th></tr>', _table(markup))


def _at(**offset: float) -> str:
    return (SHOWCASE_NOW + timedelta(**offset)).isoformat()


def _active(name: str, frequency: Frequency = OTHER, **fields) -> CronJob:
    """An active job named ``name``, its next run an hour after the showcase's moment unless
    ``fields`` say otherwise."""
    fields.setdefault("next_run_at", _at(hours=1))
    return CronJob(name, name, "active", frequency=frequency, **fields)


def _with_jobs(*jobs: CronJob, zone=None) -> str:
    """The showcase screen with ``jobs`` as its cron list, at the showcase's moment."""
    state = _state(14)
    active = sum(job.state == "active" for job in jobs)
    paused = sum(job.state == "paused" for job in jobs)
    cron = CronSummary("ok", active=active, paused=paused, jobs=jobs)
    extra = {"zone": zone} if zone is not None else {}
    markup = to_telegram_rich(replace(state.snapshot, cron=cron), now=state.now, **extra)
    assert markup is not None
    return markup


def test_every_demo_state_is_balanced_and_within_the_ceilings() -> None:
    for state in all_states():
        markup = _rich(state.number)
        parser = _Balance()
        parser.feed(markup)
        assert parser.errors == [] and parser.stack == [], state.number
        assert parser.seen <= {
            "p",
            "br",
            "hr",
            "b",
            "i",
            "code",
            "tg-time",
            "details",
            "summary",
            "table",
            "tr",
            "th",
            "td",
        }, state.number
        assert len(markup) <= rich.RICH_MESSAGE_MAX_CHARS
        assert len(rich._BLOCK_TAG.findall(markup)) <= rich.RICH_MESSAGE_MAX_BLOCKS


def test_the_main_screen_is_the_text_screen_s_blocks_a_section_each() -> None:
    """1.0.0, the probe's message 2: every block of the text form is a section, a thin line
    between two and none before the first or after the last; the top line bold with its time a
    ``<tg-time>``."""
    state = _state(21)
    parts = render_parts(
        state.snapshot, now=state.now, delivery=state.delivery, period_seconds=PERIOD_SECONDS
    )
    sections = _rich(21).split("<details>")[0].split("<hr/>")

    assert [block.key for block in parts.blocks] == [
        "head",
        "attention",
        "works",
        "limits",
        "hermes",
    ]
    assert len(sections) == len(parts.blocks)
    for block, section in zip(parts.blocks, sections, strict=True):
        if block.key == "limits":
            assert section.startswith("<p><b>🧠 Limits used</b></p><table compact bordered>")
        else:
            assert section == "<p>" + "<br>".join(_html_line(line) for line in block.lines) + "</p>"
    assert sections[:3] == [
        '<p><b>🟡 Warning · updated <tg-time unix="1790456400" format="r">Sep 26 21:00 UTC'
        "</tg-time></b></p>",
        "<p><b>🛠 Needs attention</b><br>- Cron run failed: daily-digest<br>"
        "Cron ⚠️ 1 of 27 failing</p>",
        "<p>✓\xa0Gateway ✓\xa0Telegram ✓\xa0Backup ✓\xa0Drift</p>",
    ]
    assert sections[-1] == "<p>🤖 Hermes 0.21.3 → 0.21.5</p>"


def test_the_banner_shares_the_top_line_s_section_and_an_unread_time_has_no_tag() -> None:
    stale = _rich(16).split("<hr/>")[0]
    state = _state(14)
    unread = to_telegram_rich(replace(state.snapshot, observed_at="not a time"), now=state.now)

    assert stale.startswith("<p>⚠️ DASHBOARD STALE: last confirmation 20 min ago")
    assert stale.endswith(
        '<br><b>🟢 Healthy · updated <tg-time unix="1790456400" format="r">'
        "Sep 26 21:00 UTC</tg-time></b></p>"
    )
    assert unread is not None and "tg-time" not in unread
    assert unread.startswith("<p><b>🟢 Healthy · updated time unknown</b></p><hr/>")


def test_the_cron_fold_then_one_details_fold_close_the_message() -> None:
    showcase = _summaries(_rich(21))
    bare = _summaries(_rich(1))

    # 1.0.0, the owner's pick of 24.10: the done jobs are rows of the one table, no fold of theirs.
    assert showcase == ["🕒 Cron · 27 active · 3 paused · 2 done", "Details"]
    # No cron in the bare state: no fold for it, never an empty one.
    assert bare == ["Details"]
    assert _rich(1).endswith(f"<br>Dashboard {__version__}</p></details>")


def test_details_is_a_section_per_source_a_thin_line_between_and_the_stamps_last() -> None:
    """R8: the sources in the order a reader acts on them, empty ones left out, every "no data"
    with its reason, then the screen's own stamps (its time first) and the dashboard's version;
    the old limits fold with its four-column table is gone."""
    markup = _rich(21)
    sections = _details_body(markup).split("<hr/>")

    assert markup.count("<summary>Details</summary>") == 1
    assert [section.split("</b>")[0] for section in sections[:-1]] == [
        "<p><b>🧠 Limits",
        "<p><b>📡 Telegram channel",
        "<p><b>💾 Backup · Drift",
        "<p><b>🤖 Hermes version",
        "<p><b>❔ No data · 1",
    ]
    assert sections[2] == (
        "<p><b>💾 Backup · Drift</b><br>✓ Backup Sep 26 11:00 · 10 h ago · integrity ok<br>"
        "✓ Drift 0 of 481 · checked 08:00</p>"
    )
    assert sections[-1] == (
        "<p>Screen Sep 26 21:00 UTC<br>Confirmed 20:58<br>Period 5 min<br>"
        f"Profiles 1/1 · sources 10/10<br>Dashboard {__version__}</p>"
    )
    assert "windows and resets" not in markup and "⚪ No data" not in markup
    assert "<th>At</th>" not in markup


def test_the_rules_and_the_memory_have_their_sections_in_details() -> None:
    sections = _details_body(_rich(27)).split("<hr/>")

    assert sections[1].startswith("<p><b>🗂 Memory</b><br>Memory: ")
    assert "📜 Agent rules" in _rich(25) and "📜 Agent rules" not in _rich(14)


def test_the_limits_table_has_a_row_per_line_and_a_cell_per_window() -> None:
    """R7: the provider as the line names it, ``used · resets in`` per window, the mark before a
    loud share, a row with fewer windows spanning its last cell, a state in words spanning all;
    a model's limit is a row of its own, its reset spelt."""
    table = _rich(17).split("<table compact bordered>")[1].split("</table>")[0]

    assert table.split("</tr>")[:-1] == [
        '<tr><th>Provider</th><th colspan="2">Used · resets in</th>',
        "<tr><td>Claude</td><td>42% · 2h10m</td><td>67% · 3d</td>",
        '<tr><td>Claude Fable</td><td colspan="2">⚠️ 100% · 3d</td>',
        '<tr><td>Claude Sonnet</td><td colspan="2">20% · 3d</td>',
        "<tr><td>Codex</td><td>⚠️ 93% · 1h20m</td><td>58% · 5d</td>",
        '<tr><td>Gemini</td><td colspan="2">no data</td>',
        '<tr><td>Grok</td><td colspan="2">31% · 6d</td>',
        "<tr><td>Kimi</td><td>8% · 3h40m</td><td>46% · 21d</td>",
    ]


def test_a_table_of_single_windows_spans_nothing_and_a_word_state_stays_a_word() -> None:
    state = _state(14)
    at = (SHOWCASE_NOW + timedelta(hours=2)).isoformat()
    quotas = (
        QuotaMetric(
            "Grok", "official", windows=(QuotaWindow("7d", None, at, "usage not started"),)
        ),
        QuotaMetric("Kimi", "official", windows=(QuotaWindow("5h", 33.0, at),)),
        QuotaMetric("OpenAI", "local", used=120_000),
        QuotaMetric("Claude", "expired"),
        QuotaMetric("Mistral", "official", used=95, limit=100, reset_at=at),
    )
    snapshot = replace(state.snapshot, capacity=CapacitySummary(quotas))

    markup = to_telegram_rich(snapshot, now=state.now)
    assert markup is not None
    table = markup.split("<table compact bordered>")[1].split("</table>")[0]
    text = render_dashboard(snapshot, now=state.now).splitlines()

    assert table.split("</tr>")[:-1] == [
        "<tr><th>Provider</th><th>Used · resets in</th>",
        "<tr><td>Grok</td><td>usage not started</td>",
        "<tr><td>Kimi</td><td>33% · 2h</td>",
        "<tr><td>OpenAI</td><td>locally 120,000 tokens</td>",
        "<tr><td>Claude</td><td>login expired</td>",
        "<tr><td>Mistral</td><td>⚠️ 95% · 2h</td>",
    ]
    # The same words as the text lines, from the same helpers.
    for line in (
        "Grok · usage not started",
        "Kimi 33% (2h)",
        "OpenAI · locally 120,000 tokens",
        "Claude · login expired",
        "⚠️ Mistral 95% (2h)",
    ):
        assert line in text


def test_the_cron_jobs_are_one_table_a_header_row_per_frequency_then_paused_then_done() -> None:
    """1.0.0, the owner's pick of 24.10 (probe 4, message B): one table in the look of the limits
    table, the frequency a shaded header row across both columns, the most frequent first and
    ``Other`` last, then the paused and the done; no count in a header (the fold's title has
    them), no stopwatch, no row of column names, no schedule column."""
    markup = _rich(21)
    table = _table(markup)
    rows = table.split("</tr>")[:-1]
    jobs = [row for row in rows if "<td>" in row]

    # Two folds in all, the cron jobs and Details: the done jobs are rows, no fold of their own.
    assert markup.count("<details>") == 2 and _cron_fold(markup).count("<table") == 1
    assert _headers(markup) == [
        "Every 15 min",
        "Every 30 min",
        "Every 45 min",
        "Every hour",
        "Every 2 h",
        "Every 3 h",
        "Every 4 h",
        "Every 6 h",
        "Every 12 h",
        "Daily",
        "Weekdays",
        "Weekly",
        "Monthly",
        "Other",
        "⏸ Paused",
        "✅ Done",
    ]
    assert len(jobs) == 27 + 3 + 2 and len(rows) == len(jobs) + 16
    assert all(row.count("<td") == 2 and '</td><td align="right">' in row for row in jobs)
    assert "⏱" not in markup and "✓ Done" not in markup
    assert "<th>" not in table and "Next" not in table and "Schedule" not in table
    assert rows[:4] == [
        '<tr><th colspan="2">Every 15 min</th>',
        '<tr><td>uptime-ping</td><td align="right">21:15</td>',
        '<tr><th colspan="2">Every 30 min</th>',
        '<tr><td>inbox-triage</td><td align="right">21:12</td>',
    ]


def test_the_groups_go_by_period_once_first_other_last_one_period_by_its_label() -> None:
    """``Every 12 h`` and ``Twice a day`` are both 720 minutes: the label orders them, so the
    order is total. Inside a group the summary's order stays."""
    jobs = (
        _active("z-other"),
        _active("b-twice", Frequency("Twice a day", 720.0)),
        _active("a-daily", Frequency("Daily", 1440.0)),
        _active("c-every-12", Frequency("Every 12 h", 720.0)),
        _active("d-once", Frequency("Once", 0.0)),
        _active("e-twice", Frequency("Twice a day", 720.0)),
        _active("f-quarter", Frequency("Every 15 min", 15.0)),
    )
    markup = _with_jobs(*jobs)

    assert _headers(markup) == [
        "Once",
        "Every 15 min",
        "Every 12 h",
        "Twice a day",
        "Daily",
        "Other",
    ]
    assert re.findall(r"<tr><td>([\w-]+)</td>", _table(markup)) == [
        "d-once",
        "f-quarter",
        "c-every-12",
        "b-twice",
        "e-twice",
        "a-daily",
        "z-other",
    ]


MOSCOW = ZoneInfo("Europe/Moscow")


@pytest.mark.parametrize(
    "next_run_at, zone, cell",
    [
        (_at(hours=1), None, "22:00"),
        (_at(hours=-3), None, "18:00"),
        (_at(days=1), None, "Sun 21:00"),
        (_at(days=6), None, "Fri 21:00"),
        (_at(days=7), None, "Oct 3"),
        (_at(days=40), None, "Nov 5"),
        (_at(days=-2), None, "Sep 24"),
        (None, None, "—"),
        ("not a time", None, "—"),
        # 21:00 UTC is already 00:00 of Sunday in Moscow: the days are the zone's.
        (_at(hours=1, minutes=30), MOSCOW, "01:30"),
        (_at(minutes=-30), MOSCOW, "Sep 26"),
        (_at(days=1, hours=2), MOSCOW, "Mon 02:00"),
    ],
    ids=[
        "the-reference-day",
        "earlier-the-same-day",
        "1-day-ahead",
        "6-days-ahead",
        "7-days-ahead",
        "further-ahead",
        "behind",
        "missing",
        "unreadable",
        "moscow-after-midnight-the-same-day",
        "moscow-before-midnight-behind",
        "moscow-1-day-ahead",
    ],
)
def test_an_active_job_s_date_has_one_short_form(next_run_at, zone, cell) -> None:
    """The owner, 24.10: the right column must not float. ``06:30`` on the screen's day, the
    weekday and the time on a later day less than a week ahead, the day on any other."""
    markup = _with_jobs(_active("j", next_run_at=next_run_at), zone=zone)

    assert f'<tr><td>j</td><td align="right">{cell}</td></tr>' in _table(markup)


def test_paused_and_done_jobs_show_the_day_only() -> None:
    """A paused job the day of its next run, behind or ahead; a done one the day of its last run,
    the latest first, even on the screen's own day; ``—`` without one. No time, no "was due"."""
    table = _table(_rich(14))
    paused, done = table.split('<tr><th colspan="2">⏸ Paused</th></tr>')[1].split(
        '<tr><th colspan="2">✅ Done</th></tr>'
    )
    later = CronJob("p", "later", "paused", next_run_at=_at(hours=2))

    assert paused == (
        '<tr><td>threads-deliver</td><td align="right">Sep 19</td></tr>'
        '<tr><td>survey-replies</td><td align="right">Sep 24</td></tr>'
        '<tr><td>old-campaign</td><td align="right">—</td></tr>'
    )
    assert done == (
        '<tr><td>once-reminder</td><td align="right">Sep 26</td></tr>'
        '<tr><td>once-migration</td><td align="right">Sep 20</td></tr>'
    )
    assert '<tr><td>later</td><td align="right">Sep 26</td></tr>' in _table(_with_jobs(later))
    assert "was due" not in table and "last " not in table


def test_a_failing_job_is_marked_before_its_name_and_nowhere_else_in_its_row() -> None:
    jobs = (
        _active("late", failure="overdue", next_run_at=_at(minutes=-20)),
        _active("broke", Frequency("Daily", 1440.0), failure="run"),
        _active("fine"),
    )
    table = _table(_with_jobs(*jobs))

    assert '<tr><td>⚠️ late</td><td align="right">20:40</td></tr>' in table
    assert '<tr><td>⚠️ broke</td><td align="right">22:00</td></tr>' in table
    assert '<tr><td>fine</td><td align="right">22:00</td></tr>' in table
    assert table.count("⚠️") == 2


_FAILURE_NOTE = "⚠️ before a job: its last run failed or it is overdue"
_PAUSED_NOTE = "<i>A paused job does not fire: its date is the one set before the pause.</i>"


@pytest.mark.parametrize(
    "failing, past_due, notes",
    [
        (True, True, [_FAILURE_NOTE, _PAUSED_NOTE]),
        (True, False, [_FAILURE_NOTE]),
        (False, True, [_PAUSED_NOTE]),
        (False, False, []),
    ],
)
def test_each_note_under_the_table_only_when_it_applies(failing, past_due, notes) -> None:
    """Decision of 22.10 kept: a paused job whose date is behind is no alarm, and a line says
    why; the mark before a name is explained only when a row carries it."""
    jobs = (
        _active("a", failure="run" if failing else None),
        CronJob(
            "p",
            "p",
            "paused",
            next_run_at=_at(days=-1 if past_due else 1),
            paused_past_due=past_due,
        ),
    )
    after = _cron_fold(_with_jobs(*jobs)).split("</table>")[1]

    assert after == (f"<p>{'<br>'.join(notes)}</p>" if notes else "") + "</details>"


def test_the_note_on_the_mark_is_read_off_the_rows_as_built() -> None:
    """A failing job the cut leaves out (in ``Other``, past the sixtieth row) carries no mark in
    the table, so no note explains one; shown, it carries both."""
    daily = Frequency("Daily", 1440.0)
    full = tuple(_active(f"a{i:03d}", daily) for i in range(rich.JOB_ROWS_LIMIT))
    late = _active("late", failure="overdue")

    cut = _cron_fold(_with_jobs(*full, late))
    shown = _cron_fold(_with_jobs(*full[1:], late))

    assert "late" not in cut and "⚠️" not in cut
    assert cut.split("</table>")[1] == "</details>"
    assert "<tr><td>⚠️ late</td>" in shown and f"<p>{_FAILURE_NOTE}</p>" in shown


def test_labels_that_escaping_grows_take_the_rich_form_over_a_ceiling() -> None:
    """``&``, ``"``, ``'``, ``<`` and ``>`` grow four to six times under escaping, and a label is
    whole up to 80: every set full of such labels passes 32,768 characters. Then there is no
    rich form, and the tick edits the HTML form instead (``test_probe_screen``)."""
    label = ("&\"'<>" * 16)[:80]
    jobs = (
        *(_active(f"a{i}", note=label) for i in range(rich.JOB_ROWS_LIMIT)),
        *(CronJob(f"p{i}", label, "paused") for i in range(rich.PAUSED_LIMIT)),
        *(CronJob(f"d{i}", label, "done") for i in range(rich.DONE_LIMIT)),
    )
    state = _state(14)
    cron = CronSummary("ok", active=rich.JOB_ROWS_LIMIT, paused=rich.PAUSED_LIMIT, jobs=jobs)
    plain = tuple(replace(job, name="x" * 80, note=None) for job in jobs)

    assert to_telegram_rich(replace(state.snapshot, cron=cron), now=state.now) is None
    # The same list in words escaping leaves as they are fits: the escaping passed the ceiling.
    fits = replace(state.snapshot, cron=replace(cron, jobs=plain))
    assert to_telegram_rich(fits, now=state.now) is not None


def test_each_cut_set_ends_with_how_many_are_left_out_in_its_own_part() -> None:
    """The limits keep their values: the active, the paused and the done are each cut at their
    own, the row that says how many are left out closing that set's part of the table."""
    daily = Frequency("Daily", 1440.0)
    active = tuple(_active(f"a{i:03d}", daily) for i in range(rich.JOB_ROWS_LIMIT + 5))
    paused = tuple(
        CronJob(f"p{i:03d}", f"p{i:03d}", "paused") for i in range(rich.PAUSED_LIMIT + 3)
    )
    done = tuple(CronJob(f"d{i:03d}", f"d{i:03d}", "done") for i in range(rich.DONE_LIMIT + 2))

    table = _table(_with_jobs(*active, *paused, *done))
    more = '<tr><td colspan="2">… {} more</td></tr>'

    assert more.format(5) + '<tr><th colspan="2">⏸ Paused</th></tr>' in table
    assert more.format(3) + '<tr><th colspan="2">✅ Done</th></tr>' in table
    assert table.endswith(more.format(2))
    counts = (table.count("<tr><td>a"), table.count("<tr><td>p"), table.count("<tr><td>d"))
    assert counts == (rich.JOB_ROWS_LIMIT, rich.PAUSED_LIMIT, rich.DONE_LIMIT)
    assert "<td>a059</td>" in table and "<td>a060</td>" not in table


def test_only_the_other_group_names_the_schedule_beside_the_job() -> None:
    """A named frequency's header says how often its jobs run; ``Other`` says nothing, so each
    of its jobs carries the engine's words in ``<code>``, sanitized and cut as before."""
    table = _table(_rich(21))
    long = _active("long", schedule="0 9,13,18,22 1-7 * 1,3,5 extra")
    bare = _table(_with_jobs(_active("no-schedule"), long))

    assert (
        '<tr><td>stock-alert · <code>*/30 9-17 * * 1-5</code></td><td align="right">Mon 09:00</td>'
        "</tr>"
    ) in table
    assert table.count("<code>") == 1
    assert '<tr><td>no-schedule</td><td align="right">' in bare
    assert "<td>long · <code>0 9,13,18,22 1-7 * 1,3,…</code></td>" in bare


def test_the_name_cell_is_the_label_whole_up_to_80() -> None:
    """Task 2's label (the owner's note, else the name) whole up to ``cron_jobs.NAME_LIMIT``,
    never the old cut at 24; a longer one is 79 characters and an ellipsis."""
    whole = ("Три репозитория: статусы подач и ответы площадок " + "x" * 80)[:80]
    table = _table(_with_jobs(_active("n1", note=whole), _active("z" * 81)))

    assert f"<tr><td>{whole}</td>" in table
    assert f"<tr><td>{'z' * 79}…</td>" in table


def test_a_planted_secret_in_a_label_or_a_schedule_never_reaches_the_markup() -> None:
    """The renderer sanitizes a label and a schedule once more, whoever built the job; the
    collector does it first (``test_cron_jobs``)."""
    jobs = (
        CronJob("s1", "digest", "active", schedule=PLANTED, note=f"Digest {PLANTED}"),
        CronJob("s2", f"watch {PLANTED}", "paused", schedule=PLANTED),
        CronJob("s3", "mail", "done", schedule=PLANTED, note=PLANTED),
    )
    markup = _with_jobs(*jobs)
    table = _table(markup)

    for fragment in FRAGMENTS:
        assert fragment not in markup, fragment
    assert "<tr><td>Digest [secret]" in table and " · <code>[secret]" in table
    assert "<tr><td>watch [secret]" in table and "<tr><td>[secret]" in table


def test_a_cron_without_a_list_keeps_its_section_from_the_details_alone() -> None:
    """A summary built without the list (a demo literal, an older collector): the counts and the
    details lines, no table and no invented rows."""
    state = _state(3)
    markup = _rich(3)
    cron = state.snapshot.cron

    assert cron is not None and cron.jobs == ()
    assert "<table" not in _cron_fold(markup) and "<tr>" not in _cron_fold(markup)
    assert "<summary>🕒 Cron · 9 active · 0 paused</summary><p>Cron 9 active" in markup


def test_every_limit_full_beside_the_largest_demo_state_fits_both_ceilings() -> None:
    """The worst case the limits allow, on the largest demo state: every set one over its limit,
    each active job failing under a header of its own, names of 80 Cyrillic characters, every
    schedule 24 long, every failure named in full in the cron fold's details lines."""
    largest = max(all_states(), key=lambda state: len(_rich(state.number)))
    at = _at(days=3)

    def name(i: int) -> str:
        return (f"Ежедневная проверка {i:02d}: " + "э" * 80)[:80]

    def job(i: int, state: str, **fields) -> CronJob:
        return CronJob(f"{state}{i}", name(i), state, schedule="x" * 24, **fields)

    over = rich.JOB_ROWS_LIMIT + 1
    active = [
        job(
            i,
            "active",
            next_run_at=at,
            failure="run",
            frequency=Frequency(f"Every {i} min", float(i)),
        )
        for i in range(1, over + 1)
    ]
    paused = [
        job(i, "paused", next_run_at=at, paused_past_due=True) for i in range(rich.PAUSED_LIMIT + 1)
    ]
    done = [job(i, "done", last_run_at=at) for i in range(rich.DONE_LIMIT + 1)]
    failing = tuple(
        CronFailure(f"active{i}", name(i), "run", at=at, streak=3, reason="rate limit")
        for i in range(1, over + 1)
    )
    cron = CronSummary(
        "failing", active=over, paused=len(paused), failing=failing, jobs=(*active, *paused, *done)
    )

    markup = to_telegram_rich(
        replace(largest.snapshot, cron=cron),
        now=largest.now,
        delivery=largest.delivery,
        period_seconds=PERIOD_SECONDS,
    )

    assert markup is not None
    parser = _Balance()
    parser.feed(markup)
    assert parser.errors == [] and parser.stack == []
    assert len(_headers(markup)) == rich.JOB_ROWS_LIMIT + 2
    assert f"<br>Cron {name(1)}: run failed " in markup
    assert len(markup) <= rich.RICH_MESSAGE_MAX_CHARS
    assert len(rich._BLOCK_TAG.findall(markup)) <= rich.RICH_MESSAGE_MAX_BLOCKS


@pytest.mark.parametrize("ceiling", ["RICH_MESSAGE_MAX_CHARS", "RICH_MESSAGE_MAX_BLOCKS"])
def test_a_message_over_a_ceiling_is_not_built(monkeypatch, ceiling: str) -> None:
    state = _state(14)
    monkeypatch.setattr(rich, ceiling, 10)

    assert to_telegram_rich(state.snapshot, now=state.now) is None


def test_a_thin_line_counts_as_a_block(monkeypatch) -> None:
    """R9: Telegram counts ``<hr/>`` among the 500 blocks; a message that fits only when the
    thin lines are left out is not built."""
    state = _state(14)
    markup = to_telegram_rich(state.snapshot, now=state.now)
    assert markup is not None
    blocks = len(rich._BLOCK_TAG.findall(markup))

    assert rich._BLOCK_TAG.findall("<p>a</p><hr/><p>b</p>") == ["<p", "<hr", "<p"]
    assert markup.count("<hr/>") >= 4
    monkeypatch.setattr(rich, "RICH_MESSAGE_MAX_BLOCKS", blocks - 1)
    assert to_telegram_rich(state.snapshot, now=state.now) is None
    monkeypatch.setattr(rich, "RICH_MESSAGE_MAX_BLOCKS", blocks)
    assert to_telegram_rich(state.snapshot, now=state.now) == markup


def test_a_file_name_or_a_command_in_a_cell_is_code() -> None:
    job = CronJob(
        "j1", "sync AGENTS.md /new", "active", schedule="*/30 * * * *", next_run_at=_at(hours=1)
    )

    markup = _with_jobs(job)

    assert (
        "<td>sync <code>AGENTS.md</code> <code>/new</code> · <code>*/30 * * * *</code></td>"
    ) in markup
