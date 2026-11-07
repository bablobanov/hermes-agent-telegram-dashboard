"""Account limits through the engine's own facade, called the way ``/usage`` calls it.

The gateway's ``/usage`` handler runs ``agent.account_usage.fetch_account_usage`` in a worker
thread (``asyncio.to_thread``) so a slow provider API never blocks the event loop, and treats any
failure as "no account lines". This module does the same and nothing more:

* the import happens at call time; an import failure means ``unsupported`` (the facade does not
  exist on this installation) and never an exception;
* a failing provider degrades to ``unavailable`` on its own, the other provider still answers;
* the call runs in the gateway interpreter, so there is no second process that could race the
  facade's OAuth token refresh.

With a ``Cadence`` (the plugin's tick) each provider is asked at most once per interval, success
or failure, through ``quota_cache.tick`` and the record's per-provider cache; between attempts the
last answer stands with its own ``fetched_at``. Without one (the cron path) every call asks.

The facade answers ``None`` alike for "no credential here" and "the call failed" (it swallows
every exception, on every version read). A provider whose silence says it is not set up here is
``not_configured`` instead of a "no data" line (issue #14, ``_judged``): one that never answered
since the record began, after ``NOT_CONFIGURED_AFTER`` ``None`` attempts in a row; one that
answered before, once its last answer is ``QUIET_SHOWN_SECONDS`` old. Any answer brings it back.
Without a record (the cron path) there is no memory, and ``None`` is ``not_configured`` at once.
Where the silence says nothing (``judged`` off: the launch profile's dashboard on a gateway that
multiplexes profiles, whose tick runs outside any profile scope, so the facade's lookup of
Claude's login is refused and turned into ``None``) no provider is judged: a ``None`` stays a
"no data" line.

Nothing here reads or stores credentials; the facade uses the account's own material inside the
engine. Of a snapshot only the fields ``_item_of`` names are read: never its ``identity``, ``raw``
or ``details``, so none of them reaches the record or the screen. This is one of the two named
exceptions to "no engine imports": one public function, called with the provider alone
(``/usage`` also passes the session's ``base_url`` and ``api_key``), ``read_only`` and
``identity_id`` at their defaults as ``/usage`` leaves them, the same call from 0.20.5 to 0.23.0
(0.21.6 added the keyword-only ``read_only`` and ``identity_id``, with defaults), recorded as
such in ``compat_matrix.json``.
The other is ``agent.secret_scope.get_secret`` for the key of a ``limits_sources`` entry
(``external.py``).
"""

from __future__ import annotations

import importlib
import logging
import math
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from functools import partial
from typing import Any

from .policy import sanitize_public_text
from .quota_cache import cache_for, current, last_answer
from .quota_cache import tick as quota_tick
from .timeparse import age_seconds, is_from_the_future, parse_timestamp
from .workers import Flights, StillRunning, failure_name

logger = logging.getLogger(__name__)

# Dashboard label -> provider key the facade understands.
PROVIDERS: tuple[tuple[str, str], ...] = (("claude", "anthropic"), ("codex", "openai-codex"))
FACADE_MODULE = "agent.account_usage"
FACADE_FUNCTION = "fetch_account_usage"
# The reason an item carries for the facade's ``None``.
NONE_REASON = "none"
# A provider that never answered since the record began is taken as not configured here after
# this many ``None`` attempts in a row (issue #14): at the default 300 s cadence, 10 to 15
# minutes after the install. Before that it is a "no data" line with the reason.
NOT_CONFIGURED_AFTER = 3
# A provider that answered once and then went quiet stays a "no data" line this long after its
# last answer, then is taken as not configured: the longest window these providers report is a
# week, and an answer older than that says nothing about the current one.
QUIET_SHOWN_SECONDS = 7 * 86400.0
# An item's status and reason for a provider without a line (``QuotaKind`` ``not_configured``).
NOT_CONFIGURED = "not_configured"
NOT_CONFIGURED_REASON = "not configured"
# The record's count of ``None`` attempts in a row since the last answer, per provider.
NONE_ATTEMPTS = "none_attempts"
# The reason of a provider the owner's ``limits_providers`` leaves out: never asked, no line.
OFF_REASON = "off in the dashboard settings"
# How much of each text the record keeps: what the screen shows of it (``collect``), no more.
_REASON_LIMIT = 60
_SOURCE_LIMIT = 40
_PLAN_LIMIT = 24
_LABEL_LIMIT = 24

