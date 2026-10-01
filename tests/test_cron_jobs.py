"""``cron_jobs``: ``jobs.json`` and the ticker stamps as the engine keeps them, read only.

Every job record is a literal with the engine's own field names (``cron/jobs.py`` at
v2026.9.14); the private fields (``prompt``, ``deliver``) are planted so a test can prove they
never reach the screen. The chat ids are made up.
"""

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from telegram_dashboard.compat import Environment
from telegram_dashboard.cron_jobs import (
    HOLD_TICKS,
    OVERDUE_SECONDS,
    TICKER_STALE_SECONDS,
    classify,
    collect_cron,
    error_kind,
    hold,
    job_of,
    read_ticker,
)

NOW = datetime(2026, 9, 30, 12, 30, tzinfo=UTC)


def _job(**over):
    base = {
        "id": "b1",
        "name": "daily-digest",
        "enabled": True,
        "state": "scheduled",
        "paused_at": None,
        "next_run_at": (NOW + timedelta(minutes=5)).isoformat(),
        "last_run_at": (NOW - timedelta(minutes=25)).isoformat(),
        "last_status": "ok",
        "last_error": None,
        "last_delivery_error": None,
        "failure_streak": 0,
        "prompt": "never read",
        "deliver": "telegram:-1001000000001:17",
        "script": None,
    }
    base.update(over)
    return base


def _home(tmp_path: Path, jobs, *, heartbeat=None, success=None, error=None) -> Environment:
    cron = tmp_path / "cron"
    cron.mkdir()
    (cron / "jobs.json").write_text(json.dumps({"jobs": jobs}), encoding="utf-8")
    for name, value in (("ticker_heartbeat", heartbeat), ("ticker_last_success", success)):
        if value is not None:
            (cron / name).write_text(value, encoding="utf-8")
    if error is not None:
        (cron / "ticker_last_error").write_text(error, encoding="utf-8")
    return Environment(hermes_home=tmp_path)


def _epoch(moment: datetime) -> str:
    return str(moment.timestamp())


# ----------------------------------------------------------------------------- one record


def test_job_of_reads_the_runtime_fields_and_nothing_private() -> None:
    job = job_of(_job())

    assert job is not None
    assert (
        job.job_id,
        job.name,
        job.enabled,
        job.state,
        job.paused_at,
        job.last_status,
        job.failure_streak,
    ) == ("b1", "daily-digest", True, "scheduled", None, "ok", 0)
    assert not hasattr(job, "prompt") and not hasattr(job, "deliver")


@pytest.mark.parametrize("raw", [None, [], {"name": "no id"}, {"id": 5}])
def test_job_of_refuses_a_record_that_is_not_a_job(raw) -> None:
    assert job_of(raw) is None


def test_an_ok_job_is_no_failure() -> None:
    assert classify(job_of(_job()), now=NOW, ticker_alive=True) is None


def test_a_run_failure_carries_the_engine_s_streak_and_error() -> None:
    job = job_of(_job(last_status="error", last_error="Model provider 429", failure_streak=3))

    failure = classify(job, now=NOW, ticker_alive=True)

    assert failure is not None
    assert (failure.kind, failure.streak, failure.reason) == ("run", 3, "rate limit")
    assert failure.at == (NOW - timedelta(minutes=25)).isoformat()


def test_a_delivery_failure_is_a_failure_with_an_unknown_streak() -> None:
    job = job_of(_job(last_status="delivery_failed", last_delivery_error="Not connected"))

    failure = classify(job, now=NOW, ticker_alive=True)

    assert failure is not None
    assert (failure.kind, failure.streak, failure.reason) == ("delivery", None, "not connected")


def test_blocked_config_is_blocked_and_delivery_queued_is_not_a_failure() -> None:
    blocked = classify(
        job_of(_job(last_status="blocked_config", last_error="no API key")),
        now=NOW,
        ticker_alive=True,
    )
    queued = classify(job_of(_job(last_status="delivery_queued")), now=NOW, ticker_alive=True)

    assert blocked is not None and blocked.kind == "blocked" and blocked.reason == "auth"
    assert queued is None


def test_an_unknown_status_literal_is_named_not_green() -> None:
    """Review focus 1: the engine documents ``last_status`` as a closed set, but it already grew
    (``delivery_queued``); a literal the plugin does not know is a failure named as it is."""
    job = job_of(_job(last_status="quota_hold", last_error=None))

    failure = classify(job, now=NOW, ticker_alive=True)

    assert failure is not None and failure.kind == "run" and failure.reason == "quota_hold"


