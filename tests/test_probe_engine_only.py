"""Checks of the plugin that need the Hermes engine only, not python-telegram-bot.

Skipped where ``hermes_cli`` is not importable, so the ordinary suite runs without Hermes; the CI
engine job runs them with ``REQUIRE_ENGINE_TESTS=1``, where a skip fails the run.
"""

from __future__ import annotations

from pathlib import Path

import pytest

core_override = pytest.importorskip(
    "hermes_cli.plugin_validate_core_override", reason="Hermes engine not importable here"
)

PLUGIN_DIR = Path(__file__).resolve().parents[1] / "plugin" / "telegram_dashboard_probe"


def test_the_plugin_passes_the_engine_s_no_core_override_check() -> None:
    """Catalog rule 9: the plugin rebinds no Hermes core at runtime, the registry included (a
    module of its own name in ``sys.modules``, outside ``hermes_plugins.*``). The engine's own
    static check, the one ``hermes plugins validate`` runs."""
    assert core_override.core_override_findings(PLUGIN_DIR) == []
