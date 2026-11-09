"""The disk line (1.1.1): the free space of the partition the profile home lies on, read with
``shutil.disk_usage`` and nothing else, always a line in the details. Under either floor of the
owner's settings (10 % of the partition or 2 GB by default) the disk is low: the line takes the
warning mark, an event says it under "Needs attention" and the status turns yellow.

Pinned: the words of the line and of the event; both floors and the settings that move or turn
off each; a partition that cannot be read is a reason, never a zero and never an alarm; the
event keeps its place behind five others; both ticks read it and neither counts it as a source;
the plugin and the cron tick read the floors with the same parsers.
"""

from __future__ import annotations

import asyncio
import logging
from collections import namedtuple
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from telegram_dashboard import disk
from telegram_dashboard.collect import (
    CommandResult,
    build_snapshot,
    collect_all,
    collect_all_async,
)
from telegram_dashboard.compat import Environment, parse_disk_low_gb, parse_disk_low_percent
from telegram_dashboard.render import render_dashboard
from telegram_dashboard.schema import (
    BackupSummary,
    CapacitySummary,
    DiskSummary,
    DriftSummary,
    Incident,
)

NOW = datetime(2026, 11, 9, 12, 0, tzinfo=UTC)
GB = 1_000_000_000
Usage = namedtuple("Usage", "total used free")


class _Partition:
    """``shutil.disk_usage`` of a made-up partition: ``free`` and ``total`` in GB."""

    def __init__(self, free: float, total: float = 80) -> None:
        self.total, self.free = round(total * GB), round(free * GB)
        self.asked: list[Path] = []

    def __call__(self, path: Path) -> Usage:
        self.asked.append(path)
        return Usage(self.total, self.total - self.free, self.free)


def _read(free: float, total: float = 80, **floors: float) -> disk.DiskPart:
    env = Environment(hermes_home=Path("/home/hermes/.hermes"), **floors)
    return disk.read_disk(env, usage=_Partition(free, total))


# ----------------------------------------------------------------------------- reading


def test_a_disk_with_room_is_ok_and_asks_for_the_profile_home_only() -> None:
    partition = _Partition(61.4)
    env = Environment(hermes_home=Path("/home/hermes/.hermes"))

    summary, incidents = disk.read_disk(env, usage=partition)

    assert summary == DiskSummary("ok", free_bytes=61_400_000_000, total_bytes=80_000_000_000)
    assert incidents == ()
    assert partition.asked == [Path("/home/hermes/.hermes")]


def test_under_ten_percent_of_the_partition_the_disk_is_low() -> None:
    summary, incidents = _read(7.9)  # 9.9 %, far above 2 GB

    assert summary.state == "low"
    assert incidents == (Incident("disk:low", "warning", "Disk: 7.9 GB free of 80 (9%)"),)


def test_under_two_gb_the_disk_is_low_even_on_a_small_partition() -> None:
    summary, incidents = _read(1.9, 16)  # 11.9 %, above the share

    assert summary.state == "low"
    assert [incident.title for incident in incidents] == ["Disk: 1.9 GB free of 16 (11%)"]


def test_on_the_floor_itself_the_disk_is_not_low() -> None:
    assert _read(8)[0].state == "ok"  # exactly 10 %
    assert _read(2, 16)[0].state == "ok"  # exactly 2 GB


def test_the_owner_moves_either_floor_and_zero_turns_one_off() -> None:
    assert _read(30, disk_low_percent=40)[0].state == "low"
    assert _read(4, disk_low_percent=0, disk_low_gb=5)[0].state == "low"
    assert _read(4, disk_low_percent=0, disk_low_gb=0)[0].state == "ok"
    assert _read(0, disk_low_percent=0, disk_low_gb=0)[0].state == "ok"


def test_a_partition_that_cannot_be_read_is_a_reason_not_a_zero() -> None:
    def refused(path: Path) -> Usage:
        raise PermissionError(13, "Permission denied", "/home/hermes/.hermes")

    summary, incidents = disk.read_disk(
        Environment(hermes_home=Path("/home/hermes/.hermes")), usage=refused
    )

    # The class only: the text of the error carries the path.
    assert summary == DiskSummary("unknown", detail="not read (PermissionError)")
    assert incidents == ()


