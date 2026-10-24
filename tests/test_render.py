import unicodedata
from dataclasses import replace
from datetime import UTC, datetime, timedelta, timezone

import pytest

from telegram_dashboard import __version__
from telegram_dashboard.cron_jobs import NAME_LIMIT as CRON_NAME_LIMIT
from telegram_dashboard.freshness import DeliveryRecord
from telegram_dashboard.gemini_log import incidents_for
from telegram_dashboard.render import (
    render_dashboard,
    to_telegram_html,
    to_telegram_plain,
)
from telegram_dashboard.schema import (
    BackupSummary,
    CapacitySummary,
    Coverage,
    CronFailure,
    CronSummary,
    DashboardSnapshot,
    DriftSummary,
    GatewaySummary,
    Incident,
    QuotaMetric,
    QuotaWindow,
    Refusal,
    SourceObservation,
    TrafficSummary,
    VersionSummary,
    WorkSummary,
)

NOW = datetime(2026, 9, 25, 7, 21, tzinfo=UTC)
# The top line of a screen of NOW as the neutral text carries it (1.0.0): the moment as a number
# for the tag, then the status and the time of the data.
TOP_HEALTHY = "#@1790320860 🟢 Healthy · updated Sep 25 07:21 UTC"
NBSP = "\xa0"


def _main_part(text: str) -> list[str]:
    """The lines a phone shows before the collapsed details."""
    lines = text.splitlines()
    return lines[: next((i for i, line in enumerate(lines) if line.startswith(">")), len(lines))]


def _row(*names: str) -> str:
    return " ".join(f"✓{NBSP}{name}" for name in names)


def test_critical_incident_renders_before_normal_summary() -> None:
    snapshot = DashboardSnapshot(
        overall="critical",
        observed_at="2026-09-09T00:00:00Z",
        coverage=Coverage(expected_profiles=2, observed_profiles=2),
        work=WorkSummary(executing=1),
        incidents=(
            Incident(
                incident_id="cron:delivery",
                severity="critical",
                title="cron result not delivered",
            ),
        ),
    )

    rendered = render_dashboard(snapshot)

    assert rendered.index("cron result not delivered") < rendered.index("## Work")
    assert "🔴 Critical" in rendered


def test_partial_unknown_coverage_never_renders_as_healthy_or_zero() -> None:
    snapshot = DashboardSnapshot(
        overall="unknown",
        observed_at="2026-09-09T00:00:00Z",
        coverage=Coverage(
            expected_profiles=3,
            observed_profiles=2,
            failed_sources=("profile:worker/cron",),
        ),
    )

    rendered = render_dashboard(snapshot)

    assert "⚪ Unknown" in rendered
    # Incomplete coverage is an exception and stays on the screen, not only in the details.
    assert "❔ Not checked: profiles 2/3, 1 source unavailable" in _main_part(rendered)
    assert "🟢 Healthy" not in rendered


def test_complete_coverage_leaves_the_screen_and_stays_in_the_details() -> None:
    snapshot = DashboardSnapshot(
        overall="normal",
        observed_at="2026-09-09T00:00:00Z",
        coverage=Coverage(expected_profiles=1, observed_profiles=1),
    )

    rendered = render_dashboard(snapshot)

    assert not any("Not checked" in line for line in _main_part(rendered))
    assert "> Profiles 1/1" in rendered.splitlines()


def test_work_states_are_rendered_as_distinct_counts() -> None:
    snapshot = DashboardSnapshot(
        overall="warning",
        observed_at="2026-09-09T00:00:00Z",
        work=WorkSummary(
            executing=1,
            queued=2,
            waiting_human=3,
            failed=4,
            unknown=5,
        ),
    )

    rendered = render_dashboard(snapshot)

    assert "Running: 1" in rendered
    assert "Queued: 2" in rendered
    assert "Waiting for a human: 3" in rendered
    assert "Failed: 4" in rendered
    assert "Unknown: 5" in rendered


def test_renderer_redacts_secrets_paths_and_controls_from_incidents() -> None:
    snapshot = DashboardSnapshot(
        overall="critical",
        observed_at="2026-09-09T00:00:00Z",
        incidents=(
            Incident(
                incident_id="provider:error",
                severity="critical",
                title=(
                    "Error sk-exampleSecret123456789 in /home/alice/private/config.yaml\x1b[31m"
                ),
            ),
        ),
    )

    rendered = render_dashboard(snapshot)

    assert "sk-exampleSecret123456789" not in rendered
    assert "/home/alice/private/config.yaml" not in rendered
    assert "\x1b" not in rendered
    assert "[secret]" in rendered
    assert "[path]" in rendered


def test_local_usage_never_becomes_a_provider_quota_percentage() -> None:
    snapshot = DashboardSnapshot(
        overall="normal",
        observed_at="2026-09-09T00:00:00Z",
        capacity=CapacitySummary(
            quotas=(
                QuotaMetric(
                    provider="OpenAI",
                    kind="local",
                    used=120_000,
                    limit=None,
                ),
            ),
        ),
    )

    rendered = render_dashboard(snapshot)

    assert "OpenAI · locally 120,000 tokens" in rendered.splitlines()
    assert "> OpenAI: remaining unknown, counted locally" in rendered.splitlines()
    assert "%" not in rendered  # a count is never shown as a share


def test_official_quota_with_limit_renders_the_spent_share_and_the_time_to_its_reset() -> None:
    snapshot = DashboardSnapshot(
        overall="normal",
        observed_at="2026-09-09T00:00:00Z",
        capacity=CapacitySummary(
            quotas=(
                QuotaMetric(
                    provider="OpenAI",
                    kind="official",
                    used=75,
                    limit=100,
                    reset_at="2026-09-10T00:00:00Z",
                ),
            ),
        ),
    )

    rendered = render_dashboard(snapshot)

    # The reset counts from the data time when no ``now`` is given.
    assert "OpenAI 75% (1d)" in rendered.splitlines()


def test_a_spent_limit_gets_the_mark_and_a_state_in_words_never_becomes_a_number() -> None:
    snapshot = DashboardSnapshot(
        overall="normal",
        observed_at="2026-09-25T07:21:00Z",
        capacity=CapacitySummary(
            quotas=(
                QuotaMetric("Claude", "unavailable", detail="no account token"),
                QuotaMetric(
                    "Codex",
                    "official",
                    windows=(QuotaWindow("Session", 98.0, "2026-09-25T08:42:00Z"),),
                    fetched_at="2026-09-25T07:21:00Z",
                ),
                QuotaMetric(
                    "Grok",
                    "official",
                    windows=(
                        QuotaWindow("7d", None, "2026-10-01T19:25:00Z", note="usage not started"),
                    ),
                    fetched_at="2026-09-25T07:21:00Z",
                ),
                QuotaMetric(
                    "Kimi",
                    "official",
                    windows=(
                        QuotaWindow("5h", 0.0, "2026-09-25T11:34:00Z"),
                        QuotaWindow("month", 3.0, "2026-10-25T00:00:00Z"),
                    ),
                    fetched_at="2026-09-25T07:16:00Z",
                ),
                QuotaMetric("Gemini", "unsupported", detail="source not confirmed"),
            ),
        ),
    )

    rendered = render_dashboard(snapshot, now=NOW, period_seconds=300)
    lines = rendered.splitlines()

    assert "## 🧠 Limits used" in lines  # every percent on the screen is the spent share
    assert "⚠️ Codex 98% (1h21m)" in lines  # 90% and above: the mark, only on this line
    assert "Grok · usage not started" in lines  # the provider's words, no zero
    assert "Kimi 0% (4h13m) · 3% (29d17h)" in lines  # the provider's order
    assert "Claude · no data" in lines and "Gemini · no data" in lines
    assert "🟡" not in rendered and lines[0] == TOP_HEALTHY
    # Reasons and the odd minute live in the details, grouped; the resets are on the lines.
    assert "> ## Resets" not in lines
    assert "> Data 07:21 · Kimi 07:16" in lines
    assert "> ## No data" in lines
    assert "> Claude: no account token" in lines
    assert "> Gemini: source not confirmed" in lines
    assert "> Period 5 min" in lines


def _provider_line(provider: str, *windows: QuotaWindow) -> str:
    snapshot = DashboardSnapshot(
        overall="normal",
        observed_at=NOW.isoformat(),
        capacity=CapacitySummary((QuotaMetric(provider, "official", windows=windows),)),
    )
    lines = render_dashboard(snapshot, now=NOW).splitlines()
    return next(line for line in lines if provider in line and not line.startswith(">"))


def _codex_line(*windows: QuotaWindow) -> str:
    return _provider_line("Codex", *windows)


