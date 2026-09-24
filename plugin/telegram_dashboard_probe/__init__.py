"""The dashboard as a gateway plugin: one pinned message, edited in place on every tick.

``register(ctx)`` → ``ctx.register_platform_handler("telegram", wire)`` → the Telegram adapter
calls ``wire(native, adapter)`` from its ``connect()`` → ``wire`` spawns ONE supervised task via
``ctx.spawn_task`` → every tick composes the first screen (collect → render → plain text) and
edits the same message through ``adapter.edit_message``.

Two traps this plugin is built around (found by reading 0.21.1, see the report):

* ``connect()`` runs the factory again on every reconnect with a NEW adapter instance, so a naive
  plugin would spawn a second loop each time. The guard keeps the first task and only records the
  new adapter generation.
* ``TelegramAdapter.edit_message`` does not forward to the replacement adapter after a reconnect
  (``send`` does). A loop holding the adapter it was born with would answer "Not connected"
  forever while looking alive. Each tick therefore asks ``runner.adapters`` for the current one
  and only uses an adapter that reports ``is_connected``.

Also: the factory fires BEFORE the adapter finishes ``initialize()``/``start()``, so the first
tick waits for ``is_connected`` instead of sending into a bot that is not initialised yet.

The screen is plain text on purpose: ``edit_message`` without ``finalize`` sets no parse mode,
and its MarkdownV2 path splits an over-long payload into NEW messages, which a pinned dashboard
must never do. Whatever happens while composing, the message is edited: a screen that could not
be built is replaced by a loud one-screen notice with the time, never left as yesterday's text.

The ``telegram_dashboard`` package is looked up beside this file first (the deployment copies
both directories into the same plugin folder), then in the interpreter. Settings come from the
plugin's config entry (``plugins.entries.telegram_dashboard_probe.settings``) or environment
variables; no chat id, path or token lives in code. Everything is fail-open: a failed tick is
logged and the next one runs.
"""

from __future__ import annotations

import asyncio
import importlib
import logging
import math
import os
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, tzinfo
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

logger = logging.getLogger("hermes.plugins.telegram_dashboard_probe")

PLATFORM = "telegram"
TASK_NAME = "telegram_dashboard_probe:tick"
STATE_KEY = "probe"
DEFAULT_PERIOD_SECONDS = 60.0
# float("inf") and float("nan") are valid floats; sleep(inf) parks the loop forever with the
# old message on screen. Anything non-finite falls back, the rest is clamped to this range.
MIN_PERIOD_SECONDS = 0.01
MAX_PERIOD_SECONDS = 86_400.0
DEFAULT_HERMES_HOME = "~/.hermes"
ENV_CHAT = "HERMES_DASHBOARD_PROBE_CHAT"
ENV_THREAD = "HERMES_DASHBOARD_PROBE_THREAD"
ENV_PERIOD = "HERMES_DASHBOARD_PROBE_PERIOD"
ENV_HOME = "HERMES_HOME"  # the engine's own variable; the cron path reads the same one
ENV_DRIFT_REPORT = "HERMES_DASHBOARD_PROBE_DRIFT_REPORT"
ENV_LIMITS = "HERMES_DASHBOARD_PROBE_LIMITS"
ENV_LIMITS_REFRESH = "HERMES_DASHBOARD_PROBE_LIMITS_REFRESH"
ENV_TZ = "HERMES_DASHBOARD_PROBE_TZ"
ENV_BACKUP_STATUS = "HERMES_DASHBOARD_PROBE_BACKUP_STATUS"
# The Grok and Kimi quotas are asked at most once per this many seconds each, not every tick:
# a weekly number does not move faster, and the surfaces are not documented ones. The caches
# live in the record, one per provider (``limits_cache.grok``, ``limits_cache.kimi``).
DEFAULT_LIMITS_REFRESH_SECONDS = 900.0
MIN_LIMITS_REFRESH_SECONDS = 60.0
LIMITS_CACHE_KEY = "limits_cache"
QUOTA_CACHE_PROVIDERS = ("grok", "kimi")
_FALSE_WORDS = frozenset({"0", "false", "no", "off"})
# Bot API wording for "the message you want to edit is gone"; anything else keeps the id.
LOST_MARKERS = ("message to edit not found", "message can't be edited", "message_id_invalid")
DASHBOARD_MODULES = ("collect", "compat", "freshness", "render")
# What the tick calls; a copy of the package that lacks any of it is refused at import time.
REQUIRED_API: dict[str, tuple[str, ...]] = {
    "collect": ("collect_all_async", "SubprocessRunner", "Flights"),
    "compat": ("Environment",),
    "freshness": ("record_from_plugin_state",),
    "render": ("render_dashboard", "to_telegram_plain"),
}

