"""The dashboard as a gateway plugin: one pinned message, edited in place on every tick.

``register(ctx)`` → ``ctx.register_platform_handler("telegram", wire)`` → the Telegram adapter
calls ``wire(native, adapter)`` from its ``connect()`` → ``wire`` spawns ONE supervised task via
``ctx.spawn_task`` → every tick composes the first screen (collect → render → plain text) and
edits the same message through ``adapter.edit_message``.

Three traps this plugin is built around (the first two found by reading 0.21.1, see the report;
the third is issue #12, read on 0.22.2 to 0.22.7):

* ``connect()`` runs the factory again on every reconnect with a NEW adapter instance, so a naive
  plugin would spawn a second loop each time. The guard keeps the first task and only records the
  new adapter generation.
* ``TelegramAdapter.edit_message`` does not forward to the replacement adapter after a reconnect
  (``send`` does). A loop holding the adapter it was born with would answer "Not connected"
  forever while looking alive. Each tick therefore asks ``runner.adapters`` for the current one
  and only uses an adapter that reports ``is_connected`` and was handed to this registration: the
  runner's map holds the launch profile's adapters, also for a served secondary profile's adapter.
* Registered again after a forced re-discovery (``discover_and_load(force=True)``: the gateway's
  ``reload-plugins`` verb that ``hermes plugins install/enable/update`` sends, a tool's forced
  provider lookup). The unload cancels the loop and evicts this module with its globals, then
  ``register()`` runs again with a new runtime, and the adapter never calls the new factory on a
  client it wired already: no loop at all, the record frozen at ``waiting``. What a registration
  hands to the next one therefore lives outside the evicted namespace, in a registry module of its
  own name keyed by the profile's state file (``_live``): ``wire()`` publishes the adapter, the
  gateway loop, the task and the generation; ``register()`` claims the profile and, when an
  earlier registration was handed an adapter, takes its loop over on the gateway loop
  (``ProbeRuntime.claim_profile``). One loop per profile, run by the newest registration; the
  factory of an earlier one does nothing. The launch profile's registration (marked once an
  adapter handed to it is the one the runner publishes) also takes up an adapter the runner
  published while no factory of the plugin was registered: a reconnect inside the reload.

Also: the factory fires BEFORE the adapter finishes ``initialize()``/``start()``, so the first
tick waits for ``is_connected`` instead of sending into a bot that is not initialised yet.

Only the bot that sent the message edits it. The record keeps the sender's numeric id next to
``message_id`` (``BOT_ID_KEY``); a message another bot sent is taken as lost and a new one is sent
(``_sender``): a served profile's message from 1.0.0 (that release sent the screen through the
launch profile's bot), or one the profile's earlier bot sent. In a private chat the same number
names one of this bot's own messages, which an edit would overwrite and report as the screen. On
a gateway that serves one profile a message from 1.0.0 is that profile's own, edited from the
first tick. On a multiplexing gateway, one whose profile the runner cannot tell yet waits, at
most ``SENDER_WAIT_SECONDS`` from the start, and is then taken the same way: never edited while
its sender is unknown.

The screen goes out as HTML when the adapter has a verb for it, plain otherwise. Which verb is
read off the adapter object, never off a version number (``_html_verb``): the public
``edit_message`` when its signature has a ``parse_mode`` parameter, else the adapter's own
``_edit_text(chat, id, text, parse_mode)``, else nothing. On 0.21.1 to 0.21.5 and on main the
public verb takes no parse mode: without ``finalize`` it sends plain text, and its ``finalize``
path converts to MarkdownV2 and splits an over-long payload into NEW messages, which a pinned
dashboard must never do; ``_edit_text`` (the same on all of them) takes a parse mode and raises
on refusal. Every tick tries the HTML form (bold headings, the details in a collapsed quote)
through the verb it found and, on any refusal, edits plain through the public verb in the same
tick. A plain edit that succeeds right after a failed HTML one means the HTML form was refused:
plain from then on, the record says ``screen_format: plain``, why (``html_error``) and which
verb carries the HTML when one does (``html_verb``), and HTML is tried again after
``HTML_RETRY_TICKS``. Whatever happens while composing, the message is edited: a screen that
could not be built is replaced by a loud one-screen plain notice with the time, never left as
yesterday's text.

An edit the engine held back is no edit. The engine paces each chat's sends and edits through
one shared slot (telegram adapter, #116312, read on 0.22.2 and 0.22.7): while a reply in the
same chat holds it, ``edit_message`` without ``finalize`` edits nothing and answers success with
``raw_response={"skipped": True}`` (``_skipped``). The tick records ``deferred``: no
confirmation, no verdict on a form, and the next tick edits again; a run of them that outlasts
the lagging threshold is one WARNING. The plain edits never get past the slot: the plugin never
asks for ``finalize``, which the slot does not hold back. ``_edit_text`` and the rich call are
not gated by the slot: they neither wait for it nor arm it.

Before HTML comes the rich form (0.12.0, Bot API 10.1+ Rich Messages, ``rich.py``): the same
first lines and a collapsed section per source. No verb of the adapter carries it; the call is the
engine bot's own raw Bot API call, ``adapter._bot.do_api_request``, the way the engine sends its
own rich messages, read off the object on every tick like the HTML verb (``_rich_api``). A
refused rich edit falls back to HTML, then plain, in the same tick, and gets its verdict by the
same rule (``screen_format: rich``, ``rich_error``, tried again after ``RICH_RETRY_TICKS``). A
new message is sent rich (``sendRichMessage``) when it can be: Telegram refuses to make a plain
message rich by an edit (the engine's own note, telegram adapter, v0.22.2). The form is off
unless the setting ``rich`` says true: Telegram documents no fallback text for a rich message,
so an app without Rich Messages may not show the screen at all.

The ``telegram_dashboard`` package is looked up beside this file first (the deployment copies
both directories into the same plugin folder), then in the interpreter. Settings come from the
plugin's config entry (``plugins.entries.telegram_dashboard_probe.settings``) or environment
variables; no chat id, path or token lives in code. Everything is fail-open: a failed tick is
logged and the next one runs.
"""

from __future__ import annotations