def test_a_terminal_error_state_is_a_run_failure() -> None:
    job = job_of(_job(state="error", last_status=None, last_error="schedule invalid"))

    failure = classify(job, now=NOW, ticker_alive=True)

    assert failure is not None and failure.kind == "run" and failure.reason == "error"


@pytest.mark.parametrize(
    "text, word",
    [
        ("Unauthorized: invalid API key (429 later)", "auth"),
        ("Model provider 429 rate limit", "rate limit"),
        ("Request timed out after 60s", "timeout"),
        ("Not connected", "not connected"),
        ("Bad Request: chat not found", "chat unavailable"),
        ("Connection reset by peer", "network"),
        ("PermissionError: [Errno 13]", "permission denied"),
        ("OSError: [Errno 24] Too many open files", "fd exhaustion"),
        ("Gateway is running STALE code; ticker yields", "stale code"),
        ("schedule invalid", "error"),
        (None, "error"),
    ],
)
def test_error_kind_follows_the_engine_s_order(text, word) -> None:
    assert error_kind(text) == word


PLANTED = (
    "sk-live-ABCDEFGHIJKLMNOPQRSTUV at /home/someone/.hermes/secret.json chat -1001234567890 "
    "telegram:-1001234567890:17 user 987654321"
)
FRAGMENTS = ("sk-live", "ABCDEFGHIJ", "/home/someone", "1001234567890", "987654321")


def test_planted_secret_path_and_chat_id_never_leave_the_engine_s_error_texts(
    tmp_path: Path,
) -> None:
    """Edit (b) of 01.10: the engine's error texts are reduced to a kind in a word; nothing
    planted reaches the text, the record, the block or the events, in any form the message
    takes. The sanitizer's own masks (task 0) are the second line, not the first."""
    from telegram_dashboard.render import render_dashboard, to_telegram_html, to_telegram_plain
    from telegram_dashboard.schema import DashboardSnapshot

    jobs = [
        _job(
            id="b1",
            name="digest",
            last_status="error",
            last_error=f"Model provider 429 {PLANTED}",
            failure_streak=2,
        ),
        _job(
            id="b2",
            name="mail",
            last_status="delivery_failed",
            last_delivery_error=f"Chat not found: {PLANTED}",
        ),
        _job(id="b3", name=f"watch {PLANTED}", last_status="blocked_config", last_error=PLANTED),
    ]
    env = _home(
        tmp_path,
        jobs,
        heartbeat=_epoch(NOW),
        success=_epoch(NOW),
        error=f"{_epoch(NOW)}\nPermissionError: {PLANTED}\n",
    )
    cache: dict = {}

    summary, _source, incidents = collect_cron(env, cache, now=NOW)
    snapshot = DashboardSnapshot(
        overall="warning", observed_at=NOW.isoformat(), cron=summary, incidents=incidents
    )
    text = render_dashboard(snapshot, now=NOW)

    forms = (
        text,
        to_telegram_plain(text),
        to_telegram_html(text),
        json.dumps(cache),
        repr(summary),
        repr(incidents),
    )
    for rendered in forms:
        for fragment in FRAGMENTS:
            assert fragment not in rendered, fragment
    assert "(rate limit)" in text and "(chat unavailable)" in text and "(config)" in text
    assert "> Cron ticker error: permission denied" in text.splitlines()


def test_an_overdue_job_is_a_failure_only_while_the_ticker_lives() -> None:
    late = _job(next_run_at=(NOW - timedelta(seconds=OVERDUE_SECONDS + 1)).isoformat())
    in_grace = _job(next_run_at=(NOW - timedelta(seconds=OVERDUE_SECONDS - 1)).isoformat())

    failure = classify(job_of(late), now=NOW, ticker_alive=True)

    assert failure is not None and failure.kind == "overdue"
    assert failure.at == late["next_run_at"]
    assert classify(job_of(late), now=NOW, ticker_alive=False) is None
    assert classify(job_of(in_grace), now=NOW, ticker_alive=True) is None


def test_a_paused_job_s_old_failure_is_not_a_failure() -> None:
    paused = _job(
        enabled=False, state="paused", last_status="error", last_error="x", failure_streak=2
    )

    assert classify(job_of(paused), now=NOW, ticker_alive=True) is None


