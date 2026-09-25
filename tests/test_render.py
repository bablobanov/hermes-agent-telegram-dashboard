from datetime import UTC, datetime

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
                title="Не доставлен результат cron",
            ),
        ),
    )

    rendered = render_dashboard(snapshot)

    assert rendered.index("Не доставлен результат cron") < rendered.index("## Работа")
    assert "🔴 Требует внимания" in rendered


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

    assert "⚪ Состояние неизвестно" in rendered
    # Incomplete coverage is an exception and stays on the screen, not only in the details.
    assert "Охват профилей 2/3" in _main_part(rendered)
    assert "Недоступно источников: 1" in _main_part(rendered)
    assert "🟢 Норма" not in rendered


def test_complete_coverage_leaves_the_screen_and_stays_in_the_details() -> None:
    snapshot = DashboardSnapshot(
        overall="normal",
        observed_at="2026-09-09T00:00:00Z",
        coverage=Coverage(expected_profiles=1, observed_profiles=1),
    )

    rendered = render_dashboard(snapshot)

    assert not any("Охват" in line for line in _main_part(rendered))
    assert "> Профили 1/1" in rendered.splitlines()


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

    assert "Выполняется: 1" in rendered
    assert "В очереди: 2" in rendered
    assert "Ждёт человека: 3" in rendered
    assert "Ошибки: 4" in rendered
    assert "Неизвестно: 5" in rendered


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

    assert "## Автоматика" in rendered
    assert "Scheduler: деградация" in rendered
    assert "Ошибки запусков: 2" in rendered
    assert "Пропущено: 1" in rendered
    assert "Ошибки доставки: 3" in rendered


def test_renderer_redacts_secrets_paths_and_controls_from_incidents() -> None:
    snapshot = DashboardSnapshot(
        overall="critical",
        observed_at="2026-09-09T00:00:00Z",
        incidents=(
            Incident(
                incident_id="provider:error",
                severity="critical",
                title=(
                    "Ошибка sk-exampleSecret123456789 в /home/alice/private/config.yaml\x1b[31m"
                ),
            ),
        ),
    )

    rendered = render_dashboard(snapshot)

    assert "sk-exampleSecret123456789" not in rendered
    assert "/home/alice/private/config.yaml" not in rendered
    assert "\x1b" not in rendered
    assert "[секрет]" in rendered
    assert "[путь]" in rendered


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

    assert "OpenAI · локально 120 000 токенов" in rendered.splitlines()
    assert "> OpenAI: остаток неизвестно, учтено локально" in rendered.splitlines()
    assert "%" not in rendered and "▓" not in rendered and "░" not in rendered


def test_official_quota_with_limit_renders_the_spent_share_as_a_bar() -> None:
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

    assert "OpenAI ▓▓▓▓░ 75%" in rendered.splitlines()
    assert "> OpenAI завтра 00:00" in rendered.splitlines()  # the reset, relative to the data day


def test_a_spent_limit_gets_the_mark_and_a_state_in_words_never_becomes_a_bar() -> None:
    snapshot = DashboardSnapshot(
        overall="normal",
        observed_at="2026-09-25T07:21:00Z",
        capacity=CapacitySummary(
            quotas=(
                QuotaMetric("Claude", "unavailable", detail="нет учётного токена"),
                QuotaMetric(
                    "Codex",
                    "official",
                    windows=(QuotaWindow("Session", 98.0, "2026-09-26T11:14:00Z"),),
                    fetched_at="2026-09-25T07:21:00Z",
                ),
                QuotaMetric(
                    "Grok",
                    "official",
                    windows=(
                        QuotaWindow(
                            "SuperGrok неделя", None, "2026-10-01T19:25:00Z", note="расход не начат"
                        ),
                    ),
                    fetched_at="2026-09-25T07:21:00Z",
                ),
                QuotaMetric(
                    "Kimi",
                    "official",
                    windows=(
                        QuotaWindow("5 ч", 0.0, "2026-09-25T11:34:00Z"),
                        QuotaWindow("мес", 3.0, "2026-10-25T00:00:00Z"),
                    ),
                    fetched_at="2026-09-25T07:16:00Z",
                ),
                QuotaMetric("Gemini", "unsupported", detail="источник не подтверждён"),
            ),
        ),
    )

    rendered = render_dashboard(snapshot, now=NOW, period_seconds=300)
    lines = rendered.splitlines()

    assert "⚠️ Codex ▓▓▓▓▓ 98%" in lines  # 90% and above: the mark, only on this line
    assert "Grok · расход не начат" in lines  # the provider's words, no bar, no zero
    assert "Kimi ░░░░░ 3% мес · 5 ч 0%" in lines  # the most spent window owns the bar
    assert "Claude · нет данных" in lines and "Gemini · нет данных" in lines
    assert "🟡" not in rendered and lines[0] == "🟢 Норма · 25.09 07:21 UTC"
    # Reasons, resets and the odd minute live in the details, grouped.
    assert "> ## Сбросы" in lines
    assert "> Codex завтра 11:14" in lines
    assert "> Grok 01.10" in lines
    assert "> Kimi 5 ч 11:34 · мес 25.10" in lines
    assert "> Данные 07:21 · Kimi 07:16" in lines
    assert "> ## Нет данных" in lines
    assert "> Claude: нет учётного токена" in lines
    assert "> Gemini: источник не подтверждён" in lines
    assert "> Период 5 мин" in lines


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
        "🟢 Норма · 25.09 07:21 UTC",
        "Gateway ✓ · Telegram ✓",
        "Бэкап ✓ 6 ч назад",
        "Дрейф ✓ 0 из 481",
        "",
    ]
    assert rendered.splitlines()[5:] == [
        "> ## Подробности",
        "> Подтверждено 07:16",
        "> Период 5 мин",
        "> Профили 1/1",
        ">",
        "> Бэкап 25.09 00:31 · integrity ok",
        "> Дрейф проверен 07:21",
    ]


