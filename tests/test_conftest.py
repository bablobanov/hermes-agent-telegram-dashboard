"""The suite's own rules in conftest.py.

The skip rule lives in hooks, which act on a whole run; so a small run of throwaway test files is
started in a subprocess with conftest.py as its plugin, and its outcome is read. The network
guard is proven in this process, against addresses where even a broken guard reaches nothing.
Offline, a second or two."""

from __future__ import annotations

import asyncio
import os
import re
import socket
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

TESTS_DIR = Path(__file__).resolve().parent
PACKAGE_DIR = TESTS_DIR.parent / "plugin" / "telegram_dashboard_probe"

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
    # A reason on ENGINE_JOB_SKIPS: stays a skip, and the passing test beside it runs.
    "test_allowed_skip.py": """\
        import pytest
        def test_allowed():
            pytest.skip("time.tzset is POSIX only")
        def test_passes():
            pass
        """,
}
OUTCOME = re.compile(r"(\d+) (failed|passed|skipped|error)s?\b")


def _run(tmp_path: Path, *, engine_job: bool) -> subprocess.CompletedProcess[str]:
    for name, text in THROWAWAY.items():
        (tmp_path / name).write_text(textwrap.dedent(text), encoding="utf-8")
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
    command += ["-c", str(tmp_path / "pytest.ini"), "-rfEs", str(tmp_path)]
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
    # The module skipped at import is a collection error, and the run goes on past it.
    assert _counts(run.stdout) == {"failed": 1, "passed": 1, "skipped": 1, "error": 1}, output
    # Each reason on the run's summary lines, with the variable that made it a failure.
    assert re.search(
        r"ERROR .*test_module_skip\.py - .*REQUIRE_ENGINE_TESTS=1: skipped at "
        r"test_module_skip\.py:2, .*: module reason not on the list",
        run.stdout,
    ), output
    assert re.search(
        r"FAILED .*test_function_skip\.py::test_skips - .*REQUIRE_ENGINE_TESTS=1: skipped at "
        r"test_function_skip\.py:3, .*: function reason not on the list",
        run.stdout,
    ), output
    assert re.search(r"SKIPPED \[1\] test_allowed_skip\.py:3: time\.tzset is POSIX only", output)


def test_without_the_variable_every_skip_stays_a_skip(tmp_path: Path) -> None:
    run = _run(tmp_path, engine_job=False)
    output = run.stdout + run.stderr

    assert run.returncode == 0, output
    assert _counts(run.stdout) == {"passed": 1, "skipped": 3}, output
    assert "REQUIRE_ENGINE_TESTS" not in output


# TEST-NET-1 (RFC 5737) and the reserved .invalid (RFC 6761): nothing answers there even if the
# guard let a packet out.
OFF_MACHINE = ("192.0.2.1", 443)


def test_the_guard_refuses_a_host_off_this_machine_and_records_it(no_network: list[str]) -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        with pytest.raises(
            RuntimeError, match=r"^network blocked in tests: connect 192\.0\.2\.1:443"
        ) as refused:
            sock.connect(OFF_MACHINE)
        with pytest.raises(RuntimeError, match=r"connect_ex 192\.0\.2\.1:443"):
            sock.connect_ex(OFF_MACHINE)
    with pytest.raises(RuntimeError, match=r"getaddrinfo example\.invalid:443"):
        socket.getaddrinfo("example.invalid", 443)

    assert refused.type.__name__ == "NetworkBlockedError"
    assert no_network == [
        "connect 192.0.2.1:443",
        "connect_ex 192.0.2.1:443",
        "getaddrinfo example.invalid:443",
    ]
    no_network.clear()  # refused on purpose: these must not fail this test at its end


# The platform's own loop (the proactor on Windows, the selector elsewhere) and the selector
# loop wherever it runs: the proactor connects through IOCP's ConnectEx and never calls
# socket.connect, the selector calls socket.connect.
LOOPS = {"default": asyncio.new_event_loop, "selector": asyncio.SelectorEventLoop}


@pytest.mark.parametrize("loop_name", sorted(LOOPS))
def test_an_event_loop_connect_off_this_machine_is_refused(
    loop_name: str, no_network: list[str]
) -> None:
    async def reach() -> str:
        with pytest.raises(RuntimeError, match=r"^network blocked in tests: .*192\.0\.2\.1:443"):
            await asyncio.open_connection(*OFF_MACHINE)
        return type(asyncio.get_running_loop()).__name__

    with asyncio.Runner(loop_factory=LOOPS[loop_name]) as runner:
        loop = runner.run(reach())

    via = "IocpProactor.connect" if loop == "ProactorEventLoop" else "connect"
    assert no_network == [f"{via} 192.0.2.1:443"], loop
    assert loop_name != "default" or (loop == "ProactorEventLoop") == (sys.platform == "win32")
    no_network.clear()


def test_the_guard_leaves_loopback_and_local_pairs_open(no_network: list[str]) -> None:
    with socket.create_server(("127.0.0.1", 0)) as server:
        port = server.getsockname()[1]
        with socket.create_connection(("127.0.0.1", port), timeout=5):
            pass
        assert socket.getaddrinfo("localhost", port)

        async def through_the_event_loop() -> None:
            _reader, writer = await asyncio.open_connection("127.0.0.1", port)
            writer.close()
            await writer.wait_closed()

        asyncio.run(through_the_event_loop())  # its self-pipe is a local pair too
    left, right = socket.socketpair()  # AF_UNIX on POSIX, loopback TCP on Windows
    with left, right:
        left.sendall(b"x")
        assert right.recv(1) == b"x"

    assert no_network == []
