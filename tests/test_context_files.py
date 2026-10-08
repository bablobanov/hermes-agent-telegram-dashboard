"""The rules line: whether the agent sees its context files, from the engine's saved prompts.

The five cases of the task (loaded whole, no section while the file is in the service's
directory, truncated, the session older than the file, no file at all), an installation that
shares none of our paths, the adversarial review's cases (a heading inside a file, a shortened
file, a platform long gone, a sandbox backend, an embedder's quoted line, a whitespace file, a
crafted label, the database read), and the promise that no prompt or rules text ever leaves
the reader.
"""

from __future__ import annotations

import hashlib
import logging
import os
import sqlite3
from datetime import UTC
from pathlib import Path

import pytest
from rules_fixtures import (
    AFTER_SESSION,
    BEFORE_SESSION,
    NOW,
    SECRET_RULE,
    SESSION_AT,
    Session,
    blocked,
    gateway_state,
    make_state_db,
    prompt,
    rules_text,
    section,
    truncated,
    write_file,
)

from telegram_dashboard import collect, context_files
from telegram_dashboard.collect import CommandResult, collect_all
from telegram_dashboard.compat import Environment
from telegram_dashboard.context_files import (
    agent_dir,
    gateway_platforms,
    incidents_for,
    looks_like_hermes_tree,
    read_rules,
)
from telegram_dashboard.render import render_dashboard
from telegram_dashboard.schema import PlatformRules


class _Runner:
    def run(self, argv, *, timeout_seconds):
        return CommandResult(0, "", "")


def _layout(tmp_path: Path) -> tuple[Path, Path, Path]:
    """Our own production's shape: the engine home, the agent's directory (the hermes user's
    home, where ``terminal.cwd: .`` sends it), the service's WorkingDirectory."""
    home = tmp_path / "var" / "lib" / "hermes" / ".hermes"
    agent = tmp_path / "var" / "lib" / "hermes"
    service = tmp_path / "opt" / "ailya"
    for directory in (home, agent, service):
        directory.mkdir(parents=True, exist_ok=True)
    return home, agent, service


def _one(env: Environment) -> PlatformRules:
    summary, source, _incidents = read_rules(env, now=NOW)
    assert summary.state == "observed", summary
    assert source is not None and source.state == "fresh"
    (rules,) = summary.platforms
    return rules


def _screen(env: Environment) -> list[str]:
    snapshot = collect_all(env, _Runner(), now=NOW, resolve_limits=lambda: None)
    return render_dashboard(snapshot, now=NOW, zone=UTC).splitlines()


def _main(lines: list[str]) -> list[str]:
    return [line for line in lines if not line.startswith(">")]


def _db(home: Path, agent: Path | str | None, *sections: str, at: float = SESSION_AT) -> None:
    make_state_db(home, [Session("telegram", at, prompt(list(sections), agent))])


# ------------------------------------------------------------------ the five cases of the task


def test_1_loaded_whole_is_a_check_mark_in_the_details_and_nothing_on_the_screen(
    tmp_path: Path,
) -> None:
    home, agent, service = _layout(tmp_path)
    text = rules_text()
    write_file(agent / "AGENTS.md", text, at=BEFORE_SESSION)
    _db(home, agent, section("AGENTS.md", text))
    env = Environment(hermes_home=home, gateway_dir=service)

    rules = _one(env)

    assert rules.verdict == "loaded" and rules.files == ("AGENTS.md",)
    assert rules.chars == len(section("AGENTS.md", text))
    lines = _screen(env)
    assert f"> Rules Telegram: ✓ AGENTS.md · {rules.chars:,} chars · session Oct 7 12:32" in lines
    assert not any(line.startswith("Rules") for line in lines)
    assert not any("rules" in line for line in _main(lines))


def test_2_no_section_while_the_file_is_in_the_service_directory_is_not_loaded(
    tmp_path: Path,
) -> None:
    """The 07.10 production case: ``AGENTS.md`` in the unit's WorkingDirectory, the agent in
    the hermes user's home, 313 Telegram prompts without the section."""
    home, agent, service = _layout(tmp_path)
    write_file(service / "AGENTS.md", rules_text(), at=BEFORE_SESSION)
    _db(home, agent)
    env = Environment(hermes_home=home, gateway_dir=service)

    rules = _one(env)

    assert (rules.verdict, rules.why, rules.files) == ("none", "gateway_dir", ("AGENTS.md",))
    _summary, _source, incidents = read_rules(env, now=NOW)
    assert [i.incident_id for i in incidents] == ["rules:telegram:none"]
    lines = _screen(env)
    assert not any(line.startswith("Rules") for line in lines)
    assert "- Telegram: rules not loaded" in lines
    assert (
        "> Rules Telegram: AGENTS.md only in the gateway's directory, the agent works in another"
        in lines
    )
    assert lines[0].startswith("🟡 Warning")


def test_3_a_file_over_the_budget_is_truncated_with_what_was_kept(tmp_path: Path) -> None:
    home, agent, service = _layout(tmp_path)
    text = rules_text("x" * 22_000)
    write_file(agent / "AGENTS.md", text, at=BEFORE_SESSION)
    _db(home, agent, truncated("AGENTS.md", text, 20_000))
    env = Environment(hermes_home=home, gateway_dir=service)

    rules = _one(env)

    assert rules.verdict == "truncated"
    assert (rules.kept, rules.chars) == (14_000 + 4_000, len(section("AGENTS.md", text)))
    lines = _screen(env)
    assert not any(line.startswith("Rules") for line in lines)
    assert "- Telegram: rules truncated" in lines
    kept = f"kept 18,000 of {rules.chars:,} chars"
    assert f"> Rules Telegram: AGENTS.md cut, {kept} · session Oct 7 12:32" in lines


