"""``rich``: the pinned message as a Rich Message, built from the text screen's own parts.

The text form is held by ``test_render`` and ``test_states``; here the rich one says the same
first lines, a collapsed section per source that wrote something, the two tables, and stays
within Telegram's ceilings.
"""

from dataclasses import replace
from datetime import timedelta
from html.parser import HTMLParser

import pytest

from telegram_dashboard import __version__, rich
from telegram_dashboard.render import _html_line, render_parts
from telegram_dashboard.rich import to_telegram_rich
from telegram_dashboard.schema import CronJob, CronSummary
from telegram_dashboard.states import PERIOD_SECONDS, SHOWCASE_NOW, all_states

_VOID = {"br"}


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


def test_every_demo_state_is_balanced_and_within_the_ceilings() -> None:
    for state in all_states():
        markup = _rich(state.number)
        parser = _Balance()
        parser.feed(markup)
        assert parser.errors == [] and parser.stack == [], state.number
        assert parser.seen <= {
            "p",
            "br",
            "b",
            "i",
            "code",
            "details",
            "summary",
            "table",
            "tr",
            "th",
            "td",
        }, state.number
        assert len(markup) <= rich.RICH_MESSAGE_MAX_CHARS


def test_the_first_lines_are_the_text_screen_s_own() -> None:
    state = _state(21)
    parts = render_parts(
        state.snapshot, now=state.now, delivery=state.delivery, period_seconds=PERIOD_SECONDS
    )
    markup = _rich(21)
    head = markup.split("<details>")[0]

    assert [_html_line(line) for line in parts.lines if line] == [
        line
        for paragraph in head.removeprefix("<p>").removesuffix("</p>").split("</p><p>")
        for line in paragraph.split("<br>")
    ]
    assert head.startswith("<p>🟡 Warning · Sep 26 21:00 UTC<br>Gateway ✓ · Telegram ✓")
    assert "<p><b>Needs attention</b><br>- Cron run failed: daily-digest</p>" in head


def test_a_section_per_source_that_wrote_something_and_the_version_closes_it() -> None:
    showcase = _summaries(_rich(21))
    bare = _summaries(_rich(1))

    assert showcase == [
        "🧠 Limits · windows and resets",
        "⏱ Cron · 27 active · 3 paused · 2 done",
        "✓ Done · 2",
        "📡 Telegram channel",
        "💾 Backup · Drift",
        "🤖 Hermes version",
        "⚪ No data · 1",
        "Details",
    ]
    # No rules, no memory, no traffic in the bare state: no section for them, never an empty one.
    assert "📜 Agent rules" not in bare and "🗂 Memory" not in bare
    assert "📡 Telegram channel" not in bare
    assert _rich(1).endswith(f"<p>Dashboard {__version__}</p></details>")


def test_the_limits_table_has_a_row_per_window_with_its_reset() -> None:
    table = _rich(21).split("<table compact bordered>")[1].split("</table>")[0]

    assert "<tr><th>Provider</th><th>Used</th><th>Resets in</th><th>At</th></tr>" in table
    assert "<tr><td>Codex</td><td>⚠️ 93%</td><td>1h20m</td><td>Sep 26 22:20</td></tr>" in table
    assert "<tr><td>Kimi</td><td>46%</td><td>21d</td><td>Oct 17 21:00</td></tr>" in table
    assert table.count("<tr>") == 8  # the head and seven windows; Gemini has none


def test_the_cron_table_lists_the_failing_job_first_with_its_marks() -> None:
    markup = _rich(21)
    table = markup.split("<table compact striped>")[1].split("</table>")[0]
    rows = table.split("<tr>")[2:]

    assert rows[0] == (
        "<td>⚠️ daily-digest</td><td><code>0 21 * * *</code></td>"
        "<td>Sep 27 21:00</td><td>⚠️ 20:05</td></tr>"
    )
    assert rows[1].startswith("<td>inbox-triage</td><td><code>every 30m</code></td><td>21:12</td>")
    assert len(rows) == 27
    assert "<code>*/15 * * * *</code>" in table


def test_the_paused_jobs_are_a_block_of_their_own_with_the_date_they_were_due() -> None:
    """Decision of 22.10: a paused job whose next run is behind is a mark with its date and one
    line of why, never a failure: the status stays as the collectors said."""
    markup = _rich(14)

    assert markup.startswith("<p>🟢 Healthy")
    assert (
        "<p><b>⏸ Paused · 3</b><br>threads-deliver · <code>0 10 * * *</code> · was due "
        "Sep 19 10:00<br>survey-replies · <code>every 120m</code> · was due Sep 24 14:00<br>"
        "old-campaign · <code>0 12 * * 1</code><br><i>A paused job does not fire: its date is "
        "the one set before the pause.</i></p>"
    ) in markup


def test_no_paused_job_due_in_the_past_says_no_why() -> None:
    state = _state(14)
    cron = state.snapshot.cron
    jobs = tuple(replace(job, paused_past_due=False) for job in cron.jobs)
    snapshot = replace(state.snapshot, cron=replace(cron, jobs=jobs))

    markup = to_telegram_rich(snapshot, now=state.now, delivery=state.delivery)

    assert "<b>⏸ Paused · 3</b>" in markup and "was due" not in markup
    assert "does not fire" not in markup


def test_a_long_list_is_cut_with_how_many_are_left_out() -> None:
    at = (SHOWCASE_NOW + timedelta(hours=1)).isoformat()
    jobs = tuple(
        CronJob(f"j{i}", f"job-{i:03d}", "active", next_run_at=at)
        for i in range(rich.JOB_ROWS_LIMIT + 5)
    )
    state = _state(14)
    cron = CronSummary("ok", active=len(jobs), paused=0, jobs=jobs)

    markup = to_telegram_rich(replace(state.snapshot, cron=cron), now=state.now)

    assert '<tr><td colspan="4">… 5 more</td></tr></table>' in markup
    assert markup.count("<td>job-") == rich.JOB_ROWS_LIMIT


def test_a_cron_without_a_list_keeps_its_section_from_the_details_alone() -> None:
    """A summary built without the list (a demo literal, an older collector): the counts and the
    details lines, no table and no invented rows."""
    state = _state(3)
    markup = _rich(3)
    cron = state.snapshot.cron

    assert cron is not None and cron.jobs == ()
    assert "<table compact striped>" not in markup
    assert "<summary>⏱ Cron · 9 active · 0 paused</summary><p>Cron 9 active" in markup


@pytest.mark.parametrize("ceiling", ["RICH_MESSAGE_MAX_CHARS", "RICH_MESSAGE_MAX_BLOCKS"])
def test_a_message_over_a_ceiling_is_not_built(monkeypatch, ceiling: str) -> None:
    state = _state(14)
    monkeypatch.setattr(rich, ceiling, 10)

    assert to_telegram_rich(state.snapshot, now=state.now) is None


def test_a_file_name_or_a_command_in_a_cell_is_code() -> None:
    state = _state(14)
    at = (SHOWCASE_NOW + timedelta(hours=1)).isoformat()
    jobs = (
        CronJob("j1", "sync AGENTS.md /new", "active", schedule="*/30 * * * *", next_run_at=at),
    )
    snapshot = replace(state.snapshot, cron=CronSummary("ok", active=1, paused=0, jobs=jobs))

    markup = to_telegram_rich(snapshot, now=state.now)

    assert "<td>sync <code>AGENTS.md</code> <code>/new</code></td>" in markup
    assert "<td><code>*/30 * * * *</code></td>" in markup