Fetcher = Callable[[str], Any]
Resolver = Callable[[], "Fetcher | None"]
Clock = Callable[[], datetime]

LimitsPayload = dict[str, Any]


@dataclass(frozen=True, slots=True)
class Cadence:
    """How the plugin's tick asks the facade: ``caches`` is the record's dict of per-provider
    caches (``claude``, ``codex``), ``now`` the tick's moment; each provider is asked at most
    once per ``interval_seconds``, and a last answer stands in for a missed one (a deadline, a
    worker still running) while it is younger than ``show_seconds``. ``clock`` tells the moment
    each provider's attempt starts (``elapsed_clock`` on the tick's path): the providers are
    asked one after the other, so one asked after a slow call is stamped when it was really
    asked, not at the tick's moment, or it would be due again that much early. Without it every
    provider's moment is ``now``."""

    caches: dict[str, Any]
    now: datetime
    interval_seconds: float
    show_seconds: float
    clock: Clock | None = None

    def started(self) -> datetime:
        """The moment the provider asked now starts its attempt."""
        return self.clock() if self.clock is not None else self.now


def elapsed_clock(now: datetime, monotonic: Callable[[], float] = time.monotonic) -> Clock:
    """``now`` moved on by the whole seconds elapsed since this call, measured on
    ``monotonic``: the tick's moment for a provider asked within its first second, later for
    the next by as long as the one before took. Whole seconds: the cadence counts in tens of
    seconds (an interval of at least 60, a due tolerance of 5), so the time a quick call takes
    does not move a stamp off the tick's moment."""
    start = monotonic()
    return lambda: now + timedelta(seconds=math.floor(monotonic() - start))


def import_fetcher() -> Fetcher | None:
    """The facade if this interpreter has it, else ``None``. Never raises."""
    try:
        module = importlib.import_module(FACADE_MODULE)
    except Exception as exc:  # ImportError and whatever the engine raises while importing
        logger.info("account usage facade not importable: %s", type(exc).__name__)
        return None
    fetch: object = getattr(module, FACADE_FUNCTION, None)
    if not callable(fetch):
        logger.info("account usage facade has no callable %s", FACADE_FUNCTION)
        return None
    fetcher: Fetcher = fetch
    return fetcher


def build_limits_payload(
    resolve: Resolver = import_fetcher,
    cadence: Cadence | None = None,
    listed: frozenset[str] | None = None,
    judged: bool = True,
) -> LimitsPayload:
    """Ask the facade for every provider; the answer has the shape ``parse_limits_payload`` reads.

    Without a ``cadence`` every provider is called (the cron path). With one, a provider is
    called only when its attempt is due (``quota_cache.tick``) and its cache in
    ``cadence.caches`` records the attempt; between attempts the cached answer is the item.
    Either way a provider whose silence says it is not set up here is ``not_configured``
    (``_judged``), unless ``judged`` is off: then every provider asked keeps its line, as a
    listed one does. ``listed`` is the owner's ``limits_providers`` (``None``: not set): a
    provider it names is asked and keeps its line whatever it answers, one it leaves out is
    never asked; with none to ask the facade is not even imported.

    Runs synchronously and may take as long as the slowest provider API; call it off the event
    loop (see :func:`fetch_limits_payload_off_loop`). Never raises.
    """
    if not asked(listed):
        return {"ok": True, "reason": None, "providers": [_off(key) for key, _ in PROVIDERS]}
    try:
        fetch = resolve()
    except Exception as exc:
        return _not_ok(f"unsupported: facade resolver failed ({type(exc).__name__})")
    if fetch is None:
        return _not_ok("unsupported: facade not importable")
    kept = listed is not None or not judged
    providers = []
    for key, provider in PROVIDERS:
        if listed is not None and key not in listed:
            providers.append(_off(key))
        elif cadence is None:
            item = _provider_item(key, provider, fetch)
            providers.append(_judged(key, item, None, None, pinned=kept))
        else:
            item = _provider_tick(key, provider, fetch, cadence)
            cache = cache_for(cadence.caches, key)
            providers.append(_judged(key, item, cache, cadence.now, pinned=kept))
    return {"ok": True, "reason": None, "providers": providers}