def test_a_pause_stamp_alone_is_a_pause_as_the_ticker_reads_it(tmp_path: Path) -> None:
    """0.9.1: ``enabled: true`` with a ``paused_at`` stamp is a half-pause (``cron/jobs.py``
    ``_has_pause_marker`` and ``is_job_runnable`` at v2026.9.14 and main): the ticker does not
    fire it and self-disables it on its next loop, so it is paused here too, never overdue
    whatever ``next_run_at`` says, and its old failure is history. ``hermes cron list`` shows it
    active until the engine heals it; the plugin follows what fires."""
    half = _job(
        paused_at=(NOW - timedelta(hours=2)).isoformat(),
        next_run_at=(NOW - timedelta(seconds=OVERDUE_SECONDS + 60)).isoformat(),
        last_status="error",
        last_error="x",
        failure_streak=1,
    )
    env = _home(tmp_path, [half], heartbeat=_epoch(NOW), success=_epoch(NOW))

    summary, _source, incidents = collect_cron(env, {}, now=NOW)

    assert classify(job_of(half), now=NOW, ticker_alive=True) is None
    assert (summary.state, summary.active, summary.paused) == ("ok", 0, 1)
    assert incidents == ()


def test_a_null_or_missing_pause_stamp_is_no_pause() -> None:
    """The engine writes ``paused_at: null`` on a resume and on a job created running; an older
    record may have no key at all."""
    with_null = job_of(_job(paused_at=None))
    raw = _job()
    del raw["paused_at"]
    without = job_of(raw)

    assert with_null is not None and not with_null.paused and with_null.active
    assert without is not None and not without.paused and without.active


@pytest.mark.parametrize(
    ("over", "paused", "active"),
    [
        ({"enabled": None}, True, False),  # bool(None): off
        ({"paused_at": 1727800000}, True, False),  # a number is a stamp: bool() of it
        ({"state": "Paused"}, False, True),  # the marker is ``paused`` exactly: this one fires
        ({"state": "Scheduled"}, False, True),  # a state the ticker does not know still fires
    ],
)
def test_the_fields_are_read_as_the_ticker_reads_them(over, paused, active) -> None:
    """0.9.2, review of 01.10: ``enabled`` and ``paused_at`` by their truth, ``state`` as written
    (``cron/jobs.py`` ``is_job_runnable`` and ``_has_pause_marker`` at v2026.9.14; the load does
    not normalize them). The engine writes the canonical values itself; a hand-edited
    ``jobs.json`` is read as the ticker reads it, so a job counts where it fires."""
    job = job_of(_job(**over))

    assert job is not None
    assert (job.paused, job.active) == (paused, active)


def test_a_half_paused_job_parked_in_error_is_still_a_failure_among_the_active(
    tmp_path: Path,
) -> None:
    """The engine's terminal ``error`` state is kept whatever the stamps say: the job is reported
    and counted, so the line never reads ``1 of 0`` (as for an enabled one, review of 0.9.0)."""
    broken = _job(
        state="error",
        last_status=None,
        last_error="schedule invalid",
        paused_at=(NOW - timedelta(hours=2)).isoformat(),
    )
    env = _home(tmp_path, [broken], heartbeat=_epoch(NOW))

    summary, _source, _incidents = collect_cron(env, {}, now=NOW)

    assert (summary.state, summary.active, len(summary.failing)) == ("failing", 1, 1)
    assert summary.paused == 0  # counted once: among the active, not the paused (review of 01.10)


# ----------------------------------------------------------------------------- the ticker


def test_the_ticker_reads_the_first_token_of_each_stamp(tmp_path: Path) -> None:
    _home(
        tmp_path,
        [],
        heartbeat=f"{_epoch(NOW - timedelta(seconds=30))} 4242\n",
        success=_epoch(NOW - timedelta(minutes=5)),
        error=f"{_epoch(NOW - timedelta(minutes=5))}\nPermissionError: jobs.json\n",
    )

    ticker = read_ticker(tmp_path / "cron")

    assert ticker.heartbeat_at == (NOW - timedelta(seconds=30)).isoformat()
    assert ticker.success_at == (NOW - timedelta(minutes=5)).isoformat()
    assert ticker.error == "permission denied"  # the kind, never the engine's text


def test_a_stamp_that_is_not_a_number_is_no_stamp_with_a_reason(tmp_path: Path) -> None:
    _home(tmp_path, [], heartbeat="tomorrow\n")

    ticker = read_ticker(tmp_path / "cron")

    assert ticker.heartbeat_at is None
    assert "ticker_heartbeat" in (ticker.detail or "")


# ----------------------------------------------------------------------------- the whole


