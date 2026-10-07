"""Ten static states from section 11 of the research, two for the message itself, one for the
Hermes version line, three showcase states for the catalog screenshots, two for an external
limits source, two for Gemini's 429 from the engine's log, three for the cron line and the
deaf channel, and one for the narrow phone.

The criteria of the research: no false green with partial coverage, exceptions are not pushed
out by normal metrics, the next action is nameable from the first screen. Since 25.09 one more:
the first screen is one phone screen, with the explanations in a collapsed details block.
"""

import pytest

from telegram_dashboard import __version__
from telegram_dashboard.render import TELEGRAM_TEXT_LIMIT, render_dashboard, to_telegram_html
from telegram_dashboard.states import PERIOD_SECONDS, all_states

STATES = all_states()
# A phone shows about 30 characters per line and about 15 lines of a message; emoji are wider
# than letters, so the columns are tighter than the count suggests. The lines are counted as the
# phone shows them: the blank line before the collapsed Details is one of them (decision of
# 01.10, when the cron line made the healthy screen 15 lines). The 32 is a guide (decisions of
# 01.10, from Ilya's phone) for every line of the main part but the limits block: a limits line
# keeps its exact times and wraps when it must (0.9.2), and an event with a name beyond ASCII is
# cut at eight, see test_render and test_cron_jobs.
PHONE_LINES = 15
PHONE_COLUMNS = 32


def _render(state) -> str:
    return render_dashboard(
        state.snapshot, now=state.now, delivery=state.delivery, period_seconds=PERIOD_SECONDS
    )


def _main_part(text: str) -> list[str]:
    lines = text.splitlines()
    return lines[: next((i for i, line in enumerate(lines) if line.startswith(">")), len(lines))]


def _held_to_width(main: list[str]) -> list[str]:
    """The lines the 32 is a guide for: the main part without the limits block, from its
    heading to the blank line after it."""
    if "## 🧠 Limits used" not in main:
        return main
    start = main.index("## 🧠 Limits used")
    end = next((i for i in range(start, len(main)) if not main[i]), len(main))
    return main[:start] + main[end:]


def test_twenty_six_states_are_defined_and_numbered() -> None:
    assert [state.number for state in STATES] == list(range(1, 27))


@pytest.mark.parametrize("state", STATES, ids=[f"{s.number:02d}" for s in STATES])
def test_every_state_renders_within_telegram_limit_and_matches_expected_overall(state) -> None:
    text = _render(state)
    lines = text.splitlines()

    assert state.snapshot.overall == state.expect_overall
    assert len(to_telegram_html(text)) <= TELEGRAM_TEXT_LIMIT
    # The status line carries the dated data stamp; a banner may sit above it.
    stamp = f" · {state.now:%b} {state.now.day} {state.now:%H:%M} UTC"
    assert any(line.endswith(stamp) for line in lines[:2])
    assert any(line.startswith("> Confirmed") for line in lines)
    assert "> Period 5 min" in lines
    assert text.count("- ") <= 40


@pytest.mark.parametrize(
    "state", [s for s in STATES if s.expect_overall != "normal"], ids=lambda s: f"{s.number:02d}"
)
def test_non_normal_states_never_show_green(state) -> None:
    assert "🟢 Healthy" not in _render(state)


def test_state_1_all_normal_is_green_and_names_coverage() -> None:
    text = _render(STATES[0])
    lines = text.splitlines()

    assert lines[0] == "🟢 Healthy · Sep 9 21:00 UTC"
    assert "Gateway ✓ · Telegram ✓" in lines
    assert "Drift ✓ 0 of 474" in lines
    # Two windows in the provider's order, each with the time to its own reset: one countdown
    # after two numbers would say nothing.
    assert "Claude 37% (3h) · 12% (4d3h)" in lines
    assert "Gemini · no data" in lines
    assert "> Gemini: source not confirmed" in lines
    assert "> Profiles 1/1 · sources 3/3" in lines
    assert "> Drift checked 08:00" in lines


