"""Ten static states from section 11 of the research, two for the message itself, one for the
Hermes version line and three showcase states for the catalog screenshots.

The criteria of the research: no false green with partial coverage, exceptions are not pushed
out by normal metrics, the next action is nameable from the first screen. Since 25.09 one more:
the first screen is one phone screen, with the explanations in a collapsed details block.
"""

import pytest

from telegram_dashboard.render import TELEGRAM_TEXT_LIMIT, render_dashboard, to_telegram_html
from telegram_dashboard.states import PERIOD_SECONDS, all_states

STATES = all_states()
# A phone shows about 30 characters per line and about 15 lines of a message; emoji are wider
# than letters, so the budget is tighter than the count suggests.
PHONE_LINES = 14
PHONE_COLUMNS = 32


def _render(state) -> str:
    return render_dashboard(
        state.snapshot, now=state.now, delivery=state.delivery, period_seconds=PERIOD_SECONDS
    )


def _main_part(text: str) -> list[str]:
    lines = text.splitlines()
    return lines[: next((i for i, line in enumerate(lines) if line.startswith(">")), len(lines))]


def test_sixteen_states_are_defined_and_numbered() -> None:
    assert [state.number for state in STATES] == list(range(1, 17))


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
    assert "Claude 37% (3h) · 12% (4d)" in lines
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
        "Claude 37% (3h) · 12% (4d)",
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
    ]

    assert _render(STATES[0]).splitlines() == expected


def test_state_1_fits_one_phone_screen() -> None:
    main = _main_part(_render(STATES[0]))

    assert len(main) <= PHONE_LINES, main
    assert max(len(line) for line in main) <= PHONE_COLUMNS, main


def test_state_2_polling_dead_puts_the_incident_first() -> None:
    text = _render(STATES[1])

    assert text.startswith("🔴 Critical · Sep 9 21:00 UTC")
    assert "Gateway ✓ · Telegram error" in text.splitlines()
    assert text.index("Telegram disconnected") < text.index("Limits used")


def test_state_3_delivery_failed_is_critical_without_work_block() -> None:
    text = _render(STATES[2])

    assert "cron job result not delivered" in text
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
    assert len(main) <= PHONE_LINES and max(len(line) for line in main) <= PHONE_COLUMNS


SHOWCASE_HEALTHY = [
    "🟢 Healthy · Sep 26 21:00 UTC",
    "Gateway ✓ · Telegram ✓",
    "Backup ✓ 10 h ago",
    "Drift ✓ 0 of 481",
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
    """The screenshot state for the catalog: a healthy installation with all six sources and
    every line the screen can show on 2026-09-26, made-up numbers except upstream's real
    release dates, one warning mark on a spent limit. Not a verification state: the thirteen
    above keep their golden texts."""
    state = STATES[13]
    text = _render(state)
    main = _main_part(text)
    lines = text.splitlines()

    assert state.title == "Showcase: every line of a healthy screen"
    assert main == SHOWCASE_HEALTHY
    assert len(main) <= PHONE_LINES and max(len(line) for line in main) <= PHONE_COLUMNS
    assert text.count("⚠") == 1  # the limit line only: no incident, the status stays green
    assert "Needs attention" not in text
    assert "> Confirmed 20:58" in lines
    assert "> Profiles 1/1 · sources 6/6" in lines
    assert "> Backup Sep 26 11:00 · integrity ok" in lines
    assert "> Drift checked 08:00" in lines
    assert "> Hermes 0.21.3 of Sep 14, latest 0.21.5 of Sep 24" in lines
    assert "> 2 releases behind · checked Sep 26 21:00" in lines
    assert "> Gemini: source not confirmed" in lines


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
        "",
        "## Needs attention",
        "- Config drift: 3 of 481 keys",
        *SHOWCASE_HEALTHY[4:],
    ]
    assert max(len(line) for line in main) <= PHONE_COLUMNS
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