def test_3b_a_cut_of_an_older_text_is_outdated_not_truncated(tmp_path: Path) -> None:
    home, agent, service = _layout(tmp_path)
    old = rules_text("x" * 22_000)
    write_file(agent / "AGENTS.md", old.replace("rule 3:", "rule three:"), at=BEFORE_SESSION)
    _db(home, agent, truncated("AGENTS.md", old, 20_000))

    assert _one(Environment(hermes_home=home, gateway_dir=service)).verdict == "outdated"


def test_4a_a_session_older_than_an_edited_file_is_outdated_until_new(tmp_path: Path) -> None:
    home, agent, service = _layout(tmp_path)
    old = rules_text()
    write_file(agent / "AGENTS.md", old + "\n- one more rule", at=AFTER_SESSION)
    _db(home, agent, section("AGENTS.md", old))
    env = Environment(hermes_home=home, gateway_dir=service)

    rules = _one(env)

    assert rules.verdict == "outdated" and rules.changed_at is not None
    lines = _screen(env)
    assert not any(line.startswith("Rules") for line in lines)
    assert "- Telegram: rules outdated" in lines
    assert (
        "> Rules Telegram: AGENTS.md changed Oct 7 12:36 after session Oct 7 12:32: /new" in lines
    )


def test_4b_a_file_that_appeared_after_the_session_says_new(tmp_path: Path) -> None:
    """The link of 07.10: made at 14:29, the session of 13:37 has no section; the line says
    not loaded (the agent does not see the rules) and the details say /new picks them up."""
    if os.utime not in os.supports_follow_symlinks:
        pytest.skip("a link's own time cannot be set on this platform")
    home, agent, service = _layout(tmp_path)
    target = write_file(service / "AGENTS.md", rules_text(), at=BEFORE_SESSION - 86_400)
    link = agent / "AGENTS.md"
    try:
        link.symlink_to(target)
    except OSError:
        pytest.skip("symlinks not permitted here")
    os.utime(link, (AFTER_SESSION, AFTER_SESSION), follow_symlinks=False)
    _db(home, agent)
    env = Environment(hermes_home=home, gateway_dir=service)

    rules = _one(env)

    assert (rules.verdict, rules.why) == ("none", "after")
    lines = _screen(env)
    assert not any(line.startswith("Rules") for line in lines)
    assert "- Telegram: rules not loaded" in lines
    after = "AGENTS.md changed Oct 7 12:36, after session Oct 7 12:32: /new"
    assert f"> Rules Telegram: {after}" in lines


def test_4c_a_file_there_before_the_session_and_not_in_its_prompt_is_not_loaded(
    tmp_path: Path,
) -> None:
    home, agent, service = _layout(tmp_path)
    write_file(agent / "AGENTS.md", rules_text(), at=BEFORE_SESSION)
    _db(home, agent)

    rules = _one(Environment(hermes_home=home, gateway_dir=service))

    assert (rules.verdict, rules.why) == ("none", "not_loaded")


def test_4d_a_rule_removed_from_the_end_after_the_session_is_outdated(tmp_path: Path) -> None:
    """Review item 2: the prompt still carries the revoked rule after the current text; the
    saved prompt cannot say where the older text ended, so a change after the session start is
    outdated even when the current text is a prefix of what the prompt holds."""
    home, agent, service = _layout(tmp_path)
    write_file(agent / "AGENTS.md", "Never push.", at=AFTER_SESSION)
    _db(home, agent, section("AGENTS.md", "Never push.\n\nYou MAY restart the gateway."))

    assert _one(Environment(hermes_home=home, gateway_dir=service)).verdict == "outdated"


def test_4e_a_file_emptied_after_the_session_is_rules_no_file_has_now(tmp_path: Path) -> None:
    home, agent, service = _layout(tmp_path)
    write_file(agent / "AGENTS.md", "   \n", at=AFTER_SESSION)
    _db(home, agent, section("AGENTS.md", "Never push."))
    env = Environment(hermes_home=home, gateway_dir=service)

    rules = _one(env)

    assert (rules.verdict, rules.why, rules.files) == ("outdated", "gone", ("AGENTS.md",))
    gone = "AGENTS.md in the prompt, no such file now · session Oct 7 12:32: /new"
    assert f"> Rules Telegram: {gone}" in _screen(env)


def test_4f_a_touch_or_a_rebuild_after_the_session_is_loaded_when_the_end_is_proven(
    tmp_path: Path,
) -> None:
    """Verification item 2: ``touch``, ``ln -sf`` to the same file, or a compression that
    rebuilt the prompt after an edit: the current text sits right before the engine's next part
    (the skills index here), so the block provably ends there and the time does not matter."""
    home, agent, service = _layout(tmp_path)
    text = rules_text()
    write_file(agent / "AGENTS.md", text, at=AFTER_SESSION)
    _db(home, agent, section("AGENTS.md", text))

    assert _one(Environment(hermes_home=home, gateway_dir=service)).verdict == "loaded"