import asyncio
import importlib
import importlib.machinery
import inspect
import logging
import math
import os
import sys
import threading
import time
import types
import weakref
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, tzinfo
from html import escape
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
# While the Telegram adapter is not connected yet (right after a start) the next try comes this
# soon, not a period later: with a long period the first screen after a restart would wait it.
WAITING_RETRY_SECONDS = 30.0
DEFAULT_HERMES_HOME = "~/.hermes"
ENV_CHAT = "HERMES_DASHBOARD_PROBE_CHAT"
ENV_THREAD = "HERMES_DASHBOARD_PROBE_THREAD"
ENV_PERIOD = "HERMES_DASHBOARD_PROBE_PERIOD"
ENV_HOME = "HERMES_HOME"  # the engine's own variable; the cron path reads the same one
ENV_DRIFT_REPORT = "HERMES_DASHBOARD_PROBE_DRIFT_REPORT"
ENV_LIMITS = "HERMES_DASHBOARD_PROBE_LIMITS"
ENV_LIMITS_REFRESH = "HERMES_DASHBOARD_PROBE_LIMITS_REFRESH"
ENV_FACADE_REFRESH = "HERMES_DASHBOARD_PROBE_FACADE_REFRESH"
ENV_TZ = "HERMES_DASHBOARD_PROBE_TZ"
ENV_BACKUP_STATUS = "HERMES_DASHBOARD_PROBE_BACKUP_STATUS"
ENV_CONTEXT_FILES = "HERMES_DASHBOARD_PROBE_CONTEXT_FILES"
ENV_MEMORY = "HERMES_DASHBOARD_PROBE_MEMORY"
ENV_RICH = "HERMES_DASHBOARD_PROBE_RICH"
ENV_TERMINAL_CWD = "TERMINAL_CWD"  # the engine's own variable, read for the rules line
# The external limit sources are asked at most once per this many seconds each, not every
# tick: a weekly number does not move faster.
DEFAULT_LIMITS_REFRESH_SECONDS = 900.0
MIN_LIMITS_REFRESH_SECONDS = 60.0
# Claude and Codex through the engine's usage facade are asked at most once per this many seconds
# each, success or failure, not every tick: the facade calls the providers' account APIs, and a
# 5-hour window does not need a number every minute (288 calls a day per provider at the default
# and a 60 s period).
DEFAULT_FACADE_REFRESH_SECONDS = 300.0
MIN_FACADE_REFRESH_SECONDS = 60.0
# The record's key for the caches of the providers read by the dashboard itself before 1.1.0
# (a plan, windows, the time of an attempt; no token): dropped on load, so the next save leaves
# it out of the state file.
RETIRED_LIMITS_CACHE_KEY = "limits_cache"
# The usage facade's providers (``claude``, ``codex``) keep their last attempt each under a key of
# their own, never the retired one: a record from 1.0.0 may still carry that one in another shape.
FACADE_CACHE_KEY = "facade_cache"
# The check of the latest Hermes release upstream (at most one per 15 minutes) keeps its last
# answer and attempt in the record, so a restart neither asks GitHub again nor forgets the answer.
RELEASE_CACHE_KEY = "release_cache"
# External limit sources (``limits_sources``, contract 1) keep one attempt each, by URL
# digest, under a key of their own.
EXTERNAL_CACHE_KEY = "external_cache"
# The last Gemini 429 the engine's log showed, so one that rotated out of the log stays "last".
GEMINI_CACHE_KEY = "gemini_log_cache"
# The cron line's record (0.9.0): the failures the engine erased between two ticks, held a
# while, and where the run-history read left off.
CRON_CACHE_KEY = "cron_cache"
# The channel's record (0.9.0): the counter and generation last seen, the daily gaps, the
# first sighting of a blocked send path, the last send error.
TRAFFIC_CACHE_KEY = "traffic_cache"
_FALSE_WORDS = frozenset({"0", "false", "no", "off"})
# For a setting that is off unless it says yes (``rich``).
_TRUE_WORDS = frozenset({"1", "true", "yes", "on"})
# Bot API wording for "the message you want to edit is gone"; anything else keeps the id.
LOST_MARKERS = ("message to edit not found", "message can't be edited", "message_id_invalid")
# The two of them that say the message is not there at all; "can't be edited" is also how an edit
# in a form the message does not take may be refused, so on a rich message it is no loss unless
# the rich call itself says it.
GONE_MARKERS = ("message to edit not found", "message_id_invalid")
DASHBOARD_MODULES = ("collect", "compat", "freshness", "render", "rich", "telegram_traffic")
# What the tick calls; a copy of the package that lacks any of it is refused at import time.
REQUIRED_API: dict[str, tuple[str, ...]] = {
    # VERSION_FLIGHT came with ``version_cache`` (0.6.0), collect_external and read_sources
    # with the external limit sources (0.8.0), GEMINI_LOG with ``gemini_cache`` (0.8.1),
    # CRON_RUNS and TRAFFIC with ``cron_cache``, ``traffic_probe`` and ``traffic_cache``
    # (0.9.0), RULES with the rules line's ``Environment`` fields (0.10.0), compat's CRON_NOTES
    # and parse_cron_notes with ``Environment.cron_notes`` (1.0.0), compat's LIMITS_PROVIDERS
    # and parse_limits_providers with ``Environment.limits_providers`` (issue #14), compat's
    # FACADE_UNSCOPED with ``Environment.facade_unscoped`` (1.1.0): a copy without them would
    # reject the tick's arguments every time.
    # EXTERNAL_DEFAULT_INTERVAL_SECONDS came with 1.1.0, whose tick hands no cache to providers
    # an older copy read by itself: that copy would ask them on every tick.
    # FACADE_DEFAULT_INTERVAL_SECONDS came with ``facade_caches`` (issue #16): a copy without it
    # would reject that argument, or call the facade on every tick.
    "collect": (
        "collect_all_async",
        "SubprocessRunner",
        "Flights",
        "VERSION_FLIGHT",
        "collect_external",
        "read_sources",
        "GEMINI_LOG",
        "CRON_RUNS",
        "TRAFFIC",
        "RULES",
        "EXTERNAL_DEFAULT_INTERVAL_SECONDS",
        "FACADE_DEFAULT_INTERVAL_SECONDS",
    ),
    "compat": (
        "Environment",
        "CRON_NOTES",
        "parse_cron_notes",
        "LIMITS_PROVIDERS",
        "parse_limits_providers",
        "FACADE_UNSCOPED",
    ),
    # classify_message_freshness: the screen's own lagging rule, which a run of edits the engine
    # holds back is measured against (``_defer``); in the module since 0.3.0, the first release.
    "freshness": ("record_from_plugin_state", "classify_message_freshness"),
    "render": ("render_dashboard", "to_telegram_plain", "to_telegram_html"),
    # The rich form came with 0.12.0, the module with it.
    "rich": ("to_telegram_rich",),
    "telegram_traffic": ("probe_adapter",),
}
# The Bot API name of the parse mode, passed as a string: ``telegram.constants.ParseMode.HTML``
# is that string, and importing it would tie the plugin to the engine's PTB.
HTML_PARSE_MODE = "HTML"
# The verbs an adapter may edit HTML with, in the order they are asked for: the public one when
# its signature takes a parse mode (the catalog review's order), the adapter's own otherwise.
PUBLIC_VERB = "edit_message"
PRIVATE_VERB = "_edit_text"
PARSE_MODE_PARAMETER = "parse_mode"
# A real parameter of that name. ``**kwargs`` does not count: a verb that swallows the keyword
# would send the markup as plain text and answer success.
_PARSE_MODE_KINDS = (inspect.Parameter.POSITIONAL_OR_KEYWORD, inspect.Parameter.KEYWORD_ONLY)
NO_HTML_VERB = "no HTML verb (edit_message without parse_mode, no _edit_text)"
# After a refused HTML edit the screen is plain for this many ticks, then HTML is tried once
# more: an hour at the pilot's period, so a transient failure does not lock plain in for good.
HTML_RETRY_TICKS = 12
# The flag in ``raw_response`` of an edit the engine held back while the chat's outbound slot
# was busy, and the status the tick records for it: not an edit, not a failure.
SKIPPED_FLAG = "skipped"
DEFERRED_STATUS = "deferred"
# The rich form (0.12.0): the engine bot's raw Bot API call, the way the engine sends its own rich
# messages (telegram adapter ``_bot_supports_rich``, v0.22.2); no verb of the adapter carries it.
RICH_BOT_ATTRIBUTE = "_bot"
RICH_CALL = "do_api_request"
NO_RICH_CALL = "no rich call (adapter without _bot.do_api_request)"
RICH_OFF = "off in the dashboard settings"
RICH_CEILINGS = "over Telegram's ceilings"
RICH_DEADLINE_SECONDS = 30.0
# After a refused rich edit the screen is HTML (or plain) for this many ticks, as for HTML.
RICH_RETRY_TICKS = 12
# The forum's General topic: Telegram rejects its id on a send, which goes without a topic there
# (the engine's own ``_message_thread_id_for_send``, v0.22.2).
GENERAL_TOPIC_ID = "1"
# The record's key for the numeric id of the bot that sent ``message_id``: Telegram's user id of
# the bot, which PTB keeps on the engine's bot from getMe at its start (``Bot.id``, no request),
# never anything of its credentials. Only that bot may edit the message: a bot cannot edit
# another's, and in a private chat the same number names one of its own messages.
BOT_ID_KEY = "bot_id"
BOT_ID_ATTRIBUTE = "id"
# Why the record's message is not this bot's to edit, as the journal says it; and the reason a
# tick waits while that cannot be told yet.
OTHER_BOT = "was sent by another bot than this profile's"
LAUNCH_BOT = (
    "names no sender (written before 1.1.0), and in a served profile 1.0.0 sent the screen "
    "through the launch profile's bot"
)
SENDER_UNKNOWN = "the runner has installed no adapter yet: the message's sender cannot be told"
# How long a record without a sender waits for a multiplexing runner to tell the profile (a runner
# that serves one profile tells it at once: ``_serves_one_profile``), counted from this
# registration's start on the monotonic clock (a wall clock set right after a boot would end the
# wait at once). The runner installs the launch profile's adapters once every platform of that
# profile has answered its first connect; the engine runs those connects together and bounds them
# at 45 s for Telegram and 30 s for the others (``gateway/run.py``, v0.22.2 to v0.23.0), and a
# served profile's adapters connect after that. Five minutes is several times that bound, so a
# slow start still ends inside the wait, and short enough that a served profile whose launch
# profile installs no adapter at all gets a moving screen back within minutes: under the multiplex
# a platform whose token is only in a secondary profile's ``.env`` is skipped for the launch
# profile (#64674), or the launch profile's only platform failed at start. Past the wait the
# message is taken as another bot's (``SENDER_UNPLACED``): never edited while its sender is
# unknown. On a multiplexing gateway a connect bound raised past the wait (the engine's
# ``HERMES_GATEWAY_PLATFORM_CONNECT_TIMEOUT``) may run the launch profile's own wait out: its
# message is then replaced, not edited.
SENDER_WAIT_SECONDS = 300.0
SENDER_UNPLACED = (
    "names no sender (written before 1.1.0), and the runner installed no adapter in the "
    f"{SENDER_WAIT_SECONDS:.0f} s since the start: whose bot sent it cannot be told"
)
# What says the request was refused and nothing was sent, as the engine tells it from a failure that
# may have reached Telegram (``_is_rich_fallback_error``, ``_is_rich_capability_error``, v0.22.2):
# Telegram's no, a method this Bot API server or library lacks, a call that could not be made.
_RICH_REFUSED_NAMES = frozenset({"badrequest", "forbidden", "endpointnotfound"})
_RICH_SETUP_ERRORS = (AttributeError, TypeError, NotImplementedError)
_RICH_REFUSED_WORDS = ("bad request", "unsupported", "not implemented", "no such method")

Collector = Callable[[datetime], Awaitable[Any]]

