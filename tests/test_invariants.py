"""The five properties the catalog review (hermes-agent#122408) checked by hand, pinned here so
a later change cannot undo one of them quietly:

1. in plugin mode the bot token is never read; delivery goes through the gateway's adapter
2. the plugin listens on no port
3. ``drift_command`` runs as a list of arguments, never through a shell
4. state is written through the engine's ``PluginState`` only (``HERMES_HOME/plugin-data/``)
5. nothing outlives a disable: the one task is spawned through ``ctx.spawn_task``

These read the plugin's source. The live behaviour behind each is exercised elsewhere:
``test_probe_screen.py`` and ``test_probe_plugin.py`` (delivery through the adapter, the record
through the state object, one task across reconnects), ``test_collect.py`` (the drift command).
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

from probe_fakes import PLUGIN_DIR

ENTRY = PLUGIN_DIR / "__init__.py"
PACKAGE = PLUGIN_DIR / "telegram_dashboard"
# The cron path (a separate bot, ``python -m telegram_dashboard``) owns its own token and its own
# Bot API client; the plugin must not reach either.
CRON_ONLY_MODULES = ("__main__", "telegram_api", "delivery")


def _plugin_sources() -> list[tuple[Path, str]]:
    files = [ENTRY, *sorted(PACKAGE.glob("*.py"))]
    return [(path, path.read_text(encoding="utf-8")) for path in files]


def _code_only(source: str) -> str:
    """The source without docstrings and comments: prose may name what code must not do."""
    tree = ast.parse(source)
    lines = source.splitlines()
    drop: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef):
            doc = node.body[0] if node.body else None
            if isinstance(doc, ast.Expr) and isinstance(doc.value, ast.Constant):
                drop.update(range(doc.lineno - 1, doc.end_lineno or doc.lineno))
    kept = [line.split("#", 1)[0] for i, line in enumerate(lines) if i not in drop]
    return "\n".join(kept)


def test_plugin_mode_never_reads_the_bot_token() -> None:
    entry = _code_only(ENTRY.read_text(encoding="utf-8"))
    assert not re.search(r"token", entry, re.IGNORECASE)
    # The only environment the entry reads: its own settings and the engine home.
    env_names = re.findall(r"os\.environ\.get\((\w+)", entry)
    assert env_names and set(env_names) <= {"ENV_HOME", "env_name"}, env_names
    assert not re.search(r"\.env\b|dotenv", entry)
    # Delivery is the adapter's job: no Bot API client of its own in the entry ...
    assert not re.search(r"\b(import telegram|urllib|http\.client|requests|aiohttp|httpx)\b", entry)
    # ... and the cron-path modules that carry one are never imported by the plugin, neither
    # by an import statement nor by name through importlib (DASHBOARD_MODULES).
    for name in CRON_ONLY_MODULES:
        assert not re.search(rf"(import|from)\s+[\w.]*\b{name}\b", entry), name
        assert f'"{name}"' not in entry and f"'{name}'" not in entry, name


def test_the_plugin_listens_on_no_port() -> None:
    for path, source in _plugin_sources():
        code = _code_only(source)
        assert not re.search(r"\bimport socket\b|\bfrom socket\b", code), path.name
        assert not re.search(r"http\.server|start_server|aiohttp\.web|\.listen\(|\.bind\(", code), (
            path.name
        )


def test_the_drift_command_runs_as_argv_without_a_shell() -> None:
    source = (PACKAGE / "collect.py").read_text(encoding="utf-8")
    calls = [
        node
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr in ("Popen", "run", "call", "check_output", "check_call")
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "subprocess"
    ]
    assert calls, "collect.py runs the drift command through subprocess"
    for call in calls:
        keywords = {kw.arg for kw in call.keywords}
        assert "shell" not in keywords
        first = call.args[0] if call.args else None
        # ``list(argv)`` for the drift command; ``taskkill`` below it is a literal list too.
        assert isinstance(first, ast.Call | ast.List), ast.dump(first) if first else first
    assert "shell=True" not in source
    for path, code in _plugin_sources():
        assert "os.system(" not in code, path.name


def test_state_goes_through_the_engine_s_plugin_state_only() -> None:
    entry = _code_only(ENTRY.read_text(encoding="utf-8"))
    # The entry writes nothing to disk itself: the record is ``ctx.state.set`` (the engine puts
    # it under HERMES_HOME/plugin-data/<namespace>/state.json), the reads are the collectors'.
    assert not re.search(r"\bopen\(|write_text|write_bytes|mkdir|unlink|rename|shutil", entry)
    assert re.search(r"state\.set\(STATE_KEY", entry)
    assert "plugin-data" in ENTRY.read_text(encoding="utf-8")


def test_the_one_task_is_spawned_through_the_context_and_nothing_else() -> None:
    entry = _code_only(ENTRY.read_text(encoding="utf-8"))
    assert entry.count("spawn_task(") == 1
    assert not re.search(r"create_task\(|ensure_future\(|threading|Thread\(|atexit|signal\.", entry)
    assert not re.search(r"subprocess|Popen", entry)


def test_a_bot_token_variable_is_never_a_source_key(monkeypatch) -> None:
    """Ilya's amendment of 29.09 to the subscription plan: an external limit source may name the
    variable its key lives in, but never one that holds the bot token, or the plugin would read
    the token after all (property 1 above).

    The list: the engine's Telegram adapter reads ``TELEGRAM_BOT_TOKEN`` (v2026.9.14:
    ``plugins/platforms/telegram/adapter.py:6645``, ``:6655``, ``gateway/config.py:350``); the
    cron path of this dashboard reads ``HERMES_DASHBOARD_BOT_TOKEN``; and any ``*_BOT_TOKEN``, in
    any case, for safety."""
    import json
    from datetime import UTC, datetime

    from telegram_dashboard import external
    from telegram_dashboard.__main__ import _DEFAULT_TOKEN_ENV

    assert {"TELEGRAM_BOT_TOKEN", _DEFAULT_TOKEN_ENV} == external.BOT_TOKEN_VARIABLES
    secret = "123456789:AAE-planted-bot-secret-for-this-test-only"
    now = datetime(2026, 9, 29, tzinfo=UTC)
    calls: list[object] = []

    def get(*args: object) -> tuple[int, bytes]:
        calls.append(args)
        return 200, b"{}"

    names = (
        "TELEGRAM_BOT_TOKEN",
        "HERMES_DASHBOARD_BOT_TOKEN",
        "SOME_BOT_TOKEN",
        "telegram_bot_token",
        "Other_Bot_Token",
    )
    for name in names:
        monkeypatch.setenv(name, secret)
        (source,) = external.read_sources([{"url": "http://127.0.0.1:18080/u", "key_env": name}])
        assert source.problem == "key_env names a bot token variable", name
        item = external.fetch_item(source, now=now, get=get)
        assert secret not in json.dumps(item)
    # An entry built by hand, past the settings check, is refused just the same.
    forged = external.Source(1, "http://127.0.0.1:18080/u", "TELEGRAM_BOT_TOKEN", 20.0)
    item = external.fetch_item(forged, now=now, get=get)
    assert calls == [] and item["reason"] == "key_env names a bot token variable"