def test_4g_without_a_proven_end_a_change_after_the_session_is_outdated(tmp_path: Path) -> None:
    """The conservative side, pinned: a part the reader does not know follows the text (a
    plugin's section, say), the file changed after the start: outdated until /new."""
    home, agent, service = _layout(tmp_path)
    text = rules_text()
    write_file(agent / "AGENTS.md", text, at=AFTER_SESSION)
    full = prompt([section("AGENTS.md", text)], agent, next_part="## Notes from a plugin\nx")
    make_state_db(home, [Session("telegram", SESSION_AT, full)])

    assert _one(Environment(hermes_home=home, gateway_dir=service)).verdict == "outdated"


def test_4h_a_change_time_in_the_future_is_no_time(tmp_path: Path) -> None:
    """Verification item 2: clock skew, an archive, a foreign disk; the content decides."""
    home, agent, service = _layout(tmp_path)
    text = rules_text()
    write_file(agent / "AGENTS.md", text, at=NOW.timestamp() + 7200)
    full = prompt([section("AGENTS.md", text)], agent, next_part="## Notes from a plugin\nx")
    make_state_db(home, [Session("telegram", SESSION_AT, full)])

    assert _one(Environment(hermes_home=home, gateway_dir=service)).verdict == "loaded"


def test_4i_a_mismatch_with_a_file_older_than_the_session_says_differ(tmp_path: Path) -> None:
    """Verification item 8: never "changed … after" for a change before the session."""
    home, agent, service = _layout(tmp_path)
    write_file(agent / "AGENTS.md", "Rules as they are.", at=BEFORE_SESSION)
    _db(home, agent, section("AGENTS.md", "Rules as they were."))
    env = Environment(hermes_home=home, gateway_dir=service)

    rules = _one(env)

    assert (rules.verdict, rules.changed_at) == ("outdated", None)
    differ = "AGENTS.md differ from the prompt of session Oct 7 12:32: /new"
    assert f"> Rules Telegram: {differ}" in _screen(env)


def test_4j_a_file_not_in_utf8_is_not_loaded_and_says_why(tmp_path: Path) -> None:
    """Verification item 3: Notepad's ANSI or "Unicode": the engine skips it in silence."""
    home, agent, service = _layout(tmp_path)
    (agent / "AGENTS.md").write_bytes("Правила агента: не отправлять без «да»".encode("cp1251"))
    _db(home, agent)
    env = Environment(hermes_home=home, gateway_dir=service)

    rules = _one(env)

    assert (rules.verdict, rules.why, rules.files) == ("none", "not_utf8", ("AGENTS.md",))
    lines = _screen(env)
    assert not any(line.startswith("Rules") for line in lines)
    assert "- Telegram: rules not loaded" in lines
    utf8 = "AGENTS.md not UTF-8, the engine skips it in silence: save it as UTF-8"
    assert f"> Rules Telegram: {utf8}" in lines


def test_4k_a_bom_only_file_is_an_empty_section_as_the_engine_loads_it(tmp_path: Path) -> None:
    """Verification item 6: ``strip`` keeps a BOM, so the engine loads an empty section."""
    home, agent, service = _layout(tmp_path)
    write_file(agent / "AGENTS.md", "\ufeff\n", at=BEFORE_SESSION)
    _db(home, agent, "## AGENTS.md\n\n")

    assert _one(Environment(hermes_home=home, gateway_dir=service)).verdict == "loaded"


def test_4l_a_cursor_bundle_the_engine_cut_is_truncated(tmp_path: Path) -> None:
    """Verification item 1: the engine cuts the bundle with its trailing blank line, the tier
    strips that line; the marker's total is the bundle's own length."""
    home, agent, service = _layout(tmp_path)
    a, b = "a" * 15_000, "b" * 15_000
    write_file(agent / ".cursor" / "rules" / "a.mdc", a, at=BEFORE_SESSION)
    write_file(agent / ".cursor" / "rules" / "b.mdc", b, at=BEFORE_SESSION)
    bundle = f"## .cursor/rules/a.mdc\n\n{a}\n\n## .cursor/rules/b.mdc\n\n{b}\n\n"
    head, tail = 14_000, 4_000
    marker = (
        f"\n\n[...truncated .cursorrules: kept {head}+{tail} of {len(bundle)} chars. The middle "
        "is omitted — if you need the full instructions, read the complete file with the "
        "read_file tool: /srv/agent/.cursorrules]\n\n"
    )
    _db(home, agent, bundle[:head] + marker + bundle[-tail:])

    rules = _one(Environment(hermes_home=home, gateway_dir=service))

    assert (rules.verdict, rules.chars, rules.kept) == ("truncated", len(bundle), 18_000)


def test_4m_rules_in_a_prompt_with_no_directory_to_compare_are_unknown(tmp_path: Path) -> None:
    """Verification item 5: a sandbox prompt, no TERMINAL_CWD on the host, the cron path."""
    home, _agent, _service = _layout(tmp_path)
    _db(home, None, section("AGENTS.md", "Rules."))
    env = Environment(hermes_home=home)

    summary, _source, incidents = read_rules(env, now=NOW)

    assert [p.verdict for p in summary.platforms] == ["unknown"] and incidents == ()
    unknown = "rules in the prompt, the agent's directory not known here · session Oct 7 12:32"
    assert f"> Rules Telegram: {unknown}" in _screen(env)