# Issue #12: the registry of live tick loops, one entry per profile, in a module of its own name in
# ``sys.modules``. A forced re-discovery evicts ``hermes_plugins.<slug>`` and its submodules with
# every global of this file; this name is outside that namespace, so it survives. A later release
# reads what this one wrote: an entry carries ``version``, and a reader takes only the keys it
# knows (``LIVE_KEYS``), whatever else is there. A module or an entry whose ``version`` is higher
# than this release's (``_later``) was written by a later release: this one never takes its loop
# over and never writes into it, and serves its profile only while that release's loop does
# not run (``_owns_profile``). The module
# has ``version``, ``lock`` (a ``threading.Lock``) and ``profiles`` (key -> entry); an entry has
# ``version``, ``owner``, ``adapter`` and ``runner`` (references: call one for the object, ``None``
# once it is gone; ``runner`` is the adapter's ``gateway_runner``), ``platform`` (the adapter's
# key in the runner's map), ``loop`` (the gateway's event loop), ``task``, ``generation`` and
# ``primary`` (true once an adapter handed to this profile's registration was the one the runner
# publishes: the launch profile, whose adapter the runner's map holds).
LIVE_MODULE = "_telegram_dashboard_probe_live"
LIVE_VERSION = 1
LIVE_KEYS = ("adapter", "runner", "platform", "loop", "task", "generation", "primary")


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
    # External limit sources as the config lists them; the package checks every entry.
    limits_sources: tuple[Any, ...] = ()
    # How often Claude and Codex are asked through the usage facade (``facade_refresh_seconds``).
    facade_refresh_seconds: float = DEFAULT_FACADE_REFRESH_SECONDS
    # The rules line (whether the agent sees its context files); on unless turned off.
    context_files: bool = True
    # The memory section (notebooks, the approval queue, a provider); on unless turned off.
    memory: bool = True
    # The rich form of the screen (0.12.0); off unless turned on. A rich message carries no
    # fallback text (``InputRichMessage`` has no field for one), so an app without Rich Messages
    # may not show the screen at all.
    rich: bool = False
    # The owner's label per cron job (``cron_notes``, job id to label), config only; pairs, so
    # the frozen settings hold nothing mutable. Sanitized where it is shown, like a name.
    cron_notes: tuple[tuple[str, str], ...] = ()
    # The built-in limit providers to ask (``limits_providers``, config only, issue #14):
    # ``None`` when not set (every one asked, one with no sign of an account without a line);
    # a tuple, empty or not, asks exactly those and always shows them.
    limits_providers: tuple[str, ...] | None = None


@dataclass(frozen=True)
class Dashboard:
    """The package's modules the tick needs, wherever they were imported from."""

    collect: Any
    compat: Any
    freshness: Any
    render: Any
    rich: Any
    telegram_traffic: Any
    origin: str


@dataclass(frozen=True)
class Screen:
    """One tick's text in every form. ``html`` is ``None`` for the notice: the loud path stays
    plain so that nothing can be refused on the way to the chat. ``rich`` is ``None`` for the
    notice and when the rich form was not built (over Telegram's ceilings, or a failure of its
    own); a message that is rich already gets the plain words as one rich paragraph then."""

    plain: str
    html: str | None = None
    rich: str | None = None


@dataclass(frozen=True)
class _RichOutcome:
    """What one rich edit answered: ``reason`` is ``None`` when the message shows it now; else the
    exception class, whether the call may have reached Telegram (``transient``) rather than been
    refused, and the lost-message marker its answer carried (``lost``), never its text."""

    reason: str | None = None
    transient: bool = False
    lost: str | None = None

    @property
    def refused(self) -> bool:
        """Telegram said no to this markup: not a loss, not a failure that may have reached it."""
        return self.reason is not None and not self.transient and self.lost is None


@dataclass(frozen=True)
class _EditResult:
    """What ``_edit`` and ``_send_rich`` answer when the plugin itself made the call: the public
    verb's ``SendResult`` is not constructed by the plugin, so a shape with the fields the tick
    reads. ``deferred``: the engine held the edit back (``_skipped``); ``success`` is false
    then, so nothing that reads only ``success`` takes it for an edit."""

    success: bool
    error: str | None = None
    message_id: str | None = None
    deferred: bool = False


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
        facade_refresh_seconds=_refresh(
            _setting(ctx, "facade_refresh_seconds", ENV_FACADE_REFRESH),
            name="facade_refresh_seconds",
            default=DEFAULT_FACADE_REFRESH_SECONDS,
            floor=MIN_FACADE_REFRESH_SECONDS,
        ),
        display_timezone=_setting(ctx, "display_timezone", ENV_TZ) or "UTC",
        backup_status=Path(backup_status).expanduser() if backup_status else None,
        limits_sources=_entries(_raw_setting(ctx, "limits_sources")),
        context_files=_setting(ctx, "context_files", ENV_CONTEXT_FILES).lower() not in _FALSE_WORDS,
        memory=_setting(ctx, "memory", ENV_MEMORY).lower() not in _FALSE_WORDS,
        rich=_setting(ctx, "rich", ENV_RICH).lower() in _TRUE_WORDS,
        cron_notes=_notes(_raw_setting(ctx, "cron_notes")),
        limits_providers=_providers(_raw_setting(ctx, "limits_providers")),
    )


def _entries(raw: object) -> tuple[Any, ...]:
    """``limits_sources`` as a list in the config entry; anything else is "none configured"."""
    if isinstance(raw, (list, tuple)):
        return tuple(raw)
    if raw not in (None, ""):
        logger.warning("probe: limits_sources must be a list; ignoring %r", type(raw))
    return ()


def _notes(raw: object) -> tuple[tuple[str, str], ...]:
    """``cron_notes`` read by the package's own parser (``compat.parse_cron_notes``), so the
    plugin and the cron tick agree; none when no package can be imported or one raises."""
    notes: tuple[tuple[str, str], ...] = _parsed("cron_notes", "parse_cron_notes", raw, ())
    return notes


def _providers(raw: object) -> tuple[str, ...] | None:
    """``limits_providers`` read by the package's own parser (``compat.parse_limits_providers``),
    so the plugin and the cron tick agree; not set when no package can be imported or one
    raises."""
    providers: tuple[str, ...] | None = _parsed(
        "limits_providers", "parse_limits_providers", raw, None
    )
    return providers


def _parsed(setting: str, parser: str, raw: object, fallback: Any) -> Any:
    """A setting read by the package's own parser in ``compat``, the copy beside the plugin
    first; ``fallback`` when no package can be imported or one raises (``import_dashboard`` says
    so, as it did before). Never raises: a broken copy must not stop the handler from being
    registered. The exception's type only is logged, its message may carry config text."""
    for base in (f"{__package__}.telegram_dashboard" if __package__ else "", "telegram_dashboard"):
        if not base:
            continue
        try:
            parse = getattr(importlib.import_module(f"{base}.compat"), parser)
            return parse(raw, logger, "probe: ")
        except ModuleNotFoundError:
            continue  # an absent candidate is the normal case; ``import_dashboard`` logs it
        except Exception as exc:
            logger.warning("probe: %s not read by %s: %s", setting, base, type(exc).__name__)
            continue
    return fallback


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


def _working_dir() -> Path | None:
    """The gateway process's working directory (a service unit's ``WorkingDirectory``), where an
    operator may have put the agent's rules; ``None`` when it is gone."""
    try:
        return Path.cwd()
    except OSError:
        return None


def _terminal_cwd() -> Path | None:
    """The engine's own ``TERMINAL_CWD`` in this process (a path, never a secret): where a
    sandbox backend's context-file discovery runs when the prompt names no host directory."""
    value = os.environ.get(ENV_TERMINAL_CWD, "").strip()
    return Path(value).expanduser() if value else None


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


def _refresh(
    raw: str,
    *,
    name: str = "limits_refresh_seconds",
    default: float = DEFAULT_LIMITS_REFRESH_SECONDS,
    floor: float = MIN_LIMITS_REFRESH_SECONDS,
) -> float:
    """A finite refresh interval of at least ``floor``, no ceiling; unreadable input uses the
    default. ``limits_refresh_seconds`` unless told otherwise."""
    if not raw:
        return default
    try:
        seconds = float(raw)
    except ValueError:
        seconds = math.nan
    if not math.isfinite(seconds):
        logger.warning("probe: %s %r unusable; using %.0fs", name, raw, default)
        return default
    clamped = max(seconds, floor)
    if clamped != seconds:
        logger.warning("probe: %s %r clamped to %.0fs", name, raw, clamped)
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


def _takes_parse_mode(verb: object) -> bool:
    """Whether ``verb`` is a coroutine function with a ``parse_mode`` parameter of its own."""
    if not inspect.iscoroutinefunction(verb):
        return False
    try:
        parameter = inspect.signature(verb).parameters.get(PARSE_MODE_PARAMETER)
    except (TypeError, ValueError):
        return False
    return parameter is not None and parameter.kind in _PARSE_MODE_KINDS


def _html_verb(live: Any) -> tuple[str, Any] | None:
    """The verb this adapter edits HTML with: the public ``edit_message`` when it takes a parse
    mode, else the adapter's own ``_edit_text``, else nothing. A capability read off the object
    on every tick (a reconnect brings a new adapter), never a version number."""
    public = getattr(live, PUBLIC_VERB, None)
    if _takes_parse_mode(public):
        return PUBLIC_VERB, public
    private = getattr(live, PRIVATE_VERB, None)
    if inspect.iscoroutinefunction(private):
        return PRIVATE_VERB, private
    return None


def _rich_api(live: Any) -> Any | None:
    """The engine bot's raw Bot API call when it is a coroutine function, the engine's own test
    (``_bot_supports_rich``); ``None`` without a bot (a disconnected adapter) or without the call.
    A capability read off the object on every tick, never a version number."""
    call = getattr(getattr(live, RICH_BOT_ATTRIBUTE, None), RICH_CALL, None)
    return call if inspect.iscoroutinefunction(call) else None


def _chat_id(chat: str) -> int | str:
    """The chat as the engine passes it to the Bot API: a number as a number (``-100…``)."""
    try:
        return int(chat)
    except ValueError:
        return chat.strip()


