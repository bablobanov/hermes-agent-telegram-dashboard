"""Fakes for the plugin's own logic: no engine, no network. Shared by the probe test modules.

``load_plugin`` mirrors what the engine's loader does with a directory plugin
(``hermes_cli/plugins_loader.py::_load_directory_module``): the module is a package named after
its slug with ``__path__`` set to the plugin directory, so relative imports resolve inside it.
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import sys
import time
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any

PLUGIN_DIR = Path(__file__).resolve().parents[1] / "plugin" / "telegram_dashboard_probe"
CHAT = "-1001000000001"  # fixture value, not a real chat


def load_plugin(name: str = "probe_on_fakes", plugin_dir: Path = PLUGIN_DIR) -> ModuleType:
    init_file = plugin_dir / "__init__.py"
    for cached in [key for key in sys.modules if key == name or key.startswith(f"{name}.")]:
        del sys.modules[cached]
    spec = importlib.util.spec_from_file_location(
        name, init_file, submodule_search_locations=[str(plugin_dir)]
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    module.__package__ = name
    module.__path__ = [str(plugin_dir)]
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


class FakeState:
    """Same contract as the engine's PluginState: JSON values under string keys."""

    def __init__(self) -> None:
        self.data: dict[str, Any] = {}

    def get(self, key: str, default: Any = None) -> Any:
        return self.data.get(key, default)

    def set(self, key: str, value: Any) -> None:
        self.data[key] = json.loads(json.dumps(value))


class FakeContext:
    def __init__(self, settings: dict[str, Any]) -> None:
        self._settings = settings
        self.state = FakeState()
        self.factories: list[tuple[str, Any]] = []

    def get_config(self, key: str) -> Any:
        return self._settings.get(key)

    def register_platform_handler(self, platform: str, factory: Any) -> None:
        self.factories.append((platform, factory))

    def spawn_task(self, coro: Any, name: str | None = None) -> asyncio.Task[None]:
        return asyncio.get_running_loop().create_task(coro, name=name)


class FakeAdapter:
    platform = "telegram"

    def __init__(self) -> None:
        self.gateway_runner = SimpleNamespace(adapters={"telegram": self})
        self.is_connected = True
        self.sent: list[str] = []
        self.edits: list[str] = []

    async def send(self, chat: str, text: str, metadata: Any = None) -> Any:
        self.sent.append(text)
        return SimpleNamespace(success=True, message_id="101", error=None)

    async def edit_message(self, chat: str, message_id: str, text: str) -> Any:
        self.edits.append(text)
        return SimpleNamespace(success=True, error=None)

    @property
    def last_text(self) -> str | None:
        """Whatever reached Telegram last, by either verb."""
        return self.edits[-1] if self.edits else (self.sent[-1] if self.sent else None)


async def until(predicate: Any, *, timeout: float = 3.0) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > deadline:
            raise AssertionError("condition not met in time")
        await asyncio.sleep(0.01)