Collector = Callable[[datetime], Awaitable[Any]]


@dataclass(frozen=True)
class Settings:
    chat_id: str
    thread_id: str | None
    period_seconds: float
    hermes_home: Path
    drift_report: Path | None
    drift_command: tuple[str, ...] | None
    limits_enabled: bool
    limits_refresh_seconds: float
    display_timezone: str
    backup_status: Path | None


@dataclass(frozen=True)
class Dashboard:
    """The package's modules the tick needs, wherever they were imported from."""

    collect: Any
    compat: Any
    freshness: Any
    render: Any
    origin: str


def read_settings(ctx: Any) -> Settings | None:
    """Plugin config entry first, environment second. ``None`` when there is no chat to write to."""
    chat = _setting(ctx, "chat_id", ENV_CHAT)
    if not chat:
        return None
    thread = _setting(ctx, "thread_id", ENV_THREAD) or None
    report = _setting(ctx, "drift_report", ENV_DRIFT_REPORT)
    limits = _setting(ctx, "limits_enabled", ENV_LIMITS)
    backup_status = _setting(ctx, "backup_status", ENV_BACKUP_STATUS)
    return Settings(
        chat_id=str(chat),
        thread_id=str(thread) if thread else None,
        period_seconds=_period(_setting(ctx, "period_seconds", ENV_PERIOD)),
        hermes_home=_home(ctx),
        drift_report=Path(report).expanduser() if report else None,
        drift_command=_argv(_raw_setting(ctx, "drift_command")),
        limits_enabled=limits.lower() not in _FALSE_WORDS,
        limits_refresh_seconds=_refresh(
            _setting(ctx, "limits_refresh_seconds", ENV_LIMITS_REFRESH)
        ),
        display_timezone=_setting(ctx, "display_timezone", ENV_TZ) or "UTC",
        backup_status=Path(backup_status).expanduser() if backup_status else None,
    )


def _home(ctx: Any) -> Path:
    """The profile home: the plugin's setting, else derived from its own state file
    (``<home>/plugin-data/<namespace>/state.json``, always the running profile), else the
    engine's ``HERMES_HOME`` variable (set in-process only for ``-p profile``), else
    ``~/.hermes``."""
    configured = _raw_setting(ctx, "hermes_home")
    if isinstance(configured, str) and configured.strip():
        return Path(configured.strip()).expanduser()
    state_path = getattr(getattr(ctx, "state", None), "path", None)
    if isinstance(state_path, (str, Path)):
        parents = Path(state_path).parents
        if len(parents) >= 3 and parents[1].name == "plugin-data":
            return parents[2]
    return Path(os.environ.get(ENV_HOME, "").strip() or DEFAULT_HERMES_HOME).expanduser()


def display_zone(settings: Settings) -> tzinfo:
    """The configured zone; an unknown name degrades to UTC with a warning, never raises."""
    name = settings.display_timezone
    if not name or name.upper() == "UTC":
        return UTC
    try:
        return ZoneInfo(name)
    except Exception as exc:
        # ZoneInfoNotFoundError for an unknown key; OSError for a directory name ("Europe")
        # where tzdata is a package. A typo here must not stop the plugin from loading.
        logger.warning(
            "probe: display_timezone %r unusable (%s); using UTC", name, type(exc).__name__
        )
        return UTC