def test_4n_a_link_made_before_the_session_to_an_older_file_is_loaded(tmp_path: Path) -> None:
    """Our production after 07.10 14:29: ``/var/lib/hermes/AGENTS.md`` → ``/opt/ailya``."""
    if os.utime not in os.supports_follow_symlinks:
        pytest.skip("a link's own time cannot be set on this platform")
    home, agent, service = _layout(tmp_path)
    text = rules_text()
    target = write_file(service / "AGENTS.md", text, at=BEFORE_SESSION - 86_400)
    link = agent / "AGENTS.md"
    try:
        link.symlink_to(target)
    except OSError:
        pytest.skip("symlinks not permitted here")
    os.utime(link, (BEFORE_SESSION, BEFORE_SESSION), follow_symlinks=False)
    _db(home, agent, section("AGENTS.md", text))

    assert _one(Environment(hermes_home=home, gateway_dir=service)).verdict == "loaded"


def test_5_no_file_anywhere_is_no_files_in_the_details_and_never_an_alarm(tmp_path: Path) -> None:
    home, agent, service = _layout(tmp_path)
    _db(home, agent)
    env = Environment(hermes_home=home, gateway_dir=service)

    summary, _source, incidents = read_rules(env, now=NOW)

    assert [p.verdict for p in summary.platforms] == ["no_files"] and incidents == ()
    lines = _screen(env)
    assert "> Rules Telegram: no context file where the agent works" in lines
    assert not any(line.startswith("Rules") for line in lines)


# ------------------------------------------------------------------ an installation not like ours


def test_a_foreign_home_and_working_directory_give_the_right_verdicts(tmp_path: Path) -> None:
    """A profile home under ``srv``, a project as ``terminal.cwd`` with ``CLAUDE.md``, a service
    directory elsewhere: nothing of ``/var/lib/hermes`` or ``/opt/ailya``."""
    home = tmp_path / "srv" / "hermes-data" / "profiles" / "work"
    project = tmp_path / "home" / "dana" / "code" / "atlas"
    service = tmp_path / "srv" / "hermes-data"
    text = "Use the staging database only.\n" + SECRET_RULE
    write_file(project / "CLAUDE.md", text, at=BEFORE_SESSION)
    gateway_state(home, "discord", "telegram", "work:slack")
    make_state_db(
        home,
        [
            Session("discord", SESSION_AT, prompt([section("CLAUDE.md", text)], project)),
            Session("telegram", SESSION_AT - 60, prompt([section("CLAUDE.md", text)], project)),
            Session("cli", SESSION_AT + 60, prompt([], tmp_path)),
        ],
    )

    summary, _source, incidents = read_rules(
        Environment(hermes_home=home, gateway_dir=service), now=NOW
    )

    assert [(p.platform, p.verdict, p.files) for p in summary.platforms] == [
        ("telegram", "loaded", ("CLAUDE.md",)),
        ("discord", "loaded", ("CLAUDE.md",)),
    ]
    assert incidents == ()


def test_rules_kept_only_in_the_engine_home_are_named_there(tmp_path: Path) -> None:
    """A default service runs in HERMES_HOME while ``terminal.cwd: .`` sends the agent to the
    user's home: an ``AGENTS.md`` next to ``SOUL.md`` is never loaded."""
    user = tmp_path / "home" / "dana"
    home = user / ".hermes"
    write_file(home / "AGENTS.md", rules_text(), at=BEFORE_SESSION)
    _db(home, user)

    rules = _one(Environment(hermes_home=home, gateway_dir=None))

    assert (rules.verdict, rules.why) == ("none", "home")


def test_a_claude_md_for_coding_tools_in_the_gateway_directory_is_no_alarm(tmp_path: Path) -> None:
    """Review item 18: a gateway started by hand in a repository whose CLAUDE.md is meant for
    a coding tool. Only .hermes.md and AGENTS.md count where the agent does not look."""
    home, agent, service = _layout(tmp_path)
    write_file(service / "CLAUDE.md", "For Claude Code only.", at=BEFORE_SESSION)
    write_file(service / ".cursorrules", "For Cursor only.", at=BEFORE_SESSION)
    _db(home, agent)

    assert _one(Environment(hermes_home=home, gateway_dir=service)).verdict == "no_files"


def test_a_whitespace_file_is_no_file(tmp_path: Path) -> None:
    """Review item 7: the engine strips a file and skips an empty one."""
    home, agent, service = _layout(tmp_path)
    write_file(service / "AGENTS.md", " \n\n\t\n", at=BEFORE_SESSION)
    write_file(agent / "AGENTS.md", "\n\n", at=BEFORE_SESSION)
    _db(home, agent)

    assert _one(Environment(hermes_home=home, gateway_dir=service)).verdict == "no_files"


def test_the_cron_tick_with_the_gateway_directory_from_its_config_sees_our_case(
    tmp_path: Path,
) -> None:
    """Review item 11: the cron path names the gateway's directory in its config, and a word
    turns the line off there as it does in the plugin's settings."""
    from telegram_dashboard.__main__ import _environment

    home, agent, service = _layout(tmp_path)
    write_file(service / "AGENTS.md", rules_text(), at=BEFORE_SESSION)
    _db(home, agent)

    rules = _one(_environment({"hermes_home": str(home), "gateway_dir": str(service)}))
    assert (rules.verdict, rules.why) == ("none", "gateway_dir")
    for off in ("off", "0", "False", False):
        assert not _environment({"context_files": off}).context_files_enabled
    assert _environment({}).context_files_enabled and _environment({}).gateway_dir is None