@pytest.mark.parametrize(
    ("ahead", "words"),
    [
        (timedelta(minutes=-5), "0m"),  # a reset behind the data time: due now
        (timedelta(seconds=30), "1m"),  # whole minutes, rounded up
        (timedelta(minutes=45), "45m"),
        (timedelta(minutes=59), "59m"),
        (timedelta(hours=1), "1h"),
        (timedelta(hours=1, minutes=1), "1h1m"),
        (timedelta(hours=1, minutes=21), "1h21m"),
        (timedelta(hours=3, minutes=34), "3h34m"),
        (timedelta(hours=23, minutes=59), "23h59m"),  # under a day: to the minute
        (timedelta(hours=23, minutes=59, seconds=30), "1d"),  # the seconds round up to a day
        (timedelta(days=1), "1d"),  # from a day on: to the hour
        (timedelta(days=1, minutes=1), "1d1h"),  # the hour rounded up, never early
        (timedelta(days=1, hours=5), "1d5h"),
        (timedelta(days=1, hours=5, minutes=40), "1d6h"),
        (timedelta(days=1, hours=23, minutes=59), "2d"),
        (timedelta(days=2), "2d"),
        (timedelta(days=2, hours=23, minutes=59), "3d"),
        (timedelta(days=4), "4d"),  # a zero hour is not written
        (timedelta(days=4, hours=2, minutes=37), "4d3h"),
        (timedelta(days=9, minutes=1), "9d1h"),
        (timedelta(days=23, minutes=3), "23d1h"),
    ],
)
def test_the_time_to_a_reset_is_written_in_brackets_after_the_share(
    ahead: timedelta, words: str
) -> None:
    """Ilya's rule of 01.10 (0.9.2): the time is never earlier than the real reset. Under a
    day it goes to the minute, from a day on to the hour, both rounded up."""
    reset = (NOW + ahead).isoformat()

    assert _codex_line(QuotaWindow("Session", 14.0, reset)) == f"Codex 14% ({words})"


def test_a_reset_date_that_cannot_be_read_is_a_question_mark_not_a_silence() -> None:
    assert _codex_line(QuotaWindow("Session", 14.0, "soon")) == "Codex 14% (?)"
    assert _codex_line(QuotaWindow("Session", 14.0, None)) == "Codex 14%"


def test_windows_carry_no_length_label_only_a_model_scope_in_the_provider_s_order() -> None:
    """The engine's words (``agent/account_usage.py``: Codex ``Session``/``Weekly`` by position,
    whatever the window's length; Claude ``Current session``/``Current week``) never reach the
    screen; a limit for one model keeps its scope."""
    # Whole hours and days keep the expectation short; the times have their own table.
    line = _codex_line(
        QuotaWindow("Session", 37.0, (NOW + timedelta(hours=3)).isoformat()),
        QuotaWindow("Weekly", 95.0, (NOW + timedelta(days=4)).isoformat()),
        QuotaWindow("Opus week", 5.0, (NOW + timedelta(days=4)).isoformat()),
        QuotaWindow("Sonnet week", None, None, note="usage not started"),
    )

    # The more spent window keeps its place; the mark belongs to the line.
    assert line == "⚠️ Codex 37% (3h) · 95% (4d) · Opus 5% (4d) · Sonnet usage not started"


def test_the_first_line_is_the_status_with_the_dated_stamp_and_the_details_follow_the_screen() -> (
    None
):
    snapshot = DashboardSnapshot(
        overall="normal",
        observed_at="2026-09-25T07:21:00Z",
        coverage=Coverage(expected_profiles=1, observed_profiles=1),
        gateway=GatewaySummary("running", "connected"),
        backup=BackupSummary("ok", finished_at="2026-09-25T00:31:00Z", integrity="ok"),
        drift=DriftSummary("clean", 0, 481, "2026-09-25T07:21:00Z"),
    )
    delivery = DeliveryRecord(message_id=1, last_confirmed_at="2026-09-25T07:16:00Z")

    rendered = render_dashboard(snapshot, now=NOW, delivery=delivery, period_seconds=300)

    # 1.0.0: what works is one row; the ages and counts its lines carried are in the details,
    # the mark in front of a verdict; the screen's own stamps close the block before the version.
    assert _main_part(rendered) == [
        TOP_HEALTHY,
        "",
        _row("Gateway", "Telegram", "Backup", "Drift"),
        "",
    ]
    assert rendered.splitlines()[4:] == [
        "> ## Details",
        "> ✓ Backup Sep 25 00:31 · 6 h ago · integrity ok",
        "> ✓ Drift 0 of 481 · checked 07:21",
        ">",
        "> Screen Sep 25 07:21 UTC",
        "> Confirmed 07:16",
        "> Period 5 min",
        "> Profiles 1/1",
        ">",
        f"> Dashboard {__version__}",
    ]


def test_abnormal_gateway_and_drift_are_words_on_the_screen() -> None:
    """1.0.0: an item that is not well leaves the ✓ row and stands under "Needs attention" in
    its own words; no row at all when nothing is well."""
    snapshot = DashboardSnapshot(
        overall="critical",
        observed_at="2026-09-25T07:21:00Z",
        gateway=GatewaySummary("stopped", "disconnected"),
        drift=DriftSummary("drift", 3, 481, "2026-09-25T07:21:00Z"),
    )

    main = _main_part(render_dashboard(snapshot))

    assert main[2:] == [
        "## 🛠 Needs attention",
        "Gateway stopped",
        "Telegram disconnected",
        "Drift ⚠️ 3 of 481",
        "",
    ]
    assert not any(line.startswith("✓") for line in main)


def test_plain_form_uppercases_headings_unwraps_details_and_leaves_every_other_line_untouched() -> (
    None
):
    """The engine's ``edit_message`` without ``finalize`` sends plain text: no parse mode, no
    bold, no quote. A ``#`` heading would reach the chat as a literal ``#``; upper case survives
    anywhere, and the details are simply shown in place."""
    plain = to_telegram_plain(
        "🔴 DASHBOARD STALE\n🟢 Healthy · Sep 25 07:21 UTC\n## Limits\n- Claude: 5h 37% · a < b\n"
        "\n> ## Details\n> Period 5 min\n>\n> ## Resets\n> Codex Sep 19"
    )

    assert plain == (
        "🔴 DASHBOARD STALE\n🟢 Healthy · Sep 25 07:21 UTC\nLIMITS\n- Claude: 5h 37% · a < b\n"
        "\nDETAILS\nPeriod 5 min\n\nRESETS\nCodex Sep 19"
    )
    assert "#" not in plain and ">" not in plain
    assert "<b>" not in plain and "&lt;" not in plain


def test_html_form_bolds_headings_and_folds_the_details_into_one_expandable_quote() -> None:
    html = to_telegram_html(
        "🟢 Healthy · Sep 25 07:21 UTC\n## Limits\n- a < b & c\n"
        "\n> ## Details\n> Period 5 min\n>\n> ## Resets\n> Codex Sep 19"
    )

    assert html == (
        "🟢 Healthy · Sep 25 07:21 UTC\n<b>Limits</b>\n- a &lt; b &amp; c\n\n"
        "<blockquote expandable><b>Details</b>\nPeriod 5 min\n\n<b>Resets</b>\nCodex Sep 19"
        "</blockquote>"
    )
    assert html.count("<blockquote") == 1


def test_html_form_puts_a_file_name_and_a_command_in_code_by_their_form() -> None:
    """Window of 07.10: Telegram made ``AGENTS.md`` a link and ``/new`` a command. The rule is
    the form, not a list: a name, a dot, an extension that starts with a letter; a ``/word``.
    A version, a count and a fraction stay plain; the plain form is left as it was."""
    line = (
        "> Rules Max: AGENTS.md changed Oct 7 17:29, after session Sep 24 19:23: /new\n"
        "> .hermes.md, .cursorrules, .cursor/rules/a.mdc, AGENTS.override.md, state.db.\n"
        "- Config drift: model.default, display.platforms.max.cleanup_progress\n"
        "Hermes 0.21.3 → 0.21.5 · v0.10.0 · kimi-k2.5 · gemini-2.5-pro · e.g. 1.5x\n"
        "Profiles 1/1 · sources 12/12 · 11,162 chars · the gateway's a < b"
    )

    html = to_telegram_html(line)

    coded = (
        "AGENTS.md",
        "/new",
        ".hermes.md",
        ".cursorrules",
        ".cursor/rules/a.mdc",
        "AGENTS.override.md",
        "state.db",
        "model.default",
        "display.platforms.max.cleanup_progress",
    )
    for word in coded:
        assert f"<code>{word}</code>" in html, word
    assert "<code>state.db</code>." in html
    assert html.count("<code>") == len(coded)
    assert "Hermes 0.21.3 → 0.21.5 · v0.10.0 · kimi-k2.5 · gemini-2.5-pro · e.g. 1.5x" in html
    assert "Profiles 1/1 · sources 12/12 · 11,162 chars · the gateway&#x27;s a &lt; b" in html
    assert "<code>" not in to_telegram_plain(line)


@pytest.mark.parametrize(
    ("line", "coded"),
    [
        ("send /new-session", "/new-session"),
        ("x ./new", "./new"),
        ("-/new", "-/new"),
        ("/2fa", "/2fa"),
        ("see x.com/y", "x.com/y"),
        ("2.ai", "2.ai"),
        ("AGENTS.md/", "AGENTS.md/"),
        ("https://api.example.org/bot/x", "//api.example.org/bot/x"),
        # 0.12.0: a mention and a hashtag, as a cron job's name may carry them.
        ("watch @someone", "@someone"),
        ("daily #digest", "#digest"),
        ("mail a@b.com", "a@b.com"),
        # Review of 0.12.0: a mark after a dash, a slash or a dot, and a cashtag.
        ("report-#weekly", "report-#weekly"),
        ("backup-@ilya", "backup-@ilya"),
        ("deploy/#ops", "deploy/#ops"),
        ("watch $BTC", "$BTC"),
        # Second round: a mark right after another mark.
        ("ping $@user", "$@user"),
        ("to @#alerts", "@#alerts"),
    ],
)
def test_html_form_codes_what_telegram_finds_inside_a_word(line: str, coded: str) -> None:
    """Review of 0.10.1: Telegram finds a command or a domain inside a longer word; the whole
    word goes to ``<code>``. A fraction, an abbreviation and a version stay plain."""
    assert f"<code>{coded}</code>" in to_telegram_html(line)
    assert to_telegram_html("1/1 · 12/12 · e.g. i.e. 17:29 v0.10.1") == (
        "1/1 · 12/12 · e.g. i.e. 17:29 v0.10.1"
    )
    # The inside of an entity is no hashtag, and ``C#`` is none either.
    assert to_telegram_html("the gateway's C# job") == "the gateway&#x27;s C# job"