def test_state_1_is_this_exact_screen() -> None:
    """The whole text of the normal state, as documentation of the form: every word, the order
    of the lines, the date style and the units. A change here is a change of the product."""
    expected = [
        "🟢 Healthy · Sep 9 21:00 UTC",
        "Gateway ✓ · Telegram ✓",
        "Drift ✓ 0 of 474",
        "",
        "## 🧠 Limits used",
        "Claude 37% (3h) · 12% (4d3h)",
        "Codex 61% (2h30m)",
        "Gemini · no data",
        "Grok · no data",
        "",
        "🤖 Hermes 0.21.1 ✓",
        "",
        "> ## Details",
        "> Confirmed 20:58",
        "> Period 5 min",
        "> Profiles 1/1 · sources 3/3",
        ">",
        "> Drift checked 08:00",
        "> Hermes 0.21.1 of Sep 7 is the latest · checked Sep 9 21:00",
        ">",
        "> ## No data",
        "> Gemini: source not confirmed",
        "> Grok: source not confirmed",
        ">",
        f"> Dashboard {__version__}",
    ]

    assert _render(STATES[0]).splitlines() == expected


def test_state_1_fits_one_phone_screen() -> None:
    main = _main_part(_render(STATES[0]))

    assert len(main) <= PHONE_LINES, main
    assert max(len(line) for line in _held_to_width(main)) <= PHONE_COLUMNS, main


def test_state_2_polling_dead_puts_the_incident_first() -> None:
    text = _render(STATES[1])

    assert text.startswith("🔴 Critical · Sep 9 21:00 UTC")
    assert "Gateway ✓ · Telegram error" in text.splitlines()
    assert text.index("Telegram disconnected") < text.index("Limits used")


def test_state_3_delivery_failed_is_a_warning_with_the_job_on_the_cron_line() -> None:
    """0.9.0, decision of 01.10: a result that was not delivered is a warning like a failed run;
    the job is on the Cron line, in the events and in the details with its streak and the kind
    of the error in a word. No ``## Work`` and no ``## Automation`` block."""
    state = STATES[2]
    text = _render(state)
    main = _main_part(text)

    assert state.expect_overall == "warning"
    assert main[0] == "🟡 Warning · Sep 9 21:00 UTC"
    assert "Cron ⚠️ 1 of 9 failing" in main
    assert "- Cron undelivered: daily-digest" in main
    assert (
        "> Cron daily-digest: not delivered Sep 9 20:40, 2 in a row (not connected)"
        in text.splitlines()
    )
    assert "## Work" not in text
    assert "## Automation" not in text


def test_state_9_drift_is_visible_in_both_incident_and_block() -> None:
    text = _render(STATES[8])

    assert "Config drift: 3 of 474 keys" in text
    assert "Drift ⚠️ 3 of 474" in text.splitlines()


def test_state_10_stale_source_lowers_coverage_and_names_it() -> None:
    text = _render(STATES[9])

    assert "⚪ Unknown" in text
    assert "Stale: limits" in _main_part(text)  # an exception stays on the screen


def test_state_11_stale_message_banner_is_first_line_even_when_data_is_fine() -> None:
    text = _render(STATES[10])

    assert text.startswith("🔴 DASHBOARD STALE")
    assert "🟢 Healthy" in text  # data is fine; the message is the problem, and both are said


def test_state_12_lost_message_is_named() -> None:
    text = _render(STATES[11])

    assert "🔴 Pinned message lost (21:00 UTC), NOT restored" in text


def test_html_conversion_escapes_and_bolds_headings() -> None:
    html = to_telegram_html("# Hermes Dashboard\n## Limits\n- a < b & c")

    assert html == "<b>Hermes Dashboard</b>\n<b>Limits</b>\n- a &lt; b &amp; c"


def _utf16_units(text: str) -> int:
    return len(text.encode("utf-16-le")) // 2