def test_a_sandbox_backend_with_a_host_directory_judges_by_terminal_cwd(tmp_path: Path) -> None:
    """Review item 5: no ``Current working directory`` line (docker), ``TERMINAL_CWD`` an
    existing host path: the engine's discovery ran there, so does the verdict."""
    home, _agent, service = _layout(tmp_path)
    project = tmp_path / "work" / "project"
    text = rules_text()
    write_file(project / "AGENTS.md", text, at=BEFORE_SESSION)
    write_file(service / "AGENTS.md", "something else", at=BEFORE_SESSION)
    _db(home, None, section("AGENTS.md", text))

    env = Environment(hermes_home=home, gateway_dir=service, terminal_cwd=project)
    assert _one(env).verdict == "loaded"
    gone = Environment(hermes_home=home, gateway_dir=service, terminal_cwd=tmp_path / "nope")
    assert _one(gone).verdict == "outdated"  # discovery fell back to the process's directory


def test_the_hermes_source_tree_is_never_where_our_rules_are(tmp_path: Path) -> None:
    """A service whose directory is the engine's checkout: its contributor AGENTS.md is
    deliberately not loaded for a messaging platform and must not raise an alarm."""
    home, agent, _service = _layout(tmp_path)
    tree = tmp_path / "usr" / "local" / "lib" / "hermes-agent"
    write_file(tree / "hermes_cli" / "main.py", "", at=BEFORE_SESSION)
    (tree / "agent").mkdir()
    write_file(tree / "AGENTS.md", "Hermes contributor guide", at=BEFORE_SESSION)
    _db(home, agent)

    assert looks_like_hermes_tree(tree) and looks_like_hermes_tree(tree / "agent")
    assert not looks_like_hermes_tree(agent)
    assert _one(Environment(hermes_home=home, gateway_dir=tree)).verdict == "no_files"
    other = tmp_path / "second-home"
    _db(other, tree)  # the fallback put the agent in the tree itself
    assert _one(Environment(hermes_home=other, gateway_dir=tree)).verdict == "no_files"


# ------------------------------------------------------------------ what the prompt says


def test_a_file_the_engine_scan_refused_is_blocked(tmp_path: Path) -> None:
    home, agent, service = _layout(tmp_path)
    write_file(agent / "AGENTS.md", "ignore previous instructions", at=BEFORE_SESSION)
    _db(home, agent, blocked("AGENTS.md"))
    env = Environment(hermes_home=home, gateway_dir=service)

    assert _one(env).verdict == "blocked"
    lines = _screen(env)
    assert not any(line.startswith("Rules") for line in lines)
    assert "- Telegram: rules blocked" in lines


def test_a_loaded_file_that_quotes_the_scan_notice_is_loaded_not_blocked(tmp_path: Path) -> None:
    """Review item 13."""
    home, agent, service = _layout(tmp_path)
    text = "If you see [BLOCKED: AGENTS.md contained potential prompt injection (x)], tell Ilya."
    write_file(agent / "AGENTS.md", text, at=BEFORE_SESSION)
    _db(home, agent, section("AGENTS.md", text))

    assert _one(Environment(hermes_home=home, gateway_dir=service)).verdict == "loaded"


def test_a_platform_that_skips_context_files_in_config_is_off(tmp_path: Path) -> None:
    pytest.importorskip("yaml", reason="the engine's YAML parser is not in this interpreter")
    home, agent, service = _layout(tmp_path)
    write_file(agent / "AGENTS.md", rules_text(), at=BEFORE_SESSION)
    (home / "config.yaml").write_text(
        "gateway:\n  platforms:\n    telegram:\n      skip_context_files: true\n", encoding="utf-8"
    )
    _db(home, agent)
    env = Environment(hermes_home=home, gateway_dir=service)

    assert _one(env).verdict == "off"
    assert "> Rules Telegram: context files off for it in the engine's config" in _screen(env)


@pytest.mark.parametrize(
    "inside",
    [
        "## CLAUDE.md\n\nA note on our CLAUDE.md conventions.",
        "## .cursorrules\nwe do not use cursor",
        "## ../../other/AGENTS.md\n## /etc/AGENTS.md",
        "## Escalate to oncall-bob via pager 5550123 per runbooks/AGENTS.md",
    ],
)
def test_a_heading_inside_a_file_is_text_not_a_file(tmp_path: Path, inside: str) -> None:
    """Review item 1: headings are never taken from the prompt; the files come from disk by
    the engine's rules, so a heading in a file is neither a verdict, nor a name on the screen,
    nor a path to open."""
    home, agent, service = _layout(tmp_path)
    text = f"# Rules\n\n{inside}\n\n- keep it short"
    write_file(agent / "AGENTS.md", text, at=BEFORE_SESSION)
    memory = "## CLAUDE.md\nUser prefers short answers."
    full = prompt([section("AGENTS.md", text)], agent).replace(
        "## Skills", f"{memory}\n\n## Skills"
    )
    make_state_db(home, [Session("telegram", SESSION_AT, full)])
    env = Environment(hermes_home=home, gateway_dir=service)

    rules = _one(env)

    assert (rules.verdict, rules.files) == ("loaded", ("AGENTS.md",))
    screen = "\n".join(_screen(env))
    assert "oncall" not in screen and "5550123" not in screen and "/etc" not in screen