def test_drift_that_could_not_be_checked_shows_why_and_no_check_time() -> None:
    snapshot = DashboardSnapshot(
        overall="unknown",
        observed_at="2026-09-09T00:00:00Z",
        drift=DriftSummary("unknown", detail="deadline 35 s"),
    )

    rendered = render_dashboard(snapshot)

    assert "Drift: unknown" in rendered.splitlines()
    assert "> Drift: deadline 35 s" in rendered.splitlines()
    assert "checked" not in rendered and "check time" not in rendered


# ----------------------------------------------------------------------------- the version line
#
# Information only: which Hermes the gateway runs and which release upstream marks Latest. No
# sign, no advice; the dates, the count and the time of the check live in the details.

_CHECKED = "2026-09-25T16:40:00+00:00"
_OURS = "2026-09-14T16:04:14Z"
_LATEST = "2026-09-24T10:09:38Z"
# The snapshot's own time, 30 hours after 0.21.5 came out: past the day of the new-release mark.
_VERSION_NOW = datetime(2026, 9, 25, 16, 45, tzinfo=UTC)


def _with_version(version: VersionSummary) -> DashboardSnapshot:
    return DashboardSnapshot(
        overall="normal",
        observed_at="2026-09-25T16:45:00+00:00",
        capacity=CapacitySummary((QuotaMetric("Codex", "official", used=10, limit=100),)),
        version=version,
    )


def _version_render(
    version: VersionSummary, now: datetime = _VERSION_NOW
) -> tuple[list[str], list[str]]:
    text = render_dashboard(_with_version(version), now=now, zone=UTC)
    main = _main_part(text)
    return main, text.splitlines()


@pytest.mark.parametrize(
    ("version", "line", "details"),
    [
        (
            VersionSummary("0.21.3", "0.21.5", _OURS, _LATEST, 2, 36, _CHECKED),
            "🤖 Hermes 0.21.3 → 0.21.5",
            [
                "> Hermes 0.21.3 of Sep 14, latest 0.21.5 of Sep 24",
                "> 2 releases behind · checked Sep 25 16:40",
            ],
        ),
        (
            VersionSummary("0.21.4", "0.21.5", "2026-09-21T18:10:55Z", _LATEST, 1, 36, _CHECKED),
            "🤖 Hermes 0.21.4 → 0.21.5",
            [
                "> Hermes 0.21.4 of Sep 21, latest 0.21.5 of Sep 24",
                "> 1 release behind · checked Sep 25 16:40",
            ],
        ),
        (
            VersionSummary("0.21.5", "0.21.5", _LATEST, _LATEST, 0, 36, _CHECKED),
            "🤖 Hermes 0.21.5 ✓",
            ["> Hermes 0.21.5 of Sep 24 is the latest · checked Sep 25 16:40"],
        ),
        (
            VersionSummary("0.21.3", checked_at=_CHECKED, reason="GitHub rate limit"),
            "🤖 Hermes 0.21.3 · no data",
            ["> Hermes latest: GitHub rate limit · checked Sep 25 16:40"],
        ),
        (
            VersionSummary(
                None, "0.21.5", None, _LATEST, None, 36, _CHECKED, None, "not on this installation"
            ),
            "🤖 Hermes · no data",
            [
                "> Hermes version: not on this installation",
                "> Hermes latest 0.21.5 of Sep 24 · checked Sep 25 16:40",
            ],
        ),
        (
            VersionSummary("0.21.6", "0.21.5", "2026-09-26T10:00:00Z", _LATEST, -1, 36, _CHECKED),
            "🤖 Hermes 0.21.6 · latest 0.21.5",
            [
                "> Hermes 0.21.6 of Sep 26 is newer than the latest 0.21.5 of Sep 24 · checked Sep 25 16:40"
            ],
        ),
        (
            VersionSummary("0.20.0", "0.21.5", None, _LATEST, None, 36, _CHECKED),
            "🤖 Hermes 0.20.0 · latest 0.21.5",
            [
                "> Hermes 0.20.0 not among the last 36 releases, latest 0.21.5 of Sep 24"
                " · checked Sep 25 16:40"
            ],
        ),
    ],
    ids=["behind", "one-behind", "same", "no-latest", "no-running", "newer", "not-listed"],
)
def test_the_version_line_and_its_details(
    version: VersionSummary, line: str, details: list[str]
) -> None:
    main, lines = _version_render(version)

    assert main[-2:] == [line, ""]
    assert main[-3] == ""  # its own block, apart from the limits above
    assert main.index(line) > main.index("## 🧠 Limits used")
    assert len(line) <= 32
    for expected in details:
        assert expected in lines
    for word in ("update", "upgrade", "hermes update", "⚠"):
        assert word not in line.lower()


def test_the_version_line_follows_the_limits_and_is_absent_without_a_version() -> None:
    without = render_dashboard(
        DashboardSnapshot(overall="normal", observed_at="2026-09-25T16:45:00+00:00"),
        now=NOW,
        zone=UTC,
    )

    assert "Hermes" not in without


def test_the_version_line_stands_alone_without_a_limits_block() -> None:
    snapshot = DashboardSnapshot(
        overall="normal",
        observed_at="2026-09-25T16:45:00+00:00",
        version=VersionSummary("0.21.5", "0.21.5", _LATEST, _LATEST, 0, 36, _CHECKED),
    )

    main = _main_part(render_dashboard(snapshot, now=_VERSION_NOW, zone=UTC))

    assert main[-3:] == ["", "🤖 Hermes 0.21.5 ✓", ""]


# 0.11.0: for a day after Latest came out, the version line carries 🆕 and its age; the details
# the time it came out. v0.21.6 came out on 2026-10-08 at 11:51:57 UTC.
_V0216_OUT = "2026-10-08T11:51:57Z"


@pytest.mark.parametrize(
    ("at", "line"),
    [
        (datetime(2026, 10, 8, 12, 10, tzinfo=UTC), "🤖 Hermes 0.21.3 → 0.21.6 🆕 18m"),
        (datetime(2026, 10, 8, 14, 52, tzinfo=UTC), "🤖 Hermes 0.21.3 → 0.21.6 🆕 3h"),
        (datetime(2026, 10, 9, 11, 51, tzinfo=UTC), "🤖 Hermes 0.21.3 → 0.21.6 🆕 23h"),
        (datetime(2026, 10, 9, 11, 51, 57, tzinfo=UTC), "🤖 Hermes 0.21.3 → 0.21.6"),
        (datetime(2026, 10, 8, 11, 50, tzinfo=UTC), "🤖 Hermes 0.21.3 → 0.21.6 🆕 1m"),
    ],
    ids=["18 minutes", "3 hours", "23 hours", "a day: gone", "clock behind GitHub"],
)
def test_a_release_out_less_than_a_day_ago_is_marked_new_with_its_age(
    at: datetime, line: str
) -> None:
    version = VersionSummary(
        "0.21.3", "0.21.6", _OURS, _V0216_OUT, 3, 37, checked_at=at.isoformat()
    )

    main, lines = _version_render(version, now=at)

    assert main[-2:] == [line, ""]
    assert _columns(line) <= 32
    assert ("> New release: Hermes 0.21.6 out Oct 8 11:51" in lines) == ("🆕" in line)


@pytest.mark.parametrize(
    ("running", "behind", "line"),
    [
        ("0.21.6", 0, "🤖 Hermes 0.21.6 ✓ 🆕 3h"),
        ("0.22.0.dev0+local.abcdef", 3, "🤖 Hermes → 0.21.6 🆕 3h"),
        # Too long for anything after it: the version cut, the mark only in the details.
        ("0.22.0.dev0+local.abcdef", None, "🤖 Hermes 0.22.0.dev0+local.abc…"),
        # Latest does not fit beside ours: the mark goes with it, or it would date ours.
        ("0.21.6.1-local", None, "🤖 Hermes 0.21.6.1-local"),
    ],
    ids=[
        "running it already",
        "a long version behind",
        "a long version not listed",
        "latest left out, the mark too",
    ],
)
def test_the_new_mark_keeps_the_line_within_a_phone_line(
    running: str, behind: int | None, line: str
) -> None:
    at = datetime(2026, 10, 8, 14, 52, tzinfo=UTC)
    version = VersionSummary(running, "0.21.6", None, _V0216_OUT, behind, 37, at.isoformat())

    main, lines = _version_render(version, now=at)

    assert main[-2] == line
    assert _columns(line) <= 32
    assert "> New release: Hermes 0.21.6 out Oct 8 11:51" in lines


def _columns(line: str) -> int:
    """Columns on a phone: an emoji takes two."""
    return sum(2 if unicodedata.east_asian_width(char) in "WF" else 1 for char in line)


