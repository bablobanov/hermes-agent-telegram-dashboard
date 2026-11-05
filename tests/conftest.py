"""Every run prints which tree of code is under test; a green run without it proves nothing.

With ``REQUIRE_ENGINE_TESTS=1`` (the CI ``engine`` job, or a local run in a Hermes venv) a skip
fails the run unless its reason is in ``ENGINE_JOB_SKIPS``: a test that skipped there did not run
against the engine, and a green job would say it had.

No test reaches a host off this machine: connects and name lookups off loopback are refused for
the whole process, and an attempt fails the test that made it.

The whole process runs with HERMES_HOME pointed at a temporary directory, so engine code a test
reaches never resolves the real profile of whoever runs the tests."""

import ipaddress
import os
import shutil
import socket
import sys
import tempfile
from collections.abc import Iterator
from typing import Any, Literal

import pytest

import telegram_dashboard
from telegram_dashboard import collect

ENGINE_JOB = os.environ.get("REQUIRE_ENGINE_TESTS") == "1"

# The skips an engine job may still have, by their exact reason.
ENGINE_JOB_SKIPS = frozenset(
    {
        # Skips precisely because the engine is here: the real facade would call provider APIs.
        "the engine is importable here: the real facade would call provider APIs",
        # Windows only, for local runs in an engine venv; the CI job runs on Linux, where these
        # never skip.
        "a link's own time cannot be set on this platform",
        "symlinks not permitted here",
        "directory links not permitted here",
        "time.tzset is POSIX only",
        # A floor engine of 0.21.1-0.21.5 reads config.yaml with PyYAML; hermes_yaml came with
        # 0.21.6.
        "the engine's own parser (0.21.6+) is not here",
    }
)


def pytest_report_header(config):
    return [
        f"telegram_dashboard.__file__ = {telegram_dashboard.__file__}",
        f"python = {sys.executable}",
    ]


def pytest_configure(config: pytest.Config) -> None:
    _install_network_guard()
    _use_a_temporary_hermes_home()
    if ENGINE_JOB:
        # A module skipped at import fails its collection; the rest of the suite runs on, so one
        # run names every test the engine did not run, not only the first module.
        config.option.continue_on_collection_errors = True


# The engine resolves its home on every call: HERMES_HOME, else the platform default, which is
# the real profile of whoever runs the tests (~/.hermes; %LOCALAPPDATA%\hermes on Windows).
# Set at configure time, before collection and so before any engine import; a test that needs a
# home of its own sets one, and monkeypatch brings this one back after it.
_HOME_BEFORE: list[str | None] = []
_SESSION_HOME: list[str] = []


def _use_a_temporary_hermes_home() -> None:
    if _SESSION_HOME:
        return
    _HOME_BEFORE.append(os.environ.get("HERMES_HOME"))
    _SESSION_HOME.append(tempfile.mkdtemp(prefix="hermes-test-home-"))
    os.environ["HERMES_HOME"] = _SESSION_HOME[0]


def pytest_unconfigure(config: pytest.Config) -> None:
    if not _SESSION_HOME:
        return
    before = _HOME_BEFORE[0]
    if before is None:
        os.environ.pop("HERMES_HOME", None)
    else:
        os.environ["HERMES_HOME"] = before
    shutil.rmtree(_SESSION_HOME.pop(), ignore_errors=True)


def _refused_skip(report: pytest.CollectReport | pytest.TestReport) -> str | None:
    """The failure text for a skip an engine job does not allow; ``None`` for anything else."""
    if not ENGINE_JOB or not report.skipped or hasattr(report, "wasxfail"):
        return None
    where, reason = "", str(report.longrepr)
    if isinstance(report.longrepr, tuple):
        path, lineno, reason = report.longrepr
        where = f" at {_relative(path)}:{lineno}"
    reason = reason.removeprefix("Skipped: ")
    if reason in ENGINE_JOB_SKIPS:
        return None
    return f"REQUIRE_ENGINE_TESTS=1: skipped{where}, not a skip the engine job allows: {reason}"


def _relative(path: str) -> str:
    try:
        return os.path.relpath(path)
    except ValueError:  # Windows: another drive than the working directory
        return path


def _failing(text: str, when: Literal["collect", "setup", "call", "teardown"]) -> pytest.CallInfo:
    """A call that failed with ``text`` and no traceback: the report then carries the text on
    the run's summary line too."""
    return pytest.CallInfo.from_call(lambda: pytest.fail(text, pytrace=False), when)


@pytest.hookimpl(wrapper=True)
def pytest_make_collect_report(collector: pytest.Collector):
    report = yield
    text = _refused_skip(report)
    if text is None:
        return report
    failure = collector.repr_failure(_failing(text, "collect").excinfo)
    return pytest.CollectReport(report.nodeid, "failed", failure, None)


@pytest.hookimpl(wrapper=True)
def pytest_runtest_makereport(item: pytest.Item, call: pytest.CallInfo):
    report = yield
    text = _refused_skip(report)
    if text is None:
        return report
    return pytest.TestReport.from_item_and_call(item, _failing(text, call.when))