def test_a_partition_that_reports_no_size_is_unknown() -> None:
    summary, incidents = _read(0, 0)

    assert summary == DiskSummary("unknown", detail="the partition reports no size")
    assert incidents == ()


@pytest.mark.parametrize(
    ("size", "words"),
    [
        (400_000_000, "0.4"),
        (1_640_000_000, "1.6"),
        (9_940_000_000, "9.9"),
        (9_960_000_000, "10"),
        (61_400_000_000, "61"),
        (1_234_000_000_000, "1,234"),
    ],
)
def test_a_size_is_one_decimal_under_ten_gb_and_whole_above(size: int, words: str) -> None:
    assert disk.gigabytes(size) == words


def test_the_free_figure_is_rounded_down_so_the_floor_never_reads_as_met() -> None:
    """Review of the disk line: 1.96 GB free is under the 2 GB floor and must not read 2.0."""
    summary, incidents = _read(1.96)

    assert summary.state == "low"
    assert [incident.title for incident in incidents] == ["Disk: 1.9 GB free of 80 (2%)"]
    assert disk.space_words(9_960_000_000, 80 * GB) == "9.9 GB free of 80"
    assert disk.space_words(61_900_000_000, 80 * GB) == "61 GB free of 80"


# ----------------------------------------------------------------------------- the floors


@pytest.mark.parametrize(
    ("raw", "value", "warned"),
    [
        (None, 10.0, False),
        ("", 10.0, False),
        ("  ", 10.0, False),
        (5, 5.0, False),
        (7.5, 7.5, False),
        (" 15 ", 15.0, False),
        ("0", 0.0, False),
        (100, 10.0, True),
        (-1, 10.0, True),
        ("ten", 10.0, True),
        ("nan", 10.0, True),
        (True, 10.0, True),
        ([10], 10.0, True),
    ],
)
def test_the_share_is_a_number_from_0_below_100(
    raw: object, value: float, warned: bool, caplog: pytest.LogCaptureFixture
) -> None:
    log = logging.getLogger("test.disk")
    with caplog.at_level(logging.WARNING, logger="test.disk"):
        assert parse_disk_low_percent(raw, log, "probe: ") == value
    assert bool(caplog.records) is warned
    if warned:
        assert caplog.records[0].getMessage().startswith("probe: disk_low_percent ")


def test_an_unusable_floor_names_a_list_by_its_type_and_survives_a_huge_json_integer(
    caplog: pytest.LogCaptureFixture,
) -> None:
    log = logging.getLogger("test.disk")
    with caplog.at_level(logging.WARNING, logger="test.disk"):
        assert parse_disk_low_gb(["/home/hermes/secret-path"], log) == 2.0
        assert parse_disk_low_gb(10**400, log) == 2.0  # past a float: OverflowError
    messages = [record.getMessage() for record in caplog.records]
    assert messages[0] == "disk_low_gb <class 'list'> unusable; using 2"
    assert not any("secret-path" in message for message in messages)
    assert len(messages) == 2


@pytest.mark.parametrize(
    ("raw", "value", "warned"),
    [
        (None, 2.0, False),
        ("", 2.0, False),
        (500, 500.0, False),
        ("0.5", 0.5, False),
        (0, 0.0, False),
        (-2, 2.0, True),
        ("inf", 2.0, True),
        ("2 GB", 2.0, True),
    ],
)
def test_the_amount_is_a_finite_number_from_0(
    raw: object, value: float, warned: bool, caplog: pytest.LogCaptureFixture
) -> None:
    log = logging.getLogger("test.disk")
    with caplog.at_level(logging.WARNING, logger="test.disk"):
        assert parse_disk_low_gb(raw, log) == value
    assert bool(caplog.records) is warned


# ----------------------------------------------------------------------------- the screen


def _render(summary: DiskSummary | None, incidents: tuple[Incident, ...] = (), **parts: Any) -> str:
    snapshot = build_snapshot(
        now=NOW,
        gateway=None,
        drift=parts.get("drift"),
        backup=parts.get("backup"),
        disk=summary,
        capacity=CapacitySummary(),
        sources=(),
        incidents=incidents,
    )
    return render_dashboard(snapshot, now=NOW, zone=UTC, period_seconds=300)