def test_a_failed_check_keeps_the_last_answer_and_says_as_of_when() -> None:
    version = VersionSummary(
        "0.21.3",
        "0.21.5",
        _OURS,
        _LATEST,
        2,
        36,
        checked_at=_CHECKED,
        reason="GitHub rate limit",
        confirmed_at="2026-09-25T15:00:00+00:00",
    )

    main, lines = _version_render(version)

    assert main[-2:] == ["🤖 Hermes 0.21.3 → 0.21.5", ""]
    assert "> Hermes latest as of Sep 25 15:00: GitHub rate limit · checked Sep 25 16:40" in lines
    assert "> 2 releases behind" in lines


def test_a_version_reason_is_sanitized_like_every_other_reason() -> None:
    _main, lines = _version_render(
        VersionSummary("0.21.3", checked_at=_CHECKED, reason="collector crashed: /root/.env")
    )

    assert not any("/root/.env" in line for line in lines)


_LONG = "0.22.0.dev0+local.abcdef"


@pytest.mark.parametrize(
    ("version", "keeps", "detail"),
    [
        (
            VersionSummary(_LONG, "0.21.5", None, _LATEST, None, 36, _CHECKED),
            "🤖 Hermes 0.22",
            f"> Hermes {_LONG} not among the last 36 releases",
        ),
        (
            VersionSummary(_LONG, "0.21.5", "2026-09-10T00:00:00Z", _LATEST, 3, 36, _CHECKED),
            " → 0.21.5",
            f"> Hermes {_LONG} of Sep 10, latest 0.21.5 of Sep 24",
        ),
        (
            VersionSummary(_LONG, checked_at=_CHECKED, reason="HTTP 503"),
            " · no data",
            f"> Hermes version {_LONG}",
        ),
        (
            VersionSummary("0.21.11", "0.21.10", _LATEST, _LATEST, -1, 36, _CHECKED),
            "🤖 Hermes 0.21.11 · newer",
            "> Hermes 0.21.11 of Sep 24 is newer than the latest 0.21.10 of Sep 24",
        ),
    ],
    ids=["not-listed", "behind", "no-data", "newer-two-digit"],
)
def test_a_long_version_is_cut_to_the_phone_line_and_kept_whole_in_the_details(
    version: VersionSummary, keeps: str, detail: str
) -> None:
    main, lines = _version_render(version)
    line = main[-2]

    assert line.startswith("🤖 Hermes ")
    assert len(line) <= 32, line
    assert keeps in line
    assert any(entry.startswith(detail) for entry in lines), lines


# ---------------------------------------------------------------- plans, model limits, login (0.8.0)

_IN_2H = (NOW + timedelta(hours=2)).isoformat()
_IN_4H = (NOW + timedelta(hours=4)).isoformat()


def _limits_only(*quotas: QuotaMetric) -> str:
    snapshot = DashboardSnapshot(
        overall="normal", observed_at=NOW.isoformat(), capacity=CapacitySummary(quotas)
    )
    return render_dashboard(snapshot, now=NOW)


def _screen_lines(text: str, provider: str) -> list[str]:
    return [line for line in text.splitlines() if provider in line and not line.startswith(">")]


def _details_lines(text: str) -> list[str]:
    return [line[2:] for line in text.splitlines() if line.startswith("> ")]


def test_a_model_limit_gets_its_own_marked_line_without_the_shared_reset() -> None:
    """Decision of 29.09 (replaces decision 4 of the subscription plan): the account's windows on
    the first line, every model's limit on a line of its own after it; the time to the reset is
    left out when the account's window of the same length resets at the same moment. The mark
    from the provider's severity as well as from 90%."""
    text = _limits_only(
        QuotaMetric(
            "Claude",
            "official",
            windows=(
                QuotaWindow("session", 42.0, _IN_2H, severity="normal"),
                QuotaWindow("week", 86.0, _IN_4H, severity="warning"),
                QuotaWindow("week", 100.0, _IN_4H, scope="Fable", severity="critical"),
            ),
        )
    )

    assert _screen_lines(text, "Claude") == [
        "⚠️ Claude 42% (2h) · 86% (4h)",
        "⚠️ Claude Fable 100%",
    ]
    assert all(len(line) <= 32 for line in _screen_lines(text, "Claude"))


def test_a_model_limit_spent_less_than_the_account_keeps_its_line() -> None:
    text = _limits_only(
        QuotaMetric(
            "Claude",
            "official",
            windows=(
                QuotaWindow("session", 42.0, _IN_2H),
                QuotaWindow("week", 71.0, _IN_4H),
                QuotaWindow("week", 60.0, _IN_4H, scope="Fable", severity="normal"),
            ),
        )
    )

    assert _screen_lines(text, "Claude") == ["Claude 42% (2h) · 71% (4h)", "Claude Fable 60%"]
    assert not any("Fable" in line for line in _details_lines(text))


# Decision of 01.10 (0.9.2), Ilya: accuracy over width. A limits line is never shortened to
# keep to the phone line, a long one wraps; the coarse form of 0.9.1 is gone. The time to a reset
# is never earlier than the real one (the table above).
_IN_3H34M = (NOW + timedelta(hours=3, minutes=34)).isoformat()
_IN_3H4M = (NOW + timedelta(hours=3, minutes=4)).isoformat()
_IN_4D = (NOW + timedelta(days=4)).isoformat()


def test_a_limits_line_writes_its_times_exact_whatever_its_length() -> None:
    """The two lines of the screenshots of 01.10 keep their minutes, the first at 32 wraps on the
    narrow phone, and that is accepted; a line of two windows at 100% keeps its hours of the week.
    The mark comes from the provider's own ``warning`` on the week, not from 90%."""
    wrapped = _provider_line(
        "Claude",
        QuotaWindow("5h", 29.0, _IN_3H34M),
        QuotaWindow("7d", 87.0, _IN_4D, severity="warning"),
    )
    fitting = _provider_line(
        "Claude",
        QuotaWindow("5h", 37.0, _IN_3H4M),
        QuotaWindow("7d", 88.0, _IN_4D, severity="warning"),
    )
    longest = _provider_line(
        "Claude",
        QuotaWindow("5h", 100.0, _IN_3H34M),
        QuotaWindow("7d", 100.0, (NOW + timedelta(days=4, hours=2, minutes=37)).isoformat()),
    )
    unmarked = _provider_line(
        "Claude",
        QuotaWindow("week", 37.0, _IN_3H34M, scope="Opus"),
        QuotaWindow("week", 12.0, (NOW + timedelta(days=1, hours=5)).isoformat(), scope="Sonnet"),
    )

    assert wrapped == "⚠️ Claude 29% (3h34m) · 87% (4d)"
    assert fitting == "⚠️ Claude 37% (3h4m) · 88% (4d)"
    assert longest == "⚠️ Claude 100% (3h34m) · 100% (4d3h)"
    assert unmarked == "Claude Opus 37% (3h34m) · Sonnet 12% (1d5h)"


def test_a_reset_that_cannot_be_read_stays_a_question_mark_on_a_marked_line() -> None:
    line = _provider_line(
        "Claude", QuotaWindow("5h", 100.0, _IN_3H34M), QuotaWindow("7d", 100.0, "soon")
    )

    assert line == "⚠️ Claude 100% (3h34m) · 100% (?)"


def test_a_model_limit_with_its_own_reset_says_the_time() -> None:
    """Only the account's window of the same length hides the time: a model's week that resets
    with the session, not with the week, still says when."""
    text = _limits_only(
        QuotaMetric(
            "Claude",
            "official",
            windows=(
                QuotaWindow("session", 42.0, _IN_2H),
                QuotaWindow("week", 71.0, _IN_4H),
                QuotaWindow("week", 9.0, _IN_2H, scope="Fable"),
            ),
        )
    )

    assert _screen_lines(text, "Claude") == [
        "Claude 42% (2h) · 71% (4h)",
        "Claude Fable 9% (2h)",
    ]


def test_model_limits_alone_stay_on_the_provider_s_line() -> None:
    text = _limits_only(
        QuotaMetric(
            "Claude", "official", windows=(QuotaWindow("week", 30.0, _IN_4H, scope="Fable"),)
        )
    )

    assert _screen_lines(text, "Claude") == ["Claude Fable 30% (4h)"]


def test_an_expired_login_says_so_instead_of_no_data() -> None:
    text = _limits_only(
        QuotaMetric(
            "Claude",
            "expired",
            detail="login expired",
            plan="Max 5x",
            login_expires_at="2026-09-20T21:07:24Z",
        )
    )

    assert _screen_lines(text, "Claude") == ["Claude · login expired"]
    details = _details_lines(text)
    assert "Claude login ended Sep 20" in details
    assert not any(line.startswith("Claude:") for line in details), details


def test_plans_and_the_login_date_are_in_the_details_only_when_known() -> None:
    text = _limits_only(
        QuotaMetric(
            "Claude",
            "official",
            windows=(QuotaWindow("session", 12.0, _IN_2H),),
            plan="Max 5x",
            login_expires_at="2026-10-27T21:07:24Z",
        ),
        QuotaMetric("Codex", "unavailable", detail="HTTP 502", plan="Prolite"),
        QuotaMetric(
            "Grok", "official", windows=(QuotaWindow("7d", 31.0, _IN_4H),), plan="SuperGrok"
        ),
        QuotaMetric("Kimi", "official", windows=(QuotaWindow("5h", 8.0, _IN_2H),)),
    )

    details = _details_lines(text)
    assert "Plans: Claude Max 5x · Codex Prolite · Grok SuperGrok" in details
    assert "Claude login until Oct 27" in details
    assert not any("Kimi" in line and "Plan" in line for line in details)