def asked(listed: frozenset[str] | None) -> tuple[str, ...]:
    """The facade providers to ask: every one without a list, else the ones it names."""
    return tuple(key for key, _ in PROVIDERS if listed is None or key in listed)


async def fetch_limits_payload_off_loop(
    resolve: Resolver = import_fetcher,
    *,
    timeout_seconds: float,
    flights: Flights | None = None,
    cadence: Cadence | None = None,
    listed: frozenset[str] | None = None,
    judged: bool = True,
) -> LimitsPayload:
    """``build_limits_payload`` in a worker thread with a deadline, exactly like ``/usage``.

    A deadline hit reports ``unavailable`` for this tick; the worker thread itself cannot be
    interrupted and finishes on its own, which is the price ``/usage`` pays too. With ``flights``
    shared across ticks the abandoned worker is not duplicated until it returns. With a
    ``cadence`` a miss shows each provider's own standing attempt, or its last answer while it
    is young enough (``_missed``), and the abandoned worker records its attempt when it
    returns, so the next tick serves it without asking again. With no provider to ask
    (``listed`` names none) no worker starts. ``judged`` as for :func:`build_limits_payload`.
    """
    if not asked(listed):
        return build_limits_payload(resolve, cadence, listed, judged)
    if cadence is not None:
        # Made on the loop, before the worker starts: the worker only fills them.
        for key in asked(listed):
            cache_for(cadence.caches, key)
    try:
        payload = await (flights or Flights()).run(
            "limits",
            build_limits_payload,
            resolve,
            cadence,
            listed,
            judged,
            timeout_seconds=timeout_seconds,
        )
    except TimeoutError:
        missed = f"timeout: facade did not answer within {timeout_seconds:g}s"
        return _missed(missed, cadence, listed, judged)
    except StillRunning:
        busy = "busy: the previous facade call has not returned yet"
        return _missed(busy, cadence, listed, judged)
    except Exception as exc:  # thread start failure, SystemExit in the facade; the tick goes on
        return _missed(f"error: {failure_name(exc)}", cadence, listed, judged)
    if cadence is not None and payload.get("ok") is not True:
        _drop_unused(cadence.caches)
    return payload


def _drop_unused(caches: dict[str, Any]) -> None:
    """The provider dicts made for a worker that asked nobody (no facade here): gone again, so
    the record holds no empty entry, as after the synchronous form. The worker has returned;
    nothing else writes them now."""
    for key, _provider in PROVIDERS:
        if caches.get(key) == {}:
            del caches[key]


def _not_ok(reason: str) -> LimitsPayload:
    return {"ok": False, "reason": reason, "providers": []}


def _missed(
    reason: str,
    cadence: Cadence | None,
    listed: frozenset[str] | None = None,
    judged: bool = True,
) -> LimitsPayload:
    """No answer from the worker this tick. Without a cadence the whole block is "no data" with
    the reason. With one, a provider whose own attempt still stands (its interval has not run
    out) keeps that attempt's line, a failure as much as an answer: the worker is one for both
    providers, and one provider's hang must not rewrite the other's row. A provider whose
    attempt was due shows its last answer with its own ``fetched_at`` while it is younger than
    ``show_seconds``, else "no data" with the reason, as an external source does
    (``collect._external_guarded``). A miss is neither an answer nor a ``None``: a provider
    that was not configured stays so (``_judged`` reads the record, which the miss leaves
    alone), unless ``judged`` is off: then no provider is judged by its silence."""
    if cadence is None:
        return _not_ok(reason)
    providers = []
    for key, _provider in PROVIDERS:
        if listed is not None and key not in listed:
            providers.append(_off(key))
            continue
        cache = cache_for(cadence.caches, key)
        line = current(cache, now=cadence.now, interval_seconds=cadence.interval_seconds)
        if line is None:
            line = last_answer(cache, now=cadence.now, show_seconds=cadence.show_seconds)
        item = _plan_filled(cache, line if line is not None else _failed(key, reason))
        kept = listed is not None or not judged
        providers.append(_judged(key, item, cache, cadence.now, pinned=kept))
    return {"ok": True, "reason": reason, "providers": providers}