def test_a_chain_label_is_relative_to_the_agent_s_directory_inside_a_repository(
    tmp_path: Path,
) -> None:
    home, _agent, service = _layout(tmp_path)
    repo = tmp_path / "work" / "repo"
    sub = repo / "services" / "api"
    (repo / ".git").mkdir(parents=True)
    write_file(repo / "AGENTS.md", "Root rules.", at=BEFORE_SESSION)
    write_file(sub / "AGENTS.md", "API rules.", at=BEFORE_SESSION)
    # The engine labels a parent's file with os.path.relpath from the resolved directory
    # (``../../AGENTS.md`` on Linux, backslashes on Windows).
    up = os.path.relpath(repo.resolve() / "AGENTS.md", sub.resolve())
    chain = "\n\n".join([section(up, "Root rules."), section("AGENTS.md", "API rules.")])
    _db(home, sub, chain)

    rules = _one(Environment(hermes_home=home, gateway_dir=service))

    assert rules.verdict == "loaded" and rules.files == (up, "AGENTS.md")
    assert up.replace("\\", "/") == "../../AGENTS.md"


def test_the_working_directory_is_read_back_as_the_engine_reads_it() -> None:
    """Review item 6: an embedder hint may quote the heading (escaped) and its own
    ``Current working directory:`` line; the engine takes the line under ``User home
    directory:`` in the block the prompt ends with, and so does the reader."""
    hint = "> # Hermes runtime environment\nCurrent working directory: \\workspace (mounted)"
    quoted = section("AGENTS.md", "Example:\nCurrent working directory: /tmp/x")
    end = "<!-- End Hermes runtime environment -->"
    text = prompt([quoted], "/srv/agent").replace(end, f"{hint}\n\n{end}")

    assert agent_dir(text) == Path("/srv/agent")
    assert agent_dir(prompt([], None)) is None
    legacy = "legacy\nUser home directory: /h\nCurrent working directory: /old"
    assert agent_dir(legacy) == Path("/old")


# ------------------------------------------------------------------ platforms and sessions


def test_the_latest_session_with_a_saved_prompt_is_the_one_judged(tmp_path: Path) -> None:
    """A bare row after ``/new`` (no prompt yet) does not hide the latest saved prompt."""
    home, agent, service = _layout(tmp_path)
    text = rules_text()
    write_file(agent / "AGENTS.md", text, at=BEFORE_SESSION)
    make_state_db(
        home,
        [
            Session("telegram", SESSION_AT - 7200, prompt([], agent)),
            Session("telegram", SESSION_AT, prompt([section("AGENTS.md", text)], agent)),
            Session("telegram", SESSION_AT + 300, None),
            Session("cron", SESSION_AT + 600, prompt([], agent)),
        ],
    )

    rules = _one(Environment(hermes_home=home, gateway_dir=service))

    assert rules.verdict == "loaded"
    assert rules.session_started_at is not None
    assert rules.session_started_at.startswith("2026-10-07T12:32:34")


def test_platforms_come_from_the_gateway_record_and_skip_other_profiles(tmp_path: Path) -> None:
    gateway_state(tmp_path, "slack", "telegram", "work:discord")
    assert gateway_platforms(tmp_path) == ("telegram", "slack")
    assert gateway_platforms(tmp_path / "missing") == ("telegram",)
    (tmp_path / "gateway_state.json").write_text("{not json", encoding="utf-8")
    assert gateway_platforms(tmp_path) == ("telegram",)


def test_a_platform_a_previous_process_wrote_is_not_judged(tmp_path: Path) -> None:
    """Review item 4: the engine never drops a default profile's platform key; an entry whose
    writer is not the record's own pid and start is preserved, not live (``/api/status``)."""
    gateway_state(tmp_path, "telegram", "discord", stale=("discord",))
    assert gateway_platforms(tmp_path) == ("telegram",)
    # Verification item 13: right after a start no entry is this process's yet; Telegram only,
    # never every key.
    gateway_state(tmp_path, "telegram", "discord", stale=("telegram", "discord"))
    assert gateway_platforms(tmp_path) == ("telegram",)


def test_two_platforms_in_trouble_are_two_events_and_no_line(tmp_path: Path) -> None:
    home, agent, service = _layout(tmp_path)
    write_file(service / "AGENTS.md", rules_text(), at=BEFORE_SESSION)
    gateway_state(home, "telegram", "discord")
    make_state_db(
        home,
        [
            Session("telegram", SESSION_AT, prompt([], agent)),
            Session("discord", SESSION_AT, prompt([], agent)),
        ],
    )
    lines = _screen(Environment(hermes_home=home, gateway_dir=service))

    assert not any(line.startswith("Rules") for line in lines)
    assert lines[0].startswith("🟡 Warning")
    assert "- Telegram: rules not loaded" in lines and "- Discord: rules not loaded" in lines


def test_no_session_yet_is_said_in_the_details(tmp_path: Path) -> None:
    home, _agent, service = _layout(tmp_path)
    make_state_db(home, [Session("cli", SESSION_AT, prompt([], tmp_path))])
    env = Environment(hermes_home=home, gateway_dir=service)

    summary, _source, _incidents = read_rules(env, now=NOW)

    assert summary.platforms == ()
    assert "> Rules: no platform session with a saved prompt yet" in _screen(env)


# ------------------------------------------------------------------ the database