# ----------------------------------------------------------------------------- Gemini refusals
#
# Google gives a free-tier key no quota number; the engine's log has the 429s (``gemini_log.py``).
# Decision of 29.09: a 429 is not a number and never colours the status by itself. While it is
# active the Gemini line says it instead of "no data"; the details always say the last one.
# Decision of 30.09 (0.8.2): in plain words, a refusal, the kind of limit and when it is back;
# neither the limit's number nor the retry is on the screen.

_GEMINI_REASON = "Google reports Gemini quota only with billing enabled"
_TTS = "gemini-2.5-flash-preview-tts"
_REFUSALS = "Gemini refusals seen by Hermes: "


def _gemini(refusal: Refusal | None) -> QuotaMetric:
    return QuotaMetric("Gemini", "unsupported", detail=_GEMINI_REASON, refusal=refusal)


def _ago(**delta: float) -> str:
    return (NOW - timedelta(**delta)).isoformat()


def _ahead(**delta: float) -> str:
    return (NOW + timedelta(**delta)).isoformat()


def test_a_gemini_log_without_a_429_says_so_in_the_details() -> None:
    text = _limits_only(_gemini(Refusal()))

    assert _screen_lines(text, "Gemini") == ["Gemini · no data"]
    details = _details_lines(text)
    assert f"{_REFUSALS}none" in details
    assert f"Gemini: {_GEMINI_REASON}" in details


def test_a_recent_429_marks_the_gemini_line_and_leaves_the_status_alone() -> None:
    refusal = Refusal(
        at=_ago(minutes=5), limit=10, retry_seconds=41.53, model=_TTS, active_until=_ahead(hours=1)
    )

    text = _limits_only(_gemini(refusal))

    assert text.splitlines()[0] == TOP_HEALTHY
    assert _screen_lines(text, "Gemini") == ["⚠️ Gemini hit limit 5 min ago"]
    details = _details_lines(text)
    assert f"{_REFUSALS}last Sep 25 07:16, per-minute limit, model {_TTS}" in details
    # Decision of 30.09: neither the limit's number nor the retry reaches the screen.
    assert not any("limit 10" in line or "retry" in line for line in text.splitlines())
    assert f"Gemini: {_GEMINI_REASON}" in details  # the reason for "no data" stays


def test_a_429_within_the_minute_is_just_now() -> None:
    refusal = Refusal(at=_ago(seconds=30), active_until=_ahead(minutes=59))

    assert _screen_lines(_limits_only(_gemini(refusal)), "Gemini") == [
        "⚠️ Gemini hit limit just now"
    ]


def test_after_its_window_the_429_is_only_in_the_details() -> None:
    refusal = Refusal(at=_ago(hours=2), retry_seconds=30.0, active_until=_ago(hours=1))

    text = _limits_only(_gemini(refusal))

    assert _screen_lines(text, "Gemini") == ["Gemini · no data"]
    assert f"{_REFUSALS}last Sep 25 05:21, per-minute limit" in _details_lines(text)


def test_a_daily_429_is_paused_till_the_reset() -> None:
    refusal = Refusal(
        at=_ago(hours=2, minutes=3),
        limit=15,
        retry_seconds=14580.0,
        model=_TTS,
        daily=True,
        active_until=_ahead(hours=2),
    )

    text = _limits_only(_gemini(refusal))

    assert _screen_lines(text, "Gemini") == ["⚠️ Gemini paused till 09:21"]
    assert (
        f"{_REFUSALS}last Sep 25 05:18, daily limit till Sep 25 09:21, model {_TTS}"
        in _details_lines(text)
    )


def test_the_429_is_dated_in_the_screen_s_zone() -> None:
    snapshot = DashboardSnapshot(
        overall="normal",
        observed_at=NOW.isoformat(),
        capacity=CapacitySummary((_gemini(Refusal(at=_ago(minutes=5), active_until=_ago())),)),
    )

    text = render_dashboard(snapshot, now=NOW, zone=timezone(timedelta(hours=5)))

    assert f"{_REFUSALS}last Sep 25 12:16" in _details_lines(text)


def test_a_line_whose_log_nobody_reads_has_no_refusal_words() -> None:
    text = _limits_only(QuotaMetric("Gemini", "unsupported", detail="limits disabled in config"))

    assert _screen_lines(text, "Gemini") == ["Gemini · no data"]
    assert not any("429" in line or "refusal" in line for line in text.splitlines())


def test_a_model_name_from_the_record_is_sanitized_like_every_other_word() -> None:
    refusal = Refusal(at=_ago(hours=2), model="m" * 60, active_until=_ago(hours=1))

    details = _details_lines(_limits_only(_gemini(refusal)))

    assert f"{_REFUSALS}last Sep 25 05:21, model {'m' * 39}…" in details


def test_a_line_with_numbers_keeps_them_and_the_429_stays_in_the_details() -> None:
    """An external source answering for Gemini replaces the line (decision 9); the refusal the
    log saw rides along, the numbers speak on the screen."""
    refusal = Refusal(at=_ago(minutes=5), active_until=_ahead(minutes=55))
    metric = QuotaMetric(
        "Gemini", "official", windows=(QuotaWindow("day", 40.0, _IN_4H),), refusal=refusal
    )

    text = _limits_only(metric)

    assert _screen_lines(text, "Gemini") == ["Gemini 40% (4h)"]
    assert f"{_REFUSALS}last Sep 25 07:16" in _details_lines(text)


def test_a_daily_429_is_paused_till_the_reset_in_the_screen_s_zone() -> None:
    refusal = Refusal(
        at=_ago(hours=2), retry_seconds=14580.0, daily=True, active_until=_ahead(hours=2)
    )
    snapshot = DashboardSnapshot(
        overall="warning",
        observed_at=NOW.isoformat(),
        capacity=CapacitySummary((_gemini(refusal),)),
    )

    text = render_dashboard(snapshot, now=NOW, zone=timezone(timedelta(hours=5)))

    assert _screen_lines(text, "Gemini") == ["⚠️ Gemini paused till 14:21"]
    assert f"{_REFUSALS}last Sep 25 10:21, daily limit till Sep 25 14:21" in _details_lines(text)


@pytest.mark.parametrize(
    ("refusal", "details"),
    [
        (
            Refusal(
                at=_ago(hours=5), retry_seconds=14580.0, daily=True, active_until=_ago(hours=1)
            ),
            "last Sep 25 02:21, daily limit till Sep 25 06:21",
        ),
        (
            Refusal(at=_ago(hours=2), retry_seconds=1e300, daily=True),
            "last Sep 25 05:21, daily limit",
        ),
    ],
    ids=["after-the-reset", "no-reset-computed"],
)
def test_a_daily_429_past_or_without_its_reset_is_only_in_the_details(
    refusal: Refusal, details: str
) -> None:
    text = _limits_only(_gemini(refusal))

    assert _screen_lines(text, "Gemini") == ["Gemini · no data"]
    assert f"{_REFUSALS}{details}" in _details_lines(text)
    assert incidents_for(refusal, NOW) == ()


def test_a_reset_the_screen_cannot_show_falls_back_to_the_age() -> None:
    """A reset that cannot be represented in the screen's zone: the line says when the limit was
    hit instead, and the details name the kind without the time."""
    refusal = Refusal(at=_ago(hours=2), daily=True, active_until="9999-12-31T23:00:00+00:00")
    snapshot = DashboardSnapshot(
        overall="warning",
        observed_at=NOW.isoformat(),
        capacity=CapacitySummary((_gemini(refusal),)),
    )

    text = render_dashboard(snapshot, now=NOW, zone=timezone(timedelta(hours=5)))

    assert _screen_lines(text, "Gemini") == ["⚠️ Gemini hit limit 2 h ago"]
    assert f"{_REFUSALS}last Sep 25 10:21, daily limit" in _details_lines(text)


@pytest.mark.parametrize(
    "refusal",
    [
        Refusal(at=_ago(minutes=59, seconds=59), active_until=_ahead(seconds=1)),
        Refusal(at=_ago(hours=2), daily=True, active_until=_ahead(hours=1)),
    ],
    ids=["hit-59-min", "paused"],
)
def test_the_marked_gemini_line_fits_a_phone_line(refusal: Refusal) -> None:
    (line,) = _screen_lines(_limits_only(_gemini(refusal)), "Gemini")

    assert line.startswith("⚠️ Gemini ")
    assert len(line) <= 32, line


def test_a_gemini_log_that_is_not_there_is_named_as_such() -> None:
    snapshot = DashboardSnapshot(
        overall="unknown",
        observed_at=NOW.isoformat(),
        sources=(
            SourceObservation("gemini_log", "local", "unsupported", detail="no logs directory"),
        ),
    )

    main = _main_part(render_dashboard(snapshot, now=NOW))

    assert "❔ Not checked: Gemini log (not on this installation)" in main


# ----------------------------------------------------------------------------- private ids
#
# Decision of 01.10 (0.9.0): a reason a provider or a local source answers with may quote a
# delivery target or a chat id; it reaches the details masked, like a secret or a path.