def _off(key: str) -> dict[str, Any]:
    """A provider the owner's list leaves out: never asked, no line."""
    return _hidden(key, OFF_REASON)


def _hidden(key: str, reason: str) -> dict[str, Any]:
    return {
        "provider": key,
        "status": NOT_CONFIGURED,
        "reason": reason,
        "source": None,
        "fetched_at": None,
        "windows": [],
    }


def _judged(
    key: str,
    item: dict[str, Any],
    cache: dict[str, Any] | None,
    now: datetime | None,
    *,
    pinned: bool = False,
) -> dict[str, Any]:
    """``item``, or a ``not_configured`` item for a provider whose silence says it is not set
    up on this installation (issue #14). The facade cannot tell "no credential" from "the call
    failed", so the record decides: a provider that never answered is not configured after
    ``NOT_CONFIGURED_AFTER`` ``None`` attempts in a row, one that answered before once its last
    answer is older than ``QUIET_SHOWN_SECONDS``. Without a record (``cache`` ``None``, the cron
    path) there is no memory: the facade's ``None`` is not configured at once. A provider the
    owner named in ``limits_providers``, and every provider where the silence says nothing
    (``judged`` off in the caller), is ``pinned`` and keeps its line whatever it says. The
    cache's own item stays what the attempt returned; only the payload's item changes."""
    if pinned or not _quiet(item, cache, now):
        return item
    return _hidden(key, NOT_CONFIGURED_REASON)


def _quiet(item: dict[str, Any], cache: dict[str, Any] | None, now: datetime | None) -> bool:
    if cache is None or now is None:
        return item.get("status") == "unavailable" and item.get("reason") == NONE_REASON
    answered = parse_timestamp(cache.get("answered_at"))
    age = age_seconds(answered, now) if answered is not None else None
    if answered is None or is_from_the_future(age):
        # No answer, or one stamped ahead of the clock beyond the skew tolerance (the clock was
        # set back): no usable answer, so the count of ``None`` attempts decides, or a stamp a
        # year ahead would keep a quiet provider shown for that year (B2 review M-c).
        return none_attempts(cache) >= NOT_CONFIGURED_AFTER
    return age is not None and age > QUIET_SHOWN_SECONDS


def none_attempts(cache: dict[str, Any]) -> int:
    """The record's count of ``None`` attempts in a row; a hand-edited value that is no count
    is none."""
    count = cache.get(NONE_ATTEMPTS)
    if isinstance(count, bool) or not isinstance(count, int) or count < 0:
        return 0
    return count


def _provider_tick(key: str, provider: str, fetch: Fetcher, cadence: Cadence) -> dict[str, Any]:
    """One provider on the tick's cadence: the cached item while its attempt is younger than
    the interval, else one call recorded in the provider's cache, stamped with the moment this
    provider was asked (``Cadence.started``). Runs in the worker, so a worker abandoned by its
    deadline still records its attempt when it returns."""
    cache = cache_for(cadence.caches, key)
    started = cadence.started()
    try:
        item = quota_tick(
            cache,
            now=started,
            interval_seconds=cadence.interval_seconds,
            fetch=partial(_attempt, cache, key, provider, fetch),
        )
    except Exception as exc:
        # Our own code failed around the call (reading the answer or the record): an attempt
        # too, kept for the interval, or a bug in reading would call the facade on every tick.
        logger.exception("usage facade answer for %s not read; attempt recorded", key)
        item = _crashed(cache, key, exc, started)
    except KeyboardInterrupt:
        raise
    except BaseException as exc:
        # ``SystemExit`` and the like out of the facade: an attempt as well, or the facade is
        # asked again on every tick; then on to the worker boundary, which turns it into an
        # ordinary miss of this tick (``workers.py``).
        _crashed(cache, key, exc, started)
        raise
    plan = item.get("plan")
    if isinstance(plan, str) and plan:
        cache["plan"] = plan
    return _plan_filled(cache, item)


def _crashed(cache: dict[str, Any], key: str, exc: BaseException, now: datetime) -> dict[str, Any]:
    """The attempt recorded for a failure around the call: its class name, never its text."""
    item = _failed(key, f"collector crashed: {type(exc).__name__}")
    cache["attempted_at"] = now.isoformat()
    cache["item"] = item
    return item


