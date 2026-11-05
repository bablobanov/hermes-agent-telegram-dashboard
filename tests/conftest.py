"""Every run prints which tree of code is under test; a green run without it proves nothing.

With ``REQUIRE_ENGINE_TESTS=1`` (the CI ``engine`` job, or a local run in a Hermes venv) a skip
fails the run unless its reason is in ``ENGINE_JOB_SKIPS``: a test that skipped there did not run
against the engine, and a green job would say it had."""

import os
import sys
from typing import Literal

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
    if ENGINE_JOB:
        # A module skipped at import fails its collection; the rest of the suite runs on, so one
        # run names every test the engine did not run, not only the first module.
        config.option.continue_on_collection_errors = True


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