@pytest.mark.parametrize("state", STATES, ids=[f"{s.number:02d}" for s in STATES])
def test_every_state_has_a_plain_form_without_markup_inside_the_telegram_limit(state) -> None:
    """The plugin's fallback delivers plain text (engine ``edit_message``, ``finalize=False``):
    Telegram counts UTF-16 code units, emoji count twice, a ``#`` or a ``>`` marker would be
    shown literally."""
    from telegram_dashboard.render import to_telegram_plain

    plain = to_telegram_plain(_render(state))

    assert "#" not in plain
    assert "<" not in plain
    assert not any(line.startswith(">") for line in plain.splitlines())
    assert _utf16_units(plain) <= TELEGRAM_TEXT_LIMIT
    assert plain.splitlines()[0].startswith(("🟢", "🟡", "🔴", "⚪", "⚠️"))


def test_state_13_is_three_releases_behind_as_information_only() -> None:
    """The version line when upstream is ahead: two versions and an arrow on the screen, the
    dates and the count in the details; no sign, no advice, the status stays green."""
    state = STATES[12]
    text = _render(state)
    main = _main_part(text)
    lines = text.splitlines()

    assert state.title == "Hermes three releases behind"
    assert main[0] == "🟢 Healthy · Sep 9 21:00 UTC"
    assert main[-3:] == ["", "🤖 Hermes 0.20.5 → 0.21.1", ""]
    assert "> Hermes 0.20.5 of Aug 21, latest 0.21.1 of Sep 7" in lines
    assert "> 3 releases behind · checked Sep 9 21:00" in lines
    assert "⚠" not in text
    assert len(main) <= PHONE_LINES
    assert max(len(line) for line in _held_to_width(main)) <= PHONE_COLUMNS


SHOWCASE_HEALTHY = [
    "🟢 Healthy · Sep 26 21:00 UTC",
    "Gateway ✓ · Telegram ✓",
    "Backup ✓ 10 h ago",
    "Drift ✓ 0 of 481",
    "Cron ✓ 27 jobs",
    "",
    "## 🧠 Limits used",
    "Claude 42% (2h10m) · 67% (3d)",
    "⚠️ Codex 93% (1h20m) · 58% (5d)",
    "Gemini · no data",
    "Grok 31% (6d)",
    "Kimi 8% (3h40m) · 46% (21d)",
    "",
    "🤖 Hermes 0.21.3 → 0.21.5",
    "",
]


def test_state_14_showcase_shows_every_line_of_a_healthy_screen_on_one_phone_screen() -> None:
    """The screenshot state for the catalog: a healthy installation with all ten sources and
    every line the screen can show on 2026-09-26, made-up numbers except upstream's real
    release dates, one warning mark on a spent limit. Not a verification state: the thirteen
    above keep their golden texts."""
    state = STATES[13]
    text = _render(state)
    main = _main_part(text)
    lines = text.splitlines()

    assert state.title == "Showcase: every line of a healthy screen"
    assert main == SHOWCASE_HEALTHY
    assert len(main) <= PHONE_LINES
    assert max(len(line) for line in _held_to_width(main)) <= PHONE_COLUMNS
    assert text.count("⚠") == 1  # the limit line only: no incident, the status stays green
    assert "Needs attention" not in text
    assert "> Confirmed 20:58" in lines
    assert "> Profiles 1/1 · sources 10/10" in lines
    assert "> Cron 27 active · 3 paused · ticker 20:58" in lines
    assert "> Telegram last update seen Sep 26 20:30 · polling ok 20:58" in lines
    assert "> Telegram send errors: none in the log" in lines
    assert "> Backup Sep 26 11:00 · integrity ok" in lines
    assert "> Drift checked 08:00" in lines
    assert "> Hermes 0.21.3 of Sep 14, latest 0.21.5 of Sep 24" in lines
    assert "> 2 releases behind · checked Sep 26 21:00" in lines
    assert "> Gemini: Google reports Gemini quota only with billing enabled" in lines
    assert "> Gemini refusals seen by Hermes: none" in lines