# ------------------------------------------------------------------ no network
#
# 05.11 an exploratory run of the engine's real TelegramAdapter.connect() reached
# api.telegram.org past a filter on socket.connect alone: its name lookup and asyncio's IOCP
# connect on Windows were open. Engine objects are exercised here only behind this guard.


class NetworkBlockedError(RuntimeError):
    """A test reached for a host off this machine.

    Not an OSError: code that reads an OSError as "offline" and carries on would hide the
    attempt. The attempt is recorded before the raise, so a swallowed error still fails the
    test."""


# Each refused attempt goes to the list of the running test (``no_network``); outside a test it
# goes to _STRAY, and the session fails at its end. A worker thread may outlive its test.
_STRAY: list[str] = []
_SINK: list[list[str]] = [_STRAY]
_guard_installed = False


def _loopback(host: object) -> bool:
    """``localhost`` or a loopback address, an IPv4-mapped one included; ``None`` is the local
    host to getaddrinfo."""
    if host is None:
        return True
    name = host.decode("ascii", "replace") if isinstance(host, bytes) else str(host)
    name = name.split("%", 1)[0]  # an IPv6 zone
    if name.lower() == "localhost":
        return True
    try:
        address = ipaddress.ip_address(name)
    except ValueError:
        return False  # a name: resolving it is the lookup refused here
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        return address.ipv4_mapped.is_loopback
    return address.is_loopback


def _refuse(destination: str) -> None:
    _SINK[0].append(destination)
    raise NetworkBlockedError(
        f"network blocked in tests: {destination} (only loopback and AF_UNIX are reachable)"
    )


def _check(verb: str, sock: socket.socket, address: Any) -> None:
    if sock.family not in (socket.AF_INET, socket.AF_INET6) or not isinstance(address, tuple):
        return  # AF_UNIX and the like stay on this machine
    if not _loopback(address[0]):
        _refuse(f"{verb} {address[0]}:{address[1]}")


def _install_network_guard() -> None:
    """For the whole process and from before collection, never undone: a module that connects
    at import or a worker thread that outlives its test is held too."""
    global _guard_installed
    if _guard_installed:
        return
    _guard_installed = True
    real_connect, real_connect_ex = socket.socket.connect, socket.socket.connect_ex
    real_getaddrinfo = socket.getaddrinfo

    def connect(sock: socket.socket, address: Any) -> None:
        _check("connect", sock, address)
        return real_connect(sock, address)

    def connect_ex(sock: socket.socket, address: Any) -> int:
        _check("connect_ex", sock, address)
        return real_connect_ex(sock, address)

    def getaddrinfo(host: Any, port: Any, *args: Any, **kwargs: Any) -> Any:
        # asyncio's loop.getaddrinfo comes here too.
        if not _loopback(host):
            _refuse(f"getaddrinfo {host}:{port}")
        return real_getaddrinfo(host, port, *args, **kwargs)

    socket.socket.connect = connect  # type: ignore[method-assign]
    socket.socket.connect_ex = connect_ex  # type: ignore[method-assign]
    socket.getaddrinfo = getaddrinfo
    if sys.platform == "win32":
        # asyncio's proactor loop connects through IOCP (Overlapped.ConnectEx), never through
        # socket.connect.
        from asyncio import windows_events

        real_iocp_connect = windows_events.IocpProactor.connect

        def iocp_connect(proactor: Any, conn: socket.socket, address: Any) -> Any:
            _check("IocpProactor.connect", conn, address)
            return real_iocp_connect(proactor, conn, address)

        windows_events.IocpProactor.connect = iocp_connect  # type: ignore[method-assign]


@pytest.fixture(autouse=True)
def no_network() -> Iterator[list[str]]:
    """The attempts this test made, each refused as it was made; any left at its end fail it. A
    test that makes one on purpose clears the list."""
    attempts: list[str] = []
    _SINK[0] = attempts
    try:
        yield attempts
    finally:
        _SINK[0] = _STRAY
    if attempts:
        pytest.fail(f"network attempts in this test, all refused: {attempts}", pytrace=False)


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    if _STRAY:
        session.exitstatus = pytest.ExitCode.TESTS_FAILED


def pytest_terminal_summary(terminalreporter: Any) -> None:
    if _STRAY:
        terminalreporter.write_line(f"network attempts outside a test, all refused: {_STRAY}")


def _release_stub(*, now, previous=None):
    return {
        "status": "unavailable",
        "reason": "test stub",
        "source": "github_releases",
        "checked_at": now.isoformat(),
    }


@pytest.fixture(autouse=True)
def no_provider_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """No test reaches GitHub: the default attempt is a stub unless a test passes its own; the
    running Hermes version is a fixed one, not whatever this interpreter imported."""
    monkeypatch.setattr(collect, "version_fetch_item", _release_stub)
    monkeypatch.setattr(collect, "running_version", lambda: ("0.21.3", None))
