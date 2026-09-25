"""Every run prints which tree of code is under test; a green run without it proves nothing."""

import sys

import pytest

import telegram_dashboard
from telegram_dashboard import collect


def pytest_report_header(config):
    return [
        f"telegram_dashboard.__file__ = {telegram_dashboard.__file__}",
        f"python = {sys.executable}",
    ]


def _stub(provider: str, source: str):
    def stub(*, now):
        return {
            "provider": provider,
            "status": "unavailable",
            "reason": "test stub",
            "source": source,
            "fetched_at": None,
            "windows": [],
        }

    return stub


def _release_stub(*, now):
    return {
        "status": "unavailable",
        "reason": "test stub",
        "source": "github_releases",
        "checked_at": now.isoformat(),
    }


@pytest.fixture(autouse=True)
def no_provider_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """No test reaches xAI, Kimi or GitHub: the default attempt is a stub unless a test passes
    its own; the running Hermes version is a fixed one, not whatever this interpreter imported."""
    monkeypatch.setattr(collect, "grok_fetch_item", _stub("grok", "grok_cli_billing"))
    monkeypatch.setattr(collect, "kimi_fetch_item", _stub("kimi", "kimi_code_usages"))
    monkeypatch.setattr(collect, "version_fetch_item", _release_stub)
    monkeypatch.setattr(collect, "running_version", lambda: ("0.21.3", None))