def test_state_15_showcase_warning_is_the_healthy_screen_with_a_drift_incident() -> None:
    """The same installation with three drifted keys: the status turns yellow, the incident is
    named above the limits, the drift line carries the mark. Two lines more than the healthy
    form, so the one-screen budget is not asserted: an exception is never pushed out."""
    state = STATES[14]
    text = _render(state)
    main = _main_part(text)
    lines = text.splitlines()

    assert state.title == "Showcase: the healthy screen with config drift"
    assert main == [
        "🟡 Warning · Sep 26 21:00 UTC",
        "Gateway ✓ · Telegram ✓",
        "Backup ✓ 10 h ago",
        "Drift ⚠️ 3 of 481",
        "Cron ✓ 27 jobs",
        "",
        "## Needs attention",
        "- Config drift: 3 of 481 keys",
        *SHOWCASE_HEALTHY[5:],
    ]
    assert max(len(line) for line in _held_to_width(main)) <= PHONE_COLUMNS
    assert text.count("⚠") == 2  # the drift line and the spent limit
    assert "🟢 Healthy" not in text
    assert "> Confirmed 20:58" in lines
    assert "> Drift checked 08:00" in lines
    assert "> Hermes 0.21.3 of Sep 14, latest 0.21.5 of Sep 24" in lines


def test_state_16_showcase_stale_is_the_healthy_screen_under_the_banner() -> None:
    """The same healthy screen when the pinned message itself is unconfirmed for twenty
    minutes: the banner is the first line, the data below it stays green and unchanged."""
    state = STATES[15]
    text = _render(state)
    main = _main_part(text)
    lines = text.splitlines()

    assert state.title == "Showcase: the healthy screen under the stale banner"
    assert main == [
        "🔴 DASHBOARD STALE: last confirmation 20 min ago, threshold 10 min",
        *SHOWCASE_HEALTHY,
    ]
    assert text.count("⚠") == 1
    assert "> Confirmed 20:40" in lines
    assert "> Hermes 0.21.3 of Sep 14, latest 0.21.5 of Sep 24" in lines


def test_state_17_external_source_shows_a_model_limit_plans_and_an_ending_login() -> None:
    """The showcase installation with Claude answered by an external limits source (contract 1):
    the account's two windows on its line, every model's limit on a line of its own without the
    reset it shares with the week (decision of 29.09), the plans the providers name and the login
    that ends in two days as an incident."""
    state = STATES[16]
    text = _render(state)
    main = _main_part(text)
    lines = text.splitlines()

    assert state.title == "External source: a model limit, plans and an ending login"
    assert main == [
        "🟡 Warning · Sep 26 21:00 UTC",
        "Gateway ✓ · Telegram ✓",
        "Backup ✓ 10 h ago",
        "Drift ✓ 0 of 481",
        "Cron ✓ 27 jobs",
        "",
        "## Needs attention",
        "- Claude login expires in 2 days",
        "",
        "## 🧠 Limits used",
        "Claude 42% (2h10m) · 67% (3d)",
        "⚠️ Claude Fable 100%",
        "Claude Sonnet 20%",
        *SHOWCASE_HEALTHY[8:],
    ]
    # An exception is never pushed out, so only the width is the budget here.
    assert max(len(line) for line in _held_to_width(main)) <= PHONE_COLUMNS
    assert "> Profiles 1/1 · sources 11/11" in lines
    assert not any(line.startswith("> Claude Sonnet") for line in lines)
    assert "> Plans: Claude Max 5x · Codex Prolite · Grok SuperGrok" in lines
    assert "> Claude login until Sep 28" in lines
    assert "Data " not in text  # the source answered within the tick: one stamp for all


