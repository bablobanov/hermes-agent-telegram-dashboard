"""The suite's own rules in conftest.py.

The skip rule lives in hooks, which act on a whole run; so a small run of throwaway test files is
started in a subprocess with conftest.py as its plugin, and its outcome is read. The network
guard is proven in this process, against addresses where even a broken guard reaches nothing, and
so is the temporary HERMES_HOME of the session. Offline, a second or two."""

from __future__ import annotations

import asyncio
import contextlib
import os
import re
import socket
import subprocess
import sys
import tempfile
import textwrap
from pathlib import Path
from typing import Any

import pytest

TESTS_DIR = Path(__file__).resolve().parent
PACKAGE_DIR = TESTS_DIR.parent / "plugin" / "telegram_dashboard_probe"

PROVIDER_REASON = "the engine is importable here: the real facade would call provider APIs"
# Under tests/ of the throwaway root, so their node ids name files as the allowlist does.
THROWAWAY = {
    "test_module_skip.py": """\
        import pytest
        pytest.importorskip("no_such_module_here", reason="module reason not on the list")
        def test_never_collected():
            pass
        """,
    "test_function_skip.py": """\
        import pytest
        def test_skips():
            pytest.skip("function reason not on the list")
        """,
    # The file and the reason of an entry for any platform: stays a skip, and the passing test
    # beside it runs.
    "test_collect.py": f"""\
        import pytest
        def test_allowed():
            pytest.skip("{PROVIDER_REASON}")
        def test_passes():
            pass
        """,
    # The same reason from another file.
    "test_elsewhere.py": f"""\
        import pytest
        def test_same_reason_elsewhere():
            pytest.skip("{PROVIDER_REASON}")
        """,
    # The file and the reason of a Windows-only entry.
    "test_gemini_log.py": """\
        import pytest
        def test_tzset():
            pytest.skip("time.tzset is POSIX only")
        """,
    # An xfail that stops its test, and one that never starts it; a test under an xfail marker
    # that fails with any exception stays an xfail.
    "test_xfail.py": """\
        import pytest
        def test_imperative():
            pytest.xfail("imperative reason")
        @pytest.mark.xfail(run=False, reason="never run")
        def test_never_run():
            pass
        @pytest.mark.xfail(reason="ran and failed")
        def test_ran_and_failed():
            assert False
        """,
}
OUTCOME = re.compile(r"(\d+) (xfailed|failed|passed|skipped|error)s?\b")
WINDOWS = sys.platform == "win32"


def _run(tmp_path: Path, *, engine_job: bool) -> subprocess.CompletedProcess[str]:
    (tmp_path / "tests").mkdir()
    for name, text in THROWAWAY.items():
        (tmp_path / "tests" / name).write_text(textwrap.dedent(text), encoding="utf-8")
    (tmp_path / "pytest.ini").write_text("[pytest]\n", encoding="utf-8")
    env = dict(os.environ)
    env.pop("REQUIRE_ENGINE_TESTS", None)
    if engine_job:
        env["REQUIRE_ENGINE_TESTS"] = "1"
    paths = [str(TESTS_DIR), str(PACKAGE_DIR), env.get("PYTHONPATH", "")]
    env["PYTHONPATH"] = os.pathsep.join(path for path in paths if path)
    env["PYTHONIOENCODING"] = "utf-8"
    env["COLUMNS"] = "400"  # the summary lines whole, not cut at 80 characters
    command = [sys.executable, "-m", "pytest", "-p", "conftest", "-p", "no:cacheprovider"]
    # importlib: the throwaway test_collect.py must not meet the real one on PYTHONPATH.
    command += ["-c", str(tmp_path / "pytest.ini"), "--import-mode=importlib", "-rfEsx"]
    command.append(str(tmp_path))
    return subprocess.run(
        command,
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=120,
        check=False,
    )


def _counts(output: str) -> dict[str, int]:
    """The outcomes on the run's last line; warnings aside (an engine venv brings plugins)."""
    summary = output.strip().splitlines()[-1]
    return {word: int(n) for n, word in OUTCOME.findall(summary)}


