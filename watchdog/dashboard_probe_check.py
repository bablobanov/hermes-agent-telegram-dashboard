"""Cron watchdog for the probe plugin: the ``--no-agent --script`` shape of Hermes cron.

Hermes runs it as ``python <path>`` on a schedule and delivers stdout verbatim; empty stdout is
silence. So: nothing when the message is confirmed, an alert otherwise, exit 0 either way. A
non-zero exit is reserved for the watchdog itself being broken (config or package missing): the
scheduler then raises its own "script failed" alert, and a broken watchdog never passes for a
healthy one.

Cron passes no arguments, so the config is the JSON file next to this script
(``dashboard_probe_check.json``); a path as the first argument serves manual runs and fixtures.
``--verbose`` prints the state line even when confirmed.

Repeat policy. The durable state lives in the message itself, whose freshness stamp is what the
dashboard is for; an alert is an interruption, not an indicator, and twelve an hour teach the
reader to stop reading the watchdog. Hence ``confirmed`` is silence; ``lagging`` and ``stale``
speak on the first tick past the confirmation threshold and then once an hour; ``never`` and
``lost`` speak every tick, because there is no message to carry the state and silence there
would be indistinguishable from health. A stamp with no usable age (unreadable, dated in the
future) has no anchor to count from and is treated the same way.

The repeat is stateless, counted from the confirmation stamp, so the watchdog stays read-only:
tick index = ceil(over / interval) - 1, speak when index % (3600 / interval) == 0. Edge by
construction: the hourly window is exactly one tick wide, so a tick the scheduler skipped
swallows that hour's alert and the next hour speaks again. Not a threshold bug.

The package next to this file is a cut, not the whole thing: ``telegram_dashboard/`` with the
modules in ``PACKAGE_MODULES`` only. ``tests/test_watchdog_script.py`` runs this script against a
directory holding exactly that cut, so an import creeping in breaks the test, not production.

Coverage boundary: the job runs inside the same gateway as the plugin and alerts through the same
channel, so it covers "plugin dead, gateway alive" and not "gateway dead": there both are silent,
and that silence looks like health.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
CONFIG_NAME = "dashboard_probe_check.json"
PACKAGE_MODULES = ("__init__.py", "schema.py", "freshness.py", "timeparse.py")
DEFAULT_PERIOD_SECONDS = 300
DEFAULT_INTERVAL_SECONDS = 300
REPEAT_SECONDS = 3600
EXIT_BROKEN = 2

sys.path.insert(0, str(HERE))
try:
    from telegram_dashboard.freshness import (
        DeliveryRecord,
        MessageFreshness,
        classify_message_freshness,
        confirmation_threshold_seconds,
        load_plugin_record,
        message_age_seconds,
        message_banner,
    )
except ImportError as exc:
    print(
        f"dashboard probe watchdog broken: telegram_dashboard not importable next to {HERE}: {exc}",
        file=sys.stderr,
    )
    sys.exit(EXIT_BROKEN)


class WatchdogBroken(Exception):
    """The watchdog cannot judge anything; exit non-zero so the scheduler alerts about it."""


@dataclass(frozen=True)
class Settings:
    plugin_state_path: Path
    period_seconds: int
    interval_seconds: int


def load_settings(path: Path) -> Settings:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise WatchdogBroken(f"config missing: {path}") from exc
    except (OSError, ValueError, UnicodeDecodeError) as exc:
        raise WatchdogBroken(f"config unreadable ({type(exc).__name__}): {path}") from exc
    if not isinstance(payload, dict):
        raise WatchdogBroken(f"config is not a JSON object: {path}")
    state = payload.get("plugin_state_path")
    if not isinstance(state, str) or not state:
        raise WatchdogBroken(f"config has no plugin_state_path: {path}")
    return Settings(
        plugin_state_path=Path(state).expanduser(),
        period_seconds=_positive_int(payload, "period_seconds", DEFAULT_PERIOD_SECONDS),
        interval_seconds=_positive_int(payload, "check_interval_seconds", DEFAULT_INTERVAL_SECONDS),
    )


def _positive_int(payload: dict[str, Any], key: str, default: int) -> int:
    """A misconfigured watchdog is a broken one: no silent defaults for a bad number."""
    raw = payload.get(key, default)
    if (
        isinstance(raw, bool)
        or not isinstance(raw, (int, float))
        or not math.isfinite(raw)
        or raw <= 0
    ):
        raise WatchdogBroken(f"config key {key} must be a positive number, got {raw!r}")
    return int(raw)


def should_speak(
    freshness: MessageFreshness, over_seconds: float | None, *, interval_seconds: int
) -> bool:
    """The repeat policy. ``over_seconds`` is how far past the confirmation threshold the stamp is;
    ``None`` when there is no usable stamp to count from."""
    if freshness == "confirmed":
        return False
    if freshness in ("never", "lost") or over_seconds is None or over_seconds <= 0:
        return True
    ticks_per_repeat = max(1, round(REPEAT_SECONDS / interval_seconds))
    index = math.ceil(over_seconds / interval_seconds) - 1
    return index % ticks_per_repeat == 0


def state_line(freshness: MessageFreshness, record: DeliveryRecord, *, source: str) -> str:
    """Same shape as ``python -m telegram_dashboard --check`` prints."""
    return (
        f"{freshness} source={source} message_id={record.message_id} "
        f"confirmed={record.last_confirmed_at} error={record.last_error}"
    )


def alert(
    freshness: MessageFreshness,
    record: DeliveryRecord,
    *,
    now: datetime,
    period_seconds: int,
    source: str,
) -> str:
    banner = message_banner(freshness, record, now=now, period_seconds=period_seconds) or freshness
    return f"DASHBOARD PROBE ALERT: {freshness}\n{banner}\n" + state_line(
        freshness, record, source=source
    )


def main(argv: list[str] | None = None, *, now: datetime | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="freshness watchdog for the dashboard probe plugin"
    )
    parser.add_argument("config", nargs="?", type=Path, default=HERE / CONFIG_NAME)
    parser.add_argument(
        "--verbose", action="store_true", help="print the state line even when silent"
    )
    args = parser.parse_args(argv)
    try:
        settings = load_settings(args.config)
    except WatchdogBroken as exc:
        print(f"dashboard probe watchdog broken: {exc}", file=sys.stderr)
        return EXIT_BROKEN

    moment = now or datetime.now(UTC)
    record = load_plugin_record(settings.plugin_state_path)
    freshness = classify_message_freshness(
        record, now=moment, period_seconds=settings.period_seconds
    )
    age = message_age_seconds(record, now=moment)
    over = None if age is None else age - confirmation_threshold_seconds(settings.period_seconds)
    source = f"plugin-state:{settings.plugin_state_path.name}"
    if should_speak(freshness, over, interval_seconds=settings.interval_seconds):
        print(
            alert(
                freshness, record, now=moment, period_seconds=settings.period_seconds, source=source
            )
        )
    elif args.verbose:
        print(state_line(freshness, record, source=source))
    return 0


if __name__ == "__main__":
    sys.exit(main())
