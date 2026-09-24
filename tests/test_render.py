from telegram_dashboard.render import render_dashboard
from telegram_dashboard.schema import (
    AutomationSummary,
    CapacitySummary,
    Coverage,
    DashboardSnapshot,
    Incident,
    QuotaMetric,
    WorkSummary,
)


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
    assert "Охват профилей: 2/3" in rendered
    assert "Недоступно источников: 1" in rendered
    assert "🟢 Норма" not in rendered


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

    assert "Локально учтено: 120 000 токенов" in rendered
    assert "Остаток: неизвестно" in rendered
    assert "%" not in rendered


def test_official_quota_with_limit_renders_remaining_percentage() -> None:
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

    assert "OpenAI: Остаток 25%" in rendered
    assert "Reset: 2026-09-10 00:00 UTC" in rendered


def test_plain_form_uppercases_headings_and_leaves_every_other_line_untouched() -> None:
    """The engine's ``edit_message`` without ``finalize`` sends plain text: no parse mode, no
    bold. A ``#`` heading would reach the chat as a literal ``#``; upper case survives anywhere."""
    from telegram_dashboard.render import to_telegram_plain

    plain = to_telegram_plain(
        "🔴 ДАШБОРД УСТАРЕЛ\n# Hermes Dashboard\n🟢 Норма\n## Лимиты\n- Claude: 5 ч 37% · a < b\n\n## Дрейф"
    )

    assert plain == (
        "🔴 ДАШБОРД УСТАРЕЛ\nHERMES DASHBOARD\n🟢 Норма\nЛИМИТЫ\n- Claude: 5 ч 37% · a < b\n\nДРЕЙФ"
    )
    assert "#" not in plain
    assert "<b>" not in plain and "&lt;" not in plain


def test_drift_that_could_not_be_checked_shows_why_and_no_check_time() -> None:
    from telegram_dashboard.schema import DriftSummary

    snapshot = DashboardSnapshot(
        overall="unknown",
        observed_at="2026-09-09T00:00:00Z",
        drift=DriftSummary("unknown", detail="дедлайн 35 с"),
    )

    rendered = render_dashboard(snapshot)

    assert "- неизвестно: дедлайн 35 с" in rendered
    assert "проверено" not in rendered and "время проверки" not in rendered