def test_state_18_external_source_says_the_login_expired() -> None:
    """The same installation when the source answers ``login_expired``: an answer, not "no
    data"; the line says it, the incident names it, the plan and the ended date stay in the
    details."""
    state = STATES[17]
    text = _render(state)
    main = _main_part(text)
    lines = text.splitlines()

    assert state.title == "External source: the login expired"
    assert main == [
        "🟡 Warning · Sep 26 21:00 UTC",
        "Gateway ✓ · Telegram ✓",
        "Backup ✓ 10 h ago",
        "Drift ✓ 0 of 481",
        "Cron ✓ 27 jobs",
        "",
        "## Needs attention",
        "- Claude login expired",
        "",
        "## 🧠 Limits used",
        "Claude · login expired",
        *SHOWCASE_HEALTHY[8:],
    ]
    assert max(len(line) for line in _held_to_width(main)) <= PHONE_COLUMNS
    assert "> Profiles 1/1 · sources 11/11" in lines
    assert "> Plans: Claude Max 5x · Codex Prolite · Grok SuperGrok" in lines
    assert "> Claude login ended Sep 26" in lines
    assert "> Claude: " not in text  # not a "no data" reason


def test_the_external_states_leave_the_showcase_untouched() -> None:
    """Demos 17 and 18 build on the showcase installation; 14 to 16 keep their screens (the
    catalog screenshots are taken from them) and carry no plan, no model limit, no login."""
    for state in STATES[13:16]:
        text = _render(state)
        assert "Plans:" not in text
        assert "login" not in text
        assert "> Profiles 1/1 · sources 10/10" in text.splitlines()


def test_the_external_states_carry_the_events_the_collector_builds() -> None:
    """The demo incidents are literals; they must stay what the tick says for the same answer."""
    from telegram_dashboard.collect import _login_incidents

    for state in STATES[16:18]:
        claude = next(q for q in state.snapshot.capacity.quotas if q.provider == "Claude")
        assert _login_incidents("Claude", claude, state.now) == state.snapshot.incidents


# ----------------------------------------------------------------------------- Gemini 429s (19-20)

_TTS = "gemini-2.5-flash-preview-tts"


def test_state_19_a_per_minute_429_marks_the_gemini_line_and_the_status_stays_green() -> None:
    """Decision of 29.09: a 429 Google gave within a per-minute window is the mark on the Gemini
    line for an hour, with its age in words; no event, the status stays green. The details say
    the last one and why there is no number."""
    state = STATES[18]
    text = _render(state)
    main = _main_part(text)
    lines = text.splitlines()

    assert state.title == "Gemini minute quota hit"
    assert main == [*SHOWCASE_HEALTHY[:9], "⚠️ Gemini hit limit 20 min ago", *SHOWCASE_HEALTHY[10:]]
    assert len(main) <= PHONE_LINES
    assert max(len(line) for line in _held_to_width(main)) <= PHONE_COLUMNS
    assert text.count("⚠") == 2  # the spent Codex limit and the Gemini line
    assert "Needs attention" not in text
    assert (
        f"> Gemini refusals seen by Hermes: last Sep 26 20:40, per-minute limit, model {_TTS}"
        in lines
    )
    assert "> Gemini: Google reports Gemini quota only with billing enabled" in lines
    assert "> Profiles 1/1 · sources 10/10" in lines


def test_state_20_a_daily_429_is_an_event_until_the_reset() -> None:
    """A retry longer than a per-minute window is the daily quota: an event and the yellow
    status until the reset Google named, the mark on the line as well. Two lines more than the
    healthy form, so only the width is the budget: an exception is never pushed out."""
    state = STATES[19]
    text = _render(state)
    main = _main_part(text)
    lines = text.splitlines()

    assert state.title == "Gemini day quota hit"
    assert main == [
        "🟡 Warning · Sep 26 21:00 UTC",
        *SHOWCASE_HEALTHY[1:5],
        "",
        "## Needs attention",
        "- Gemini daily limit used up",
        *SHOWCASE_HEALTHY[5:9],
        "⚠️ Gemini paused till 23:00",
        *SHOWCASE_HEALTHY[10:],
    ]
    assert max(len(line) for line in _held_to_width(main)) <= PHONE_COLUMNS
    assert text.count("⚠") == 2
    assert (
        "> Gemini refusals seen by Hermes: last Sep 26 18:57, daily limit till Sep 26 23:00, "
        f"model {_TTS}" in lines
    )


