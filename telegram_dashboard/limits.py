"""Account limits through the engine's own facade, called the way ``/usage`` calls it.

The gateway's ``/usage`` handler runs ``agent.account_usage.fetch_account_usage`` in a worker
thread (``asyncio.to_thread``) so a slow provider API never blocks the event loop, and treats any
failure as "no account lines". This module does the same and nothing more:

* the import happens at call time; an import failure means ``unsupported`` (the facade does not
  exist on this installation) and never an exception;
* a failing provider degrades to ``unavailable`` on its own, the other provider still answers;
* the call runs in the gateway interpreter, so there is no second process that could race the
  facade's OAuth token refresh.

Nothing here reads or stores credentials; the facade uses the account's own material inside the
engine. This is the named exception to "no engine imports": one public function with the same
signature on 0.20.5 and 0.21.1, recorded as such in ``compat_matrix.json``.
"""

from __future__ import annotations

import importlib
import logging
from collections.abc import Callable
from datetime import datetime
from typing import Any

from .workers import Flights, StillRunning, failure_name

logger = logging.getLogger(__name__)

# Dashboard label -> provider key the facade understands.
PROVIDERS: tuple[tuple[str, str], ...] = (("claude", "anthropic"), ("codex", "openai-codex"))
FACADE_MODULE = "agent.account_usage"
FACADE_FUNCTION = "fetch_account_usage"

Fetcher = Callable[[str], Any]
Resolver = Callable[[], "Fetcher | None"]

LimitsPayload = dict[str, Any]


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


def build_limits_payload(resolve: Resolver = import_fetcher) -> LimitsPayload:
    """Ask the facade for every provider; the answer has the shape ``parse_limits_payload`` reads.

    Runs synchronously and may take as long as the slowest provider API; call it off the event
    loop (see :func:`fetch_limits_payload_off_loop`). Never raises.
    """
    try:
        fetch = resolve()
    except Exception as exc:
        return _not_ok(f"unsupported: facade resolver failed ({type(exc).__name__})")
    if fetch is None:
        return _not_ok("unsupported: facade not importable")
    return {
        "ok": True,
        "reason": None,
        "providers": [_provider_item(key, provider, fetch) for key, provider in PROVIDERS],
    }


async def fetch_limits_payload_off_loop(
    resolve: Resolver = import_fetcher,
    *,
    timeout_seconds: float,
    flights: Flights | None = None,
) -> LimitsPayload:
    """``build_limits_payload`` in a worker thread with a deadline, exactly like ``/usage``.

    A deadline hit reports ``unavailable`` for this tick; the worker thread itself cannot be
    interrupted and finishes on its own, which is the price ``/usage`` pays too. With ``flights``
    shared across ticks the abandoned worker is not duplicated until it returns.
    """
    try:
        return await (flights or Flights()).run(
            "limits", build_limits_payload, resolve, timeout_seconds=timeout_seconds
        )
    except TimeoutError:
        return _not_ok(f"timeout: facade did not answer within {timeout_seconds:g}s")
    except StillRunning:
        return _not_ok("busy: the previous facade call has not returned yet")
    except Exception as exc:  # thread start failure, SystemExit in the facade; the tick goes on
        return _not_ok(f"error: {failure_name(exc)}")


def _not_ok(reason: str) -> LimitsPayload:
    return {"ok": False, "reason": reason, "providers": []}


def _provider_item(key: str, provider: str, fetch: Fetcher) -> dict[str, Any]:
    item: dict[str, Any] = {
        "provider": key,
        "status": "unavailable",
        "reason": None,
        "source": None,
        "fetched_at": None,
        "windows": [],
    }
    try:
        snapshot = fetch(provider)
    except Exception as exc:
        item["reason"] = type(exc).__name__
        return item
    if snapshot is None:
        item["reason"] = "none"
        return item
    available = bool(getattr(snapshot, "available", False))
    item["status"] = "available" if available else "unavailable"
    item["reason"] = _text(getattr(snapshot, "unavailable_reason", None))
    item["source"] = _text(getattr(snapshot, "source", None))
    item["fetched_at"] = _iso(getattr(snapshot, "fetched_at", None))
    for window in getattr(snapshot, "windows", ()) or ():
        used = getattr(window, "used_percent", None)
        item["windows"].append(
            {
                "label": _text(getattr(window, "label", None)),
                "used_percent": used if isinstance(used, (int, float)) else None,
                "reset_at": _iso(getattr(window, "reset_at", None)),
            }
        )
    return item


def _iso(value: object) -> str | None:
    if isinstance(value, datetime):
        try:
            return value.isoformat()
        except (ValueError, OverflowError):
            return None
    return None


def _text(value: object) -> str | None:
    return str(value) if value is not None else None