def test_the_inline_prompt_of_an_older_schema_is_read(tmp_path: Path) -> None:
    home, agent, service = _layout(tmp_path)
    text = rules_text()
    write_file(agent / "AGENTS.md", text, at=BEFORE_SESSION)
    make_state_db(
        home,
        [Session("telegram", SESSION_AT, prompt([section("AGENTS.md", text)], agent))],
        legacy=True,
    )

    assert _one(Environment(hermes_home=home, gateway_dir=service)).verdict == "loaded"


def test_no_state_db_is_not_observed_and_a_broken_one_is_no_data(tmp_path: Path) -> None:
    env = Environment(hermes_home=tmp_path)
    summary, source, _ = read_rules(env, now=NOW)
    assert summary.state == "unsupported" and source is not None and source.state == "unsupported"
    assert any("agent rules (not on this installation)" in line for line in _screen(env))

    (tmp_path / "state.db").write_bytes(b"this is not a database at all, not even close" * 20)
    summary, source, _ = read_rules(env, now=NOW)
    assert summary.state == "unknown" and source is not None and source.state == "unavailable"
    assert source.detail == "state.db: DatabaseError"
    lines = _screen(env)
    assert "> Rules: state.db: DatabaseError" in lines
    assert any("agent rules (unavailable)" in line for line in lines)


def test_only_the_judged_session_s_prompt_is_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review item 3: two statements per platform, the row by the source index and then one
    prompt by its hash; no statement reads other sessions' prompts (a rollback-journal
    database holds its writer back for as long as a read runs)."""
    home, agent, service = _layout(tmp_path)
    many = [
        Session("telegram", SESSION_AT - 60 * i, prompt([section("AGENTS.md", f"v{i}")], agent))
        for i in range(50)
    ]
    make_state_db(home, many)
    statements: list[str] = []
    real = context_files.open_readonly

    def traced(path: Path) -> sqlite3.Connection:
        conn = real(path)
        conn.set_trace_callback(statements.append)
        return conn

    monkeypatch.setattr(context_files, "open_readonly", traced)
    _one(Environment(hermes_home=home, gateway_dir=service))

    reads = [s for s in statements if "FROM sessions" in s or "FROM system_prompts" in s]
    assert len(reads) == 2, reads
    assert "system_prompts" not in reads[0] and "prompt FROM" not in reads[0]
    assert reads[1].startswith("SELECT prompt FROM system_prompts WHERE hash =")


def test_the_database_is_left_as_it_was_and_a_wal_writer_is_not_held_back(
    tmp_path: Path,
) -> None:
    home, agent, service = _layout(tmp_path)
    text = rules_text()
    write_file(agent / "AGENTS.md", text, at=BEFORE_SESSION)
    path = make_state_db(
        home, [Session("telegram", SESSION_AT, prompt([section("AGENTS.md", text)], agent))]
    )
    writer = sqlite3.connect(path, timeout=0.5)
    writer.execute("PRAGMA journal_mode=WAL")
    writer.execute("INSERT INTO sessions (id, source, started_at) VALUES ('w', 'cron', 1)")
    writer.commit()
    wal = path.with_name(path.name + "-wal")

    def digest() -> list[str]:
        return [hashlib.sha256(p.read_bytes()).hexdigest() for p in (path, wal)]

    try:
        before = digest()
        rules = _one(Environment(hermes_home=home, gateway_dir=service))
        after = digest()
        writer.execute("INSERT INTO sessions (id, source, started_at) VALUES ('w2', 'cron', 2)")
        writer.commit()  # nothing of the reader is left holding the file
    finally:
        writer.close()  # the writer's own checkpoint, not the reader's

    assert rules.verdict == "loaded"
    assert after == before


def test_any_failure_is_one_unavailable_source_with_its_class_and_nothing_logged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Review item 10: a permission error deep in a path, on the cron path too, never kills
    the tick and never logs a message that may carry a path."""
    caplog.set_level(logging.DEBUG)
    home, agent, service = _layout(tmp_path)
    _db(home, agent)

    def denied(where: Path):
        raise PermissionError(13, "denied", str(where))

    monkeypatch.setattr(context_files, "discover", denied)
    env = Environment(hermes_home=home, gateway_dir=service)
    summary, source, incidents = read_rules(env, now=NOW)

    assert (summary.state, summary.detail) == ("unknown", "rules: PermissionError")
    assert source is not None and source.state == "unavailable" and incidents == ()
    assert str(tmp_path) not in caplog.text
    assert "> Rules: rules: PermissionError" in _screen(env)


def test_the_line_off_in_the_settings_is_named_in_the_details_and_counted_nowhere(
    tmp_path: Path,
) -> None:
    home, agent, service = _layout(tmp_path)
    _db(home, agent)
    env = Environment(hermes_home=home, gateway_dir=service, context_files_enabled=False)

    snapshot = collect_all(env, _Runner(), now=NOW, resolve_limits=lambda: None)
    lines = render_dashboard(snapshot, now=NOW, zone=UTC).splitlines()

    assert "context_files" not in [s.name for s in snapshot.sources]
    assert not any(line.startswith("Rules") for line in lines)
    assert "> Rules: off in the dashboard settings" in lines


# ------------------------------------------------------------------ the screen


def test_every_event_of_the_rules_fits_the_phone_width() -> None:
    """Review item 14, verification item 9: the 32 columns the cron events keep, for every
    platform the engine knows and an unknown one with a long name."""
    from telegram_dashboard.context_files import _PLATFORM_LABELS

    verdicts = ("none", "outdated", "truncated", "blocked")
    for platform in (*_PLATFORM_LABELS, "wecom_callback", "a_plugin_platform_with_a_long_name"):
        judged = [PlatformRules(platform, v) for v in verdicts]  # type: ignore[arg-type]
        events = incidents_for(judged)
        assert len(events) == 4
        assert all(len(f"- {event.title}") <= 32 for event in events), events