def test_the_gemini_states_carry_what_the_log_reader_builds() -> None:
    """The demo refusals and the event are literals; they must stay what ``gemini_log`` builds
    from the same log entry, and the showcase line what it says for a log without a 429."""
    from dataclasses import replace

    from telegram_dashboard.gemini_log import NO_QUOTA_REASON, activate, incidents_for
    from telegram_dashboard.schema import Refusal

    def gemini(state):
        return next(q for q in state.snapshot.capacity.quotas if q.provider == "Gemini")

    for state in STATES[13:18]:
        assert gemini(state).detail == NO_QUOTA_REASON
        assert gemini(state).refusal == Refusal()
    for state in STATES[18:20]:
        refusal = gemini(state).refusal
        assert gemini(state).detail == NO_QUOTA_REASON
        assert refusal is not None
        assert activate(replace(refusal, daily=False, active_until=None)) == refusal
        assert incidents_for(refusal, state.now) == state.snapshot.incidents


# ----------------------------------------------------------------------------- cron and traffic (21-23)
#
# 0.9.0: the showcase installation when a run fails three times in a row, when the cron ticker
# goes silent, and when the adapter is connected but its own counters say the channel is deaf.
# The events are literals; task 4 holds them to what ``incidents_for`` builds.


def test_state_21_a_run_failed_three_times_in_a_row() -> None:
    state = STATES[20]
    text = _render(state)
    main = _main_part(text)

    assert state.title == "Cron: a run failed three times in a row"
    assert main == [
        "🟡 Warning · Sep 26 21:00 UTC",
        *SHOWCASE_HEALTHY[1:4],
        "Cron ⚠️ 1 of 27 failing",
        "",
        "## Needs attention",
        "- Cron run failed: daily-digest",
        *SHOWCASE_HEALTHY[5:],
    ]
    assert max(len(line) for line in _held_to_width(main)) <= PHONE_COLUMNS
    assert (
        "> Cron daily-digest: run failed Sep 26 20:05, 3 in a row (rate limit)" in text.splitlines()
    )


def test_state_22_the_ticker_went_silent_is_critical() -> None:
    state = STATES[21]
    text = _render(state)
    main = _main_part(text)

    assert state.title == "Cron: the ticker went silent"
    assert main[0] == "🔴 Critical · Sep 26 21:00 UTC"
    assert "Cron ⚠️ ticker silent 12 min" in main
    assert "- Cron ticker silent 12 min" in main
    assert "> Cron 27 active · 3 paused · ticker 20:48" in text.splitlines()


def test_state_23_connected_but_deaf_is_critical_and_keeps_the_times() -> None:
    state = STATES[22]
    text = _render(state)
    main = _main_part(text)

    assert state.title == "Telegram connected but deaf: sends blocked"
    assert main[0] == "🔴 Critical · Sep 26 21:00 UTC"
    assert "Gateway ✓ · Telegram ⚠️ no sends" in main
    assert "- Telegram: sends blocked" in main
    assert (
        "> Telegram last update seen Sep 26 20:30 · polling ok 20:58 · sends blocked since 20:40"
        in text.splitlines()
    )
    # The events block adds three lines to the healthy form; an exception is never pushed out.
    assert len(main) <= PHONE_LINES + 3