def test_a_provider_reason_with_a_chat_target_is_masked_in_the_details() -> None:
    snapshot = DashboardSnapshot(
        overall="normal",
        observed_at=NOW.isoformat(),
        capacity=CapacitySummary(
            (
                QuotaMetric(
                    "Grok",
                    "unavailable",
                    detail="HTTP 400 for telegram:-1001234567890:17 user 987654321",
                ),
            )
        ),
        incidents=(Incident("grok:error", "warning", "Grok refused chat -1001234567890"),),
    )

    text = render_dashboard(snapshot, now=NOW)

    assert "1001234567890" not in text and "987654321" not in text
    assert "Grok: HTTP 400 for [target] user [id]" in _details_lines(text)
    assert "- Grok refused chat [id]" in _main_part(text)


# ----------------------------------------------------------------------------- cron and traffic
#
# 0.9.0: the Cron line in the top block from the engine's own records (``cron_jobs.py``,
# ``cron_runs.py``) and the word on the Telegram line when the adapter is connected but the
# channel is deaf (``telegram_traffic.py``). The times are always in the details.

CRON_OK = CronSummary(
    "ok",
    active=27,
    paused=3,
    ticker_at="2026-09-25T07:19:00Z",
    ticker_ok_at="2026-09-25T07:19:00Z",
)


def _cron_snapshot(
    cron: CronSummary | None,
    traffic: TrafficSummary | None = None,
    incidents: tuple[Incident, ...] = (),
) -> DashboardSnapshot:
    return DashboardSnapshot(
        overall="normal",
        observed_at="2026-09-25T07:21:00Z",
        gateway=GatewaySummary("running", "connected", "2026-09-25T07:19:00Z"),
        cron=cron,
        traffic=traffic,
        incidents=tuple(incidents),
    )


def test_the_cron_line_counts_active_jobs_and_names_paused_in_the_details() -> None:
    text = render_dashboard(_cron_snapshot(CRON_OK), now=NOW)
    lines = text.splitlines()

    # 1.0.0: cron in the ✓ row, its count in the details.
    assert lines[2] == _row("Gateway", "Telegram", "Cron")
    assert "> Cron 27 active · 3 paused · ticker 07:19" in lines


def test_a_failing_job_is_on_the_line_in_the_events_and_in_the_details() -> None:
    failure = CronFailure(
        "b1", "daily-digest", "run", at="2026-09-25T07:05:00Z", streak=3, reason="rate limit"
    )
    cron = replace(CRON_OK, state="failing", failing=(failure,))

    text = render_dashboard(_cron_snapshot(cron), now=NOW)

    assert "Cron ⚠️ 1 of 27 failing" in _main_part(text)
    assert (
        "> Cron daily-digest: run failed Sep 25 07:05, 3 in a row (rate limit)" in text.splitlines()
    )


@pytest.mark.parametrize(
    "kind, words",
    [
        ("delivery", "not delivered Sep 25 07:05, streak unknown (not connected)"),
        ("blocked", "blocked Sep 25 07:05 (not connected)"),
    ],
)
def test_delivery_and_blocked_failures_read_as_such(kind: str, words: str) -> None:
    failure = CronFailure(
        "b1", "daily-digest", kind, at="2026-09-25T07:05:00Z", reason="not connected"
    )
    cron = replace(CRON_OK, state="failing", failing=(failure,))

    text = render_dashboard(_cron_snapshot(cron), now=NOW)

    assert f"> Cron daily-digest: {words}" in text.splitlines()


def test_an_overdue_job_names_the_missed_moment() -> None:
    failure = CronFailure("b1", "daily-digest", "overdue", at="2026-09-25T06:50:00Z")
    cron = replace(CRON_OK, state="failing", failing=(failure,))

    text = render_dashboard(_cron_snapshot(cron), now=NOW)

    assert "> Cron daily-digest: overdue since Sep 25 06:50" in text.splitlines()


def test_the_failure_line_in_the_details_names_the_job_in_full() -> None:
    """1.0.0: the failure carries the owner's label, which "Needs attention" and the cron table
    show whole up to ``cron_jobs.NAME_LIMIT``; the details line takes the same limit, not 24.
    The reason keeps its own cut."""
    label = "Sends Threads topics to SMM every morning"
    longer = "x" * (CRON_NAME_LIMIT + 5)
    failing = (
        CronFailure("b1", label, "run", at="2026-09-25T07:05:00Z", streak=1, reason="r" * 40),
        CronFailure("b2", longer, "overdue", at="2026-09-25T06:50:00Z"),
    )
    cron = replace(CRON_OK, state="failing", failing=failing)

    lines = render_dashboard(_cron_snapshot(cron), now=NOW).splitlines()

    assert f"> Cron {label}: run failed Sep 25 07:05, 1 run ({'r' * 23}…)" in lines
    assert f"> Cron {'x' * (CRON_NAME_LIMIT - 1)}…: overdue since Sep 25 06:50" in lines


def test_a_held_failure_is_in_the_details_only_with_the_run_that_replaced_it() -> None:
    held = CronFailure(
        "b1",
        "daily-digest",
        "run",
        at="2026-09-25T06:35:00Z",
        streak=1,
        reason="x",
        recovered_at="2026-09-25T06:40:00Z",
    )
    cron = replace(CRON_OK, held=(held,))

    text = render_dashboard(_cron_snapshot(cron), now=NOW)

    assert _row("Gateway", "Telegram", "Cron") in _main_part(text)
    assert "> Cron daily-digest: run failed Sep 25 06:35, ok since 06:40" in text.splitlines()


@pytest.mark.parametrize(
    "state, line, detail",
    [
        ("stalled", "Cron ⚠️ ticker silent 12 min", "> Cron ticker error: permission denied"),
        ("ticks_failing", "Cron ⚠️ ticks failing 12 min", "> Cron ticker error: permission denied"),
    ],
)
def test_a_dead_or_failing_ticker_is_the_line_and_the_reason_is_in_the_details(
    state: str, line: str, detail: str
) -> None:
    cron = replace(
        CRON_OK,
        state=state,
        ticker_at="2026-09-25T07:09:00Z",
        ticker_ok_at="2026-09-25T07:09:00Z",
        ticker_error="permission denied",
    )

    text = render_dashboard(_cron_snapshot(cron), now=NOW)

    assert line in _main_part(text)
    assert detail in text.splitlines()


@pytest.mark.parametrize(
    "state, source, line, reason",
    [
        (
            "unsupported",
            "unsupported",
            "❔ Not checked: cron (not on this installation)",
            "> Cron: no cron directory",
        ),
        (
            "unknown",
            "unavailable",
            "❔ Not checked: cron (unavailable)",
            "> Cron: jobs.json unreadable: ValueError",
        ),
    ],
)
def test_no_cron_data_is_never_a_zero(state: str, source: str, line: str, reason: str) -> None:
    """1.0.0, fix round 1: with its source as the tick records it, the "❔ Not checked" line says
    it once; never a ✓, never under "Needs attention"; the reason in the details."""
    cron = CronSummary(state, detail=reason[len("> Cron: ") :])
    snapshot = replace(
        _cron_snapshot(cron), sources=(SourceObservation("cron", "official", source),)
    )

    text = render_dashboard(snapshot, now=NOW)
    main = _main_part(text)

    assert line in main
    assert _row("Gateway", "Telegram") in main
    assert not any(item.startswith("Cron") for item in main) and "Needs attention" not in text
    assert reason in text.splitlines()
    assert "0 jobs" not in text


@pytest.mark.parametrize(
    "state, word",
    [("no_sends", "⚠️ no sends"), ("stalled", "⚠️ stalled"), ("quiet", "⚠️ quiet")],
)
def test_a_deaf_channel_is_a_word_on_the_telegram_line(state: str, word: str) -> None:
    traffic = TrafficSummary(
        state,
        last_update_seen_at="2026-09-25T03:20:00Z",
        polling_at="2026-09-25T07:20:00Z",
        sends_blocked_since="2026-09-25T07:10:00Z" if state == "no_sends" else None,
        quiet_seconds=4 * 3600 if state == "quiet" else None,
        threshold_seconds=3 * 3600 if state == "quiet" else None,
    )

    text = render_dashboard(_cron_snapshot(CRON_OK, traffic), now=NOW)
    main = _main_part(text)

    # 1.0.0: Telegram leaves the ✓ row and stands under "Needs attention" with the word.
    line = next(item for item in main if item.startswith("Telegram"))
    assert line == f"Telegram {word}"
    assert len(line) <= 32
    assert _row("Gateway", "Cron") in main


def test_traffic_times_are_always_in_the_details() -> None:
    traffic = TrafficSummary(
        "ok",
        last_update_seen_at="2026-09-25T07:00:00Z",
        polling_at="2026-09-25T07:20:00Z",
        last_send_error_at="2026-09-25T06:58:00Z",
    )

    text = render_dashboard(_cron_snapshot(CRON_OK, traffic), now=NOW)

    assert _row("Gateway", "Telegram", "Cron") in _main_part(text)
    assert "> ✓ Telegram last update seen Sep 25 07:00 · polling ok 07:20" in text.splitlines()
    assert "> Telegram send errors: last Sep 25 06:58" in text.splitlines()


