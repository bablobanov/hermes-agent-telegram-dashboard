"""``widget.view``: the widget's screen from one collection (limits, disk, cron, head)."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta, timezone

import pytest

from telegram_dashboard import render, rich, timeparse
from telegram_dashboard.cron_frequency import OTHER
from telegram_dashboard.disk import space_words
from telegram_dashboard.schema import (
    CapacitySummary,
    CronFailure,
    CronJob,
    CronSummary,
    DiskSummary,
    Frequency,
    GatewaySummary,
    QuotaMetric,
    QuotaWindow,
)
from telegram_dashboard.states import all_states
from telegram_dashboard.widget import view as wv

NOW = datetime(2026, 9, 26, 21, 0, tzinfo=UTC)
ZONE = timezone(timedelta(hours=5), "+05")
GB = 1024**3
DAILY = Frequency("Daily", 1440)
EVERY_15 = Frequency("Every 15 min", 15)
ONCE = Frequency("Once", 0)


def at(minutes: int) -> str:
    return (NOW + timedelta(minutes=minutes)).isoformat()


def official(name: str, *windows: QuotaWindow) -> QuotaMetric:
    return QuotaMetric(name, "official", windows=windows)


def job(job_id: str, state: str = "active", frequency: Frequency = DAILY, **fields):
    fields.setdefault("name", job_id)
    return CronJob(job_id, state=state, frequency=frequency, **fields)


def snapshot_one():
    return all_states()[0].snapshot


def screen(snapshot, zone=ZONE):
    parts = render.render_parts(snapshot, now=NOW, delivery=None, period_seconds=300, zone=zone)
    return wv.view(snapshot, parts, NOW, zone), parts


# ----------------------------------------------------------------------------- limits: cells


def test_cells_follow_the_sources_order_with_its_labels_capitalised():
    quota = official(
        "Claude",
        QuotaWindow("Session", 37, at(180)),
        QuotaWindow("Weekly", 12, at(2880)),
        QuotaWindow("week", 3, at(4000)),
    )
    found = wv.cells(quota, NOW)
    assert [cell[0] for cell in found] == ["Session", "Weekly", "Week"]


def test_title_raises_the_first_letter_only_and_names_an_empty_label():
    assert wv.title("week") == "Week"
    assert wv.title("Weekly") == "Weekly"
    assert wv.title("") == "Limit"


def test_a_model_window_joins_the_cell_of_the_account_window_with_its_label_any_case():
    quota = official(
        "Claude",
        QuotaWindow("Session", 37, at(180)),
        QuotaWindow("Weekly", 12, at(2880)),
        QuotaWindow("weekly", 5, at(2880), scope="Fable"),
    )
    found = wv.cells(quota, NOW)
    assert [cell[0] for cell in found] == ["Session", "Weekly"]
    assert [bar[:2] for bar in found[1][1]] == [["", 12], ["Fable", 5]]


def test_the_reset_is_said_once_when_the_model_window_resets_with_the_account_one():
    quota = official(
        "Claude",
        QuotaWindow("Weekly", 12, at(2880)),
        QuotaWindow("Weekly", 5, at(2880), scope="Fable"),
    )
    (cell,) = wv.cells(quota, NOW)
    assert cell[2] == "resets in 2d"
    assert all(len(bar) == 3 for bar in cell[1])


def test_each_bar_carries_its_own_reset_and_the_cell_has_none_when_they_differ():
    quota = official(
        "Claude",
        QuotaWindow("Weekly", 12, at(2880)),
        QuotaWindow("Weekly", 5, at(3000), scope="Fable"),
    )
    (cell,) = wv.cells(quota, NOW)
    assert cell[2] is None
    assert [bar[3] for bar in cell[1]] == ["resets in 2d", "resets in 2d2h"]


def test_a_model_window_without_an_account_window_of_its_label_gets_its_own_cell():
    quota = official(
        "Claude",
        QuotaWindow("Session", 37, at(180)),
        QuotaWindow("Design", 5, at(600), scope="Opus"),
    )
    found = wv.cells(quota, NOW)
    assert [cell[0] for cell in found] == ["Session", "Design"]
    assert found[1][1] == [["Opus", 5, 0]]
    assert found[1][2] == "resets in 10h"


# ----------------------------------------------------------------------------- limits: provider


def test_one_cell_puts_title_and_reset_in_the_line_with_bars_named_all_and_the_model():
    quota = official(
        "Codex",
        QuotaWindow("Week", 16, at(183)),
        QuotaWindow("Week", 20, at(183), scope="Fable"),
    )
    name, found, line = wv.provider(quota, NOW, ZONE)
    assert name == "Codex"
    assert found == [[[["All", 16, 0], ["Fable", 20, 0]], ""]]
    assert line == "Week · resets in 3h3m"


def test_one_cell_with_one_unnamed_bar_keeps_the_title_as_label_and_the_reset_alone_in_the_line():
    quota = official("Codex", QuotaWindow("5h", 16, at(183)))
    assert wv.provider(quota, NOW, ZONE) == [
        "Codex",
        [[[["5h", 16, 0]], ""]],
        "resets in 3h3m",
    ]


def test_one_cell_with_separate_resets_keeps_them_on_the_bars_and_only_the_title_in_the_line():
    quota = official(
        "Codex",
        QuotaWindow("Week", 16, at(183)),
        QuotaWindow("Week", 16, at(983), scope="Fable"),
    )
    _name, found, line = wv.provider(quota, NOW, ZONE)
    assert [bar[0] for bar in found[0][0]] == ["All", "Fable"]
    assert [bar[3] for bar in found[0][0]] == ["resets in 3h3m", "resets in 16h23m"]
    assert line == "Week"


def test_two_cells_have_no_line_and_the_account_bar_is_labelled_by_the_title():
    quota = official(
        "Codex",
        QuotaWindow("5h", 16, at(183)),
        QuotaWindow("Week", 40, at(983)),
        QuotaWindow("Week", 8, at(983), scope="Fable"),
    )
    assert wv.provider(quota, NOW, ZONE) == [
        "Codex",
        [
            [[["5h", 16, 0]], "resets in 3h3m"],
            [[["Week", 40, 0], ["Fable", 8, 0]], "resets in 16h23m"],
        ],
    ]


def test_a_cell_of_models_only_carries_its_title_in_the_reset_words():
    quota = official(
        "Codex",
        QuotaWindow("5h", 16, at(183)),
        QuotaWindow("Week", 5, at(983), scope="Fable"),
    )
    _name, found = wv.provider(quota, NOW, ZONE)
    assert found[1] == [[["Fable", 5, 0]], "Week · resets in 16h23m"]


def test_an_expired_login_has_no_cells_and_says_so():
    assert wv.provider(QuotaMetric("Claude", "expired"), NOW, ZONE) == [
        "Claude",
        None,
        "login expired",
    ]


def test_a_provider_without_official_numbers_says_the_dashboards_state_words():
    quota = QuotaMetric("Gemini", "unsupported")
    assert wv.provider(quota, NOW, ZONE) == [
        "Gemini",
        None,
        render._state_cell(quota, NOW, ZONE),
    ]


def test_an_official_provider_without_windows_takes_the_state_words_too():
    quota = QuotaMetric("Claude", "official")
    assert wv.provider(quota, NOW, ZONE) == [
        "Claude",
        None,
        render._state_cell(quota, NOW, ZONE)[:48],
    ]


def test_a_window_without_a_number_shows_its_note_and_the_providers_word():
    quota = official("Codex", QuotaWindow("5h", None, at(5), note="paused", severity="critical"))
    bar = wv.provider(quota, NOW, ZONE)[1][0][0][0]
    assert bar == ["5h", "paused", 2]
    quota = official("Codex", QuotaWindow("5h", None, at(5), note="paused", severity="warning"))
    assert wv.provider(quota, NOW, ZONE)[1][0][0][0] == ["5h", "paused", 1]


@pytest.mark.parametrize(
    ("percent", "said", "expected"),
    [
        (0, None, 0),
        (render.QUOTA_WARN_PERCENT - 1, None, 0),
        (render.QUOTA_WARN_PERCENT, None, 1),
        (99, None, 1),
        (100, None, 2),
        (130, None, 2),
        (10, "warning", 1),
        (10, "critical", 2),
        (10, "normal", 0),
        (None, "critical", 2),  # no number: the provider's own word is the verdict
        (None, None, 0),
    ],
)
def test_severity_is_the_90_mark_the_full_bar_or_the_providers_word(percent, said, expected):
    assert wv.severity(percent, said) == expected


def test_a_percent_is_rounded_before_its_severity_is_judged():
    quota = official("Codex", QuotaWindow("5h", 99.6, at(5)))
    assert wv.provider(quota, NOW, ZONE)[1][0][0][0][1:] == [100, 2]


def test_a_providers_own_word_raises_the_bar_in_the_card():
    quota = official("Codex", QuotaWindow("5h", 50, at(5), severity="warning"))
    assert wv.provider(quota, NOW, ZONE)[1][0][0][0][2] == 1


def test_only_the_providers_the_dashboard_draws_get_a_card():
    snapshot = replace(
        snapshot_one(),
        capacity=CapacitySummary(
            (
                official("Claude", QuotaWindow("5h", 10, at(60))),
                QuotaMetric("Nobody", "not_configured"),
            )
        ),
    )
    found, _ = screen(snapshot)
    assert [p[0] for p in found.providers] == ["Claude"]
    assert [q.provider for q in render.drawn_quotas(snapshot.capacity.quotas, NOW)] == ["Claude"]


# ----------------------------------------------------------------------------- disk


def test_disk_row_is_the_space_words_the_percent_used_and_severity_zero():
    disk = DiskSummary("ok", free_bytes=61 * GB, total_bytes=80 * GB)
    row = wv.disk_row(replace(snapshot_one(), disk=disk))
    assert row == [space_words(61 * GB, 80 * GB), 24, 0]


def test_a_low_disk_is_severity_one_and_only_that_state():
    low = DiskSummary("low", free_bytes=GB, total_bytes=80 * GB)
    unknown = DiskSummary("unknown", free_bytes=GB, total_bytes=80 * GB)
    assert wv.disk_row(replace(snapshot_one(), disk=low))[2] == 1
    assert wv.disk_row(replace(snapshot_one(), disk=unknown))[2] == 0


@pytest.mark.parametrize(
    "disk",
    [
        None,
        DiskSummary("unknown"),
        DiskSummary("ok", free_bytes=GB, total_bytes=0),
        DiskSummary("ok", free_bytes=None, total_bytes=80 * GB),
    ],
)
def test_no_disk_row_without_a_disk_a_total_or_a_free_figure(disk):
    assert wv.disk_row(replace(snapshot_one(), disk=disk)) is None


def test_disk_percent_stays_within_zero_and_a_hundred():
    full = DiskSummary("low", free_bytes=0, total_bytes=80 * GB)
    assert wv.disk_row(replace(snapshot_one(), disk=full))[1] == 100


# ----------------------------------------------------------------------------- cron


def a_cron() -> CronSummary:
    jobs = (
        job("a", "active", DAILY, name="daily-a", schedule="0 9 * * *", next_run_at=at(300)),
        job("b", "active", OTHER, name="odd", schedule="weird", next_run_at=at(30)),
        job("c", "active", EVERY_15, name="ping", schedule="every 15m", next_run_at=at(5)),
        job("d", "active", ONCE, name="one", schedule="once", next_run_at=at(500)),
        job("e", "active", DAILY, name="daily-b", next_run_at=at(400), failure="run"),
        job("f", "paused", DAILY, name="stopped", next_run_at=at(2000)),
        job("g", "done", DAILY, name="finished", last_run_at=at(-3000)),
    )
    failing = (
        CronFailure("e", "daily-b", "run", at=at(-60), streak=3, reason="timeout"),
        CronFailure("zz", "ghost", "overdue", at=at(-60)),
    )
    return CronSummary("ok", active=5, paused=1, ticker_at=at(-2), jobs=jobs, failing=failing)


def test_groups_come_in_frequency_order_with_once_first_and_other_last():
    cron = wv.cron_view(a_cron(), NOW, ZONE)
    assert [label for label, _ in cron.groups] == ["Once", "Every 15 min", "Daily", "Other"]
    expected = [j.frequency.label for j in rich._by_frequency(a_cron().jobs) if j.failure is None]
    assert [label for label, _ in cron.groups] == list(dict.fromkeys(expected))


def test_inside_a_group_the_summarys_job_order_stays():
    jobs = tuple(
        job(str(i), "active", DAILY, name=name, next_run_at=at(100 - i))
        for i, name in enumerate(("zeta", "alpha", "mid"))
    )
    cron = wv.cron_view(CronSummary("ok", active=3, ticker_at=at(0), jobs=jobs), NOW, ZONE)
    (group,) = cron.groups
    assert [row[0] for row in group[1]] == ["zeta", "alpha", "mid"]


def test_a_row_is_name_minutes_to_the_run_and_the_next_run_in_short_words():
    cron = wv.cron_view(a_cron(), NOW, ZONE)
    rows = dict(cron.groups)
    assert rows["Every 15 min"] == (["ping", 5, timeparse.format_short(at(5), NOW, ZONE)],)
    assert rows["Daily"] == (["daily-a", 300, timeparse.format_short(at(300), NOW, ZONE)],)


def test_a_job_without_a_next_run_has_no_minutes_and_a_dash():
    jobs = (job("a", "active", DAILY, name="lost"),)
    cron = wv.cron_view(CronSummary("ok", active=1, jobs=jobs), NOW, ZONE)
    assert cron.groups == (("Daily", (["lost", None, "—"],)),)


def test_other_rows_name_the_jobs_schedule_after_its_name():
    rows = dict(wv.cron_view(a_cron(), NOW, ZONE).groups)["Other"]
    assert rows[0][0] == "odd · weird"


def test_failing_jobs_leave_the_groups_and_are_listed_as_failing_with_a_note():
    cron = wv.cron_view(a_cron(), NOW, ZONE)
    names = [row[0] for _, rows in cron.groups for row in rows]
    assert "daily-b" not in names
    note = f"{timeparse.format_day_time(at(-60), ZONE)} · 3 in a row · timeout"
    assert cron.failing[0] == ["daily-b", wv.FAILURE_WORDS["run"], note]
    assert cron.failing[0][1] == "failed"


def test_a_failure_whose_job_is_not_listed_is_still_in_failing():
    cron = wv.cron_view(a_cron(), NOW, ZONE)
    assert cron.failing[1] == [
        "ghost",
        "overdue",
        timeparse.format_day_time(at(-60), ZONE),
    ]


def test_failure_words_are_the_dashboards_for_every_kind():
    assert wv.FAILURE_WORDS == {
        "run": "failed",
        "delivery": "not delivered",
        "blocked": "blocked",
        "overdue": "overdue",
    }


def test_a_single_run_failure_says_one_run():
    failure = CronFailure("e", "x", "delivery", at=at(-5), streak=1)
    assert wv.failure_note(failure, ZONE).endswith("· 1 run")


def test_paused_rows_carry_the_day_of_the_next_run_and_done_rows_of_the_last():
    cron = wv.cron_view(a_cron(), NOW, ZONE)
    assert cron.paused == (["stopped", timeparse.format_day(at(2000), ZONE)],)
    assert cron.done == (["finished", timeparse.format_day(at(-3000), ZONE)],)


def test_the_head_has_the_active_count_and_the_tickers_clock_in_the_zone():
    head = wv.cron_view(a_cron(), NOW, ZONE).head
    assert head == f"5 active · ticker {timeparse.format_in_zone(at(-2), ZONE, '%H:%M')}"
    assert head.endswith("01:58")


def test_the_head_says_no_heartbeat_without_a_ticker():
    cron = CronSummary("ok", active=2, jobs=())
    assert wv.cron_view(cron, NOW, ZONE).head == "2 active · no heartbeat"


@pytest.mark.parametrize(
    ("state", "suffix"), [("stalled", " · stalled"), ("ticks_failing", " · ticks failing")]
)
def test_a_stalled_or_failing_ticker_adds_its_suffix(state, suffix):
    cron = CronSummary(state, active=1, ticker_at=at(-2), jobs=())
    assert wv.cron_view(cron, NOW, ZONE).head.endswith(suffix)


def test_an_unknown_state_or_no_summary_is_no_data_and_an_unsupported_one_not_observed():
    assert wv.cron_view(None, NOW, ZONE).head == "No data"
    assert wv.cron_view(CronSummary("unknown"), NOW, ZONE) == wv.CronView("No data")
    assert wv.cron_view(CronSummary("unsupported"), NOW, ZONE) == wv.CronView("Not observed")


# ----------------------------------------------------------------------------- head and items


def test_unchecked_items_split_on_commas_outside_parentheses():
    line = "❔ Not checked: Gemini log (not on this installation, x), profiles 2/3"
    assert wv.unchecked_items(line) == [
        "Gemini log (not on this installation, x)",
        "profiles 2/3",
    ]


def test_unchecked_items_take_a_line_without_the_prefix_whole():
    assert wv.unchecked_items("one, two") == ["one", "two"]
    assert wv.unchecked_items("") == []


def test_headline_strips_the_time_tag_the_emoji_and_the_stamp():
    assert wv.headline("#@1790456400 🟢 Healthy · updated Sep 26 21:00 UTC") == "Healthy"


def test_headline_of_nothing_is_status():
    assert wv.headline("") == "Status"
    assert wv.headline("#@12 🟢") == "Status"


def test_unavailable_view_is_unknown_with_a_note_and_keeps_the_zone():
    found = wv.unavailable("no collection", ZONE)
    assert (found.status, found.headline) == ("unknown", wv.UNAVAILABLE)
    assert found.blocks == (("note", ("no collection",)),)
    assert found.zone is ZONE


# ----------------------------------------------------------------------------- view()


def test_view_keeps_only_the_status_blocks_in_their_order():
    for state in all_states():
        found, parts = screen(state.snapshot)
        keys = [key for key, _ in found.blocks]
        assert set(keys) <= set(wv.STATUS_BLOCKS), state.number
        assert keys == [key for key in wv.STATUS_BLOCKS if key in keys], state.number
        assert len(parts.blocks) >= len(keys)


def test_view_takes_every_block_of_the_status_blocks_that_has_lines():
    found, parts = screen(all_states()[14].snapshot)
    wanted = {b.key for b in parts.blocks if b.lines and b.key in wv.STATUS_BLOCKS}
    assert {key for key, _ in found.blocks} == wanted
    assert "attention" in wanted and "works" in wanted


def test_the_details_leave_out_the_cron_group_but_keep_the_others():
    found, parts = screen(all_states()[13].snapshot)
    drawn = {g.key for g in parts.details.groups if g.lines}
    assert "cron" in drawn
    assert {key for key, _ in found.details} == drawn - {"cron"}


def test_view_status_headline_and_zone_come_from_the_snapshot_and_parts():
    state = all_states()[1]
    found, _ = screen(state.snapshot)
    assert found.status == state.snapshot.overall == "critical"
    assert found.headline == "Critical"
    assert found.zone is ZONE


@pytest.mark.parametrize(
    ("process", "telegram", "expected"),
    [
        ("running", "connected", False),
        ("stopped", "connected", True),
        ("running", "degraded", True),
        ("running", "unknown", True),
        ("unknown", "connected", True),
    ],
)
def test_settling_when_the_gateway_is_not_running_or_telegram_not_connected(
    process, telegram, expected
):
    snapshot = replace(snapshot_one(), gateway=GatewaySummary(process, telegram))
    assert wv.settling(snapshot) is expected
    assert screen(snapshot)[0].settling is expected


def test_no_gateway_is_not_settling():
    assert wv.settling(replace(snapshot_one(), gateway=None)) is False


def test_view_builds_the_cron_and_disk_from_the_snapshot():
    disk = DiskSummary("low", free_bytes=GB, total_bytes=80 * GB)
    snapshot = replace(snapshot_one(), cron=a_cron(), disk=disk)
    found, _ = screen(snapshot)
    assert found.cron == wv.cron_view(a_cron(), NOW, ZONE)
    assert found.disk == wv.disk_row(snapshot)