def test_collect_cron_counts_active_and_paused_and_is_fresh(tmp_path: Path) -> None:
    jobs = [
        _job(),
        _job(id="b2", enabled=False, state="paused"),
        _job(id="b3", state="completed", enabled=False),
    ]
    env = _home(
        tmp_path,
        jobs,
        heartbeat=_epoch(NOW - timedelta(seconds=30)),
        success=_epoch(NOW - timedelta(seconds=30)),
    )

    summary, source, incidents = collect_cron(env, {}, now=NOW)

    assert (summary.state, summary.active, summary.paused) == ("ok", 1, 1)
    assert (source.name, source.state) == ("cron", "fresh")
    assert incidents == ()


def test_a_stale_heartbeat_is_stalled_and_critical(tmp_path: Path) -> None:
    env = _home(
        tmp_path, [_job()], heartbeat=_epoch(NOW - timedelta(seconds=TICKER_STALE_SECONDS + 1))
    )

    summary, _source, incidents = collect_cron(env, {}, now=NOW)

    assert summary.state == "stalled"
    assert [(i.incident_id, i.severity, i.title) for i in incidents] == [
        ("cron:ticker", "critical", "Cron ticker silent 3 min")
    ]


def test_a_fresh_heartbeat_with_a_stale_success_is_ticks_failing_with_the_error(
    tmp_path: Path,
) -> None:
    env = _home(
        tmp_path,
        [_job()],
        heartbeat=_epoch(NOW),
        success=_epoch(NOW - timedelta(hours=2)),
        error=f"{_epoch(NOW)}\nPermissionError: jobs.json\n",
    )

    summary, _source, incidents = collect_cron(env, {}, now=NOW)

    assert summary.state == "ticks_failing" and summary.ticker_error == "permission denied"
    assert incidents[0].title == "Cron ticks failing 2 h" and incidents[0].severity == "critical"


def test_no_heartbeat_file_is_not_a_verdict(tmp_path: Path) -> None:
    summary, source, incidents = collect_cron(_home(tmp_path, [_job()]), {}, now=NOW)

    assert summary.state == "ok" and summary.ticker_at is None
    assert source.state == "fresh" and incidents == ()


def test_failures_make_the_state_failing_and_at_most_three_events(tmp_path: Path) -> None:
    jobs = [
        _job(id=f"b{i}", name=f"job-{i}", last_status="error", last_error="x") for i in range(5)
    ]
    env = _home(tmp_path, [*jobs, _job(id="ok")], heartbeat=_epoch(NOW))

    summary, _source, incidents = collect_cron(env, {}, now=NOW)

    assert summary.state == "failing" and len(summary.failing) == 5 and summary.active == 6
    assert [i.title for i in incidents] == [
        "Cron run failed: job-0",
        "Cron run failed: job-1",
        "Cron run failed: job-2",
    ]
    assert all(i.severity == "warning" for i in incidents)


def test_a_long_name_is_cut_so_the_event_fits_the_phone(tmp_path: Path) -> None:
    """Decision of 01.10: eleven characters and an ellipsis in the event, the full name (up to
    24) in the details."""
    job = _job(
        name="dashboard-probe-check-and-more",
        last_status="delivery_failed",
        last_delivery_error="x",
    )
    env = _home(tmp_path, [job], heartbeat=_epoch(NOW))

    _summary, _source, incidents = collect_cron(env, {}, now=NOW)

    assert incidents[0].title == "Cron undelivered: dashboard-p…"
    assert len("- " + incidents[0].title) <= 32


def test_a_name_beyond_ascii_is_cut_shorter_in_the_event(tmp_path: Path) -> None:
    """Decision of 01.10 (0.9.1), from Ilya's phone: Cyrillic glyphs are wider in a proportional
    font, so an event of 32 characters with a Cyrillic name wrapped where the Latin one of the
    same count fits. A name with any character beyond ASCII is cut at eight and an ellipsis;
    the full name (up to 24) stays in the details, as before."""
    job = _job(name="Еженедельный отчёт", last_status="delivery_failed", last_delivery_error="x")
    env = _home(tmp_path, [job], heartbeat=_epoch(NOW))

    summary, _source, incidents = collect_cron(env, {}, now=NOW)

    assert incidents[0].title == "Cron undelivered: Еженедел…"
    assert len("- " + incidents[0].title) <= 29
    assert summary.failing[0].name == "Еженедельный отчёт"


