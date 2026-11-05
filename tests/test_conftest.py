"""The suite's own rules in conftest.py, proven on throwaway test files in a separate pytest.

The rules live in hooks, which act on a whole run; so a small run is started in a subprocess with
conftest.py as its plugin, and its outcome is read. Offline, a second or two."""

from __future__ import annotations

import os
import re
import subprocess
import sys
import textwrap
from pathlib import Path

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