def test_room_on_the_disk_is_a_details_line_and_nothing_on_the_screen() -> None:
    text = _render(*_read(61.4))
    lines = text.splitlines()

    assert "> Disk: 61 GB free of 80" in lines
    assert "Needs attention" not in text and "⚠" not in text
    assert not any(line.startswith("✓") and "Disk" in line for line in lines)


def test_a_low_disk_is_marked_in_the_details_and_named_under_needs_attention() -> None:
    summary, incidents = _read(1.6)
    snapshot = build_snapshot(
        now=NOW,
        gateway=None,
        drift=None,
        disk=summary,
        capacity=CapacitySummary(),
        sources=(),
        incidents=incidents,
    )
    lines = render_dashboard(snapshot, now=NOW, zone=UTC, period_seconds=300).splitlines()

    assert snapshot.overall == "warning"
    attention = lines.index("## 🛠 Needs attention")
    assert lines[attention + 1] == "- Disk: 1.6 GB free of 80 (2%)"
    assert "> ⚠️ Disk: 1.6 GB free of 80" in lines


def test_a_disk_not_read_says_why_in_the_details() -> None:
    lines = _render(DiskSummary("unknown", detail="not read (PermissionError)")).splitlines()

    assert "> Disk: not read (PermissionError)" in lines
    assert not any(line.startswith("## 🛠") for line in lines)


def test_a_snapshot_without_the_disk_has_no_disk_line() -> None:
    assert "Disk" not in _render(None)


def test_the_disk_line_follows_backup_and_drift_in_their_group() -> None:
    text = _render(
        *_read(61.4),
        backup=BackupSummary("ok", "2026-11-09T06:00:00+00:00", integrity="ok"),
        drift=DriftSummary("clean", 0, 481, "2026-11-09T08:00:00+00:00"),
    )
    lines = text.splitlines()

    drift = lines.index("> ✓ Drift 0 of 481 · checked 08:00")
    assert lines[drift - 1] == "> ✓ Backup Nov 9 06:00 · 6 h ago · integrity ok"
    assert lines[drift + 1] == "> Disk: 61 GB free of 80"


def test_the_disk_event_keeps_its_place_behind_five_others() -> None:
    others = tuple(
        Incident(f"cron:run:j{number}", "warning", f"Cron failed: job {number}")
        for number in range(6)
    )
    _, low = _read(1.6)

    snapshot = build_snapshot(
        now=NOW,
        gateway=None,
        drift=None,
        disk=DiskSummary("low", 1_600_000_000, 80_000_000_000),
        capacity=CapacitySummary(),
        sources=(),
        incidents=(*others, *low),
    )

    assert len(snapshot.incidents) == 5
    assert snapshot.incidents[-1] == low[0]


# ----------------------------------------------------------------------------- the ticks


class Runner:
    def run(self, argv: Any, *, timeout_seconds: float) -> CommandResult:
        return CommandResult(0, "", "")


def test_the_plugin_s_tick_reads_the_disk_and_counts_no_source(tmp_path: Path) -> None:
    partition = _Partition(1.6)
    env = Environment(hermes_home=tmp_path, limits_enabled=False)

    snapshot = asyncio.run(collect_all_async(env, Runner(), now=NOW, disk_usage=partition))

    assert partition.asked == [tmp_path]
    assert snapshot.disk == DiskSummary("low", 1_600_000_000, 80_000_000_000)
    assert "disk:low" in [incident.incident_id for incident in snapshot.incidents]
    assert "disk" not in [source.name for source in snapshot.sources]


def test_the_cron_tick_reads_the_disk_too(tmp_path: Path) -> None:
    env = Environment(hermes_home=tmp_path, limits_enabled=False, disk_low_gb=70)

    snapshot = collect_all(env, Runner(), now=NOW, disk_usage=_Partition(61.4))

    assert snapshot.disk is not None and snapshot.disk.state == "low"
    assert "disk:low" in [incident.incident_id for incident in snapshot.incidents]