@pytest.mark.parametrize(
    "telegram, traffic, sources, attention, mark",
    [
        ("degraded", "ok", (), "Telegram error", "⚠️ "),
        ("disconnected", "reconnecting", (), "Telegram disconnected", "⚠️ "),
        (
            "unknown",
            "ok",
            (SourceObservation("gateway_state", "official", "unavailable"),),
            None,
            "",
        ),
    ],
    ids=["degraded", "disconnected", "not checked"],
)
def test_the_telegram_line_in_the_details_takes_the_item_s_own_verdict(
    telegram: str, traffic: str, sources: tuple, attention: str | None, mark: str
) -> None:
    """Fix round 1: counters that say ok next to an adapter that is not connected are no ✓ in
    the details while "Needs attention" says Telegram is broken; one not checked has no mark."""
    snapshot = replace(
        _cron_snapshot(CRON_OK, TrafficSummary(traffic, polling_at="2026-09-25T07:20:00Z")),
        gateway=GatewaySummary("running", telegram, None),
        sources=sources,
    )

    text = render_dashboard(snapshot, now=NOW)
    main = _main_part(text)

    assert f"> {mark}Telegram last update seen never · polling ok 07:20" in text.splitlines()
    assert (attention in main) if attention else ("Needs attention" not in text)
    assert not any("✓\xa0Telegram" in line for line in main)


def test_the_deaf_word_never_hides_a_disconnected_adapter() -> None:
    snapshot = replace(
        _cron_snapshot(CRON_OK, TrafficSummary("no_sends")),
        gateway=GatewaySummary("running", "disconnected", None),
    )

    main = _main_part(render_dashboard(snapshot, now=NOW))

    assert "Telegram disconnected" in main
    assert _row("Gateway", "Cron") in main


# ----------------------------------------------------------------------------- the dashboard's version


def test_the_details_name_the_dashboard_s_own_version() -> None:
    """Decision of 01.10: the installed copy can be told from the pinned message. The line is
    the package's own constant, in the details only."""
    from telegram_dashboard import __version__

    text = render_dashboard(
        DashboardSnapshot(overall="normal", observed_at=NOW.isoformat()), now=NOW
    )

    assert f"Dashboard {__version__}" in _details_lines(text)
    assert not any("Dashboard" in line for line in _main_part(text))


def test_the_dashboard_s_version_closes_the_details_after_a_blank_line() -> None:
    """Ilya, 01.10 (0.9.2): the version is the last line of the collapsed block, a blank line
    before it; 1.0.0: after the screen's own stamps, which come after the reasons of "No data"
    and are there even when nothing else is."""
    from telegram_dashboard import __version__
    from telegram_dashboard.states import PERIOD_SECONDS, all_states

    state = all_states()[0]  # the normal state, with "No data" for Gemini and Grok
    full = render_dashboard(
        state.snapshot, now=state.now, delivery=state.delivery, period_seconds=PERIOD_SECONDS
    ).splitlines()
    bare = render_dashboard(
        DashboardSnapshot(overall="normal", observed_at=NOW.isoformat()), now=NOW
    ).splitlines()

    assert full[-8:] == [
        "> Grok: source not confirmed",
        ">",
        "> Screen Sep 9 21:00 UTC",
        "> Confirmed 20:58",
        "> Period 5 min",
        "> Profiles 1/1 · sources 3/3",
        ">",
        f"> Dashboard {__version__}",
    ]
    assert sum(line.startswith("> Dashboard") for line in full) == 1
    assert bare[-4:] == [
        "> ## Details",
        "> Screen Sep 25 07:21 UTC",
        ">",
        f"> Dashboard {__version__}",
    ]


def test_a_message_cut_at_the_limit_keeps_the_dashboard_s_version_last() -> None:
    """Review of 0.9.2: an over-long message is cut from its end, and the version now closes the
    details; the cut takes the lines before it, the version stays whole after a blank line.
    1.0.0: the screen's own stamps close the block before the version, and stay as well."""
    from telegram_dashboard import __version__
    from telegram_dashboard.render import TELEGRAM_TEXT_LIMIT

    failing = tuple(
        CronFailure(
            f"j{i}", f"nightly-report-{i:03d}", "run", at=NOW.isoformat(), streak=1, reason="x"
        )
        for i in range(80)
    )
    snapshot = DashboardSnapshot(
        overall="warning",
        observed_at=NOW.isoformat(),
        cron=CronSummary("failing", active=80, failing=failing),
    )

    text = render_dashboard(snapshot, now=NOW)
    lines = text.splitlines()

    assert len(text) <= TELEGRAM_TEXT_LIMIT
    assert lines[-5].endswith("…")  # the cut, before the stamps and the version
    assert lines[-4:] == [">", "> Screen Sep 25 07:21 UTC", ">", f"> Dashboard {__version__}"]


# ----------------------------------------------------------------------------- review of 01.10


def test_a_ticker_verdict_without_a_readable_stamp_fits_the_line() -> None:
    """Review, minor 4: no ``age unknown`` on the line (33 columns); the words are dropped."""
    cron = replace(CRON_OK, state="stalled", ticker_at=None, ticker_ok_at=None)

    text = render_dashboard(_cron_snapshot(cron), now=NOW)
    main = _main_part(text)

    assert "Cron ⚠️ ticker silent" in main
    # The top line and the ✓ row are not held to the phone line (1.0.0, see test_states).
    assert all(len(line) <= 32 for line in main if not line.startswith(("#@", "✓")))


def test_a_quiet_channel_without_a_gap_history_names_no_usual_gap() -> None:
    """Review, minor 6: ``usual gap up to 3 h`` was the floor halved, not a gap ever seen."""
    traffic = TrafficSummary(
        "quiet",
        last_update_seen_at="2026-09-25T00:20:00Z",
        polling_at="2026-09-25T07:20:00Z",
        quiet_seconds=7 * 3600,
        threshold_seconds=None,
    )

    text = render_dashboard(_cron_snapshot(CRON_OK, traffic), now=NOW)

    assert "> ⚠️ Telegram last update seen Sep 25 00:20 · polling ok 07:20 · quiet 7 h" in (
        text.splitlines()
    )
    assert "usual gap" not in text


def test_a_ticker_stamp_that_could_not_be_read_names_its_reason_in_the_details() -> None:
    """Review, minor 9: a stamp that is not a number was ``ticker no heartbeat`` without the
    reason the scan recorded."""
    cron = replace(
        CRON_OK, ticker_at=None, ticker_ok_at=None, detail="ticker_heartbeat is not a stamp"
    )

    text = render_dashboard(_cron_snapshot(cron), now=NOW)

    assert (
        "> Cron 27 active · 3 paused · ticker no heartbeat (ticker_heartbeat is not a stamp)"
        in (text.splitlines())
    )


def test_the_usual_gap_in_the_details_is_the_longest_gap_seen() -> None:
    """Round 2, minor 4: the clause names the longest gap this installation has seen."""
    traffic = TrafficSummary(
        "quiet",
        last_update_seen_at="2026-09-24T22:20:00Z",
        polling_at="2026-09-25T07:20:00Z",
        quiet_seconds=9 * 3600,
        threshold_seconds=8 * 3600,
        usual_gap_seconds=4 * 3600,
    )

    text = render_dashboard(_cron_snapshot(CRON_OK, traffic), now=NOW)

    assert (
        "> ⚠️ Telegram last update seen Sep 24 22:20 · polling ok 07:20 · quiet 9 h, usual gap up to 4 h"
        in text.splitlines()
    )


def test_a_success_stamp_that_could_not_be_read_names_its_reason_beside_a_live_heartbeat() -> None:
    """Round 2, minor 5: the reason is shown whenever the scan recorded one, not only without
    a heartbeat."""
    cron = replace(CRON_OK, ticker_ok_at=None, detail="ticker_last_success is not a stamp")

    text = render_dashboard(_cron_snapshot(cron), now=NOW)

    assert "> Cron 27 active · 3 paused · ticker 07:19 (ticker_last_success is not a stamp)" in (
        text.splitlines()
    )


# ----------------------------------------------------------------------------- the main screen, 1.0.0
#
# The look Ilya accepted in the live probe: the top line bold with the time of the data, what
# needs attention, one row of what works, one line of what was not checked, the limits, the
# Hermes version; the ages and counts in the details.

_FIVE_HOURS = "2026-09-25T02:21:00Z"


def test_the_top_line_is_bold_with_its_time_a_tag_in_html_and_its_words_in_plain() -> None:
    """R1: ``<tg-time format="r">`` reads "18 minutes ago" on Telegram Desktop; Android shows
    the words inside it, so they stay the full date with the zone. The plain form has no tag and
    no upper case."""
    from zoneinfo import ZoneInfo

    snapshot = DashboardSnapshot(overall="warning", observed_at=NOW.isoformat())

    text = render_dashboard(snapshot, now=NOW, zone=ZoneInfo("Asia/Yekaterinburg"))

    assert text.splitlines()[0] == "#@1790320860 🟡 Warning · updated Sep 25 12:21 +05"
    assert int(NOW.timestamp()) == 1790320860
    assert to_telegram_html(text).splitlines()[0] == (
        '<b>🟡 Warning · updated <tg-time unix="1790320860" format="r">Sep 25 12:21 +05'
        "</tg-time></b>"
    )
    assert to_telegram_plain(text).splitlines()[0] == "🟡 Warning · updated Sep 25 12:21 +05"
    assert "> Screen Sep 25 12:21 +05" in text.splitlines()  # the top line's time, in the details


def test_a_time_before_1970_is_time_unknown_with_no_raw_marker_in_any_form() -> None:
    snapshot = DashboardSnapshot(overall="warning", observed_at="1960-01-01T00:00:00+00:00")

    text = render_dashboard(snapshot)
    html = to_telegram_html(text)

    assert text.splitlines()[0] == "#@ 🟡 Warning · updated time unknown"
    assert html.splitlines()[0] == "<b>🟡 Warning · updated time unknown</b>"
    assert "@-" not in html and "tg-time" not in html
    assert to_telegram_plain(text).splitlines()[0] == "🟡 Warning · updated time unknown"