def test_state_24_the_narrow_phone_keeps_the_exact_times_and_the_short_event() -> None:
    """0.9.1, decisions of 01.10 from Ilya's phone: the 32 is a guide and an event with a name
    beyond ASCII is cut at eight. 0.9.2: the marked Claude line keeps its exact times at 32 and
    wraps on the narrow phone, which is accepted. The event is a literal, held to what
    ``cron_jobs.incidents_for`` builds for the same record."""
    state = STATES[23]
    text = _render(state)
    main = _main_part(text)
    healthy = [
        "⚠️ Claude 29% (3h34m) · 87% (4d)" if line.startswith("Claude ") else line
        for line in SHOWCASE_HEALTHY
    ]

    assert state.title == "The narrow phone: a marked limits line and a Cyrillic job name"
    assert main == [
        "🟡 Warning · Sep 26 21:00 UTC",
        *healthy[1:4],
        "Cron ⚠️ 1 of 27 failing",
        "",
        "## Needs attention",
        "- Cron undelivered: Утренняя…",
        *healthy[5:],  # the Claude line exact, the week exactly four days away
    ]
    assert "⚠️ Codex 93% (1h20m) · 58% (5d)" in main
    # The events block adds three lines to the healthy form; an exception is never pushed out.
    assert len(main) <= PHONE_LINES + 3
    assert max(len(line) for line in _held_to_width(main)) <= PHONE_COLUMNS
    assert (
        "> Cron Утренняя сводка проекта: not delivered Sep 26 20:05, 1 run (chat unavailable)"
        in text.splitlines()
    )


def test_the_cron_states_carry_what_the_collector_builds() -> None:
    """The demo events of states 3, 21, 22 and 24 are literals; they must stay what
    ``cron_jobs`` says for the same block."""
    from telegram_dashboard.cron_jobs import incidents_for

    for state in (STATES[2], STATES[20], STATES[21], STATES[23]):
        assert state.snapshot.cron is not None
        assert incidents_for(state.snapshot.cron, now=state.now) == state.snapshot.incidents


def test_state_25_rules_only_in_the_gateway_s_directory_is_a_warning_with_the_place() -> None:
    lines = _render(STATES[24]).splitlines()

    assert lines[0].startswith("🟡 Warning")
    assert "Rules ⚠️ Telegram: not loaded" in lines
    assert "- Telegram: rules not loaded" in lines
    assert (
        "> Rules Telegram: AGENTS.md only in the gateway's directory, the agent works in another"
        in lines
    )
    assert "> Profiles 1/1 · sources 11/11" in lines


def test_state_26_an_edited_file_says_new() -> None:
    lines = _render(STATES[25]).splitlines()

    assert "Rules ⚠️ Telegram: outdated" in lines
    assert (
        "> Rules Telegram: AGENTS.md changed Sep 26 20:12 after session Sep 26 19:32: /new" in lines
    )


def test_the_rules_states_carry_what_the_collector_builds() -> None:
    """The demo events of states 25 and 26 are literals; they must stay what
    ``context_files.incidents_for`` says for the same verdicts."""
    from telegram_dashboard.context_files import incidents_for

    for state in (STATES[24], STATES[25]):
        assert state.snapshot.rules is not None
        assert incidents_for(state.snapshot.rules.platforms) == state.snapshot.incidents


def test_the_healthy_showcase_with_its_rules_loaded_keeps_its_fifteen_lines() -> None:
    """Review item 14: the rules line comes up to the screen only when an agent does not see
    its rules; loaded, the confirmation is a details line and the phone screen stays 15."""
    from dataclasses import replace

    from telegram_dashboard.schema import PlatformRules, RulesSummary, SourceObservation

    state = STATES[13]
    rules = RulesSummary(
        "observed",
        (PlatformRules("telegram", "loaded", "2026-09-26T19:32:34+00:00", ("AGENTS.md",), 11_162),),
    )
    source = SourceObservation(
        "context_files", "official", "fresh", observed_at=state.snapshot.observed_at
    )
    snapshot = replace(state.snapshot, rules=rules, sources=(*state.snapshot.sources, source))
    text = render_dashboard(
        snapshot, now=state.now, delivery=state.delivery, period_seconds=PERIOD_SECONDS
    )

    assert _main_part(text) == _main_part(_render(state))
    assert len(_main_part(text)) <= PHONE_LINES
    assert (
        "> Rules Telegram: ✓ AGENTS.md · 11,162 chars · session Sep 26 19:32" in text.splitlines()
    )