@pytest.mark.parametrize(
    ("name", "shown"),
    [
        ("Дайджесты", "Дайджесты"),  # nine characters fit whole, as twelve Latin ones do
        ("Дайджестик", "Дайджест…"),  # the tenth brings the cut: eight and an ellipsis
    ],
)
def test_a_name_beyond_ascii_is_whole_up_to_nine_characters(
    tmp_path: Path, name: str, shown: str
) -> None:
    job = _job(name=name, last_status="delivery_failed", last_delivery_error="x")
    env = _home(tmp_path, [job], heartbeat=_epoch(NOW))

    _summary, _source, incidents = collect_cron(env, {}, now=NOW)

    assert incidents[0].title == f"Cron undelivered: {shown}"


@pytest.mark.parametrize(
    "payload, detail",
    [("[]", "jobs.json is not an object"), ("{", "jobs.json unreadable: JSONDecodeError")],
)
def test_a_broken_jobs_file_is_no_data_with_the_reason_and_an_event(
    tmp_path: Path, payload, detail
) -> None:
    (tmp_path / "cron").mkdir()
    (tmp_path / "cron" / "jobs.json").write_text(payload, encoding="utf-8")

    summary, source, incidents = collect_cron(Environment(hermes_home=tmp_path), {}, now=NOW)

    assert (summary.state, summary.detail, source.state) == ("unknown", detail, "unavailable")
    assert [i.incident_id for i in incidents] == ["cron:unreadable"]


def test_no_cron_directory_or_no_jobs_file_is_not_on_this_installation(tmp_path: Path) -> None:
    summary, source, _ = collect_cron(Environment(hermes_home=tmp_path), {}, now=NOW)
    assert (summary.state, summary.detail, source.state) == (
        "unsupported",
        "no cron directory",
        "unsupported",
    )

    (tmp_path / "cron").mkdir()
    summary, source, _ = collect_cron(Environment(hermes_home=tmp_path), {}, now=NOW)
    assert (summary.state, summary.detail) == ("unsupported", "jobs.json not found")


# ----------------------------------------------------------------------------- the record


def test_a_failure_is_held_two_ticks_after_the_engine_erased_it(tmp_path: Path) -> None:
    """#118354: a later ok run replaces the failure in ``jobs.json``; the details keep it for
    ``HOLD_TICKS`` ticks with the run that replaced it. No ticker stamps here: the ticks are half
    an hour apart, and a heartbeat that old would be a dead ticker, which is another test."""
    cache: dict = {}
    failing = _home(tmp_path, [_job(last_status="error", last_error="x", failure_streak=1)])

    summary, _s, _i = collect_cron(failing, cache, now=NOW)
    assert len(summary.failing) == 1 and summary.held == ()

    (tmp_path / "cron" / "jobs.json").write_text(
        json.dumps({"jobs": [_job(last_run_at=NOW.isoformat())]}), encoding="utf-8"
    )
    for tick in range(HOLD_TICKS):
        later = NOW + timedelta(minutes=30 * (tick + 1))
        summary, _s, incidents = collect_cron(failing, cache, now=later)
        assert summary.state == "ok" and incidents == ()
        assert [(h.kind, h.recovered_at) for h in summary.held] == [("run", NOW.isoformat())]

    summary, _s, _i = collect_cron(failing, cache, now=NOW + timedelta(hours=2))
    assert summary.held == () and cache["held"] == {}


def test_the_held_record_is_bounded_and_tolerates_junk() -> None:
    entries = {
        f"b{i}": {"kind": "run", "at": "2026-09-30T00:00:00+00:00", "name": f"j{i}", "shown": 0}
        for i in range(80)
    }
    cache = {"held": {"junk": 5, **entries}}

    held = hold(cache, (), {}, now=NOW)

    assert "junk" not in cache["held"] and len(held) == 50


# ----------------------------------------------------------------------------- review of 01.10


def test_a_job_parked_in_the_error_state_is_counted_among_the_active(tmp_path: Path) -> None:
    """Review, important 1: an enabled job the engine parked in its terminal ``error`` state is a
    job that should be running; it is counted in the total, so the line never reads
    ``1 of 0 failing``."""
    from telegram_dashboard.render import render_dashboard
    from telegram_dashboard.schema import DashboardSnapshot

    broken = _job(id="b1", state="error", last_status=None, last_error="schedule invalid")
    env = _home(tmp_path, [broken], heartbeat=_epoch(NOW))

    summary, _source, _incidents = collect_cron(env, {}, now=NOW)
    text = render_dashboard(
        DashboardSnapshot(overall="warning", observed_at=NOW.isoformat(), cron=summary), now=NOW
    )

    assert (summary.state, summary.active, len(summary.failing)) == ("failing", 1, 1)
    assert "Cron ⚠️ 1 of 1 failing" in text.splitlines()


