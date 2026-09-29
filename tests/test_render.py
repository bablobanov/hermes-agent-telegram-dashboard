from datetime import UTC, datetime, timedelta, timezone

import pytest

from telegram_dashboard.freshness import DeliveryRecord
from telegram_dashboard.render import (
    render_dashboard,
    to_telegram_html,
    to_telegram_plain,
)
from telegram_dashboard.schema import (
    AutomationSummary,
    BackupSummary,
    CapacitySummary,
    Coverage,
    DashboardSnapshot,
    DriftSummary,
    GatewaySummary,
    Incident,
    QuotaMetric,
    QuotaWindow,
    Refusal,
    SourceObservation,
    VersionSummary,
    WorkSummary,
)

NOW = datetime(2026, 9, 25, 7, 21, tzinfo=UTC)


def _main_part(text: str) -> list[str]:
    """The lines a phone shows before the collapsed details."""
    lines = text.splitlines()
    return lines[: next((i for i, line in enumerate(lines) if line.startswith(">")), len(lines))]


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
    assert "Profile coverage 2/3" in _main_part(rendered)
    assert "Sources unavailable: 1" in _main_part(rendered)
    assert "🟢 Healthy" not in rendered


def test_complete_coverage_leaves_the_screen_and_stays_in_the_details() -> None:
    snapshot = DashboardSnapshot(
        overall="normal",
        observed_at="2026-09-09T00:00:00Z",
        coverage=Coverage(expected_profiles=1, observed_profiles=1),
    )

    rendered = render_dashboard(snapshot)

    assert not any("Profile coverage" in line for line in _main_part(rendered))
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


def test_scheduler_runs_and_delivery_are_rendered_separately() -> None:
    snapshot = DashboardSnapshot(
        overall="warning",
        observed_at="2026-09-09T00:00:00Z",
        automation=AutomationSummary(
            scheduler="degraded",
            failed_runs=2,
            missed_runs=1,
            delivery_failed=3,
        ),
    )

    rendered = render_dashboard(snapshot)

    assert "## Automation" in rendered
    assert "Scheduler: degraded" in rendered
    assert "Failed runs: 2" in rendered
    assert "Missed: 1" in rendered
    assert "Delivery failed: 3" in rendered


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
    assert "OpenAI 75% (24h)" in rendered.splitlines()


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
    assert "Kimi 0% (4h13m) · 3% (29d)" in lines  # the provider's order
    assert "Claude · no data" in lines and "Gemini · no data" in lines
    assert "🟡" not in rendered and lines[0] == "🟢 Healthy · Sep 25 07:21 UTC"
    # Reasons and the odd minute live in the details, grouped; the resets are on the lines.
    assert "> ## Resets" not in lines
    assert "> Data 07:21 · Kimi 07:16" in lines
    assert "> ## No data" in lines
    assert "> Claude: no account token" in lines
    assert "> Gemini: source not confirmed" in lines
    assert "> Period 5 min" in lines


def _codex_line(*windows: QuotaWindow) -> str:
    snapshot = DashboardSnapshot(
        overall="normal",
        observed_at=NOW.isoformat(),
        capacity=CapacitySummary((QuotaMetric("Codex", "official", windows=windows),)),
    )
    lines = render_dashboard(snapshot, now=NOW).splitlines()
    return next(line for line in lines if "Codex" in line and not line.startswith(">"))


@pytest.mark.parametrize(
    ("ahead", "words"),
    [
        (timedelta(minutes=-5), "0m"),  # a reset behind the data time: due now
        (timedelta(seconds=30), "1m"),  # whole minutes, rounded up
        (timedelta(minutes=45), "45m"),
        (timedelta(hours=1), "1h"),
        (timedelta(hours=1, minutes=21), "1h21m"),
        (timedelta(days=1), "24h"),
        (timedelta(days=1, hours=5, minutes=40), "1d5h"),
        (timedelta(days=2, hours=23, minutes=59), "2d"),  # from two days on, whole days
    ],
)
def test_the_time_to_a_reset_is_written_in_brackets_after_the_share(
    ahead: timedelta, words: str
) -> None:
    reset = (NOW + ahead).isoformat()

    assert _codex_line(QuotaWindow("Session", 14.0, reset)) == f"Codex 14% ({words})"


