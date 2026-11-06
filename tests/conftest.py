"""Every run prints which tree of code is under test; a green run without it proves nothing.

With ``REQUIRE_ENGINE_TESTS=1`` (the CI ``engine`` job, or a local run in a Hermes venv) a skip,
or an xfail that stopped its test, fails the run unless ``ENGINE_JOB_SKIPS`` allows its file,
reason and platform: a test that did not run to its end there did not run against the engine,
and a green job would say it had.

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
from collections.abc import Callable, Iterator
from typing import Any, Literal

import pytest

import telegram_dashboard
from telegram_dashboard import collect

ENGINE_JOB = os.environ.get("REQUIRE_ENGINE_TESTS") == "1"

# The skips an engine job may still have: (the test's file, as its node id names it, the exact
# reason) -> the one platform it may skip on, or None for any. A reason allowed in one file is
# refused in every other, a Windows-only one wherever else the job runs (CI runs on Linux).
ENGINE_JOB_SKIPS: dict[tuple[str, str], str | None] = {
    # Skips precisely because the engine is here: the real facade would call provider APIs.
    (
        "tests/test_collect.py",
        "the engine is importable here: the real facade would call provider APIs",
    ): None,
    # Windows, for local runs in an engine venv: no os.utime of a link itself, no symlink without
    # the privilege, no time.tzset.
    ("tests/test_context_files.py", "a link's own time cannot be set on this platform"): "win32",
    ("tests/test_context_files.py", "symlinks not permitted here"): "win32",
    ("tests/test_context_files_engine.py", "directory links not permitted here"): "win32",
    ("tests/test_gemini_log.py", "time.tzset is POSIX only"): "win32",
}


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
# home of its own sets one, and monkeypatch brings this one back after it. It is never set back:
# the value before it may be the real profile (in the shell these tests were written in it is),
# and the engine's atexit hooks or a late thread could resolve it after the session.
_SESSION_HOME: list[str] = []
# The OS temp root the home was made in. ``gateway.run`` imports the engine's bootstrap, which
# points TMPDIR, TMP and TEMP at ``<HERMES_HOME>/cache/scratch`` when none of them is set and
# drops tempfile's cached root (``export_scratch_tmp_env``, 0.22.2 to 0.23.0). On the Linux
# runners none is set, so from the collection of such a test module on ``tempfile.gettempdir()``
# names a directory inside the session home: the home is proved temporary against this root.
_SESSION_TEMP_ROOT: list[str] = []


def _use_a_temporary_hermes_home() -> None:
    if _SESSION_HOME:
        return
    _SESSION_TEMP_ROOT.append(tempfile.gettempdir())
    _SESSION_HOME.append(tempfile.mkdtemp(prefix="hermes-test-home-"))
    os.environ["HERMES_HOME"] = _SESSION_HOME[0]


@pytest.fixture
def session_temp_root() -> str:
    """The OS temp root the session's HERMES_HOME was made in (``_SESSION_TEMP_ROOT``)."""
    return _SESSION_TEMP_ROOT[0]


def pytest_unconfigure(config: pytest.Config) -> None:
    """The directory goes; HERMES_HOME keeps naming it until the process exits."""
    if _SESSION_HOME:
        shutil.rmtree(_SESSION_HOME.pop(), ignore_errors=True)


def _refusal(report: pytest.CollectReport | pytest.TestReport, call: Any = None) -> str | None:
    """The failure text for a test an engine job did not run to its end and does not allow;
    ``None`` for anything else. A skip, and an xfail that stopped the test: ``pytest.xfail`` in
    it, or ``xfail(run=False)``, which never starts it. A test under an xfail marker that fails
    with any exception (anywhere in its body, or a fixture at setup) stays an xfail under the
    variable too; the suite has no xfail today."""
    if not ENGINE_JOB or not report.skipped:
        return None
    where = ""
    if hasattr(report, "wasxfail"):
        excinfo = getattr(call, "excinfo", None)
        if excinfo is None or not excinfo.errisinstance(pytest.xfail.Exception):
            return None
        what, reason = "xfailed before its end", str(report.wasxfail)
    else:
        what, reason = "skipped", str(report.longrepr)
        if isinstance(report.longrepr, tuple):
            path, lineno, reason = report.longrepr
            where = f" at {_relative(path)}:{lineno}"
        reason = reason.removeprefix("Skipped: ")
    key = (report.nodeid.split("::")[0], reason)
    if key in ENGINE_JOB_SKIPS and ENGINE_JOB_SKIPS[key] in (None, sys.platform):
        return None
    return f"REQUIRE_ENGINE_TESTS=1: {what}{where}, not one the engine job allows here: {reason}"


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
    text = _refusal(report)
    if text is None:
        return report
    failure = collector.repr_failure(_failing(text, "collect").excinfo)
    return pytest.CollectReport(report.nodeid, "failed", failure, None)