def test_abnormal_gateway_and_drift_are_words_on_the_screen() -> None:
    snapshot = DashboardSnapshot(
        overall="critical",
        observed_at="2026-09-25T07:21:00Z",
        gateway=GatewaySummary("stopped", "disconnected"),
        drift=DriftSummary("drift", 3, 481, "2026-09-25T07:21:00Z"),
    )

    lines = render_dashboard(snapshot).splitlines()

    assert "Gateway остановлен · Telegram не подключён" in lines
    assert "Дрейф ⚠️ 3 из 481" in lines


def test_plain_form_uppercases_headings_unwraps_details_and_leaves_every_other_line_untouched() -> (
    None
):
    """The engine's ``edit_message`` without ``finalize`` sends plain text: no parse mode, no
    bold, no quote. A ``#`` heading would reach the chat as a literal ``#``; upper case survives
    anywhere, and the details are simply shown in place."""
    plain = to_telegram_plain(
        "🔴 ДАШБОРД УСТАРЕЛ\n🟢 Норма · 25.09 07:21 UTC\n## Лимиты\n- Claude: 5 ч 37% · a < b\n"
        "\n> ## Подробности\n> Период 5 мин\n>\n> ## Сбросы\n> Codex 19.09"
    )

    assert plain == (
        "🔴 ДАШБОРД УСТАРЕЛ\n🟢 Норма · 25.09 07:21 UTC\nЛИМИТЫ\n- Claude: 5 ч 37% · a < b\n"
        "\nПОДРОБНОСТИ\nПериод 5 мин\n\nСБРОСЫ\nCodex 19.09"
    )
    assert "#" not in plain and ">" not in plain
    assert "<b>" not in plain and "&lt;" not in plain


def test_html_form_bolds_headings_and_folds_the_details_into_one_expandable_quote() -> None:
    html = to_telegram_html(
        "🟢 Норма · 25.09 07:21 UTC\n## Лимиты\n- a < b & c\n"
        "\n> ## Подробности\n> Период 5 мин\n>\n> ## Сбросы\n> Codex 19.09"
    )

    assert html == (
        "🟢 Норма · 25.09 07:21 UTC\n<b>Лимиты</b>\n- a &lt; b &amp; c\n\n"
        "<blockquote expandable><b>Подробности</b>\nПериод 5 мин\n\n<b>Сбросы</b>\nCodex 19.09"
        "</blockquote>"
    )
    assert html.count("<blockquote") == 1


def test_drift_that_could_not_be_checked_shows_why_and_no_check_time() -> None:
    snapshot = DashboardSnapshot(
        overall="unknown",
        observed_at="2026-09-09T00:00:00Z",
        drift=DriftSummary("unknown", detail="дедлайн 35 с"),
    )

    rendered = render_dashboard(snapshot)

    assert "Дрейф: неизвестно" in rendered.splitlines()
    assert "> Дрейф: дедлайн 35 с" in rendered.splitlines()
    assert "проверен" not in rendered and "время проверки" not in rendered