def test_a_surprise_from_the_call_is_a_reason_and_one_event_not_a_dead_tick(
    tmp_path: Path,
) -> None:
    def broken(path: Path) -> Usage:
        raise RuntimeError("/home/hermes/.hermes is odd")

    env = Environment(hermes_home=tmp_path, limits_enabled=False)

    snapshot = asyncio.run(collect_all_async(env, Runner(), now=NOW, disk_usage=broken))

    assert snapshot.disk == DiskSummary("unknown", detail="collector crashed: RuntimeError")
    assert "collector:disk" in [incident.incident_id for incident in snapshot.incidents]


def test_the_real_call_answers_for_the_profile_home(tmp_path: Path) -> None:
    summary, _ = disk.read_disk(Environment(hermes_home=tmp_path))

    assert summary.state in ("ok", "low")
    assert summary.total_bytes and summary.free_bytes is not None
    assert 0 <= summary.free_bytes <= summary.total_bytes


# ----------------------------------------------------------------------------- the settings


def test_the_plugin_reads_the_floors_from_its_settings_or_the_environment(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from probe_fakes import CHAT, FakeContext, load_plugin

    plugin = load_plugin()
    for name in (
        "HERMES_DASHBOARD_PROBE_CHAT",
        "HERMES_DASHBOARD_PROBE_DISK_LOW_PERCENT",
        "HERMES_DASHBOARD_PROBE_DISK_LOW_GB",
    ):
        monkeypatch.delenv(name, raising=False)
    base = {"chat_id": CHAT, "hermes_home": str(tmp_path), "limits_enabled": False}

    def floors(**overrides: Any) -> tuple[float, float]:
        settings = plugin.read_settings(FakeContext({**base, **overrides}))
        assert settings is not None
        return settings.disk_low_percent, settings.disk_low_gb

    assert floors() == (10.0, 2.0)
    assert floors(disk_low_percent=5, disk_low_gb=20) == (5.0, 20.0)
    assert floors(disk_low_percent="lots") == (10.0, 2.0)
    monkeypatch.setenv("HERMES_DASHBOARD_PROBE_DISK_LOW_PERCENT", "15")
    monkeypatch.setenv("HERMES_DASHBOARD_PROBE_DISK_LOW_GB", "0")
    assert floors() == (15.0, 0.0)
    assert floors(disk_low_percent=5) == (5.0, 0.0)  # the config entry first


def test_the_plugin_hands_the_floors_to_the_collector(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from probe_fakes import CHAT, FakeContext, load_plugin

    plugin = load_plugin()
    ctx = FakeContext(
        {
            "chat_id": CHAT,
            "hermes_home": str(tmp_path),
            "limits_enabled": False,
            "disk_low_percent": 12,
            "disk_low_gb": 3,
        }
    )
    runtime = plugin.register(ctx)
    assert runtime is not None
    seen: list[Any] = []

    async def spy(env: Any, *args: Any, **kwargs: Any) -> Any:
        seen.append(env)
        return "snapshot"

    monkeypatch.setattr(runtime.dashboard.collect, "collect_all_async", spy)

    assert asyncio.run(runtime._collect(NOW)) == "snapshot"
    assert (seen[0].disk_low_percent, seen[0].disk_low_gb) == (12.0, 3.0)


def test_the_plugin_s_defaults_are_the_package_s() -> None:
    from probe_fakes import load_plugin

    from telegram_dashboard import compat

    plugin = load_plugin()

    assert plugin.DEFAULT_DISK_LOW_PERCENT == compat.DEFAULT_DISK_LOW_PERCENT
    assert plugin.DEFAULT_DISK_LOW_GB == compat.DEFAULT_DISK_LOW_GB


def test_the_cron_config_carries_the_floors_too(tmp_path: Path) -> None:
    from telegram_dashboard.__main__ import _environment

    env = _environment({"hermes_home": str(tmp_path), "disk_low_percent": 5, "disk_low_gb": 1.5})
    default = _environment({"hermes_home": str(tmp_path), "disk_low_gb": "much"})

    assert (env.disk_low_percent, env.disk_low_gb) == (5.0, 1.5)
    assert (default.disk_low_percent, default.disk_low_gb) == (10.0, 2.0)