def _thread_for_send(thread_id: str | None) -> int | None:
    """``message_thread_id`` for a send: none without a topic and for General."""
    if not thread_id or thread_id == GENERAL_TOPIC_ID:
        return None
    try:
        return int(thread_id)
    except ValueError:
        return None


def _rich_refused(exc: BaseException) -> bool:
    """Nothing was sent: Telegram said no (``BadRequest``, ``Forbidden``), the Bot API server or
    the library has no such method, or the call could not be made. A timeout or the network is
    not a refusal: the request may have reached Telegram."""
    if type(exc).__name__.lower() in _RICH_REFUSED_NAMES or isinstance(exc, _RICH_SETUP_ERRORS):
        return True
    if getattr(exc, "error_code", None) == 404:
        return True
    text = str(exc).lower()
    if ("method" in text or "endpoint" in text) and (
        "not found" in text or "does not exist" in text
    ):
        return True
    return any(word in text for word in _RICH_REFUSED_WORDS)


def _says(result: Any, markers: tuple[str, ...]) -> bool:
    """Whether a failed edit's error carries one of ``markers``."""
    error = str(getattr(result, "error", None) or "").lower()
    return any(marker in error for marker in markers)


def _skipped(result: Any) -> bool:
    """Whether the public verb answered success without editing: the engine held the edit back
    while the chat's outbound slot was busy (``raw_response`` carries ``SKIPPED_FLAG``)."""
    raw = getattr(result, "raw_response", None)
    held = isinstance(raw, dict) and bool(raw.get(SKIPPED_FLAG))
    return held and bool(getattr(result, "success", False))


def _deferred(result: Any) -> bool:
    """Whether this tick's edit was held back (``_EditResult.deferred``), neither made nor
    failed."""
    return getattr(result, "deferred", False) is True


def _bot_id(live: Any) -> int | None:
    """The numeric id of the bot behind ``live``: what PTB keeps from getMe on the engine's bot
    at its start (``Bot.id``, the bot the sends and edits go through), read without a request.
    ``None`` without a bot, before it started (PTB raises then) or for anything not a number."""
    try:
        found = getattr(getattr(live, RICH_BOT_ATTRIBUTE, None), BOT_ID_ATTRIBUTE, None)
    except Exception:
        return None
    return _a_bot_id(found)


def _a_bot_id(value: object) -> int | None:
    """``value`` when it can be a bot's id (a positive whole number), else ``None``."""
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        return None
    return value


def _multiplex_verdict(runner: object) -> bool | None:
    """Whether ``runner`` multiplexes profiles, read the way the runner reads it
    (``GatewayRunner._multiplex_on``: ``config.multiplex_profiles``, settled at boot by
    ``resolve_multiplex_mode``; v0.22.2 to v0.23.0; 0.23.0 may turn it on at runtime when a
    second profile appears, so it is read when asked). ``None`` for a runner without a config,
    which tells nothing, and when the verdict cannot be read."""
    config = getattr(runner, "config", None)
    if config is None:
        return None
    try:
        return bool(getattr(config, "multiplex_profiles", False))
    except Exception as exc:
        logger.debug("probe: the runner's multiplex verdict unreadable (%s)", type(exc).__name__)
        return None


def _serves_one_profile(runner: object) -> bool:
    """Whether ``runner`` serves only the profile it was started for (``_multiplex_verdict``
    false). Such a runner starts no secondary profile's adapters: a single-profile home, a
    standalone profile. ``False`` when the verdict tells nothing (the map decides then)."""
    return _multiplex_verdict(runner) is False


def _rich_message(markup: str) -> dict[str, Any]:
    """``InputRichMessage`` with Telegram's own detection of links, commands, mentions, hashtags
    and the like switched off: the second line behind ``<code>`` (``render._code_spans``)."""
    return {"html": markup, "skip_entity_detection": True}


def _message_id_of(answer: Any) -> str | None:
    """The new message's id from the raw answer, in the shapes the engine reads it in."""
    if isinstance(answer, dict):
        found = answer.get("message_id")
        if found is None and isinstance(answer.get("result"), dict):
            found = answer["result"].get("message_id")
    else:
        found = getattr(answer, "message_id", None)
    return str(found) if found is not None else None


def _rich_plain(text: str) -> str:
    """Plain words as one rich paragraph, escaped and nothing else: what a rich message gets when
    the screen has no rich form (the notice, a form over the ceilings)."""
    return "<p>" + "<br>".join(escape(line) for line in text.split("\n")) + "</p>"


@dataclass(frozen=True)
class _Live:
    """The registry module's lock and entries, as this release reads them; ``later`` when the
    module says a later release made it (read only, never written)."""

    lock: Any
    profiles: dict[Any, Any]
    later: bool = False


@dataclass(frozen=True)
class _Handover:
    """What a registration takes over from an earlier one of its profile. ``adapter`` is
    ``None`` when it is gone and the profile is the launch profile (``primary``): the runner's
    map then has the adapter to use."""

    loop: asyncio.AbstractEventLoop
    adapter: Any
    task: Any
    generation: int
    primary: bool = False
    runner: Callable[[], Any] | None = None
    platform: Any = None


def _live() -> _Live | None:
    """The process's registry of live tick loops, created on first use (``LIVE_MODULE``).
    ``None`` when a module of that name has another shape: no takeover then, as before #12."""
    module = sys.modules.get(LIVE_MODULE)
    if module is None:
        fresh = types.ModuleType(LIVE_MODULE, "telegram_dashboard_probe: live tick loops (#12)")
        spec = importlib.machinery.ModuleSpec(LIVE_MODULE, None)  # find_spec answers for it
        vars(fresh).update(__spec__=spec, version=LIVE_VERSION, lock=threading.Lock(), profiles={})
        module = sys.modules.setdefault(LIVE_MODULE, fresh)
    lock, profiles = getattr(module, "lock", None), getattr(module, "profiles", None)
    if not isinstance(profiles, dict) or not callable(getattr(lock, "__enter__", None)):
        return None
    return _Live(lock, profiles, _later(getattr(module, "version", None)))


def _later(version: object) -> bool:
    """Whether a registry module's or entry's ``version`` is a later release's than this one's
    (``LIVE_VERSION``): a whole number above it."""
    if isinstance(version, bool) or not isinstance(version, int):
        return False
    return version > LIVE_VERSION


def _written_later(live: _Live, entry: object) -> bool:
    """Whether a later release wrote the registry module or this entry: neither taken over nor
    written by this one."""
    return live.later or (isinstance(entry, dict) and _later(entry.get("version")))


def profile_key(ctx: Any, settings: Settings) -> str:
    """The registry key of the profile a registration serves: the resolved path of its own state
    file (``<home>/plugin-data/<namespace>/state.json``, one per profile; ``register()`` reads it
    under the profile's scope), case-folded where the file system is. Without one, the home the
    settings name. The process's ``HERMES_HOME`` is not the key: under the multiplex it names the
    launch profile, not this one."""
    state_path = getattr(getattr(ctx, "state", None), "path", None)
    path = Path(state_path) if isinstance(state_path, (str, Path)) else settings.hermes_home
    try:
        resolved = path.expanduser().resolve()
    except (OSError, RuntimeError):  # an unreadable link: the path as given, made absolute
        resolved = path.expanduser().absolute()
    return os.path.normcase(str(resolved))


def _ref(target: object) -> Callable[[], Any]:
    """A weak reference where the object takes one, else a strong one: what an entry holds."""
    try:
        return weakref.ref(target)
    except TypeError:
        return lambda: target


def _deref(reference: object) -> Any:
    """What an entry's reference points at; ``None`` for a dead or missing one."""
    return reference() if callable(reference) else None


def _alive(task: object) -> bool:
    """A task that has not finished, read by its ``done()``; anything else is not one."""
    done = getattr(task, "done", None)
    return callable(done) and not done()


def _connected(adapter: object) -> bool:
    return adapter is not None and bool(getattr(adapter, "is_connected", False))


def _known(entry: object) -> dict[str, Any]:
    """The keys of a registry entry this release knows, whichever release wrote it."""
    if not isinstance(entry, dict):
        return {}
    return {key: entry[key] for key in LIVE_KEYS if key in entry}


def _owner(entry: object) -> Any:
    """The registration an entry names as its profile's, ``None`` when it names none alive."""
    return _deref(entry.get("owner")) if isinstance(entry, dict) else None


def _generation(entry: dict[str, Any]) -> int:
    """The profile's adapter generation an entry carries; 0 for none readable."""
    generation = entry.get("generation")
    if not isinstance(generation, int) or isinstance(generation, bool) or generation < 1:
        return 0
    return generation


def _handover(entry: dict[str, Any]) -> _Handover | None:
    """What a takeover needs from an earlier registration's entry: the gateway loop, still open,
    and the adapter that registration was handed, still alive, or for the launch profile the
    runner, whose map has its adapter; ``None`` without them."""
    loop, adapter = entry.get("loop"), _deref(entry.get("adapter"))
    if not isinstance(loop, asyncio.AbstractEventLoop) or loop.is_closed():
        return None
    primary = entry.get("primary") is True
    runner = entry.get("runner") if callable(entry.get("runner")) else None
    if adapter is None and not (primary and _deref(runner) is not None):
        return None
    generation, task = max(_generation(entry), 1), entry.get("task")
    return _Handover(loop, adapter, task, generation, primary, runner, entry.get("platform"))


