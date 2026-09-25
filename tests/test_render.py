from datetime import UTC, datetime, timedelta

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

    assert "## Limits used" in lines  # every percent on the screen is the spent share
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
