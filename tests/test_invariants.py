"""The five properties the catalog review (hermes-agent#122408) checked by hand, pinned here so
a later change cannot undo one of them quietly:

1. in plugin mode the bot token is never read; delivery goes through the gateway's adapter
2. the plugin listens on no port
3. ``drift_command`` runs as a list of arguments, never through a shell
4. state is written through the engine's ``PluginState`` only (``HERMES_HOME/plugin-data/``)
5. nothing outlives a disable: the one task is spawned through ``ctx.spawn_task``

Four more since 0.9.0 (the cron sources and the traffic probe), numbered 6 to 9 below
their tests; 6 widened and 10 and 11 added in 0.10.0 (the rules line reads ``state.db``; no
invisible character), 12 in 0.10.1 (no word of the screen becomes a link or a command).

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
    # The only environment the entry reads: its own settings, the engine home and the engine's
    # TERMINAL_CWD (a path, for the rules line's sandbox case, 0.10.0).
    env_names = re.findall(r"os\.environ\.get\((\w+)", entry)
    allowed = {"ENV_HOME", "ENV_TERMINAL_CWD", "env_name"}
    assert env_names and set(env_names) <= allowed, env_names
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


# ----------------------------------------------------------------------------- 0.9.0, cron sources
#
# Four more properties for 0.9.0 (plan of 01.10): the run history is read-only at the
# connection and at the statement, nothing under HERMES_HOME/cron is ever written, the entry
# registers nothing but the platform handler and the one task, and the traffic probe reads
# the adapter's counters without calling anything on it.

CRON_SOURCE_MODULES = ("cron_jobs.py", "cron_runs.py", "telegram_traffic.py")


def test_the_runs_database_is_opened_read_only_and_query_only() -> None:
    """6. Two SQLite files are ever opened, executions.db (0.9.0) and state.db (0.10.0, the rules
    line), both through the one ``sqlite3.connect`` in ``cron_runs.open_readonly``: mode=ro in
    the URI, uri=True, PRAGMA query_only right after; no write statement, no sqlite3 CLI."""
    sources = _plugin_sources()
    assert any(path.name == "cron_runs.py" for path, _ in sources)
    for path, source in sources:
        code = _code_only(source)
        if path.name == "context_files.py":
            assert "sqlite3.connect" not in code and "open_readonly(" in code
            assert not re.search(r"\b(INSERT|UPDATE|DELETE|CREATE|DROP|VACUUM|ALTER)\b", code)
            assert not re.search(r"journal_mode|\.commit\(|executescript|immutable", code)
            assert "subprocess" not in code
            continue
        if path.name != "cron_runs.py":
            assert "sqlite3" not in code, path.name
            continue
        calls = [
            node
            for node in ast.walk(ast.parse(source))
            if isinstance(node, ast.Call) and ast.unparse(node.func) == "sqlite3.connect"
        ]
        assert len(calls) == 1
        (call,) = calls
        assert {kw.arg for kw in call.keywords} >= {"uri", "timeout"}
        assert any(
            isinstance(kw.value, ast.Constant) and kw.value.value is True
            for kw in call.keywords
            if kw.arg == "uri"
        )
        assert "?mode=ro" in code and "PRAGMA query_only = 1" in code
        assert not re.search(r"\b(INSERT|UPDATE|DELETE|CREATE|DROP|VACUUM|ALTER|REPLACE)\b", code)
        assert not re.search(r"journal_mode|\.commit\(|executescript|immutable=1", code)
        assert "state.db" not in code and "subprocess" not in code


def test_the_cron_sources_never_write_under_the_engine_home_and_never_reach_the_network() -> None:
    """7. jobs.json, the ticker stamps and the database are read; nothing under HERMES_HOME/cron
    is ever written, renamed or removed, and the new modules import no network client (the
    words themselves may appear: ``cron_jobs`` names ``socket`` in an error-kind pattern)."""
    clients = r"urllib|http\.client|requests|aiohttp|httpx|socket"
    for name in CRON_SOURCE_MODULES:
        code = _code_only((PACKAGE / name).read_text(encoding="utf-8"))
        assert not re.search(
            r"write_text|write_bytes|\.open\((\"|')[wax]|mkdir|unlink|rename\(|os\.replace"
            r"|shutil|os\.remove",
            code,
        ), name
        assert not re.search(rf"\b(import|from)\s+({clients})\b", code), name


def test_the_rules_line_reads_and_never_writes_logs_text_or_reaches_the_network() -> None:
    """10. The rules line (0.10.0) reads the engine's prompts and the agent's context files:
    nothing is written, renamed or removed anywhere, no network client is imported, and the
    one log call names an exception class, never a prompt, a file's text or a path."""
    code = _code_only((PACKAGE / "context_files.py").read_text(encoding="utf-8"))
    assert not re.search(
        r"write_text|write_bytes|\.open\(|mkdir|unlink|rename\(|os\.replace|shutil|os\.remove",
        code,
    )
    clients = r"urllib|http\.client|requests|aiohttp|httpx|socket"
    assert not re.search(rf"\b(import|from)\s+({clients})\b", code)
    log_calls = re.findall(r"logger\.\w+\((.*)\)", code)
    assert log_calls == ['"rules: config.yaml not read (%s)", type(exc).__name__'], log_calls