class ProbeRuntime:
    """One per ``register()``; survives adapter replacement, owns exactly one task."""

    def __init__(self, ctx: Any, settings: Settings, dashboard: Dashboard | None) -> None:
        self._ctx = ctx
        self.settings = settings
        self.dashboard = dashboard
        self.zone = display_zone(settings)
        # register() runs as the gateway starts: what the engine says once per process is said
        # after this moment (``Environment.process_started_at``).
        self.started_at = datetime.now(UTC)
        # The same moment on the monotonic clock, which the wait for a message's sender is counted
        # from (``SENDER_WAIT_SECONDS``).
        self.started_monotonic = time.monotonic()
        self.collector: Collector = self._collect
        # One worker per source across ticks: a source abandoned by its deadline is not
        # started again until it returns (collect.Flights).
        self.flights: Any = dashboard.collect.Flights() if dashboard is not None else None
        self.task: asyncio.Task[None] | None = None
        self.latest: Any = None
        self.generation = 0
        # Every adapter handed here, by id: a reference to it and its generation.
        self._generations: dict[int, tuple[Callable[[], Any], int]] = {}
        # The registry key of the profile this registration serves (issue #12), and whether the
        # engine unloaded it since (``retire``).
        self.key = profile_key(ctx, settings)
        self._retired = False
        # The runner and the platform key of the adapters handed here, and whether this is the
        # launch profile's registration (``_note_published``).
        self._runner: Callable[[], Any] | None = None
        self._platform: Any = None
        self.primary = False
        self.ticks = 0
        self.record: dict[str, Any] = {}
        # ``None`` until the first edit answers; ``False`` while HTML is refused, with the tick
        # from which it is worth trying again.
        self.html: bool | None = None
        self.html_retry_tick = 0
        # The same for the rich form, which is tried before HTML.
        self.rich: bool | None = None
        self.rich_retry_tick = 0
        self._rich_render_error: str | None = None
        # The last reason a full form was refused on a rich message, said once in the journal.
        self._rich_form_refused: str | None = None
        # Whether the current run of edits the engine held back said its one WARNING (``_defer``).
        self._defer_warned = False
        # The adapter's own counters, read as attributes before each screen is composed.
        self.traffic_probe: Any = None

    # ------------------------------------------------------------------ factory (connect-time)

    def wire(self, native: Any, adapter: Any) -> None:
        """Called by the adapter's ``connect()``; a reconnect calls it AGAIN with a new adapter.
        The factory of a registration that was unloaded, or that a newer one of its profile
        replaced, does nothing (issue #12)."""
        if self._retired or not self._owns_profile():
            logger.warning("probe: handler factory of an earlier registration called; ignored")
            return
        self.generation += 1
        self._hand(adapter)
        if _alive(self.task):
            self._publish()
            logger.warning(
                "probe: adapter generation %d wired while the loop runs; keeping one loop",
                self.generation,
            )
            return
        self._start()
        logger.info("probe: tick loop spawned on adapter generation %d", self.generation)

    def _hand(self, adapter: Any) -> None:
        self.latest = adapter
        gone = [key for key, (reference, _) in self._generations.items() if reference() is None]
        for key in gone:
            del self._generations[key]
        self._generations[id(adapter)] = (_ref(adapter), self.generation)
        runner = getattr(adapter, "gateway_runner", None)
        if runner is not None:
            self._runner, self._platform = _ref(runner), getattr(adapter, "platform", None)

    def _start(self) -> None:
        """The one place the loop is spawned: through the engine, so an unload cancels it."""
        self.task = self._ctx.spawn_task(self.run(), name=TASK_NAME)
        self._publish()

    # ------------------------------------------------------------------ registered again (#12)

    def claim_profile(self) -> None:
        """At ``register()``, on the engine's load worker thread: this registration serves its
        profile from now on. When an earlier one in this process was handed an adapter, its loop
        is taken over on the gateway loop (``_adopt``): the engine never calls this
        registration's factory on a client it wired already. An entry a later release wrote is
        left as it is (``_written_later``), said once."""
        live = _live()
        if live is None:
            return
        with live.lock:
            current = live.profiles.get(self.key)
            previous, later = _known(current), _written_later(live, current)
            if not later:
                live.profiles[self.key] = {**previous, "version": LIVE_VERSION, "owner": _ref(self)}
        if later:
            logger.warning(
                "probe: this profile's registry entry was written by a later release; left as "
                "it is, its loop not taken over"
            )
            return
        # The profile's count of adapters goes on, and so does the marker: the record's
        # adapter_generation never drops when a reconnect wires this one before _adopt runs.
        self.generation, self.primary = _generation(previous), previous.get("primary") is True
        handover = _handover(previous)
        if handover is None:
            return
        try:
            handover.loop.call_soon_threadsafe(self._adopt, handover)
        except RuntimeError as exc:  # the loop closed after the check: nothing left to take over
            logger.warning("probe: earlier tick loop not taken over (%s)", type(exc).__name__)

    def _adopt(self, handover: _Handover) -> None:
        """On the gateway loop: the earlier registration's loop stops (if the unload has not
        stopped it yet) and this one's starts on the adapter that one was handed. Nothing when
        this registration was unloaded meanwhile, no longer serves the profile, or runs a loop
        already (a reconnect wired it first). The launch profile's registration starts without
        an adapter when the earlier one's is gone: the runner's map has the current one."""
        if self._retired or _alive(self.task) or not self._owns_profile():
            return
        if _alive(handover.task):
            handover.task.cancel()
        self.generation = handover.generation
        self.primary = self.primary or handover.primary
        if handover.adapter is not None:
            self._hand(handover.adapter)
        else:
            self._runner, self._platform = handover.runner, handover.platform
        self._start()
        logger.info(
            "probe: plugin registered again; tick loop taken over on adapter generation %d",
            self.generation,
        )

    def retire(self) -> None:
        """``ctx.on_unload``: the engine unloaded this registration (a disable, an update, a
        forced re-discovery). Its factory and a takeover scheduled for it do nothing from now
        on; the registry entry stays for the next registration of the profile."""
        self._retired = True

    def _owns_profile(self) -> bool:
        """Whether this registration serves its profile: the newest one claimed it. With no
        registry, or no entry for the profile, every registration does, as before #12. An entry
        a later release wrote is never claimed: this registration serves while that release's
        loop does not run, so there is one loop per profile all the same."""
        live = _live()
        if live is None:
            return True
        with live.lock:
            entry = live.profiles.get(self.key)
        if _written_later(live, entry):
            return not _alive(_known(entry).get("task"))
        return not isinstance(entry, dict) or _owner(entry) is self

    def _publish(self) -> None:
        """What a later registration of this profile needs to take the loop over: the adapter
        last handed here and its runner, the gateway loop (``wire``, ``_adopt`` and the tick run
        on it), the task, the generation and the launch-profile marker. Only the registration
        that serves the profile writes."""
        live = _live()
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        if live is None:
            return
        entry = {
            "version": LIVE_VERSION,
            "owner": _ref(self),
            "adapter": _ref(self.latest),
            "runner": self._runner,
            "platform": self._platform,
            "loop": loop,
            "task": self.task,
            "generation": self.generation,
            "primary": self.primary,
        }
        with live.lock:
            current = live.profiles.get(self.key)
            if _written_later(live, current):
                return
            if not isinstance(current, dict) or _owner(current) is self:
                live.profiles[self.key] = entry

    # ------------------------------------------------------------------ resolution

    def live_adapter(self) -> Any | None:
        """The adapter the runner publishes now when it is one handed to this registration (a
        replacement's ``connect()`` hands it here first), else the one last handed to us;
        connected only. The runner's map holds the launch profile's adapters, also for the
        adapter of a served secondary profile: one never handed here is never used, or a
        secondary profile's screen would go out through another profile's bot. The launch
        profile's registration takes such an adapter up as handed (``_note_published``)."""
        published = self._published()
        if published is not None:
            self._note_published(published)
        candidates = [published] if self.generation_of(published) else []
        candidates.append(self.latest)
        for candidate in candidates:
            if _connected(candidate):
                return candidate
        return None

    def _published(self) -> Any:
        """What the runner publishes now for the platform of the adapters handed here: the
        launch profile's adapter, whichever profile this registration serves."""
        adapters = getattr(_deref(self._runner), "adapters", None)
        return adapters.get(self._platform) if isinstance(adapters, dict) else None

    def _note_published(self, published: Any) -> None:
        """The launch-profile marker (issue #12). An adapter handed here that the runner
        publishes makes this the launch profile's registration: a served secondary profile's
        adapters are never in the runner's map. The launch profile's registration takes a
        connected published adapter it was never handed as handed: Telegram reconnected while
        no factory of this plugin was registered (inside a reload), and the new adapter's
        ``connect()`` had nothing to call. A secondary profile's registration never does."""
        if self.generation_of(published):
            if not self.primary:
                self.primary = True
                self._publish()
            return
        if self.primary and _connected(published):
            self.generation += 1
            self._hand(published)
            self._publish()
            logger.info(
                "probe: Telegram reconnected while the plugin was loading; adapter generation %d",
                self.generation,
            )

    def generation_of(self, adapter: Any) -> int:
        """The generation ``adapter`` was handed here with; 0 for one never handed here. Held by
        reference, not by ``id`` alone: a gone adapter's id may come back on another object."""
        reference, generation = self._generations.get(id(adapter), (None, 0))
        return generation if adapter is not None and _deref(reference) is adapter else 0

    # ------------------------------------------------------------------ loop

    async def run(self) -> None:
        self._load_record()
        loop = asyncio.get_running_loop()
        while self._serves():
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
            await asyncio.sleep(max(0.0, self._pause() - elapsed))

    def _serves(self) -> bool:
        """Whether the loop goes on: its registration was not unloaded and still serves its
        profile (issue #12). Read before every tick, so one loop per profile does not hang on
        timing: a loop that a takeover left running, or one spawned right after its
        registration was unloaded, ends at its next tick instead of editing on."""
        if self._retired:
            logger.info("probe: tick loop ends: this registration was unloaded")
            return False
        if not self._owns_profile():
            logger.info("probe: tick loop ends: a newer registration serves this profile")
            return False
        return True

    def _pause(self) -> float:
        """The whole period after a tick that got to Telegram; a short retry while waiting for
        the adapter, so the first screen after a restart does not wait a whole period."""
        period = self.settings.period_seconds
        if self.record.get("last_status") == "waiting":
            return min(period, WAITING_RETRY_SECONDS)
        return period

    async def tick(self) -> None:
        live = self.live_adapter()
        if live is None:
            # Composing costs a facade call; not worth it while nothing can be delivered.
            self._note(status="waiting", error="no connected Telegram adapter")
            return
        self.ticks += 1
        self.traffic_probe = self._probe(live)
        now = datetime.now(UTC)
        screen = await self.compose(now)
        live = self.live_adapter()
        if live is None:
            self._note(status="waiting", error="no connected Telegram adapter")
            return
        generation = self.generation_of(live)
        chat = self.settings.chat_id
        if await self._edited(live, chat, screen, generation, now):
            return
        # Creation is rich when the setting is on and the adapter's bot can send it
        # (``_send_rich``). Otherwise the public verb as plain text (the adapter's own markdown
        # conversion, which the screen has no constructs for); the next tick edits it as HTML.
        result = await self._send_rich(live, chat, screen)
        if result is None:
            metadata = {"thread_id": self.settings.thread_id} if self.settings.thread_id else None
            result = await live.send(chat, screen.plain, metadata=metadata)
            if result.success:
                # Plain on the screen, whatever the record said of the message before it.
                self.record["screen_format"] = "plain"
                self.record["html_verb"] = None
        if result.success and result.message_id:
            if self.record.get("lost_at"):
                self.record["recreated_at"] = now.isoformat()
            self.record["message_id"] = str(result.message_id)
            self.record["chat_id"] = chat
            self.record["thread_id"] = self.settings.thread_id
            self._sent_by(live)
            self._note(status="created", error=None, generation=generation, confirmed=True)
            return
        self._note(
            status="send_failed", error=str(result.error or "no message id"), generation=generation
        )

    async def _edited(
        self, live: Any, chat: str, screen: Screen, generation: int, now: datetime
    ) -> bool:
        """The record's message edited: ``True`` when the tick is done with it (edited, held
        back, failed, or its sender cannot be told yet), ``False`` when a new message goes out
        (none in this chat, lost, sent by another bot, or its sender still unknown once the wait
        ran out, ``_sender``)."""
        message_id = self.record.get("message_id")
        if not message_id or self.record.get("chat_id") != chat:
            return False
        sender = self._sender(live)
        if sender == SENDER_UNKNOWN:
            self._note(status="waiting", error=SENDER_UNKNOWN)
            return True
        if sender is not None:
            logger.error("probe: message %s %s; sending a new one", message_id, sender)
            self._lost(now)
            return False
        result = await self._edit(live, chat, str(message_id), screen)
        if result.success:
            self._note(status="edited", error=None, generation=generation, confirmed=True)
            return True
        if _deferred(result):
            self._defer(generation)
            return True
        error = str(result.error or "")
        if any(marker in error.lower() for marker in LOST_MARKERS):
            logger.error("probe: message %s lost (%s); sending a new one", message_id, error)
            self._lost(now)
            return False
        self._note(status="edit_failed", error=error, generation=generation)
        return True

    def _lost(self, now: datetime) -> None:
        self.record["lost_at"] = now.isoformat()
        self.record["message_id"] = None

    def _sender(self, live: Any) -> str | None:
        """Why the record's message is not the bot behind ``live``'s to edit, ``SENDER_UNKNOWN``
        while that cannot be told yet, ``None`` when it is. The record keeps the sender's id
        (``BOT_ID_KEY``): another id is another bot (the profile's bot was replaced). A record
        without one was written before 1.1.0: the launch profile's message went through its own
        bot then as now, so it is edited and the id kept (on a runner that serves one profile,
        from the first tick); a served profile's went through the launch profile's bot. While a
        multiplexing runner tells neither (``_served``), the tick waits, for
        ``SENDER_WAIT_SECONDS`` from this registration's start; then the message is taken as
        another bot's (``SENDER_UNPLACED``), as a message whose sender is unknown is never
        edited. A bot that tells no id leaves nothing to compare: edited as before."""
        current = _bot_id(live)
        if current is None:
            return None
        stored = _a_bot_id(self.record.get(BOT_ID_KEY))
        if stored is not None:
            return None if stored == current else OTHER_BOT
        served = self._served()
        if served is None:
            return SENDER_UNKNOWN if self._sender_wait_left() else SENDER_UNPLACED
        if served:
            return LAUNCH_BOT
        self.record[BOT_ID_KEY] = current
        return None

    def _served(self) -> bool | None:
        """Whether this registration serves a secondary profile of a multiplexing gateway. False
        on a runner that serves one profile (``_serves_one_profile``): it starts no other
        profile's adapters, so whatever its map holds yet, this is the launch profile. Otherwise
        read off the runner's map, which holds the launch profile's adapters
        (``_note_published``): true when it publishes a Telegram adapter never handed here; false
        for the launch profile, for an adapter without a runner, and for a map with no Telegram
        adapter (1.0.0 then sent through this profile's own); ``None`` while the map is empty (a
        start: the runner installs the adapters that connected once every platform has
        answered; or a launch profile that installs none, which ``_sender`` waits for only so
        long)."""
        if self.primary:
            return False
        runner = _deref(self._runner)
        if _serves_one_profile(runner):
            return False
        adapters = getattr(runner, "adapters", None)
        if not isinstance(adapters, dict):
            return False
        if not adapters:
            return None
        return adapters.get(self._platform) is not None

    def _unscoped(self) -> bool:
        """Whether this tick may run outside any profile scope on a gateway that multiplexes
        profiles: the launch profile's adapter connects outside one and the tick runs in that
        context, so the facade's silence says nothing here (``Environment.facade_unscoped``).
        Taken so until the runner tells that this registration serves another profile
        (``_served`` true): a start's ticks come before the launch marker, while the record
        already holds the empty answers counted before the restart."""
        return _multiplex_verdict(_deref(self._runner)) is True and self._served() is not True

    def _sender_wait_left(self) -> bool:
        """Whether a record without a sender still waits for the runner to tell the profile:
        less than ``SENDER_WAIT_SECONDS`` since this registration started."""
        return time.monotonic() - self.started_monotonic < SENDER_WAIT_SECONDS

    def _sent_by(self, live: Any) -> None:
        """The new message's sender into the record, or no sender when the bot tells none."""
        sender = _bot_id(live)
        if sender is None:
            self.record.pop(BOT_ID_KEY, None)
        else:
            self.record[BOT_ID_KEY] = sender

    def _defer(self, generation: int) -> None:
        """The engine held this tick's edit back: the screen did not change, so no confirmation
        and no error; the next tick edits again. One INFO line when a run of them starts, and
        one WARNING once, while the run lasts, the last confirmation is past the screen's own
        lagging threshold: from then on the screen and ``--check`` say lagging, and the journal
        says why."""
        if self.record.get("last_status") != DEFERRED_STATUS:
            self._defer_warned = False
            logger.info(
                "probe: edit deferred: the engine held it back while the chat's outbound slot "
                "was busy; the next tick edits again"
            )
        if not self._defer_warned and self._lagging(datetime.now(UTC)):
            self._defer_warned = True
            logger.warning(
                "probe: edits held back while the chat's outbound slot stays busy; the screen "
                "still shows the text confirmed at %s, past the lagging threshold",
                self._confirmed_at(),
            )
        self._note(status=DEFERRED_STATUS, error=None, generation=generation)

    def _lagging(self, now: datetime) -> bool:
        """Whether the screen's own rule (``classify_message_freshness`` at the render period)
        calls the message lagging or worse now; never without the package, whose screen is the
        loud notice anyway."""
        dashboard = self.dashboard
        if dashboard is None:
            return False
        record = dashboard.freshness.record_from_plugin_state({STATE_KEY: self.record})
        verdict = dashboard.freshness.classify_message_freshness(
            record, now=now, period_seconds=self._render_period()
        )
        return bool(verdict != "confirmed")

    def _confirmed_at(self) -> str:
        """The last confirmation in the display zone, for the journal."""
        try:
            moment = datetime.fromisoformat(str(self.record.get("last_confirmed_at")))
        except ValueError:
            return "an unknown time"
        return f"{moment.astimezone(self.zone):%Y-%m-%d %H:%M %Z}"

    # ------------------------------------------------------------------ the edit

    async def _edit(self, live: Any, chat: str, message_id: str, screen: Screen) -> Any:
        """Rich first when the adapter's bot has the call for it (``_rich_api``), then HTML, then
        plain (``_edit_html_or_plain``), all in this tick. A refused rich edit gets its verdict
        by the HTML rule: only when the edit right after it landed.

        On a message the record says is rich, only the rich call's own answer may say the message
        is lost: an edit in another form may be refused in the words of a lost one
        (LOST_MARKERS), and the tick would send a second message; an HTML or plain answer counts
        only when it says the message is not there (GONE_MARKERS). A rich edit there that may
        have reached Telegram ends the tick; rich again next tick. An edit the engine held back
        answered nothing: deferred, no verdict, whatever the message is."""
        rich_message = self.record.get("screen_format") == "rich"
        outcome = await self._try_rich_edit(live, chat, message_id, screen, rich_message)
        if outcome is not None and outcome.reason is None:
            return _EditResult(success=True)
        if outcome is not None and rich_message and (outcome.lost or outcome.transient):
            return _EditResult(success=False, error=f"rich edit: {outcome.lost or outcome.reason}")
        refused = outcome.reason if outcome is not None else None
        result = await self._edit_html_or_plain(live, chat, message_id, screen)
        if _deferred(result):
            return result
        if result.success and refused is not None:
            self._rich_verdict(False, refused)
        if not result.success and rich_message and not _says(result, GONE_MARKERS):
            failed = refused or "no rich edit"
            return _EditResult(success=False, error=f"edit of a rich message failed ({failed})")
        return result

    async def _try_rich_edit(
        self, live: Any, chat: str, message_id: str, screen: Screen, rich_message: bool
    ) -> _RichOutcome | None:
        """The rich edit when this tick has one, else ``None`` (no markup for it, or no call for
        it, which the record says). On a rich message whose full form is refused, the same tick
        sends its plain words as one rich paragraph, which leaves Telegram nothing to refuse in
        the markup: an HTML or plain edit of a rich message may be refused for good."""
        markup, why = self._rich_markup(screen, rich_message)
        if markup is None:
            return None
        api = _rich_api(live)
        if api is None:
            self.record["rich_error"] = NO_RICH_CALL
            return None
        outcome = await self._edit_rich(api, chat, message_id, markup)
        plain = _rich_plain(screen.plain)
        if rich_message and markup != plain and outcome.refused:
            if self._rich_form_refused != outcome.reason:
                logger.warning(
                    "probe: rich form refused on a rich message (%s); its plain words instead",
                    outcome.reason,
                )
            self._rich_form_refused = outcome.reason
            outcome, why = await self._edit_rich(api, chat, message_id, plain), outcome.reason
        elif markup == screen.rich and outcome.reason is None:
            self._rich_form_refused = None
        if outcome.reason is None:
            self._rich_verdict(True, None)
            # The full form, or the plain words and why: the setting off, a refused form.
            self.record["rich_error"] = why
        return outcome

    def _rich_markup(self, screen: Screen, rich_message: bool) -> tuple[str | None, str | None]:
        """What a rich edit sends this tick and why it is not the full form: the rich form; on a
        message that is rich already, its plain words as one rich paragraph when there is no
        form (the notice, the ceilings, a build failure) or the setting is off. ``None`` when
        this tick has no rich edit: the setting off or rich refused lately, on a message that is
        not rich."""
        plain = _rich_plain(screen.plain) if rich_message else None
        if not self.settings.rich:
            self.record["rich_error"] = RICH_OFF
            return plain, RICH_OFF
        if not rich_message and not self._rich_wanted():
            return None, None
        return screen.rich or plain, None

    async def _edit_rich(self, api: Any, chat: str, message_id: str, markup: str) -> _RichOutcome:
        """As the engine edits rich (``_try_edit_rich``, v0.22.2): no topic on an edit, and "not
        modified" is the message already saying this."""
        try:
            target = int(message_id)
        except ValueError:
            return _RichOutcome("ValueError")
        payload = {
            "chat_id": _chat_id(chat),
            "message_id": target,
            "rich_message": _rich_message(markup),
        }
        try:
            await asyncio.wait_for(
                api("editMessageText", api_kwargs=payload), RICH_DEADLINE_SECONDS
            )
        except (asyncio.CancelledError, KeyboardInterrupt):
            raise
        except BaseException as exc:
            text = str(exc).lower()
            if "not modified" not in text:
                # Class name and a known marker only: the text may carry chat ids.
                lost = next((marker for marker in LOST_MARKERS if marker in text), None)
                return _RichOutcome(type(exc).__name__, not _rich_refused(exc), lost)
        return _RichOutcome()

    async def _send_rich(self, live: Any, chat: str, screen: Screen) -> _EditResult | None:
        """A new message sent rich (``sendRichMessage``, into the topic as the engine routes a
        send), or ``None`` for plain: the setting off, no rich form, no call for it, or a refusal.
        A call that may have reached Telegram is a failed send and no plain one follows it in this
        tick, which would be a second message if the first arrived; the next tick sends again,
        which is the same risk as after a plain send that timed out."""
        if not self.settings.rich:
            self.record["rich_error"] = RICH_OFF
            return None
        api = _rich_api(live)
        if screen.rich is None or api is None or not self._rich_wanted():
            return None
        payload: dict[str, Any] = {
            "chat_id": _chat_id(chat),
            "rich_message": _rich_message(screen.rich),
        }
        thread = _thread_for_send(self.settings.thread_id)
        if thread is not None:
            payload["message_thread_id"] = thread
        try:
            answer = await asyncio.wait_for(
                api("sendRichMessage", api_kwargs=payload), RICH_DEADLINE_SECONDS
            )
        except (asyncio.CancelledError, KeyboardInterrupt):
            raise
        except BaseException as exc:
            if _rich_refused(exc):
                self._rich_verdict(False, type(exc).__name__)
                return None
            return _EditResult(success=False, error=f"sendRichMessage: {type(exc).__name__}")
        message_id = _message_id_of(answer)
        if message_id is None:
            return _EditResult(success=False, error="sendRichMessage: no message id")
        self._rich_verdict(True, None)
        return _EditResult(success=True, message_id=message_id)

    def _rich_wanted(self) -> bool:
        return self.rich is not False or self.ticks >= self.rich_retry_tick

    def _rich_verdict(self, ok: bool, reason: str | None) -> None:
        if ok:
            if self.rich is not True:
                logger.info("probe: the screen is edited as rich")
            self.rich = True
            self.record["screen_format"] = "rich"
        else:
            if self.rich is not False:
                logger.warning(
                    "probe: rich refused (%s); HTML or plain, rich tried again in %d ticks",
                    reason,
                    RICH_RETRY_TICKS,
                )
            self.rich = False
            self.rich_retry_tick = self.ticks + RICH_RETRY_TICKS
        # Saved with the tick's own note, right after this edit; the form that landed instead
        # wrote ``screen_format`` itself.
        self.record["rich_error"] = reason

    async def _edit_html_or_plain(
        self, live: Any, chat: str, message_id: str, screen: Screen
    ) -> Any:
        """HTML through the verb the adapter has for it (``_html_verb``), plain through the
        public ``edit_message`` otherwise; the record says which form is on the screen and
        which verb carried it.

        The verbs cannot tell "HTML refused" from "network down" on their own: the plain edit
        right after a failed HTML one does. Plain succeeded, HTML did not: the form was refused.
        Both failed: no verdict, the tick's own error handling takes the plain result. The
        public verb held back by the engine (``_skipped``) landed nothing: deferred, no verdict,
        and the record keeps the form it had."""
        reason: str | None = None
        if screen.html is not None and self._html_wanted():
            verb = _html_verb(live)
            if verb is None:
                reason = NO_HTML_VERB
            else:
                name, edit_html = verb
                try:
                    if name == PUBLIC_VERB:
                        result = await edit_html(
                            chat, message_id, screen.html, parse_mode=HTML_PARSE_MODE
                        )
                        if _skipped(result):
                            # A plain edit right after it would be held back the same way.
                            return _EditResult(success=False, deferred=True)
                        if result.success:
                            self._html_verdict(True, None, name)
                            return result
                        # A SendResult, not an exception; the record keeps the adapter's error
                        # texts already (last_error), bounded here.
                        reason = f"{PUBLIC_VERB}: {str(result.error or 'refused')[:80]}"
                    else:
                        await edit_html(chat, message_id, screen.html, HTML_PARSE_MODE)
                        self._html_verdict(True, None, name)
                        return _EditResult(success=True)
                except (asyncio.CancelledError, KeyboardInterrupt):
                    raise
                except BaseException as exc:
                    if "not modified" in str(exc).lower():
                        # Telegram compared our text with the live message: it exists and
                        # already says this, in HTML.
                        self._html_verdict(True, None, name)
                        return _EditResult(success=True)
                    # Class name only: the message may carry chat ids.
                    reason = type(exc).__name__
        result = await live.edit_message(chat, message_id, screen.plain)
        if _skipped(result):
            return _EditResult(success=False, deferred=True)
        if result.success:
            # Plain on the screen now, the notice too; the verdict below says why when HTML failed.
            self.record["screen_format"] = "plain"
            self.record["html_verb"] = None
        if reason is not None and result.success:
            self._html_verdict(False, reason, None)
        return result

    def _html_wanted(self) -> bool:
        return self.html is not False or self.ticks >= self.html_retry_tick

    def _html_verdict(self, ok: bool, reason: str | None, verb: str | None) -> None:
        if ok:
            if self.html is not True:
                logger.info("probe: the screen is edited as HTML through %s", verb)
            self.html = True
        else:
            if self.html is not False:
                logger.warning(
                    "probe: HTML edit refused (%s); plain text, HTML tried again in %d ticks",
                    reason,
                    HTML_RETRY_TICKS,
                )
            self.html = False
            self.html_retry_tick = self.ticks + HTML_RETRY_TICKS
        # Saved with the tick's own note, right after this edit.
        self.record["screen_format"] = "html" if ok else "plain"
        self.record["html_error"] = reason
        self.record["html_verb"] = verb if ok else None

    # ------------------------------------------------------------------ the screen

    async def compose(self, now: datetime) -> Screen:
        """The first screen in both forms, or a loud one-screen plain notice. Never raises."""
        dashboard = self.dashboard
        if dashboard is None:
            self.record["last_render_error"] = "ImportError"
            return Screen(
                self._notice(now, "package telegram_dashboard not importable, screen not built")
            )
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
            html: str = dashboard.render.to_telegram_html(text)
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
            return Screen(self._notice(now, f"screen not built: {reason}"))
        self.record["last_render_error"] = None
        return Screen(
            plain=plain, html=html, rich=self._rich_form(dashboard, snapshot, record, now)
        )

    def _rich_form(
        self, dashboard: Dashboard, snapshot: Any, record: Any, now: datetime
    ) -> str | None:
        """The rich form, or ``None``: over Telegram's ceilings, or a failure of its own, which
        leaves the HTML and plain forms as they are. Said once per reason in the journal (a
        traceback for a failure) and on every tick in the record (``rich_render_error``)."""
        try:
            markup = dashboard.rich.to_telegram_rich(
                snapshot,
                now=now,
                delivery=record,
                period_seconds=self._render_period(),
                zone=self.zone,
            )
        except (asyncio.CancelledError, KeyboardInterrupt):
            raise
        except Exception as exc:
            reason = type(exc).__name__
            if self._rich_render_error != reason:
                logger.exception(
                    "probe: rich form not built; fallback this tick (HTML or plain words)"
                )
            return self._rich_built(None, reason)
        if not isinstance(markup, str):
            if self._rich_render_error != RICH_CEILINGS:
                logger.warning(
                    "probe: rich form over Telegram's ceilings; fallback this tick "
                    "(HTML, or plain words on a rich message)"
                )
            return self._rich_built(None, RICH_CEILINGS)
        return self._rich_built(markup, None)

    def _rich_built(self, markup: str | None, reason: str | None) -> str | None:
        self._rich_render_error = reason
        self.record["rich_render_error"] = reason
        return markup

    def _record_as_delivered(self, now: datetime) -> dict[str, Any]:
        """The record as it will be once this text lands.

        The text is composed before delivery. When the message was lost and this tick will send
        a new one, any text that lands IS the recreation: the banner must say "recreated",
        not "NOT restored" for a whole period on the very message that restored it.
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
            limits_sources=self.settings.limits_sources,
            context_files_enabled=self.settings.context_files,
            memory_enabled=self.settings.memory,
            cron_notes=self.settings.cron_notes,
            limits_providers=self.settings.limits_providers,
            facade_unscoped=self._unscoped(),
            gateway_dir=_working_dir(),
            terminal_cwd=_terminal_cwd(),
            process_started_at=self.started_at,
        )
        runner = dashboard.collect.SubprocessRunner()
        return await dashboard.collect.collect_all_async(
            env,
            runner,
            now=now,
            flights=self.flights,
            version_cache=self.release_cache(),
            facade_caches=self.facade_cache(),
            facade_interval_seconds=self.settings.facade_refresh_seconds,
            external_caches=self.external_cache(),
            external_interval_seconds=self.settings.limits_refresh_seconds,
            period_seconds=self.settings.period_seconds,
            gemini_cache=self.gemini_cache(),
            cron_cache=self.cron_cache(),
            traffic_probe=self.traffic_probe,
            traffic_cache=self.traffic_cache(),
        )

    def release_cache(self) -> dict[str, Any]:
        """The upstream release check's cache inside the record, the same object on every tick
        so the collector's write lands in the record and in the state file with it."""
        cache = self.record.get(RELEASE_CACHE_KEY)
        if not isinstance(cache, dict):
            cache = {}
            self.record[RELEASE_CACHE_KEY] = cache
        return cache

    def facade_cache(self) -> dict[str, Any]:
        """The usage facade's caches inside the record, one per provider, the same object on
        every tick so a worker that returns after its deadline still writes into the record."""
        cache = self.record.get(FACADE_CACHE_KEY)
        if not isinstance(cache, dict):
            cache = {}
            self.record[FACADE_CACHE_KEY] = cache
        return cache

    def external_cache(self) -> dict[str, Any]:
        """The external limit sources' caches inside the record, one per URL digest, the same
        object on every tick so a worker that returns late still writes into the record."""
        cache = self.record.get(EXTERNAL_CACHE_KEY)
        if not isinstance(cache, dict):
            cache = {}
            self.record[EXTERNAL_CACHE_KEY] = cache
        return cache

    def gemini_cache(self) -> dict[str, Any]:
        """The Gemini log's record inside the plugin's record, the same object on every tick so
        a reader that returns after the deadline still writes into the record."""
        cache = self.record.get(GEMINI_CACHE_KEY)
        if not isinstance(cache, dict):
            cache = {}
            self.record[GEMINI_CACHE_KEY] = cache
        return cache

    def cron_cache(self) -> dict[str, Any]:
        """The cron line's record inside the plugin's record, the same object on every tick:
        where the history read left off and the failures the details still hold."""
        cache = self.record.get(CRON_CACHE_KEY)
        if not isinstance(cache, dict):
            cache = {}
            self.record[CRON_CACHE_KEY] = cache
        return cache

    def traffic_cache(self) -> dict[str, Any]:
        """The channel's record inside the plugin's record, the same object on every tick."""
        cache = self.record.get(TRAFFIC_CACHE_KEY)
        if not isinstance(cache, dict):
            cache = {}
            self.record[TRAFFIC_CACHE_KEY] = cache
        return cache

    def _probe(self, live: Any) -> Any:
        """The adapter's own counters, read as attributes before the screen is composed; a
        package without the module (an older copy) means no traffic line."""
        dashboard = self.dashboard
        if dashboard is None:
            return None
        try:
            return dashboard.telegram_traffic.probe_adapter(live)
        except Exception as exc:  # the probe never raises by contract; belt and braces
            logger.warning("probe: traffic counters not read (%s)", type(exc).__name__)
            return None

    def _render_period(self) -> int:
        return max(1, round(self.settings.period_seconds))

    def _notice(self, now: datetime, reason: str) -> str:
        """What the message says when the screen could not be built: loud, dated, one screen."""
        stamp = now.astimezone(self.zone)
        minutes = max(1, self._render_period() // 60)
        return "\n".join(
            [
                f"⚠️ HERMES DASHBOARD: {reason}",
                f"Data: none · tick {self.ticks}",
                f"Updated: {stamp:%Y-%m-%d %H:%M %Z} · period {minutes} min",
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
        self.record.pop(RETIRED_LIMITS_CACHE_KEY, None)

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


def _announce_sources(runtime: ProbeRuntime) -> None:
    """The start-up check of ``limits_sources``: a refused entry is named in the log once, and
    on the screen every tick (its line says why)."""
    if not runtime.settings.limits_sources or runtime.dashboard is None:
        return
    sources = runtime.dashboard.collect.read_sources(runtime.settings.limits_sources)
    for source in sources:
        if source.problem:
            logger.warning("probe: limits source %d refused: %s", source.number, source.problem)
    logger.info("probe: %d external limits source(s) configured", len(sources))


def _on_unload(ctx: Any, callback: Callable[[], None]) -> bool:
    """``callback`` at the engine's unload of this registration, through ``ctx.on_unload``
    (0.21.1 on). False when the engine refused it: the method answers ``None`` only once this
    load ran past the engine's deadline (``plugins.load_timeout_seconds``), which counts the
    plugin as failed and ignores its registrations. True otherwise, also without the method
    (the ownership rule alone keeps one loop per profile then)."""
    subscribe = getattr(ctx, "on_unload", None)
    if not callable(subscribe):
        return True
    try:
        handle = subscribe(callback)
    except Exception as exc:
        logger.warning("probe: unload callback not registered (%s)", type(exc).__name__)
        return True
    return handle is not None


def register(ctx: Any) -> ProbeRuntime | None:
    """The order matters (issue #12): the unload callback first, then the claim of the profile,
    then the factory. A replacement adapter's ``connect()`` may call the factory as soon as it
    is registered; claimed before, it is this registration's own. A load the engine abandoned
    claims nothing."""
    settings = read_settings(ctx)
    if settings is None:
        logger.error(
            "probe: no chat configured (settings.chat_id or %s); handler not registered", ENV_CHAT
        )
        return None
    runtime = ProbeRuntime(ctx, settings, import_dashboard())
    _announce_sources(runtime)
    if _on_unload(ctx, runtime.retire):
        runtime.claim_profile()
    else:
        logger.warning("probe: the engine abandoned this load past its deadline; no takeover")
    ctx.register_platform_handler(PLATFORM, runtime.wire)
    logger.info(
        "probe: registered Telegram handler factory (period %.0fs)", settings.period_seconds
    )
    # The plugin-data directory name carries a digest; the watchdog config needs this exact path.
    state_path = getattr(getattr(ctx, "state", None), "path", None)
    if state_path is not None:
        logger.info("probe: delivery record for --check plugin_state_path: %s", state_path)
    return runtime