@pytest.hookimpl(wrapper=True)
def pytest_runtest_makereport(item: pytest.Item, call: pytest.CallInfo):
    report = yield
    text = _refusal(report, call)
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
    at import or a worker thread that outlives its test is held too.

    Python's own ways off the machine: a connect, a datagram sent to an address, a name or
    address lookup. A client written in C that opens its own sockets or resolves on its own
    (uvloop, grpc, c-ares, pycurl) passes under any Python-level guard; none is on the test
    path today.

    A reverse lookup of this machine's own name is refused too, so socket.getfqdn() without an
    argument, email.utils.make_msgid(), smtplib.SMTP() and a server bound to "" raise
    NetworkBlockedError ("gethostbyaddr <hostname>"): tests bind and connect to 127.0.0.1 or
    localhost."""
    global _guard_installed
    if _guard_installed:
        return
    _guard_installed = True
    _guard_sockets()
    _guard_lookups()
    if sys.platform == "win32":
        _guard_proactor()


def _guard_sockets() -> None:
    real_connect, real_connect_ex = socket.socket.connect, socket.socket.connect_ex
    real_sendto = socket.socket.sendto

    def connect(sock: socket.socket, address: Any) -> None:
        _check("connect", sock, address)
        return real_connect(sock, address)

    def connect_ex(sock: socket.socket, address: Any) -> int:
        _check("connect_ex", sock, address)
        return real_connect_ex(sock, address)

    def sendto(sock: socket.socket, data: Any, *args: Any) -> int:
        # (data, address) or (data, flags, address)
        _check("sendto", sock, args[-1] if args else None)
        return real_sendto(sock, data, *args)

    socket.socket.connect = connect  # type: ignore[method-assign]
    socket.socket.connect_ex = connect_ex  # type: ignore[method-assign]
    socket.socket.sendto = sendto  # type: ignore[assignment]
    if hasattr(socket.socket, "sendmsg"):  # POSIX only
        real_sendmsg = socket.socket.sendmsg

        def sendmsg(sock: socket.socket, *args: Any) -> int:
            # (buffers, ancdata, flags, address), the address optional
            _check("sendmsg", sock, args[3] if len(args) > 3 else None)
            return real_sendmsg(sock, *args)

        socket.socket.sendmsg = sendmsg  # type: ignore[method-assign]


def _guard_lookups() -> None:
    real_getaddrinfo, real_getnameinfo = socket.getaddrinfo, socket.getnameinfo
    real_byname, real_byname_ex = socket.gethostbyname, socket.gethostbyname_ex
    real_byaddr = socket.gethostbyaddr

    def getaddrinfo(host: Any, port: Any, *args: Any, **kwargs: Any) -> Any:
        # asyncio's loop.getaddrinfo comes here too.
        if not _loopback(host):
            _refuse(f"getaddrinfo {host}:{port}")
        return real_getaddrinfo(host, port, *args, **kwargs)

    def getnameinfo(sockaddr: Any, flags: int) -> Any:
        if not _loopback(sockaddr[0]):
            _refuse(f"getnameinfo {sockaddr[0]}:{sockaddr[1]}")
        return real_getnameinfo(sockaddr, flags)

    def by(verb: str, real: Any) -> Any:
        def lookup(name: Any) -> Any:
            if not _loopback(name):
                _refuse(f"{verb} {name}")
            return real(name)

        return lookup

    socket.getaddrinfo = getaddrinfo
    socket.getnameinfo = getnameinfo
    socket.gethostbyname = by("gethostbyname", real_byname)
    socket.gethostbyname_ex = by("gethostbyname_ex", real_byname_ex)
    socket.gethostbyaddr = by("gethostbyaddr", real_byaddr)


def _guard_proactor() -> None:
    """asyncio's proactor loop connects and sends datagrams through IOCP (Overlapped.ConnectEx,
    WSASendTo), never through the socket's own methods."""
    from asyncio import windows_events

    real_connect = windows_events.IocpProactor.connect
    real_sendto = windows_events.IocpProactor.sendto

    def connect(proactor: Any, conn: socket.socket, address: Any) -> Any:
        _check("IocpProactor.connect", conn, address)
        return real_connect(proactor, conn, address)

    def sendto(
        proactor: Any, conn: socket.socket, buf: Any, flags: int = 0, addr: Any = None
    ) -> Any:
        _check("IocpProactor.sendto", conn, addr)
        return real_sendto(proactor, conn, buf, flags, addr)

    windows_events.IocpProactor.connect = connect  # type: ignore[method-assign]
    windows_events.IocpProactor.sendto = sendto  # type: ignore[method-assign]


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


@pytest.fixture
def loopback_rule() -> Callable[[object], bool]:
    """The guard's own decision of what stays on this machine, for the test that pins it."""
    return _loopback


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
