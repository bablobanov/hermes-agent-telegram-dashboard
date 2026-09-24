"""Ten static states from section 11 of the research plus two for the message itself.

The criteria of the research: no false green with partial coverage, exceptions are not pushed
out by normal metrics, the next action is nameable from the first screen.
"""

import pytest

from telegram_dashboard.render import TELEGRAM_TEXT_LIMIT, render_dashboard, to_telegram_html
from telegram_dashboard.states import NOW, PERIOD_SECONDS, all_states

STATES = all_states()


def _render(state) -> str:
    return render_dashboard(
        state.snapshot, now=NOW, delivery=state.delivery, period_seconds=PERIOD_SECONDS
    )


def test_twelve_states_are_defined_and_numbered() -> None:
    assert [state.number for state in STATES] == list(range(1, 13))


@pytest.mark.parametrize("state", STATES, ids=[f"{s.number:02d}" for s in STATES])
def test_every_state_renders_within_telegram_limit_and_matches_expected_overall(state) -> None:
    text = _render(state)

    assert state.snapshot.overall == state.expect_overall
    assert len(to_telegram_html(text)) <= TELEGRAM_TEXT_LIMIT
    assert "Данные: 21:00 UTC" in text
    assert "Сообщение подтверждено:" in text
    assert "Обновлено: 2026-09-09 21:00 UTC" in text
    assert text.count("- ") <= 40


@pytest.mark.parametrize(
    "state", [s for s in STATES if s.expect_overall != "normal"], ids=lambda s: f"{s.number:02d}"
)
def test_non_normal_states_never_show_green(state) -> None:
    assert "🟢 Норма" not in _render(state)


def test_state_1_all_normal_is_green_and_names_coverage() -> None:
    text = _render(STATES[0])

    assert text.splitlines()[1] == "🟢 Норма"
    assert "Охват источников: 3/3" in text
    assert "Gateway: работает · Telegram: подключён" in text
    # Two windows, two resets: one date after two percentages would not say which window it ends.
    assert "Claude: 5 ч 37% (сброс 10.09 00:00) · 7 дн 12% (сброс 14.09 00:00)" in text
    assert "Gemini: нет данных (источник не подтверждён)" in text
    assert "✅ 0 из 474 ключей расходятся · проверено 08:00 UTC" in text


def test_state_2_polling_dead_puts_the_incident_first() -> None:
    text = _render(STATES[1])

    assert "🔴 Требует внимания" in text
    assert "Gateway: работает · Telegram: ошибка" in text
    assert text.index("Telegram не подключён") < text.index("## Лимиты")


def test_state_3_delivery_failed_is_critical_without_work_block() -> None:
    text = _render(STATES[2])

    assert "Не доставлен результат cron-задачи" in text
    assert "## Работа" not in text
    assert "## Автоматика" not in text


def test_state_9_drift_is_visible_in_both_incident_and_block() -> None:
    text = _render(STATES[8])

    assert "Дрейф конфига: 3 из 474 ключей" in text
    assert "⚠️ 3 из 474 ключей расходятся" in text


def test_state_10_stale_source_lowers_coverage_and_names_it() -> None:
    text = _render(STATES[9])

    assert "⚪ Состояние неизвестно" in text
    assert "Устарело: лимиты" in text


def test_state_11_stale_message_banner_is_first_line_even_when_data_is_fine() -> None:
    text = _render(STATES[10])

    assert text.startswith("🔴 ДАШБОРД УСТАРЕЛ")
    assert "🟢 Норма" in text  # data is fine; the message is the problem, and both are said


def test_state_12_lost_message_is_named() -> None:
    text = _render(STATES[11])

    assert "🔴 Закреплённое сообщение пропало (21:00 UTC), НЕ восстановлено" in text


def test_html_conversion_escapes_and_bolds_headings() -> None:
    html = to_telegram_html("# Hermes Dashboard\n## Лимиты\n- a < b & c")

    assert html == "<b>Hermes Dashboard</b>\n<b>Лимиты</b>\n- a &lt; b &amp; c"


def _utf16_units(text: str) -> int:
    return len(text.encode("utf-16-le")) // 2


@pytest.mark.parametrize("state", STATES, ids=[f"{s.number:02d}" for s in STATES])
def test_every_state_has_a_plain_form_without_markup_inside_the_telegram_limit(state) -> None:
    """The plugin path delivers plain text (engine ``edit_message``, ``finalize=False``): Telegram
    counts UTF-16 code units, emoji count twice, and a ``#`` would be shown literally."""
    from telegram_dashboard.render import to_telegram_plain

    plain = to_telegram_plain(_render(state))

    assert "#" not in plain
    assert "<" not in plain
    assert _utf16_units(plain) <= TELEGRAM_TEXT_LIMIT
    assert plain.splitlines()[0].startswith(("HERMES DASHBOARD", "🔴", "🟡", "⚠️"))