def _period(raw: str) -> float:
    """A finite period inside [MIN, MAX]; unreadable or non-finite input uses the default."""
    if not raw:
        return DEFAULT_PERIOD_SECONDS
    try:
        period = float(raw)
    except ValueError:
        logger.warning(
            "probe: period_seconds %r unreadable; using %.0fs", raw, DEFAULT_PERIOD_SECONDS
        )
        return DEFAULT_PERIOD_SECONDS
    if not math.isfinite(period):
        logger.warning(
            "probe: period_seconds %r not finite; using %.0fs", raw, DEFAULT_PERIOD_SECONDS
        )
        return DEFAULT_PERIOD_SECONDS
    clamped = min(max(period, MIN_PERIOD_SECONDS), MAX_PERIOD_SECONDS)
    if clamped != period:
        logger.warning("probe: period_seconds %r clamped to %.2fs", raw, clamped)
    return clamped


def _refresh(raw: str) -> float:
    """A finite refresh interval of at least MIN; unreadable input uses the default."""
    if not raw:
        return DEFAULT_LIMITS_REFRESH_SECONDS
    try:
        seconds = float(raw)
    except ValueError:
        seconds = math.nan
    if not math.isfinite(seconds):
        logger.warning(
            "probe: limits_refresh_seconds %r unusable; using %.0fs",
            raw,
            DEFAULT_LIMITS_REFRESH_SECONDS,
        )
        return DEFAULT_LIMITS_REFRESH_SECONDS
    clamped = max(seconds, MIN_LIMITS_REFRESH_SECONDS)
    if clamped != seconds:
        logger.warning("probe: limits_refresh_seconds %r clamped to %.0fs", raw, clamped)
    return clamped


def _argv(raw: object) -> tuple[str, ...] | None:
    """A command as a list of strings in the config entry; anything else is "not configured"."""
    if isinstance(raw, (list, tuple)) and raw and all(isinstance(part, str) for part in raw):
        return tuple(raw)
    if raw not in (None, ""):
        logger.warning("probe: drift_command must be a list of strings; ignoring %r", type(raw))
    return None


def _raw_setting(ctx: Any, key: str) -> object:
    getter = getattr(ctx, "get_config", None)
    if not callable(getter):
        return None
    try:
        return getter(key)
    except Exception as exc:  # config unreadable must not kill the plugin
        logger.warning("probe: reading setting %s failed: %s", key, type(exc).__name__)
        return None


def _setting(ctx: Any, key: str, env_name: str) -> str:
    value = _raw_setting(ctx, key)
    if value in (None, ""):
        value = os.environ.get(env_name, "")
    return str(value).strip()


def import_dashboard() -> Dashboard | None:
    """``telegram_dashboard`` beside the plugin, else the interpreter's own; ``None`` for neither.

    Never raises: a missing or broken package is announced on the screen, not in a traceback.
    """
    candidates: list[tuple[str, str]] = []
    if __package__:
        candidates.append(("vendored", f"{__package__}.telegram_dashboard"))
    candidates.append(("installed", "telegram_dashboard"))
    for origin, base in candidates:
        try:
            modules = {
                name: importlib.import_module(f"{base}.{name}") for name in DASHBOARD_MODULES
            }
        except ModuleNotFoundError as exc:
            # The package itself is absent: expected for one of the two candidates. A module
            # missing INSIDE a present package is a broken copy and deserves a warning.
            level = logging.INFO if str(exc.name or "").startswith(base) else logging.WARNING
            logger.log(level, "probe: %s package not importable: %s", origin, exc.name)
            continue
        except Exception as exc:
            logger.warning("probe: %s package broken: %s", origin, type(exc).__name__)
            continue
        missing = [
            f"{name}.{attr}"
            for name, attrs in REQUIRED_API.items()
            for attr in attrs
            if not hasattr(modules[name], attr)
        ]
        if missing:
            # An older copy: refused here, once, instead of AttributeError on every tick.
            logger.warning("probe: %s package lacks %s; skipped", origin, ", ".join(missing))
            continue
        logger.info("probe: dashboard package from %s (%s)", origin, base)
        return Dashboard(origin=origin, **modules)
    logger.error("probe: telegram_dashboard package not importable; the screen cannot be built")
    return None