def test_a_reset_date_that_cannot_be_read_is_a_question_mark_not_a_silence() -> None:
    assert _codex_line(QuotaWindow("Session", 14.0, "soon")) == "Codex 14% (?)"
    assert _codex_line(QuotaWindow("Session", 14.0, None)) == "Codex 14%"


def test_windows_carry_no_length_label_only_a_model_scope_in_the_provider_s_order() -> None:
    """The engine's words (``agent/account_usage.py``: Codex ``Session``/``Weekly`` by position,
    whatever the window's length; Claude ``Current session``/``Current week``) never reach the
    screen; a limit for one model keeps its scope."""
    line = _codex_line(
        QuotaWindow("Session", 37.0, (NOW + timedelta(hours=3)).isoformat()),
        QuotaWindow("Weekly", 95.0, (NOW + timedelta(days=4, hours=2)).isoformat()),
        QuotaWindow("Opus week", 5.0, (NOW + timedelta(days=4, hours=2)).isoformat()),
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

    assert _main_part(rendered) == [
        "🟢 Healthy · Sep 25 07:21 UTC",
        "Gateway ✓ · Telegram ✓",
        "Backup ✓ 6 h ago",
        "Drift ✓ 0 of 481",
        "",
    ]
    assert rendered.splitlines()[5:] == [
        "> ## Details",
        "> Confirmed 07:16",
        "> Period 5 min",
        "> Profiles 1/1",
        ">",
        "> Backup Sep 25 00:31 · integrity ok",
        "> Drift checked 07:21",
    ]


def test_abnormal_gateway_and_drift_are_words_on_the_screen() -> None:
    snapshot = DashboardSnapshot(
        overall="critical",
        observed_at="2026-09-25T07:21:00Z",
        gateway=GatewaySummary("stopped", "disconnected"),
        drift=DriftSummary("drift", 3, 481, "2026-09-25T07:21:00Z"),
    )

    lines = render_dashboard(snapshot).splitlines()

    assert "Gateway stopped · Telegram disconnected" in lines
    assert "Drift ⚠️ 3 of 481" in lines


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


def _with_version(version: VersionSummary) -> DashboardSnapshot:
    return DashboardSnapshot(
        overall="normal",
        observed_at="2026-09-25T16:45:00+00:00",
        capacity=CapacitySummary((QuotaMetric("Codex", "official", used=10, limit=100),)),
        version=version,
    )


def _version_render(version: VersionSummary) -> tuple[list[str], list[str]]:
    text = render_dashboard(_with_version(version), now=NOW, zone=UTC)
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

    main = _main_part(render_dashboard(snapshot, now=NOW, zone=UTC))

    assert main[-3:] == ["", "🤖 Hermes 0.21.5 ✓", ""]


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


# ----------------------------------------------------------------------------- Gemini 429s (0.8.1)
#
# Google gives a free-tier key no quota number; the engine's log has the 429s (``gemini_log.py``).
# Decision of 29.09: a 429 is not a number and never colours the status by itself. While it is
# active the Gemini line says it instead of "no data"; the details always say the last one.

_GEMINI_REASON = "Google reports Gemini quota only with billing enabled"
_TTS = "gemini-2.5-flash-preview-tts"


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
    assert "Gemini 429s, engine calls only: none in the log" in details
    assert f"Gemini: {_GEMINI_REASON}" in details


def test_a_recent_429_marks_the_gemini_line_and_leaves_the_status_alone() -> None:
    refusal = Refusal(
        at=_ago(minutes=5), limit=10, retry_seconds=41.53, model=_TTS, active_until=_ahead(hours=1)
    )

    text = _limits_only(_gemini(refusal))

    assert text.splitlines()[0] == "🟢 Healthy · Sep 25 07:21 UTC"
    assert _screen_lines(text, "Gemini") == ["⚠️ Gemini 429 · 5 min ago"]
    details = _details_lines(text)
    assert (
        f"Gemini 429s, engine calls only: last Sep 25 07:16, limit 10, retry 42 s, model {_TTS}"
        in details
    )
    assert f"Gemini: {_GEMINI_REASON}" in details  # the reason for "no data" stays


def test_a_429_within_the_minute_is_just_now() -> None:
    refusal = Refusal(at=_ago(seconds=30), active_until=_ahead(minutes=59))

    assert _screen_lines(_limits_only(_gemini(refusal)), "Gemini") == ["⚠️ Gemini 429 · just now"]


def test_after_its_window_the_429_is_only_in_the_details() -> None:
    refusal = Refusal(at=_ago(hours=2), retry_seconds=30.0, active_until=_ago(hours=1))

    text = _limits_only(_gemini(refusal))

    assert _screen_lines(text, "Gemini") == ["Gemini · no data"]
    assert "Gemini 429s, engine calls only: last Sep 25 05:21, retry 30 s" in _details_lines(text)


def test_a_daily_429_names_the_reset_instead_of_the_retry() -> None:
    refusal = Refusal(
        at=_ago(hours=2, minutes=3),
        limit=15,
        retry_seconds=14580.0,
        model=_TTS,
        daily=True,
        active_until=_ahead(hours=2),
    )

    text = _limits_only(_gemini(refusal))

    assert _screen_lines(text, "Gemini") == ["⚠️ Gemini 429 · 2 h ago"]
    assert (
        f"Gemini 429s, engine calls only: last Sep 25 05:18, limit 15, resets Sep 25 09:21, "
        f"model {_TTS}" in _details_lines(text)
    )


def test_the_429_is_dated_in_the_screen_s_zone() -> None:
    snapshot = DashboardSnapshot(
        overall="normal",
        observed_at=NOW.isoformat(),
        capacity=CapacitySummary((_gemini(Refusal(at=_ago(minutes=5), active_until=_ago())),)),
    )

    text = render_dashboard(snapshot, now=NOW, zone=timezone(timedelta(hours=5)))

    assert "Gemini 429s, engine calls only: last Sep 25 12:16" in _details_lines(text)


def test_a_line_whose_log_nobody_reads_has_no_429_words() -> None:
    text = _limits_only(QuotaMetric("Gemini", "unsupported", detail="limits disabled in config"))

    assert _screen_lines(text, "Gemini") == ["Gemini · no data"]
    assert not any("429" in line for line in text.splitlines())


def test_a_model_name_from_the_record_is_sanitized_like_every_other_word() -> None:
    refusal = Refusal(at=_ago(hours=2), model="m" * 60, active_until=_ago(hours=1))

    details = _details_lines(_limits_only(_gemini(refusal)))

    assert f"Gemini 429s, engine calls only: last Sep 25 05:21, model {'m' * 39}…" in details


def test_a_line_with_numbers_keeps_them_and_the_429_stays_in_the_details() -> None:
    """An external source answering for Gemini replaces the line (decision 9); the refusal the
    log saw rides along, the numbers speak on the screen."""
    refusal = Refusal(at=_ago(minutes=5), active_until=_ahead(minutes=55))
    metric = QuotaMetric(
        "Gemini", "official", windows=(QuotaWindow("day", 40.0, _IN_4H),), refusal=refusal
    )

    text = _limits_only(metric)

    assert _screen_lines(text, "Gemini") == ["Gemini 40% (4h)"]
    assert "Gemini 429s, engine calls only: last Sep 25 07:16" in _details_lines(text)


def test_a_gemini_log_that_is_not_there_is_named_as_such() -> None:
    snapshot = DashboardSnapshot(
        overall="unknown",
        observed_at=NOW.isoformat(),
        sources=(
            SourceObservation("gemini_log", "local", "unsupported", detail="no logs directory"),
        ),
    )

    main = _main_part(render_dashboard(snapshot, now=NOW))

    assert "Not observed: Gemini log (not on this installation)" in main
