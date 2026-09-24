"""Worker threads whose failures come back as ordinary exceptions.

``asyncio.to_thread`` runs the callable in a Task of its own. When that callable raises
``SystemExit`` (a ``BaseException``), the Task's step re-raises it out of the event loop itself,
before the awaiting coroutine's ``except`` ever runs: a ``sys.exit()`` inside a library called
from a worker would take the whole gateway loop down. The wrapper here converts anything but the
operator's interrupt into ``WorkerCrashed``, an ``Exception``, at the thread boundary, so the
awaiting code degrades one source instead of dying.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any, TypeVar

T = TypeVar("T")


class WorkerCrashed(Exception):
    """A ``BaseException`` from a worker thread, carried across as an ``Exception``."""

    def __init__(self, original: BaseException) -> None:
        super().__init__(type(original).__name__)
        self.original = original


def failure_name(exc: BaseException) -> str:
    """The class name to report: the original one when the failure crossed a worker boundary."""
    if isinstance(exc, WorkerCrashed):
        return type(exc.original).__name__
    return type(exc).__name__


def _guarded(fn: Callable[..., T], *args: Any, **kwargs: Any) -> T:
    try:
        return fn(*args, **kwargs)
    except KeyboardInterrupt:
        raise
    except Exception:
        raise
    except BaseException as exc:  # SystemExit, GeneratorExit and the like
        raise WorkerCrashed(exc) from exc


async def run_in_worker(fn: Callable[..., T], *args: Any, **kwargs: Any) -> T:
    """``asyncio.to_thread`` whose failures are always ``Exception`` instances."""
    return await asyncio.to_thread(_guarded, fn, *args, **kwargs)


class StillRunning(Exception):
    """The previous worker for this name has not returned yet; nothing was started."""


class Flights:
    """One worker per name at a time.

    A deadline releases the awaiting coroutine, not the thread: ``asyncio.wait_for`` cannot
    interrupt a thread, so an abandoned worker keeps running in the executor. Started again on
    every tick, a source that hangs adds one thread per tick to the gateway's shared default
    executor until nothing else in the engine can use it. Here the abandoned task is remembered
    and the next call for the same name is refused with ``StillRunning`` until it returns.
    """

    def __init__(self) -> None:
        self._tasks: dict[str, asyncio.Task[Any]] = {}

    def busy(self, name: str) -> bool:
        task = self._tasks.get(name)
        return task is not None and not task.done()

    async def run(
        self, name: str, fn: Callable[..., T], *args: Any, timeout_seconds: float, **kwargs: Any
    ) -> T:
        """``run_in_worker`` under a deadline; ``StillRunning`` when the previous one is not back.

        Raises ``TimeoutError`` at the deadline and keeps the task so a later call sees it.
        """
        if self.busy(name):
            raise StillRunning(name)
        task: asyncio.Task[T] = asyncio.ensure_future(run_in_worker(fn, *args, **kwargs))
        # The result of an abandoned task is read here, or asyncio logs "exception was never
        # retrieved" when it is collected.
        task.add_done_callback(_retrieve)
        self._tasks[name] = task
        try:
            # shield: the deadline must not cancel the task, or done() would say True while the
            # thread still runs and the next tick would start a duplicate.
            return await asyncio.wait_for(asyncio.shield(task), timeout_seconds)
        finally:
            if task.done():
                self._tasks.pop(name, None)


def _retrieve(task: asyncio.Task[Any]) -> None:
    if not task.cancelled():
        task.exception()