def test_the_plugin_registers_no_hook_tool_middleware_or_command() -> None:
    """8. The catalog entry says ``no tools or hooks`` (provides_hooks: [], provides_tools: []):
    the entry asks the context for a platform handler factory and a task, nothing else."""
    entry = _code_only(ENTRY.read_text(encoding="utf-8"))
    assert not re.search(r"register_(hook|tool|middleware|command|locale)", entry)
    assert entry.count("register_platform_handler(") == 1


def test_the_traffic_probe_reads_attributes_and_calls_no_adapter_method() -> None:
    """9. The adapter's four counters and its public send gate are read as attributes of the
    object; the probe calls nothing
    on it, so a renamed attribute is no data, never an exception in the adapter."""
    source = (PACKAGE / "telegram_traffic.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    probe = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == "probe_adapter"
    )
    allowed = {"getattr", "isinstance", "all", "Probe", "_age", "time.monotonic"}
    for node in ast.walk(probe):
        if isinstance(node, ast.Call):
            assert ast.unparse(node.func) in allowed, ast.unparse(node)


def test_the_plugin_folder_carries_no_invisible_character() -> None:
    """11. The catalog's validator scans the plugin for invisible Unicode (a literal BOM, zero
    width and bidirectional marks) and warns; 0.10.0 once shipped two literal U+FEFF where the
    escape was meant. Every file of the plugin folder, escapes only."""
    invisible = {0xFEFF, 0x200B, 0x200C, 0x200D, 0x2060, 0x00AD}
    invisible |= set(range(0x202A, 0x202F)) | set(range(0x2066, 0x206A))
    for path in sorted(PLUGIN_DIR.rglob("*")):
        if not path.is_file() or "__pycache__" in path.parts:
            continue
        text = path.read_text(encoding="utf-8")
        assert not [c for c in text if ord(c) in invisible], path.name


def test_no_word_of_the_screen_becomes_a_link_a_command_a_mention_or_a_hashtag() -> None:
    """12. Telegram turns words into entities by itself, ``parse_mode=HTML`` or not: a word
    ending in a top-level domain into a link (``AGENTS.md`` opened a site in the window of
    07.10), a ``/word`` into a command a tap sends to the chat the dashboard lives in (``/new``
    would reset that chat's session), ``@word`` and ``#word`` into a mention and a hashtag. Every
    demo state as HTML, outside ``<code>`` (where Telegram finds none), holds none of them, and
    neither do the forms Telegram finds inside a longer word, which a cron job's name or a reason
    could carry. The detectors are Telegram's rules, not the renderer's."""
    import html as html_module

    from telegram_dashboard.render import render_dashboard, to_telegram_html
    from telegram_dashboard.states import PERIOD_SECONDS, all_states

    entities = {
        "link": re.compile(r"[\w-]+\.[^\W\d_]{2,}(?![\w])"),
        "url": re.compile(r"://"),
        "command": re.compile(r"(?<![\w/])/\w"),
        "mention": re.compile(r"(?<!\w)@\w{3,}"),
        "hashtag": re.compile(r"(?<!\w)#\w+"),
    }
    screens = [
        render_dashboard(s.snapshot, now=s.now, delivery=s.delivery, period_seconds=PERIOD_SECONDS)
        for s in all_states()
    ]
    # Review of 0.10.1: a command or a domain inside a word, a digit after the slash, a digit name.
    screens.append(
        "- Cron send /new-session: failing\n> x ./new · -/new · /2fa · see x.com/y · 2.ai · "
        "AGENTS.md/ · state.db. · https://api.example.org/bot/x"
    )
    checked = 0
    for number, text in enumerate(screens, start=1):
        markup = to_telegram_html(text)
        shown = html_module.unescape(
            re.sub(r"<[^>]+>", " ", re.sub(r"<code>.*?</code>", " ", markup))
        )
        for name, pattern in entities.items():
            assert not pattern.findall(shown), (number, name, pattern.findall(shown))
        checked += markup.count("<code>")
    assert checked >= 12  # AGENTS.md (25, 26), /new (26), state.db (8) and the nine forms above