def _attempt(
    cache: dict[str, Any], key: str, provider: str, fetch: Fetcher, *, now: datetime
) -> dict[str, Any]:
    """One call. Any snapshot is an answer, with windows or without (Claude on an API key): the
    provider's ``answered_at`` moves and its count of ``None`` attempts is gone. The facade's
    ``None`` adds one to that count; a failure (an exception) is neither and leaves both."""
    snapshot, failure = _call(provider, fetch)
    if failure == NONE_REASON:
        cache[NONE_ATTEMPTS] = none_attempts(cache) + 1
    if failure is not None:
        return _failed(key, failure)
    cache["answered_at"] = now.isoformat()
    cache.pop(NONE_ATTEMPTS, None)
    return _item_of(key, snapshot)


def _plan_filled(cache: dict[str, Any], item: dict[str, Any]) -> dict[str, Any]:
    """A line without an answer still names the plan the provider named last, as an external
    source's does (``collect.external_part``)."""
    if item.get("status") == "available" or item.get("plan"):
        return item
    remembered = cache.get("plan")
    if isinstance(remembered, str) and remembered:
        return {**item, "plan": remembered}
    return item


def _provider_item(key: str, provider: str, fetch: Fetcher) -> dict[str, Any]:
    snapshot, failure = _call(provider, fetch)
    if failure is not None:
        return _failed(key, failure)
    return _item_of(key, snapshot)


def _call(provider: str, fetch: Fetcher) -> tuple[Any, str | None]:
    """The facade's snapshot, or why there is none: ``none`` for its ``None``, the exception's
    class name for a failure (never its text, which may carry an answer)."""
    try:
        snapshot = fetch(provider)
    except Exception as exc:
        return None, type(exc).__name__
    if snapshot is None:
        return None, NONE_REASON
    return snapshot, None


def _failed(key: str, reason: str) -> dict[str, Any]:
    return {
        "provider": key,
        "status": "unavailable",
        "reason": reason,
        "source": None,
        "fetched_at": None,
        "windows": [],
    }


def _item_of(key: str, snapshot: Any) -> dict[str, Any]:
    """The fields of a snapshot the screen uses, each by name: never ``identity`` (the account's
    principal), ``raw`` (the whole answer) or ``details`` (a credit balance and the like). Each
    text is cleaned and cut before it reaches the record, to the length the screen shows, the
    way an external source's are (``external._text``)."""
    available = bool(getattr(snapshot, "available", False))
    windows: list[dict[str, Any]] = []
    for window in getattr(snapshot, "windows", ()) or ():
        used = getattr(window, "used_percent", None)
        windows.append(
            {
                "label": _text(getattr(window, "label", None), _LABEL_LIMIT),
                "used_percent": used if isinstance(used, (int, float)) else None,
                "reset_at": _iso(getattr(window, "reset_at", None)),
            }
        )
    return {
        "provider": key,
        "status": "available" if available else "unavailable",
        "reason": _reason_of(snapshot),
        "source": _text(getattr(snapshot, "source", None), _SOURCE_LIMIT),
        "fetched_at": _iso(getattr(snapshot, "fetched_at", None)),
        # The plan the facade names (Codex: ``plan_type`` title-cased); the details show it.
        "plan": _text(getattr(snapshot, "plan", None), _PLAN_LIMIT),
        "windows": windows,
    }


def _reason_of(snapshot: Any) -> str | None:
    """The provider's own reason, cleaned and cut. One that reads exactly like the item's marker
    for the facade's ``None`` (``NONE_REASON``) is no reason, or an answer would pass for an
    empty one: hidden on the cron path, "no answer" on the tick's (B2 review N1)."""
    reason = _text(getattr(snapshot, "unavailable_reason", None), _REASON_LIMIT)
    return None if reason == NONE_REASON else reason


def _iso(value: object) -> str | None:
    if isinstance(value, datetime):
        try:
            return value.isoformat()
        except (ValueError, OverflowError):
            return None
    return None


def _text(value: object, limit: int) -> str | None:
    return sanitize_public_text(str(value), limit=limit) if value is not None else None
