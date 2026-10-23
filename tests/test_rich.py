"""``rich``: the pinned message as a Rich Message, built from the text screen's own parts.

The text form is held by ``test_render`` and ``test_states``; here the rich one says the same
blocks, a section each with a thin line between two, the limits as a table, the cron fold, one
Details fold with a section per source that wrote something, and stays within Telegram's
ceilings.
"""

from dataclasses import replace
from datetime import timedelta
from html.parser import HTMLParser

import pytest

from telegram_dashboard import __version__, rich
from telegram_dashboard.render import _html_line, render_parts
from telegram_dashboard.rich import to_telegram_rich
from telegram_dashboard.schema import (
    CapacitySummary,
    CronJob,
    CronSummary,
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

    assert showcase == ["🕒 Cron · 27 active · 3 paused · 2 done", "✓ Done · 2", "Details"]
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
    )
    snapshot = replace(state.snapshot, capacity=CapacitySummary(quotas))

    markup = to_telegram_rich(snapshot, now=state.now)
    assert markup is not None
    table = markup.split("<table compact bordered>")[1].split("</table>")[0]

    assert table.split("</tr>")[:-1] == [
        "<tr><th>Provider</th><th>Used · resets in</th>",
        "<tr><td>Grok</td><td>usage not started</td>",
        "<tr><td>Kimi</td><td>33% · 2h</td>",
        "<tr><td>OpenAI</td><td>locally 120,000 tokens</td>",
        "<tr><td>Claude</td><td>login expired</td>",
    ]


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

    assert markup.startswith("<p><b>🟢 Healthy · updated ")
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
    assert "<summary>🕒 Cron · 9 active · 0 paused</summary><p>Cron 9 active" in markup


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
    state = _state(14)
    at = (SHOWCASE_NOW + timedelta(hours=1)).isoformat()
    jobs = (
        CronJob("j1", "sync AGENTS.md /new", "active", schedule="*/30 * * * *", next_run_at=at),
    )
    snapshot = replace(state.snapshot, cron=CronSummary("ok", active=1, paused=0, jobs=jobs))

    markup = to_telegram_rich(snapshot, now=state.now)

    assert "<td>sync <code>AGENTS.md</code> <code>/new</code></td>" in markup
    assert "<td><code>*/30 * * * *</code></td>" in markup