def test_a_disabled_job_parked_in_error_is_no_failure_and_is_counted_paused(
    tmp_path: Path,
) -> None:
    """0.9.2, review of 01.10: a job switched off (``enabled: false``) is never fired whatever its
    state, and ``hermes cron status`` does not read it (``list_jobs(include_disabled=False)``,
    v2026.9.14): the engine's ``error`` on it is history, as for any paused job. It is counted
    among the paused, so the line never reads ``1 of 0 failing``."""
    from telegram_dashboard.render import render_dashboard
    from telegram_dashboard.schema import DashboardSnapshot

    off = _job(
        id="b1", enabled=False, state="error", last_status=None, last_error="schedule invalid"
    )
    env = _home(tmp_path, [off], heartbeat=_epoch(NOW), success=_epoch(NOW))

    summary, _source, incidents = collect_cron(env, {}, now=NOW)
    text = render_dashboard(
        DashboardSnapshot(overall="normal", observed_at=NOW.isoformat(), cron=summary), now=NOW
    )

    assert classify(job_of(off), now=NOW, ticker_alive=True) is None
    assert (summary.state, summary.active, summary.paused) == ("ok", 0, 1)
    assert summary.failing == () and incidents == ()
    assert "Cron ✓ 0 jobs" in text.splitlines()


def test_a_completed_job_left_enabled_by_hand_is_no_failure(tmp_path: Path) -> None:
    """Review of 0.9.2: the engine turns a job off when it completes; a record left
    ``enabled: true`` and ``completed`` by hand counts neither active nor paused, so a failure
    on it would read ``1 of 0 failing``. A failure is reported only for a job counted among the
    active."""
    done = _job(state="completed", last_status="error", last_error="x")
    env = _home(tmp_path, [done], heartbeat=_epoch(NOW), success=_epoch(NOW))

    summary, _source, incidents = collect_cron(env, {}, now=NOW)

    assert classify(job_of(done), now=NOW, ticker_alive=True) is None
    assert (summary.state, summary.active, summary.paused) == ("ok", 0, 0)
    assert summary.failing == () and incidents == ()


@pytest.mark.parametrize(
    "text",
    [
        "Model yielded an empty response",
        "Tool 'connection_pool' misconfigured",
        "quota_hold_until reached",
    ],
)
def test_error_kind_matches_words_not_substrings(text: str) -> None:
    """Review, minor 2: ``yield``, ``connection`` and ``quota`` are words, not substrings."""
    assert error_kind(text) == "error"


def test_a_ticker_verdict_without_a_readable_stamp_carries_no_age() -> None:
    """Review, minor 4: ``age unknown`` made the line 33 columns; the words are dropped."""
    from telegram_dashboard.cron_jobs import incidents_for
    from telegram_dashboard.schema import CronSummary

    (event,) = incidents_for(CronSummary("stalled"), now=NOW)

    assert event.title == "Cron ticker silent"


def test_the_held_record_keeps_the_newest_entries_when_it_is_full() -> None:
    """Review, minor 7: the record trims the oldest entries, and returns what it keeps."""
    entries = {
        f"b{i}": {"kind": "run", "at": "2026-09-30T00:00:00+00:00", "name": f"j{i}", "shown": 0}
        for i in range(80)
    }
    cache = {"held": entries}

    held = hold(cache, (), {}, now=NOW)

    ids = [h.job_id for h in held]
    assert (ids[0], ids[-1]) == ("b30", "b79")
    assert "b79" in cache["held"] and "b0" not in cache["held"]


def test_a_failure_recorded_again_moves_to_the_end_of_the_record() -> None:
    """Round 2, minor 8: a re-recorded failure takes the newest position, so the trim of the
    oldest entries means what it says."""
    from telegram_dashboard.schema import CronFailure

    cache = {
        "held": {
            "a": {"kind": "run", "at": "2026-09-30T00:00:00+00:00", "name": "ja", "shown": 0},
            "b": {"kind": "run", "at": "2026-09-30T00:00:00+00:00", "name": "jb", "shown": 0},
        }
    }
    again = CronFailure("a", "ja", "run", at="2026-09-30T01:00:00+00:00")

    hold(cache, (again,), {}, now=NOW)

    assert list(cache["held"]) == ["b", "a"]