def test_a_time_that_cannot_be_read_is_time_unknown_with_no_tag_in_any_form() -> None:
    snapshot = DashboardSnapshot(overall="unknown", observed_at="not a time")

    text = render_dashboard(snapshot)
    html = to_telegram_html(text)

    assert text.splitlines()[0] == "#@ ⚪ Unknown · updated time unknown"
    assert html.splitlines()[0] == "<b>⚪ Unknown · updated time unknown</b>"
    assert "tg-time" not in html
    assert to_telegram_plain(text).splitlines()[0] == "⚪ Unknown · updated time unknown"
    assert "> Screen time unknown" in text.splitlines()


def test_only_a_status_label_of_ours_makes_a_top_line_and_its_words_stay_escaped() -> None:
    """R1, no free-form HTML from the data: the converters take the top line's kind only with one
    of the status labels and a number of digits alone; anything else is an ordinary heading,
    escaped and coded."""
    forged = to_telegram_html("#@1 <i>x</i> · updated now\n#@x1 🟢 Healthy · updated now")
    stamp = to_telegram_html("#@1 🟢 Healthy · updated <i>Sep AGENTS.md")

    assert "tg-time" not in forged and "<i>" not in forged
    assert stamp == (
        '<b>🟢 Healthy · updated <tg-time unix="1" format="r">&lt;i&gt;Sep '
        "<code>AGENTS.md</code></tg-time></b>"
    )


@pytest.mark.parametrize(
    ("confirmed", "banner"),
    [
        ("2026-09-25T07:12:00Z", "Update lagging: last confirmation 9 min ago"),
        ("2026-09-25T06:51:00Z", "DASHBOARD STALE: last confirmation 30 min ago, threshold 10 min"),
    ],
    ids=["lagging", "stale"],
)
def test_the_banner_s_circle_is_the_warning_mark_and_its_words_stay(
    confirmed: str, banner: str
) -> None:
    """R2: one coloured circle per screen, the top line's. ``message_banner`` keeps its red or
    yellow: its words are the watchdog's alert as well."""
    from telegram_dashboard.freshness import classify_message_freshness, message_banner

    delivery = DeliveryRecord(message_id=1, last_confirmed_at=confirmed)
    snapshot = DashboardSnapshot(overall="normal", observed_at=NOW.isoformat())

    lines = render_dashboard(snapshot, now=NOW, delivery=delivery, period_seconds=300).splitlines()
    freshness = classify_message_freshness(delivery, now=NOW, period_seconds=300)
    words = message_banner(freshness, delivery, now=NOW, period_seconds=300)

    assert lines[:2] == [f"⚠️ {banner}", TOP_HEALTHY]
    assert words is not None and words.startswith(("🟡 ", "🔴 ")) and words.endswith(banner)


def test_a_broken_item_leaves_the_row_and_stands_after_the_events() -> None:
    """R4, R5: the first five events in their order, then the line of every item the row leaves
    out, in its 0.12.0 words, even one an event already names; the row keeps the rest, a
    no-break space between the mark and the name, a space between two items."""
    events = tuple(Incident(f"e:{i}", "warning", f"event {i}") for i in range(5))
    snapshot = DashboardSnapshot(
        overall="warning",
        observed_at=NOW.isoformat(),
        gateway=GatewaySummary("running", "connected"),
        backup=BackupSummary("failed", finished_at=_FIVE_HOURS, detail="disk full"),
        drift=DriftSummary("clean", 0, 481, NOW.isoformat()),
        cron=CRON_OK,
        incidents=(*events[:2], Incident("backup:failed", "warning", "Backup failed"), *events[2:]),
    )

    text = render_dashboard(snapshot, now=NOW)

    assert _main_part(text) == [
        "#@1790320860 🟡 Warning · updated Sep 25 07:21 UTC",
        "",
        "## 🛠 Needs attention",
        "- event 0",
        "- event 1",
        "- Backup failed",
        "- event 2",
        "- event 3",
        "Backup ⚠️ failed 5 h ago",
        "",
        "✓\xa0Gateway ✓\xa0Telegram ✓\xa0Drift ✓\xa0Cron",
        "",
    ]
    assert "> ⚠️ Backup Sep 25 02:21 · 5 h ago · disk full" in text.splitlines()


def test_an_old_backup_is_no_check_mark_in_the_row() -> None:
    """R5: well only where the 0.12.0 line says so with nothing loud beside it; a backup older
    than 26 hours keeps its whole line under "Needs attention"."""
    snapshot = DashboardSnapshot(
        overall="warning",
        observed_at=NOW.isoformat(),
        backup=BackupSummary("ok", finished_at=_ago(hours=30), integrity="ok"),
        drift=DriftSummary("clean", 0, 481, NOW.isoformat()),
    )

    text = render_dashboard(snapshot, now=NOW)

    assert _main_part(text)[2:6] == [
        "## 🛠 Needs attention",
        "Backup ✓ 30 h ago ⚠️ older than 26 h",
        "",
        _row("Drift"),
    ]
    assert "> ⚠️ Backup Sep 24 01:21 · 30 h ago · integrity ok" in text.splitlines()


def test_an_absent_summary_adds_nothing_anywhere() -> None:
    """R5: ``None`` is a source not on this installation; no row, no line, no event of ours."""
    text = render_dashboard(DashboardSnapshot(overall="normal", observed_at=NOW.isoformat()))

    assert _main_part(text) == [TOP_HEALTHY, ""]
    for word in ("Gateway", "Telegram", "Backup", "Drift", "Cron", "Needs attention"):
        assert word not in text


def test_what_was_not_checked_is_one_line_and_nothing_vanishes() -> None:
    """R6: the sources not observed with their reasons, the stale ones, the profiles not seen
    and the failed sources the list does not already name, in one line; the full count stays
    in the details."""
    snapshot = DashboardSnapshot(
        overall="unknown",
        observed_at=NOW.isoformat(),
        coverage=Coverage(2, 1, failed_sources=("backup", "profile:worker/cron", "profile:x")),
        sources=(
            SourceObservation("gemini_log", "local", "unsupported"),
            SourceObservation("backup", "derived", "unavailable"),
            SourceObservation("limits", "official", "stale"),
            SourceObservation("drift", "derived", "fresh"),
        ),
    )

    text = render_dashboard(snapshot, now=NOW)

    assert _main_part(text)[2] == (
        "❔ Not checked: Gemini log (not on this installation), backup (unavailable), "
        "limits (stale), profiles 1/2, 2 sources unavailable"
    )
    assert "> Profiles 1/2 · sources 2/4" in text.splitlines()
    one = replace(snapshot, coverage=Coverage(1, 1, failed_sources=("profile:x",)), sources=())
    assert "❔ Not checked: 1 source unavailable" in render_dashboard(one).splitlines()


def test_a_source_not_on_this_installation_is_said_once_by_the_not_checked_line() -> None:
    """Fix round 1: an installation without a backup status and a drift source. Backup and drift
    are neither in the ✓ row nor under "Needs attention" (no block at all); the ❔ line names
    each once, the details keep the reasons, in the text and the rich form alike."""
    from telegram_dashboard.rich import to_telegram_rich

    snapshot = DashboardSnapshot(
        overall="unknown",  # what the collectors derive from two sources not observed
        observed_at=NOW.isoformat(),
        gateway=GatewaySummary("running", "connected"),
        backup=BackupSummary("unsupported", detail="backup source not configured"),
        drift=DriftSummary("unsupported", detail="drift source not configured"),
        cron=CRON_OK,
        sources=(
            SourceObservation("gateway_state", "official", "fresh"),
            SourceObservation("drift", "derived", "unsupported"),
            SourceObservation("backup", "derived", "unsupported"),
            SourceObservation("cron", "official", "fresh"),
        ),
    )

    text = render_dashboard(snapshot, now=NOW)
    markup = to_telegram_rich(snapshot, now=NOW)

    assert _main_part(text) == [
        "#@1790320860 ⚪ Unknown · updated Sep 25 07:21 UTC",
        "",
        _row("Gateway", "Telegram", "Cron"),
        "",
        "❔ Not checked: drift (not on this installation), backup (not on this installation)",
        "",
    ]
    assert "> Backup: backup source not configured" in text.splitlines()
    assert "> Drift: drift source not configured" in text.splitlines()
    assert markup is not None and "Needs attention" not in markup
    assert markup.count("not on this installation") == 2
    assert "not observed" not in text and "not observed" not in markup


def test_no_data_from_a_source_the_line_does_not_name_stays_under_needs_attention() -> None:
    """R6, nothing vanishes: no data is left to the ❔ line only when it names the source."""
    snapshot = DashboardSnapshot(
        overall="warning",
        observed_at=NOW.isoformat(),
        backup=BackupSummary("unknown", detail="status file missing"),
    )
    named = replace(snapshot, sources=(SourceObservation("backup", "derived", "unavailable"),))

    alone = _main_part(render_dashboard(snapshot, now=NOW))
    said = _main_part(render_dashboard(named, now=NOW))

    assert alone[2:4] == ["## 🛠 Needs attention", "Backup: no data"]
    assert said[2:] == ["❔ Not checked: backup (unavailable)", ""]