def test_in_an_engine_job_a_skip_off_the_list_fails_the_run_and_says_why(tmp_path: Path) -> None:
    run = _run(tmp_path, engine_job=True)
    output = run.stdout + run.stderr

    assert run.returncode == 1, output
    # The module skipped at import is a collection error, and the run goes on past it; the
    # xfail that never starts the test is an error at setup. The Windows-only entry holds on
    # Windows alone.
    assert _counts(run.stdout) == {
        "failed": 3 if WINDOWS else 4,
        "passed": 1,
        "skipped": 2 if WINDOWS else 1,
        "xfailed": 1,
        "error": 2,
    }, output
    # Each reason on the run's summary lines, with the variable that made it a failure.
    refused = r" - .*REQUIRE_ENGINE_TESTS=1: "
    lines = [
        rf"ERROR tests/test_module_skip\.py{refused}skipped at "
        r"tests[\\/]test_module_skip\.py:2, .*: module reason not on the list",
        rf"FAILED tests/test_function_skip\.py::test_skips{refused}skipped at "
        r"tests[\\/]test_function_skip\.py:3, .*: function reason not on the list",
        rf"FAILED tests/test_elsewhere\.py::test_same_reason_elsewhere{refused}skipped at "
        rf"tests[\\/]test_elsewhere\.py:3, .*: {re.escape(PROVIDER_REASON)}",
        rf"FAILED tests/test_xfail\.py::test_imperative{refused}xfailed before its end, "
        r".*: imperative reason",
        rf"ERROR tests/test_xfail\.py::test_never_run{refused}xfailed before its end, "
        r".*: \[NOTRUN\] never run",
        rf"SKIPPED \[1\] tests[\\/]test_collect\.py:3: {re.escape(PROVIDER_REASON)}",
        r"XFAIL tests/test_xfail\.py::test_ran_and_failed - ran and failed",
        r"SKIPPED \[1\] tests[\\/]test_gemini_log\.py:3: time\.tzset is POSIX only"
        if WINDOWS
        else rf"FAILED tests/test_gemini_log\.py::test_tzset{refused}.*time\.tzset is POSIX only",
    ]
    for line in lines:
        assert re.search(line, run.stdout), (line, output)


def test_without_the_variable_every_skip_stays_a_skip(tmp_path: Path) -> None:
    run = _run(tmp_path, engine_job=False)
    output = run.stdout + run.stderr

    assert run.returncode == 0, output
    assert _counts(run.stdout) == {"passed": 1, "skipped": 5, "xfailed": 3}, output
    assert "REQUIRE_ENGINE_TESTS" not in output


# TEST-NET-1 (RFC 5737) and the reserved .invalid (RFC 6761): nothing answers there even if the
# guard let a packet out.
OFF_MACHINE = ("192.0.2.1", 443)
OFF_MACHINE_UDP = ("192.0.2.1", 53)


def test_the_guard_refuses_a_host_off_this_machine_and_records_it(no_network: list[str]) -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        with pytest.raises(
            RuntimeError, match=r"^network blocked in tests: connect 192\.0\.2\.1:443"
        ) as refused:
            sock.connect(OFF_MACHINE)
        with pytest.raises(RuntimeError, match=r"connect_ex 192\.0\.2\.1:443"):
            sock.connect_ex(OFF_MACHINE)
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as udp:
        with pytest.raises(RuntimeError, match=r"sendto 192\.0\.2\.1:53"):
            udp.sendto(b"x", OFF_MACHINE_UDP)
        with pytest.raises(RuntimeError, match=r"sendto 192\.0\.2\.1:53"):
            udp.sendto(b"x", 0, OFF_MACHINE_UDP)
        if hasattr(udp, "sendmsg"):  # POSIX; Windows sockets have no sendmsg
            with pytest.raises(RuntimeError, match=r"sendmsg 192\.0\.2\.1:53"):
                udp.sendmsg([b"x"], [], 0, OFF_MACHINE_UDP)
    with pytest.raises(RuntimeError, match=r"getaddrinfo example\.invalid:443"):
        socket.getaddrinfo("example.invalid", 443)
    with pytest.raises(RuntimeError, match=r"gethostbyname example\.invalid"):
        socket.gethostbyname("example.invalid")
    with pytest.raises(RuntimeError, match=r"gethostbyname_ex example\.invalid"):
        socket.gethostbyname_ex("example.invalid")
    with pytest.raises(RuntimeError, match=r"gethostbyaddr 192\.0\.2\.1"):
        socket.gethostbyaddr("192.0.2.1")
    with pytest.raises(RuntimeError, match=r"getnameinfo 192\.0\.2\.1:443"):
        socket.getnameinfo(OFF_MACHINE, 0)

    assert refused.type.__name__ == "NetworkBlockedError"
    sent = ["sendto 192.0.2.1:53"] * 2 + (
        ["sendmsg 192.0.2.1:53"] if hasattr(socket.socket, "sendmsg") else []
    )
    assert no_network == [
        "connect 192.0.2.1:443",
        "connect_ex 192.0.2.1:443",
        *sent,
        "getaddrinfo example.invalid:443",
        "gethostbyname example.invalid",
        "gethostbyname_ex example.invalid",
        "gethostbyaddr 192.0.2.1",
        "getnameinfo 192.0.2.1:443",
    ]
    no_network.clear()  # refused on purpose: these must not fail this test at its end


# The platform's own loop (the proactor on Windows, the selector elsewhere) and the selector
# loop wherever it runs: the proactor connects and sends datagrams through IOCP and never calls
# the socket's own methods, the selector calls them.
LOOPS = {"default": asyncio.new_event_loop, "selector": asyncio.SelectorEventLoop}


