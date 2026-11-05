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

Nothing here reads or stores credentials; the facade uses the account's own material inside the
engine. Of a snapshot only the fields ``_item_of`` names are read: never its ``identity``, ``raw``
or ``details``, so none of them reaches the record or the screen. This is one of the two named
exceptions to "no engine imports": one public function with the same signature on 0.20.5 and
0.21.1, recorded as such in ``compat_matrix.json``. The other is ``agent.secret_scope.get_secret``
for the key of a ``limits_sources`` entry (``external.py``).
"""

from __future__ import annotations

import importlib
import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from functools import partial
from typing import Any

from .policy import sanitize_public_text
from .quota_cache import cache_for, current, last_answer
from .quota_cache import tick as quota_tick
from .workers import Flights, StillRunning, failure_name

logger = logging.getLogger(__name__)

# Dashboard label -> provider key the facade understands.
PROVIDERS: tuple[tuple[str, str], ...] = (("claude", "anthropic"), ("codex", "openai-codex"))
FACADE_MODULE = "agent.account_usage"
FACADE_FUNCTION = "fetch_account_usage"
# How much of each text the record keeps: what the screen shows of it (``collect``), no more.
_REASON_LIMIT = 60
_SOURCE_LIMIT = 40
_PLAN_LIMIT = 24
_LABEL_LIMIT = 24

Fetcher = Callable[[str], Any]
Resolver = Callable[[], "Fetcher | None"]

LimitsPayload = dict[str, Any]


@dataclass(frozen=True, slots=True)
class Cadence:
    """How the plugin's tick asks the facade: ``caches`` is the record's dict of per-provider
    caches (``claude``, ``codex``), ``now`` the tick's moment; each provider is asked at most
    once per ``interval_seconds``, and a last answer stands in for a missed one (a deadline, a
    worker still running) while it is younger than ``show_seconds``."""

    caches: dict[str, Any]
    now: datetime
    interval_seconds: float
    show_seconds: float


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
    resolve: Resolver = import_fetcher, cadence: Cadence | None = None
) -> LimitsPayload:
    """Ask the facade for every provider; the answer has the shape ``parse_limits_payload`` reads.

    Without a ``cadence`` every provider is called (the cron path). With one, a provider is
    called only when its attempt is due (``quota_cache.tick``) and its cache in
    ``cadence.caches`` records the attempt; between attempts the cached answer is the item.

    Runs synchronously and may take as long as the slowest provider API; call it off the event
    loop (see :func:`fetch_limits_payload_off_loop`). Never raises.
    """
    try:
        fetch = resolve()
    except Exception as exc:
        return _not_ok(f"unsupported: facade resolver failed ({type(exc).__name__})")
    if fetch is None:
        return _not_ok("unsupported: facade not importable")
    if cadence is None:
        providers = [_provider_item(key, provider, fetch) for key, provider in PROVIDERS]
    else:
        providers = [_provider_tick(key, provider, fetch, cadence) for key, provider in PROVIDERS]
    return {"ok": True, "reason": None, "providers": providers}


async def fetch_limits_payload_off_loop(
    resolve: Resolver = import_fetcher,
    *,
    timeout_seconds: float,
    flights: Flights | None = None,
    cadence: Cadence | None = None,
) -> LimitsPayload:
    """``build_limits_payload`` in a worker thread with a deadline, exactly like ``/usage``.

    A deadline hit reports ``unavailable`` for this tick; the worker thread itself cannot be
    interrupted and finishes on its own, which is the price ``/usage`` pays too. With ``flights``
    shared across ticks the abandoned worker is not duplicated until it returns. With a
    ``cadence`` a miss shows each provider's own standing attempt, or its last answer while it
    is young enough (``_missed``), and the abandoned worker records its attempt when it
    returns, so the next tick serves it without asking again.
    """
    if cadence is not None:
        # Made on the loop, before the worker starts: the worker only fills them.
        for key, _provider in PROVIDERS:
            cache_for(cadence.caches, key)
    try:
        return await (flights or Flights()).run(
            "limits", build_limits_payload, resolve, cadence, timeout_seconds=timeout_seconds
        )
    except TimeoutError:
        return _missed(f"timeout: facade did not answer within {timeout_seconds:g}s", cadence)
    except StillRunning:
        return _missed("busy: the previous facade call has not returned yet", cadence)
    except Exception as exc:  # thread start failure, SystemExit in the facade; the tick goes on
        return _missed(f"error: {failure_name(exc)}", cadence)


def _not_ok(reason: str) -> LimitsPayload:
    return {"ok": False, "reason": reason, "providers": []}


def _missed(reason: str, cadence: Cadence | None) -> LimitsPayload:
    """No answer from the worker this tick. Without a cadence the whole block is "no data" with
    the reason. With one, a provider whose own attempt still stands (its interval has not run
    out) keeps that attempt's line, a failure as much as an answer: the worker is one for both
    providers, and one provider's hang must not rewrite the other's row. A provider whose
    attempt was due shows its last answer with its own ``fetched_at`` while it is younger than
    ``show_seconds``, else "no data" with the reason, as an external source does
    (``collect._external_guarded``)."""
    if cadence is None:
        return _not_ok(reason)
    providers = []
    for key, _provider in PROVIDERS:
        cache = cache_for(cadence.caches, key)
        line = current(cache, now=cadence.now, interval_seconds=cadence.interval_seconds)
        if line is None:
            line = last_answer(cache, now=cadence.now, show_seconds=cadence.show_seconds)
        providers.append(_plan_filled(cache, line if line is not None else _failed(key, reason)))
    return {"ok": True, "reason": reason, "providers": providers}


def _provider_tick(key: str, provider: str, fetch: Fetcher, cadence: Cadence) -> dict[str, Any]:
    """One provider on the tick's cadence: the cached item while its attempt is younger than
    the interval, else one call recorded in the provider's cache. Runs in the worker, so a
    worker abandoned by its deadline still records its attempt when it returns."""
    cache = cache_for(cadence.caches, key)
    try:
        item = quota_tick(
            cache,
            now=cadence.now,
            interval_seconds=cadence.interval_seconds,
            fetch=partial(_attempt, cache, key, provider, fetch),
        )
    except Exception as exc:
        # Our own code failed around the call (reading the answer or the record): an attempt
        # too, kept for the interval, or a bug in reading would call the facade on every tick.
        logger.exception("usage facade answer for %s not read; attempt recorded", key)
        item = _failed(key, f"collector crashed: {type(exc).__name__}")
        cache["attempted_at"] = cadence.now.isoformat()
        cache["item"] = item
    plan = item.get("plan")
    if isinstance(plan, str) and plan:
        cache["plan"] = plan
    return _plan_filled(cache, item)


def _attempt(
    cache: dict[str, Any], key: str, provider: str, fetch: Fetcher, *, now: datetime
) -> dict[str, Any]:
    """One call. Any snapshot is an answer, with windows or without (Claude on an API key): the
    provider's ``answered_at`` moves. The facade's ``None`` and a failure are not answers."""
    snapshot, failure = _call(provider, fetch)
    if failure is not None:
        return _failed(key, failure)
    cache["answered_at"] = now.isoformat()
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
        return None, "none"
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
        "reason": _text(getattr(snapshot, "unavailable_reason", None), _REASON_LIMIT),
        "source": _text(getattr(snapshot, "source", None), _SOURCE_LIMIT),
        "fetched_at": _iso(getattr(snapshot, "fetched_at", None)),
        # The plan the facade names (Codex: ``plan_type`` title-cased); the details show it.
        "plan": _text(getattr(snapshot, "plan", None), _PLAN_LIMIT),
        "windows": windows,
    }


def _iso(value: object) -> str | None:
    if isinstance(value, datetime):
        try:
            return value.isoformat()
        except (ValueError, OverflowError):
            return None
    return None


def _text(value: object, limit: int) -> str | None:
    return sanitize_public_text(str(value), limit=limit) if value is not None else None
