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


def load_plugin(
    name: str = "probe_on_fakes", plugin_dir: Path = PLUGIN_DIR, *, vendored: bool = False
) -> ModuleType:
    """The plugin as the engine loads it: a package named ``name`` from ``plugin_dir``.

    The package sits beside the plugin in the repository too (the deployment shape), but the tests
    patch it under the name they import, ``telegram_dashboard`` (conftest's network stub among
    them). A plugin that took the copy beside it would run the same files under a second module
    name that no patch reaches. So by default the plugin gets an empty ``__path__`` (the list
    imports search for submodules; an empty ``submodule_search_locations`` would not do, importlib
    fills it with the file's folder) and takes the installed package, which is those very files;
    ``vendored=True`` lets it see the copy."""
    init_file = plugin_dir / "__init__.py"
    for cached in [key for key in sys.modules if key == name or key.startswith(f"{name}.")]:
        del sys.modules[cached]
    spec = importlib.util.spec_from_file_location(
        name, init_file, submodule_search_locations=[str(plugin_dir)]
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    module.__package__ = name
    module.__path__ = [str(plugin_dir)] if vendored else []
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
    """The two verbs the plugin uses on the engine's Telegram adapter, the way 0.21.x has them:
    the public ``edit_message`` (plain text, a ``SendResult``) and the private ``_edit_text``
    (a parse mode, raises on refusal). ``reject_html`` is the message Telegram would answer the
    HTML edit with; ``fail_plain`` makes the public edit fail the same way every time."""

    platform = "telegram"

    def __init__(self, *, reject_html: str | None = None, fail_plain: str | None = None) -> None:
        self.gateway_runner = SimpleNamespace(adapters={"telegram": self})
        self.is_connected = True
        self.reject_html = reject_html
        self.fail_plain = fail_plain
        self.sent: list[str] = []
        self.edits: list[str] = []  # plain, through edit_message
        self.html_edits: list[tuple[str, Any]] = []  # (text, parse_mode), through _edit_text
        self.texts: list[str] = []  # everything that reached Telegram, in order

    async def send(self, chat: str, text: str, metadata: Any = None) -> Any:
        self.sent.append(text)
        self.texts.append(text)
        return SimpleNamespace(success=True, message_id="101", error=None)

    async def edit_message(self, chat: str, message_id: str, text: str) -> Any:
        if self.fail_plain is not None:
            return SimpleNamespace(success=False, error=self.fail_plain)
        self.edits.append(text)
        self.texts.append(text)
        return SimpleNamespace(success=True, error=None)

    async def _edit_text(
        self, chat_id: str, message_id: str, text: str, parse_mode: Any = None
    ) -> None:
        self.html_edits.append((text, parse_mode))
        if self.reject_html is not None:
            raise RuntimeError(self.reject_html)
        self.texts.append(text)

    @property
    def last_text(self) -> str | None:
        """Whatever reached Telegram last, by any verb."""
        return self.texts[-1] if self.texts else None


class PlainOnlyAdapter(FakeAdapter):
    """An adapter of an engine that has no ``_edit_text``: the attribute exists on the class as
    ``None`` so ``getattr`` finds nothing callable, the way a missing method reads."""

    _edit_text = None  # type: ignore[assignment]


async def until(predicate: Any, *, timeout: float = 3.0) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > deadline:
            raise AssertionError("condition not met in time")
        await asyncio.sleep(0.01)