@pytest.mark.parametrize("loop_name", sorted(LOOPS))
def test_an_event_loop_off_this_machine_is_refused(loop_name: str, no_network: list[str]) -> None:
    async def reach() -> str:
        with pytest.raises(RuntimeError, match=r"^network blocked in tests: .*192\.0\.2\.1:443"):
            await asyncio.open_connection(*OFF_MACHINE)
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as udp:
            udp.setblocking(False)
            with pytest.raises(RuntimeError, match=r"^network blocked in tests: .*192\.0\.2\.1:53"):
                await asyncio.get_running_loop().sock_sendto(udp, b"x", OFF_MACHINE_UDP)
        return type(asyncio.get_running_loop()).__name__

    with asyncio.Runner(loop_factory=LOOPS[loop_name]) as runner:
        loop = runner.run(reach())

    via = "IocpProactor." if loop == "ProactorEventLoop" else ""
    assert no_network == [f"{via}connect 192.0.2.1:443", f"{via}sendto 192.0.2.1:53"], loop
    assert loop_name != "default" or (loop == "ProactorEventLoop") == (sys.platform == "win32")
    no_network.clear()


# All of 127.0.0.0/8, ::1, IPv4-mapped loopback and the name localhost; None is the local host
# to getaddrinfo.
THIS_MACHINE = [
    "127.0.0.1",
    "127.8.9.10",
    "127.255.255.254",
    "::1",
    "::ffff:127.0.0.1",
    "localhost",
    "LocalHost",
    b"127.0.0.1",
    None,
]
OFF_THIS_MACHINE = [
    "192.0.2.1",
    "10.0.0.1",
    "128.0.0.1",
    "0.0.0.0",
    "::",
    "::ffff:192.0.2.1",
    "2001:db8::1",
    "fe80::1%1",
    "example.invalid",
    "localhost.example.invalid",
    "127.0.0.1.example.invalid",
    "",
]


@pytest.mark.parametrize("host", THIS_MACHINE)
def test_the_guard_counts_these_as_this_machine(loopback_rule: Any, host: object) -> None:
    assert loopback_rule(host)


@pytest.mark.parametrize("host", OFF_THIS_MACHINE)
def test_the_guard_counts_these_off_this_machine(loopback_rule: Any, host: object) -> None:
    assert not loopback_rule(host)


def _round_trip(host: str, family: socket.AddressFamily) -> None:
    """A real connect to a listener on ``host``, plain and through the event loop."""
    with socket.create_server((host, 0), family=family) as server:
        port = server.getsockname()[1]
        with socket.create_connection((host, port), timeout=5):
            pass

        async def through_the_event_loop() -> None:
            _reader, writer = await asyncio.open_connection(host, port)
            writer.close()
            await writer.wait_closed()

        asyncio.run(through_the_event_loop())  # its self-pipe is a local pair too


def test_the_guard_leaves_loopback_and_local_pairs_open(no_network: list[str]) -> None:
    _round_trip("127.0.0.1", socket.AF_INET)
    assert socket.getaddrinfo("localhost", 80)
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as receiver:
        receiver.bind(("127.0.0.1", 0))
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sender:
            sender.sendto(b"x", receiver.getsockname())
        receiver.settimeout(5)
        assert receiver.recv(1) == b"x"
    left, right = socket.socketpair()  # AF_UNIX on POSIX, loopback TCP on Windows
    with left, right:
        left.sendall(b"x")
        assert right.recv(1) == b"x"

    assert no_network == []


def test_the_guard_leaves_ipv6_loopback_open_where_the_host_has_it(
    no_network: list[str], loopback_rule: Any
) -> None:
    """A real round trip on ::1 where this host allows one. Where it does not (no IPv6, or the
    host refuses the connect: WinError 10013 on the Windows machine of 06.11), the guard's own
    decision for ::1 is the proof, and nothing is skipped. A refusal by the guard is a
    RuntimeError, never the OSError caught here, and it would be recorded too."""
    assert loopback_rule("::1")
    with contextlib.suppress(OSError):
        _round_trip("::1", socket.AF_INET6)

    assert no_network == []


def test_the_session_runs_with_a_temporary_hermes_home(session_temp_root: str) -> None:
    """The home is a directory of its own, made in the OS temp root. The engine's bootstrap may
    move the process's temp root afterwards (see ``_SESSION_TEMP_ROOT`` in conftest), and then
    only into that home: never back to a directory outside it."""
    home = Path(os.environ["HERMES_HOME"]).resolve()
    root = Path(session_temp_root).resolve()

    assert home.name.startswith("hermes-test-home-") and home.is_dir()
    assert home.parent == root
    now = Path(tempfile.gettempdir()).resolve()
    assert now == root or now.is_relative_to(home), now


def test_the_engine_resolves_its_home_to_the_temporary_one() -> None:
    """The engine's own resolver (hermes_constants.get_hermes_home, 0.22.2 and 0.22.7): what
    any engine code a test reaches uses for its profile."""
    constants = pytest.importorskip("hermes_constants", reason="Hermes is not here")

    assert constants.get_hermes_home() == Path(os.environ["HERMES_HOME"])
    assert Path(os.environ["HERMES_HOME"]).name.startswith("hermes-test-home-")
