"""Entry point for the cron tick: collect, render, deliver. Also ``--check`` and ``--demo``.

Configuration is a JSON file; the bot token is read from the environment variable named in it.
No chat ids, topic ids, paths or tokens live in code.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from datetime import UTC, datetime, tzinfo
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .collect import SubprocessRunner, collect_all
from .compat import Environment
from .delivery import DeliveryStore, deliver
from .freshness import check_exit_code, classify_message_freshness, load_plugin_record
from .render import render_dashboard, to_telegram_html
from .states import NOW, PERIOD_SECONDS, all_states

_DEFAULT_TOKEN_ENV = "HERMES_DASHBOARD_BOT_TOKEN"
logger = logging.getLogger("telegram_dashboard")


def _load_config(path: Path) -> dict[str, object]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("config must be a JSON object")
    return payload


def _environment(config: dict[str, object]) -> Environment:
    home = config.get("hermes_home") or os.environ.get("HERMES_HOME") or "~/.hermes"
    drift_command = config.get("drift_command")
    drift_report = config.get("drift_report")
    backup_status = config.get("backup_status")
    return Environment(
        hermes_home=Path(str(home)).expanduser(),
        drift_command=tuple(str(part) for part in drift_command)
        if isinstance(drift_command, list)
        else None,
        drift_report=Path(str(drift_report)).expanduser()
        if isinstance(drift_report, str)
        else None,
        limits_enabled=bool(config.get("limits_enabled", True)),
        backup_status=Path(str(backup_status)).expanduser()
        if isinstance(backup_status, str) and backup_status
        else None,
    )


def _zone(config: dict[str, object]) -> tzinfo:
    name = config.get("display_timezone")
    if not isinstance(name, str) or not name or name.upper() == "UTC":
        return UTC
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        logger.warning("display_timezone %r unknown, using UTC", name)
        return UTC


def _period(config: dict[str, object]) -> int:
    raw = config.get("period_seconds", 300)
    return int(raw) if isinstance(raw, (int, float)) and raw > 0 else 300


def run_tick(config: dict[str, object], *, dry_run: bool) -> int:
    now = datetime.now(UTC)
    env = _environment(config)
    period = _period(config)
    store = DeliveryStore(
        Path(str(config.get("state_path", "dashboard-delivery.json"))).expanduser()
    )
    record = store.load()
    snapshot = collect_all(env, SubprocessRunner(), now=now)
    text = render_dashboard(
        snapshot, now=now, delivery=record, period_seconds=period, zone=_zone(config)
    )
    if dry_run:
        print(text)
        return 0
    token = os.environ.get(str(config.get("token_env", _DEFAULT_TOKEN_ENV)), "")
    if not token:
        logger.error("bot token env var is empty; nothing delivered")
        return 2
    from .telegram_api import BotApiTransport

    chat_id = str(config.get("chat_id") or "")
    thread = config.get("thread_id")
    if not chat_id:
        logger.error("chat_id missing in config; nothing delivered")
        return 2
    outcome = deliver(
        to_telegram_html(text),
        transport=BotApiTransport(token),
        record=record,
        chat_id=chat_id,
        thread_id=str(thread) if thread not in (None, "") else None,
        now=now,
        recreate_on_loss=bool(config.get("recreate_on_loss", True)),
        log=logger,
    )
    store.save(outcome.record)
    logger.info("delivery: %s %s", outcome.status, outcome.detail)
    return 0 if outcome.status in ("created", "edited", "unchanged", "recreated") else 1


def run_check(config: dict[str, object]) -> int:
    """Freshness of the pinned message for an external watchdog; exit 0/1/2.

    ``plugin_state_path`` points at the engine's PluginState file of the probe plugin
    (``<HERMES_HOME>/plugin-data/<namespace>/state.json``); without it the cron path's own
    delivery record (``state_path``) is read. The two paths never write the same file.
    """
    plugin_state = config.get("plugin_state_path")
    if isinstance(plugin_state, str) and plugin_state:
        path = Path(plugin_state).expanduser()
        record = load_plugin_record(path)
        source = f"plugin-state:{path.name}"
    else:
        store = DeliveryStore(
            Path(str(config.get("state_path", "dashboard-delivery.json"))).expanduser()
        )
        record = store.load()
        source = f"delivery-record:{store.path.name}"
    freshness = classify_message_freshness(
        record, now=datetime.now(UTC), period_seconds=_period(config)
    )
    print(
        f"{freshness} source={source} message_id={record.message_id} "
        f"confirmed={record.last_confirmed_at} error={record.last_error}"
    )
    return check_exit_code(freshness)


def run_demo(number: int) -> int:
    for state in all_states():
        if state.number == number:
            print(f"[{state.number}] {state.title}\n")
            print(
                render_dashboard(
                    state.snapshot, now=NOW, delivery=state.delivery, period_seconds=PERIOD_SECONDS
                )
            )
            return 0
    print(f"no state {number}; available 1..{len(all_states())}", file=sys.stderr)
    return 2


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="telegram_dashboard")
    parser.add_argument("--config", type=Path, help="JSON config file")
    parser.add_argument("--dry-run", action="store_true", help="collect and print, do not deliver")
    parser.add_argument("--check", action="store_true", help="report message freshness; exit 0/1/2")
    parser.add_argument("--demo", type=int, metavar="N", help="render static verification state N")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args(argv)
    # Windows consoles default to a legacy code page; the message is Cyrillic with emoji.
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            reconfigure(encoding="utf-8", errors="replace")
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )
    if args.demo is not None:
        return run_demo(args.demo)
    if args.config is None:
        parser.error("--config is required unless --demo is given")
    try:
        config = _load_config(args.config)
    except (OSError, ValueError) as exc:
        logger.error("config unreadable: %s", type(exc).__name__)
        return 2
    if args.check:
        return run_check(config)
    return run_tick(config, dry_run=args.dry_run)


if __name__ == "__main__":
    sys.exit(main())