def test_a_rules_event_keeps_a_place_when_five_other_events_fill_the_screen() -> None:
    """Review of 0.10.1: every other source has a line of its own on the screen, the rules only
    their events; the cut to five must not leave an agent without its rules in the details."""
    from telegram_dashboard.collect import build_snapshot
    from telegram_dashboard.schema import CapacitySummary, Incident

    others = tuple(Incident(f"cron:job{n}", "warning", f"Cron job{n}: failing") for n in range(5))
    rules = incidents_for([PlatformRules("telegram", "none"), PlatformRules("max", "outdated")])

    def snapshot(incidents: tuple[Incident, ...]):
        return build_snapshot(
            now=NOW,
            gateway=None,
            drift=None,
            capacity=CapacitySummary(),
            sources=(),
            incidents=incidents,
        )

    full = snapshot(others + rules)
    assert full.incidents == (*others[:4], rules[0])
    assert full.overall == "warning"
    assert "- Telegram: rules not loaded" in render_dashboard(full, now=NOW, zone=UTC).splitlines()
    # Room enough: nothing moves.
    assert snapshot(others[:2] + rules).incidents == (*others[:2], *rules)
    assert snapshot(others).incidents == others


# ------------------------------------------------------------------ nothing of the text leaves


def test_no_prompt_or_rules_text_reaches_the_screen_the_snapshot_or_the_log(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Every verdict once, the secret phrase in every file and prompt: zero occurrences in the
    rendered screen, in the snapshot's repr and in the log; no path of the fixture either."""
    caplog.set_level(logging.DEBUG)
    home, agent, service = _layout(tmp_path)
    text = rules_text()
    write_file(agent / "AGENTS.md", text + "\nedited", at=AFTER_SESSION)
    write_file(service / "AGENTS.md", text, at=BEFORE_SESSION)
    gateway_state(home, "telegram", "discord", "slack", "signal")
    make_state_db(
        home,
        [
            Session("telegram", SESSION_AT, prompt([section("AGENTS.md", text)], agent)),
            Session(
                "discord", SESSION_AT, prompt([truncated("AGENTS.md", text * 30, 20_000)], agent)
            ),
            Session("slack", SESSION_AT, prompt([], service)),
            Session("signal", SESSION_AT, prompt([], agent)),
        ],
    )
    env = Environment(hermes_home=home, gateway_dir=service)

    snapshot = collect_all(env, _Runner(), now=NOW, resolve_limits=lambda: None)
    screen = render_dashboard(snapshot, now=NOW, zone=UTC)

    assert snapshot.rules is not None and len(snapshot.rules.platforms) == 4
    for haystack in (screen, repr(snapshot), caplog.text):
        assert haystack.count("aubergine-7f3a") == 0
        assert haystack.count("keep the change small") == 0
        assert str(tmp_path) not in haystack


def test_the_reader_logs_nothing_on_a_healthy_tick(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG, logger="telegram_dashboard")
    home, agent, service = _layout(tmp_path)
    _db(home, agent)

    read_rules(Environment(hermes_home=home, gateway_dir=service), now=NOW)

    assert [r for r in caplog.records if r.name.startswith("telegram_dashboard")] == []


def test_the_async_tick_reads_the_rules_in_a_worker_under_its_deadline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import asyncio
    import threading

    from telegram_dashboard.collect import collect_all_async

    home, agent, service = _layout(tmp_path)
    _db(home, agent)
    release = threading.Event()

    def stuck(env, *, now):
        release.wait(5)
        return read_rules(env, now=now)

    monkeypatch.setattr(collect, "read_rules", stuck)
    env = Environment(hermes_home=home, gateway_dir=service, limits_enabled=False)
    try:
        snapshot = asyncio.run(
            collect_all_async(env, _Runner(), now=NOW, rules_timeout_seconds=0.05)
        )
    finally:
        release.set()

    by_name = {s.name: s for s in snapshot.sources}
    assert by_name["context_files"].state == "unavailable"
    assert by_name["context_files"].detail == "state.db read timed out"
    assert snapshot.rules is not None and snapshot.rules.state == "unknown"


def test_a_wal_writer_in_the_middle_of_a_transaction_neither_blocks_nor_is_blocked(
    tmp_path: Path,
) -> None:
    """Verification item 11: the reader runs while the gateway's write transaction is open, and
    the writer commits right after with a short busy timeout."""
    home, agent, service = _layout(tmp_path)
    text = rules_text()
    write_file(agent / "AGENTS.md", text, at=BEFORE_SESSION)
    path = make_state_db(
        home, [Session("telegram", SESSION_AT, prompt([section("AGENTS.md", text)], agent))]
    )
    writer = sqlite3.connect(path, timeout=0.5, isolation_level=None)
    try:
        writer.execute("PRAGMA journal_mode=WAL")
        writer.execute("BEGIN IMMEDIATE")
        writer.execute("INSERT INTO sessions (id, source, started_at) VALUES ('w', 'cron', 1)")
        rules = _one(Environment(hermes_home=home, gateway_dir=service))
        writer.execute("COMMIT")
    finally:
        writer.close()

    assert rules.verdict == "loaded"
