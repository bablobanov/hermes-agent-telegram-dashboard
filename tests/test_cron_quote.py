"""1.1.2: the cron jobs as a quote of their own before Details in the text form (look C of the
probe of 23.11, the owner's pick on Android): a bold line per frequency, a row per job, its name
and then its next run, the paused and the done with their day only; cron's details lines under
the list, no longer in Details; the list cut to what the rest of the screen leaves."""

from dataclasses import replace
from datetime import UTC, datetime

from telegram_dashboard import __version__
from telegram_dashboard.cron_frequency import ONCE, OTHER, Frequency
from telegram_dashboard.render import (
    CRON_FAILURE_NOTE,
    CRON_PAUSED_NOTE,
    render_dashboard,
    to_telegram_html,
    to_telegram_plain,
)
from telegram_dashboard.schema import (
    CronFailure,
    CronJob,
    CronSummary,
    DashboardSnapshot,
    GatewaySummary,
)

NOW = datetime(2026, 9, 25, 7, 21, tzinfo=UTC)  # a Friday
NB = chr(0xA0)  # the no-break space, written by its number: ruff reads the glyph as a space
JOINT = f"{NB}·{NB}"
EVERY_15 = Frequency("Every 15 min", 15.0)
DAILY = Frequency("Daily", 1440.0)
FAILURE = CronFailure(
    "j-digest", "daily-digest", "run", at="2026-09-25T07:05:00Z", streak=3, reason="rate limit"
)
JOBS = (
    CronJob(
        "j-digest",
        "daily-digest",
        "active",
        None,
        "2026-09-26T06:00:00Z",
        None,
        "run",
        frequency=DAILY,
    ),
    CronJob("j-ping", "uptime-ping", "active", None, "2026-09-25T07:30:00Z", frequency=EVERY_15),
    CronJob("j-launch", "launch", "active", None, "2026-10-05T09:00:00Z", frequency=ONCE),
    CronJob("j-odd", "odd", "active", "0 9 1,15 * *", "2026-10-09T09:00:00Z", frequency=OTHER),
    CronJob("j-survey", "survey", "paused", None, "2026-09-20T10:00:00Z", paused_past_due=True),
    CronJob("j-migrate", "migrate", "done", None, None, "2026-09-24T12:00:00Z"),
)
CRON = CronSummary(
    "failing",
    active=4,
    paused=1,
    failing=(FAILURE,),
    ticker_at="2026-09-25T07:19:00Z",
    ticker_ok_at="2026-09-25T07:19:00Z",
    jobs=JOBS,
)
TITLE = f"> ## 🕒 Cron · 4{NB}active · 1{NB}paused · 1{NB}done"


def _snapshot(cron: CronSummary | None) -> DashboardSnapshot:
    return DashboardSnapshot(
        overall="warning",
        observed_at="2026-09-25T07:21:00Z",
        gateway=GatewaySummary("running", "connected", "2026-09-25T07:19:00Z"),
        cron=cron,
    )


def _lines(cron: CronSummary | None = CRON) -> list[str]:
    return render_dashboard(_snapshot(cron), now=NOW).splitlines()


def test_the_jobs_are_a_quote_of_their_own_before_details() -> None:
    lines = _lines()
    start = lines.index(TITLE)

    assert lines[start - 1] == ""  # a blank line before it, as before Details
    assert lines[start + 1 : start + 13] == [
        "> ## ONCE",
        f"> launch{JOINT}Oct 5",
        "> ## EVERY 15 MIN",
        f"> uptime-ping{JOINT}07:30",
        "> ## DAILY",
        f"> ⚠️ daily-digest{JOINT}Sat 06:00",
        "> ## OTHER",
        f"> odd{JOINT}0 9 1,15 * *{JOINT}Oct 9",
        "> ## ⏸ PAUSED",
        f"> survey{JOINT}Sep 20",
        "> ## ✅ DONE",
        f"> migrate{JOINT}Sep 24",
    ]
    details = lines.index("> ## Details")
    # Cron's details lines under the list, a blank line between, then the notes; none in Details.
    assert lines[start + 13 : details] == [
        ">",
        "> Cron 4 active · 1 paused · ticker 07:19",
        "> Cron daily-digest: run failed Sep 25 07:05, 3 in a row (rate limit)",
        f"> {CRON_FAILURE_NOTE}",
        f"> {CRON_PAUSED_NOTE}",
        "",
    ]
    assert not any(line.startswith("> Cron ") for line in lines[details:])


def test_the_html_form_draws_two_quotes_with_the_run_in_code() -> None:
    html = to_telegram_html(render_dashboard(_snapshot(CRON), now=NOW))

    assert html.count("<blockquote expandable>") == 2
    # Two quotes in a row are parted by their frames: no blank line between them.
    assert "</blockquote>\n<blockquote expandable><b>Details</b>" in html
    assert f"<blockquote expandable><b>🕒 Cron · 4{NB}active" in html
    assert f"\nuptime-ping{JOINT}<code>07:30</code>\n" in html
    assert f"\n⚠️ daily-digest{JOINT}<code>Sat 06:00</code>\n" in html
    assert f"\nodd{JOINT}<code>0 9 1,15 * *</code>{JOINT}<code>Oct 9</code>\n" in html
    assert "\n<b>EVERY 15 MIN</b>\n" in html