class ProbeRuntime:
    """One per ``register()``; survives adapter replacement, owns exactly one task."""

    def __init__(self, ctx: Any, settings: Settings, dashboard: Dashboard | None) -> None:
        self._ctx = ctx
        self.settings = settings
        self.dashboard = dashboard
        self.zone = display_zone(settings)
        self.collector: Collector = self._collect
        # One worker per source across ticks: a source abandoned by its deadline is not
        # started again until it returns (collect.Flights).
        self.flights: Any = dashboard.collect.Flights() if dashboard is not None else None
        self.task: asyncio.Task[None] | None = None
        self.latest: Any = None
        self.generation = 0
        self._generations: dict[int, int] = {}
        self.ticks = 0
        self.record: dict[str, Any] = {}

    # ------------------------------------------------------------------ factory (connect-time)

    def wire(self, native: Any, adapter: Any) -> None:
        """Called by the adapter's ``connect()``; a reconnect calls it AGAIN with a new adapter."""
        self.generation += 1
        self.latest = adapter
        self._generations[id(adapter)] = self.generation
        if self.task is not None and not self.task.done():
            logger.warning(
                "probe: adapter generation %d wired while the loop runs; keeping one loop",
                self.generation,
            )
            return
        self.task = self._ctx.spawn_task(self.run(), name=TASK_NAME)
        logger.info("probe: tick loop spawned on adapter generation %d", self.generation)

    # ------------------------------------------------------------------ resolution

    def live_adapter(self) -> Any | None:
        """The adapter the runner publishes now, else the one last handed to us; connected only."""
        latest = self.latest
        runner = getattr(latest, "gateway_runner", None)
        adapters = getattr(runner, "adapters", None)
        candidates: list[Any] = []
        if isinstance(adapters, dict):
            candidates.append(adapters.get(getattr(latest, "platform", None)))
        candidates.append(latest)
        for candidate in candidates:
            if candidate is not None and bool(getattr(candidate, "is_connected", False)):
                return candidate
        return None

    def generation_of(self, adapter: Any) -> int:
        return self._generations.get(id(adapter), 0)

    # ------------------------------------------------------------------ loop

    async def run(self) -> None:
        self._load_record()
        loop = asyncio.get_running_loop()
        while True:
            started = loop.time()
            try:
                await self.tick()
            except (asyncio.CancelledError, KeyboardInterrupt):
                raise
            except BaseException as exc:
                # BaseException, not Exception: SystemExit from a worker thread comes back
                # through to_thread and must not take the loop down with the old text pinned.
                # The record must say it: a loop that only logs leaves last_status=edited
                # on disk while every tick dies, and a watchdog reading the state sees
                # a healthy updater. Class name only: the message may carry chat ids.
                logger.exception("probe: tick failed; next tick continues")
                self._note(status="tick_failed", error=type(exc).__name__)
            # Fixed cadence: sleep what is left of the period, not the whole period. Otherwise a
            # slow collector pushes every confirmation past the freshness threshold and the
            # message banners "lagging" on a healthy system.
            elapsed = loop.time() - started
            await asyncio.sleep(max(0.0, self.settings.period_seconds - elapsed))

    async def tick(self) -> None:
        if self.live_adapter() is None:
            # Composing costs a facade call; not worth it while nothing can be delivered.
            self._note(status="waiting", error="no connected Telegram adapter")
            return
        self.ticks += 1
        now = datetime.now(UTC)
        text = await self.compose(now)
        live = self.live_adapter()
        if live is None:
            self._note(status="waiting", error="no connected Telegram adapter")
            return
        generation = self.generation_of(live)
        chat = self.settings.chat_id
        message_id = self.record.get("message_id")
        if message_id and self.record.get("chat_id") == chat:
            result = await live.edit_message(chat, str(message_id), text)
            if result.success:
                self._note(status="edited", error=None, generation=generation, confirmed=True)
                return
            error = str(result.error or "")
            if any(marker in error.lower() for marker in LOST_MARKERS):
                logger.error("probe: message %s lost (%s); sending a new one", message_id, error)
                self.record["lost_at"] = now.isoformat()
                self.record["message_id"] = None
            else:
                self._note(status="edit_failed", error=error, generation=generation)
                return
        metadata = {"thread_id": self.settings.thread_id} if self.settings.thread_id else None
        result = await live.send(chat, text, metadata=metadata)
        if result.success and result.message_id:
            if self.record.get("lost_at"):
                self.record["recreated_at"] = now.isoformat()
            self.record["message_id"] = str(result.message_id)
            self.record["chat_id"] = chat
            self.record["thread_id"] = self.settings.thread_id
            self._note(status="created", error=None, generation=generation, confirmed=True)
            return
        self._note(
            status="send_failed", error=str(result.error or "no message id"), generation=generation
        )

    # ------------------------------------------------------------------ the screen

    async def compose(self, now: datetime) -> str:
        """The first screen as plain text, or a loud one-screen notice. Never raises."""
        dashboard = self.dashboard
        if dashboard is None:
            self.record["last_render_error"] = "ImportError"
            return self._notice(now, "пакет telegram_dashboard не импортируется, экран не собран")
        try:
            snapshot = await self.collector(now)
            record = dashboard.freshness.record_from_plugin_state(
                {STATE_KEY: self._record_as_delivered(now)}
            )
            text = dashboard.render.render_dashboard(
                snapshot,
                now=now,
                delivery=record,
                period_seconds=self._render_period(),
                zone=self.zone,
            )
            plain: str = dashboard.render.to_telegram_plain(text)
        except (asyncio.CancelledError, KeyboardInterrupt):
            raise
        except BaseException as exc:
            # Class name only: the text of an exception may carry chat ids or paths. One
            # traceback per failure class, then one line per tick: a persistent failure must
            # not fill the journal with the same traceback every minute.
            reason = type(exc).__name__
            if self.record.get("last_render_error") == reason:
                logger.warning("probe: screen still not composed (%s); notice sent again", reason)
            else:
                logger.exception("probe: screen not composed; the message gets a notice instead")
            self.record["last_render_error"] = reason
            return self._notice(now, f"экран не собран: {reason}")
        self.record["last_render_error"] = None
        return plain

    def _record_as_delivered(self, now: datetime) -> dict[str, Any]:
        """The record as it will be once this text lands.

        The text is composed before delivery. When the message was lost and this tick will send
        a new one, any text that lands IS the recreation: the banner must say "создано заново",
        not "НЕ восстановлено" for a whole period on the very message that restored it.
        """
        record = dict(self.record)
        chat = self.settings.chat_id
        will_send = not record.get("message_id") or record.get("chat_id") != chat
        if will_send and record.get("lost_at"):
            record["recreated_at"] = now.isoformat()
        return record

    async def _collect(self, now: datetime) -> Any:
        dashboard = self.dashboard
        assert dashboard is not None  # compose() checks before calling
        env = dashboard.compat.Environment(
            hermes_home=self.settings.hermes_home,
            drift_command=self.settings.drift_command,
            drift_report=self.settings.drift_report,
            limits_enabled=self.settings.limits_enabled,
            backup_status=self.settings.backup_status,
        )
        runner = dashboard.collect.SubprocessRunner()
        caches = self.quota_caches()
        return await dashboard.collect.collect_all_async(
            env,
            runner,
            now=now,
            flights=self.flights,
            grok_cache=caches["grok"],
            grok_interval_seconds=self.settings.limits_refresh_seconds,
            kimi_cache=caches["kimi"],
            kimi_interval_seconds=self.settings.limits_refresh_seconds,
        )

    def quota_caches(self) -> dict[str, dict[str, Any]]:
        """One cache dict per provider inside the record's ``limits_cache``, the same objects on
        every tick so the collector's writes land in the record.

        The layout before Kimi kept Grok's attempt at the top level (``attempted_at``, ``item``);
        a record in that shape is moved under ``grok`` once, so a restart keeps its interval.
        """
        cache = self.record.get(LIMITS_CACHE_KEY)
        if not isinstance(cache, dict):
            cache = {}
        if "attempted_at" in cache or "item" in cache:
            cache = {"grok": cache}
        self.record[LIMITS_CACHE_KEY] = cache
        for provider in QUOTA_CACHE_PROVIDERS:
            if not isinstance(cache.get(provider), dict):
                cache[provider] = {}
        return cache

    def _render_period(self) -> int:
        return max(1, round(self.settings.period_seconds))

    def _notice(self, now: datetime, reason: str) -> str:
        """What the message says when the screen could not be built: loud, dated, one screen."""
        stamp = now.astimezone(self.zone)
        minutes = max(1, self._render_period() // 60)
        return "\n".join(
            [
                f"⚠️ HERMES DASHBOARD: {reason}",
                f"Данные: нет · tick {self.ticks}",
                f"Обновлено: {stamp:%Y-%m-%d %H:%M %Z} · период {minutes} мин",
            ]
        )

    # ------------------------------------------------------------------ durable record

    def _load_record(self) -> None:
        state = getattr(self._ctx, "state", None)
        try:
            stored = state.get(STATE_KEY, {}) if state is not None else {}
        except Exception as exc:
            logger.warning(
                "probe: plugin state unreadable (%s); starting empty", type(exc).__name__
            )
            stored = {}
        self.record = dict(stored) if isinstance(stored, dict) else {}

    def _note(
        self,
        *,
        status: str,
        error: str | None,
        generation: int | None = None,
        confirmed: bool = False,
    ) -> None:
        # The confirmation is stamped now, after the edit, not with the tick's start time: the
        # next tick measures the age of this stamp against its own start.
        stamp = datetime.now(UTC).isoformat()
        self.record["last_status"] = status
        self.record["last_error"] = error
        self.record["ticks"] = self.ticks
        self.record["last_attempt_at"] = stamp
        if generation is not None:
            self.record["adapter_generation"] = generation
        if confirmed:
            self.record["last_confirmed_at"] = stamp
        if error:
            logger.warning("probe: %s: %s", status, error)
        state = getattr(self._ctx, "state", None)
        if state is None:
            return
        try:
            state.set(STATE_KEY, self.record)
        except Exception as exc:
            logger.warning("probe: plugin state not saved (%s)", type(exc).__name__)


def register(ctx: Any) -> ProbeRuntime | None:
    settings = read_settings(ctx)
    if settings is None:
        logger.error(
            "probe: no chat configured (settings.chat_id or %s); handler not registered", ENV_CHAT
        )
        return None
    runtime = ProbeRuntime(ctx, settings, import_dashboard())
    ctx.register_platform_handler(PLATFORM, runtime.wire)
    logger.info(
        "probe: registered Telegram handler factory (period %.0fs)", settings.period_seconds
    )
    # The plugin-data directory name carries a digest; the watchdog config needs this exact path.
    state_path = getattr(getattr(ctx, "state", None), "path", None)
    if state_path is not None:
        logger.info("probe: delivery record for --check plugin_state_path: %s", state_path)
    return runtime
