"""Checks of the plugin that need the Hermes engine only, not python-telegram-bot.

Skipped where ``hermes_cli`` is not importable, so the ordinary suite runs without Hermes; the CI
engine job runs them with ``REQUIRE_ENGINE_TESTS=1``, where a skip fails the run.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

core_override = pytest.importorskip(
    "hermes_cli.plugin_validate_core_override", reason="Hermes engine not importable here"
)

PLUGIN_DIR = Path(__file__).resolve().parents[1] / "plugin" / "telegram_dashboard_probe"
LIVE_MODULE = "_telegram_dashboard_probe_live"


def test_the_plugin_passes_the_engine_s_no_core_override_check() -> None:
    """Catalog rule 9: the plugin rebinds no Hermes core at runtime, the registry included (a
    module of its own name in ``sys.modules``, outside ``hermes_plugins.*``). The engine's own
    static check, the one ``hermes plugins validate`` runs."""
    assert core_override.core_override_findings(PLUGIN_DIR) == []


@pytest.mark.parametrize("abandoned", [False, True])
def test_a_load_the_engine_abandoned_claims_no_profile(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, abandoned: bool
) -> None:
    """Review M4 on the engine's own context: past ``plugins.load_timeout_seconds`` the engine
    marks the context abandoned and its registrars, ``on_unload`` included, answer ``None``.
    Then ``register()`` claims no profile in the registry (and the factory is ignored). The
    control: a load in time claims the profile and registers the factory."""
    import sys

    from hermes_cli.plugins import PluginContext, PluginManager, PluginManifest
    from probe_fakes import CHAT, load_plugin

    for name in [name for name in os.environ if name.startswith("HERMES_DASHBOARD_PROBE_")]:
        monkeypatch.delenv(name)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("HERMES_DASHBOARD_PROBE_CHAT", CHAT)
    monkeypatch.setenv("HERMES_DASHBOARD_PROBE_LIMITS", "0")
    monkeypatch.delitem(sys.modules, LIVE_MODULE, raising=False)
    manager = PluginManager()
    manifest = PluginManifest(name="telegram_dashboard_probe", version="1.1.0", description="probe")
    ctx = PluginContext(manifest, manager)
    if abandoned:
        ctx._abandon_load()  # what run_with_load_deadline does once the deadline passed

    runtime = load_plugin().register(ctx)

    assert runtime is not None
    factories = manager.get_platform_handler_factories("telegram")
    registry = sys.modules.get(LIVE_MODULE)
    claimed = registry is not None and runtime.key in registry.profiles
    assert claimed is not abandoned
    assert [name for _, name in factories] == ([] if abandoned else ["telegram_dashboard_probe"])
    monkeypatch.delitem(sys.modules, LIVE_MODULE, raising=False)
    manager.unload()