def test_a_job_named_like_a_heading_or_a_hashtag_stays_a_name() -> None:
    """A name is whatever the owner typed: ``#weekly`` is no bold line in either form, and a tap
    on it opens no hashtag search."""
    job = CronJob("j-w", "#weekly", "active", None, "2026-09-25T08:00:00Z", frequency=EVERY_15)
    cron = replace(CRON, jobs=(job,), state="ok", active=1, paused=0, failing=())
    text = render_dashboard(_snapshot(cron), now=NOW)

    assert f"\n<code>#weekly</code>{JOINT}<code>08:00</code>\n" in to_telegram_html(text)
    assert f"\n#weekly{JOINT}08:00\n" in to_telegram_plain(text)


def test_the_plain_form_keeps_the_rows_and_a_blank_line_before_details() -> None:
    plain = to_telegram_plain(render_dashboard(_snapshot(CRON), now=NOW)).splitlines()

    start = plain.index(f"🕒 CRON · 4{NB}ACTIVE · 1{NB}PAUSED · 1{NB}DONE")
    assert plain[start + 1 : start + 3] == ["ONCE", f"launch{JOINT}Oct 5"]
    assert plain[plain.index("DETAILS") - 1] == ""


def test_no_list_leaves_the_cron_lines_in_details_as_before() -> None:
    """No jobs, no data, no source: no quote, cron's line in Details as in 1.1.1."""
    unread = CronSummary("unknown", detail="jobs.json unreadable")
    for cron, line in (
        (
            replace(CRON, jobs=(), state="ok", failing=()),
            "> Cron 4 active · 1 paused · ticker 07:19",
        ),
        (unread, "> Cron: jobs.json unreadable"),
        (CronSummary("unsupported"), "> Cron: source not configured"),
    ):
        lines = _lines(cron)
        assert not any(row.startswith("> ## 🕒 Cron") for row in lines)
        assert lines.index(line) > lines.index("> ## Details")


def test_a_long_list_is_cut_to_what_the_screen_leaves_and_details_stays_whole() -> None:
    """Two hundred jobs with long names: the rows that fit, ``… N more`` for the rest, never a
    frequency's line without a job under it; Details whole, its stamps and the version last."""
    jobs = tuple(
        CronJob(
            f"j{i}",
            f"Long job name number {i:03d} for the team",
            "active",
            None,
            "2026-09-25T08:00:00Z",
            frequency=Frequency(f"Every {i + 1} min", float(i + 1)),
        )
        for i in range(200)
    )
    cron = replace(CRON, jobs=jobs, state="ok", active=200, paused=0, failing=())
    text = render_dashboard(_snapshot(cron), now=NOW)
    lines = text.splitlines()

    assert len(text) <= 3900
    more = next(line for line in lines if line.startswith("> … ") and line.endswith(" more"))
    shown = sum(1 for line in lines if line.startswith("> Long job name"))
    assert int(more.split()[2]) == 200 - shown and shown > 0
    assert not lines[lines.index(more) - 1].startswith("> ## ")
    assert lines[-1] == f"> Dashboard {__version__}" and "> ## Details" in lines
    assert to_telegram_html(text).count("<blockquote expandable>") == 2


def test_the_notes_follow_the_rows_kept() -> None:
    """A failure note explains a mark the list shows: no marked row, no note."""
    calm = replace(CRON, jobs=JOBS[1:3], state="ok", active=2, paused=0, failing=())
    lines = _lines(calm)

    assert f"> {CRON_FAILURE_NOTE}" not in lines and f"> {CRON_PAUSED_NOTE}" not in lines
    assert lines[lines.index("> ## ONCE") - 1] == f"> ## 🕒 Cron · 2{NB}active · 0{NB}paused"


def test_a_long_run_of_failures_leaves_no_quote_and_details_whole_as_in_1_1_1() -> None:
    """Review of 1.1.2: every job failing, each named in 80 characters. Cron's lines under the
    list left no room for a job, and the cut ate Details up to its heading, its stamps glued to
    the cron quote. Not one job fits now: no quote, cron's lines in Details, cut at its end."""
    names = [f"{i:02d} " + "n" * 77 for i in range(30)]
    jobs = tuple(
        CronJob(
            f"j{i}", name, "active", None, "2026-09-25T08:00:00Z", None, "run", frequency=EVERY_15
        )
        for i, name in enumerate(names)
    )
    failing = tuple(
        CronFailure(f"j{i}", name, "run", at="2026-09-25T07:05:00Z", streak=3, reason="auth")
        for i, name in enumerate(names)
    )
    cron = replace(CRON, jobs=jobs, active=30, paused=0, failing=failing)
    text = render_dashboard(_snapshot(cron), now=NOW)
    lines = text.splitlines()

    assert len(text) <= 3900
    assert not any(line.startswith("> ## 🕒 Cron") for line in lines)
    assert lines.index("> Cron 30 active · 0 paused · ticker 07:19") > lines.index("> ## Details")
    assert lines[-1] == f"> Dashboard {__version__}"
    assert to_telegram_html(text).count("<blockquote expandable>") == 1


def test_the_list_is_measured_as_telegram_counts_it() -> None:
    """A character past the BMP is two UTF-16 units to Telegram: names full of emoji still keep
    the screen within the limit."""
    jobs = tuple(
        CronJob(
            f"j{i}",
            f"🚀🚀🚀 job {i:03d} 🛰🛰🛰",
            "active",
            None,
            "2026-09-25T08:00:00Z",
            frequency=EVERY_15,
        )
        for i in range(300)
    )
    cron = replace(CRON, jobs=jobs, state="ok", active=300, paused=0, failing=())
    text = render_dashboard(_snapshot(cron), now=NOW)

    assert len(text.encode("utf-16-le")) // 2 <= 3900
    assert any(line.startswith("> 🚀🚀🚀 job") for line in text.splitlines())
