import json
from pathlib import Path

import pytest

from telegram_dashboard.compat import (
    Environment,
    load_matrix,
    probe_drift,
    probe_gateway_state,
    verification_for,
)


def test_bundled_matrix_loads_and_describes_every_mvp_source() -> None:
    matrix = load_matrix()

    assert {"gateway_state", "limits", "drift"} <= set(matrix["sources"])
    for name, entry in matrix["sources"].items():
        assert entry.get("probe"), name
        assert isinstance(entry.get("verified"), dict), name


def test_the_version_line_is_described_as_a_capability_and_an_unauthenticated_read() -> None:
    entry = load_matrix()["sources"]["hermes_version"]

    assert "sys.modules" in entry["probe"] and "importlib.metadata" in entry["probe"]
    assert "api.github.com" in entry["contract"] and "no token" in entry["contract"]
    assert {"0.21.1", "0.21.3"} <= set(entry["verified"])


def test_verification_is_honest_about_unknown_versions() -> None:
    matrix = load_matrix()

    assert verification_for(matrix, "gateway_state", "0.21.1").startswith("verified on 0.21.1")
    assert "not verified" in verification_for(matrix, "gateway_state", "0.22.0")
    assert verification_for(matrix, "nope", "0.21.1") == "source not described in the matrix"


def test_matrix_schema_is_validated(tmp_path: Path) -> None:
    bad = tmp_path / "m.json"
    bad.write_text(json.dumps({"schema": 99, "sources": {}}), encoding="utf-8")
    with pytest.raises(ValueError):
        load_matrix(bad)


def test_probes_answer_capability_not_version(tmp_path: Path) -> None:
    env = Environment(hermes_home=tmp_path)
    assert probe_gateway_state(env).status == "unsupported"

    (tmp_path / "gateway_state.json").write_text(
        json.dumps({"updated_at": "x", "platforms": {}}), encoding="utf-8"
    )
    assert probe_gateway_state(env).status == "supported"

    (tmp_path / "gateway_state.json").write_text("{", encoding="utf-8")
    assert probe_gateway_state(env).status == "unknown"

    assert probe_drift(env).status == "unsupported"
    assert (
        probe_drift(Environment(hermes_home=tmp_path, drift_command=("python", "x.py"))).status
        == "supported"
    )
    missing = Environment(hermes_home=tmp_path, drift_command=(str(tmp_path / "absent"), "x"))
    assert probe_drift(missing).status == "unsupported"


def test_the_external_sources_and_the_codex_plan_are_rows_of_the_matrix() -> None:
    """0.8.0: the external limit sources say what they take from the engine (1.1.0: the key
    through get_secret, in the tick's own context); the Codex plan is a field of the facade the
    limits row already calls, read in the engine source."""
    sources = load_matrix()["sources"]
    external = sources["external_limits"]
    codex = sources["codex_plan"]

    assert "limits_sources" in external["probe"] and "loopback" in external["probe"]
    assert "_BOT_TOKEN" in external["probe"]
    assert "contract 1" in external["contract"] and "25 s" in external["contract"]
    assert "agent.secret_scope.get_secret" in external["key_probe"]
    assert "UnscopedSecretError" in external["key_probe"] and "read-by-code" in str(
        external["verified"]
    )
    assert "tick's own context" in external["key_probe"]
    for field in ("key_probe", "caveat"):
        assert "agent.secret_scope.get_secret" in external[field], field
        assert "agent.secret_scope.UnscopedSecretError" in external[field], field
    assert "isinstance" in external["key_probe"]
    assert "only where the module has no such class" in external["key_probe"]
    assert "if that connect ran inside one" in external["key_probe"]
    assert {"0.21.1", "0.21.6", "0.22.2", "0.22.5", "0.22.7"} == set(external["verified"])
    assert "f27cdbce5a" in external["verified"]["0.21.6"]
    assert ":219" in external["verified"]["0.21.6"] and ":115" in external["verified"]["0.21.6"]
    assert "_title_case_slug" in codex["contract"] and "Session" in codex["contract"]
    assert {"0.21.1", "0.21.3"} <= set(codex["verified"])


def test_the_gemini_log_is_a_row_of_the_matrix_that_names_what_it_never_reads() -> None:
    """0.8.1: the Gemini line reads the tail of the engine's error log. The row says what is
    read, what never is, and that only the engine's own calls are in that log; the limits row
    no longer calls Gemini unconfirmed."""
    sources = load_matrix()["sources"]
    row = sources["gemini_log"]

    assert "logs/" in row["probe"] and "unsupported" in row["probe"]
    reads = " ".join(row["reads"])
    assert "256 KB" in reads and "HTTP 429" in reads
    assert "the whole of logs/errors.log on the first read" in reads and "4 MB" in reads
    assert {"auth.json", "state.db", "errors.log.1", "errors.log.2"} <= set(row["never_reads"])
    assert "hermes_logging.py" in row["contract"] and "gemini_log_cache" in row["contract"]
    assert "chat_completion_helpers" in row["contract"]
    assert "0.21.3" in row["verified"]
    assert "engine's own calls" in row["caveat"]
    assert "always unsupported" not in sources["limits"]["contract"]


def test_the_limits_row_says_how_often_the_facade_is_asked_and_what_the_record_keeps() -> None:
    """Issue #16: each facade provider is asked once per ``facade_refresh_seconds`` and its last
    attempt is kept in the record under ``facade_cache``; the row says so, and names what of a
    snapshot never reaches the record."""
    row = load_matrix()["sources"]["limits"]

    assert "facade_refresh_seconds" in row["contract"] and "facade_cache" in row["contract"]
    assert "default 300 s" in row["contract"] and "288 calls a day" in row["contract"]
    assert "answered_at" in row["contract"] and "cron path keeps no record" in row["contract"]
    for field in ("identity", "raw", "details", "class name"):
        assert field in row["caveat"], field


def test_the_limits_row_says_when_a_provider_is_not_configured() -> None:
    """Issue #14: the row names the rule the code runs (three ``None`` attempts in a row for a
    provider that never answered, seven days after the last answer for one that did), the
    record's count, the allow-list, and that the limits source leaves with the last line; the
    Gemini row says when the log is not read and when its line is drawn."""
    sources = load_matrix()["sources"]
    row, gemini = sources["limits"], sources["gemini_log"]

    for words in (
        "not_configured",
        "3 None attempts in a row",
        "none_attempts",
        "7 days",
        "limits_providers",
        "there is none when no provider has one",
        "not_configured at once unless the provider is listed",
    ):
        assert words in row["contract"], words
    assert "limits_providers list without gemini = not read" in gemini["probe"]
    assert "drawn only while a refusal is active" in gemini["contract"]


def test_the_cron_sources_and_the_traffic_probe_are_rows_of_the_matrix() -> None:
    """0.9.0: the cron line reads the engine's cron files and its run history read-only; the
    deaf verdict reads four private counters off the live adapter as a capability and the
    send gate through its public property. Each row
    says what is read, what never is, and on which engine source it was read."""
    matrix = load_matrix()
    sources = matrix["sources"]
    cron = sources["cron"]
    runs = sources["cron_runs"]
    traffic = sources["telegram_traffic"]

    assert "jobs.json" in cron["probe"] and "first token" in cron["probe"]
    assert {"prompt", "script", "deliver"} <= set(cron["never_reads"])
    assert any("paused_at" in line for line in cron["reads"])  # 0.9.1: the half-pause
    assert "pause marker" in cron["contract"]
    assert "200" in cron["contract"] and "15 min" in cron["contract"]
    assert "error_kind" in cron["contract"] or "kind of the error" in cron["contract"]
    assert verification_for(matrix, "cron", "0.21.3").startswith("verified on 0.21.3")
    assert "main" in cron["verified"] and "6ec05205a9" in cron["verified"]["main"]
    # 1.0.0: how often a job runs comes from the stored schedule, the words from its display.
    reads = " ".join(cron["reads"])
    assert any(line.startswith("schedule.kind") for line in cron["reads"])
    for field in ("schedule.expr", "schedule.minutes", "schedule.run_at", "schedule_display"):
        assert field in reads, field
    assert verification_for(matrix, "cron", "0.22.2").startswith("verified on 0.22.2")
    assert ":780" in cron["verified"]["0.22.2"] and ":1908-1909" in cron["verified"]["0.22.2"]

    assert "mode=ro" in runs["probe"] and "query_only" in runs["contract"]
    assert "error" in runs["never_reads"] and "state.db" in runs["never_reads"]
    assert "1000" in runs["contract"]
    assert "0.21.3" in runs["verified"]

    assert "_updates_received_total" in traffic["probe"]
    assert "public property send_path_degraded" in traffic["probe"]
    assert "_send_path_degraded" not in traffic["probe"]  # the private flag is not read
    assert "attribute" in traffic["contract"] and "never" in traffic["contract"]
    assert "0.21.1" in traffic["verified"] and "base.py:2007" in traffic["verified"]["0.21.3"]
    assert "capability" in traffic["caveat"] and "promise" in traffic["caveat"]
    assert "0.21.3" in traffic["verified"] and "main" in traffic["verified"]
    assert "Failed to send Telegram message" in traffic["contract"]


def test_every_engine_source_has_a_row_for_hermes_0_21_6() -> None:
    """0.11.0: the audit of 08.10 read every engine touchpoint of 0.21.6 against 0.21.5; the
    plugin's own scripts and the engine-free sources have no engine to verify against."""
    engine_free = {"drift", "backup"}
    for name, entry in load_matrix()["sources"].items():
        if name not in engine_free:
            assert "0.21.6" in entry["verified"], name


def test_only_the_rows_the_engine_job_runs_say_executed_on_0_22_2_and_0_22_7() -> None:
    """1.1.0: the floor is 0.22.2, the engines the CI engine job runs the suite against are
    0.22.2 and 0.22.7. A row names 0.22.7 only when a test of that run covers it, and says so
    for both; a row the run does not cover keeps what it had (0.22.2 read in the source for the
    cron and rich rows). The floor and the manifest agree (tests/test_dashboard_version.py)."""
    matrix = load_matrix()
    run = {
        "plugin_platform_handler",
        "screen_html",
        "hermes_version",
        "external_limits",
        "context_files",
        "memory",
    }
    sources = matrix["sources"]

    def executed(entry: dict, version: str) -> bool:
        return str(entry["verified"].get(version, "")).startswith("executed")

    assert {name for name, entry in sources.items() if executed(entry, "0.22.7")} == run
    for name in run:
        for version in ("0.22.2", "0.22.7"):
            assert sources[name]["verified"][version].startswith("executed, CI engine job"), name
    for name in ("cron", "screen_rich"):
        assert sources[name]["verified"]["0.22.2"].startswith("read-by-code"), name
    # The usage facade is read in the source on the supported engines: the run skips its test,
    # which would call the providers' APIs.
    for version in ("0.22.2", "0.22.7", "0.23.0"):
        assert sources["limits"]["verified"][version].startswith("read-by-code"), version
    assert matrix["floors"]["delivery_plugin"]["minimum_version"] == "0.22.2"


def test_the_memory_section_is_a_row_that_names_what_it_never_reads() -> None:
    entry = load_matrix()["sources"]["memory"]

    assert "hermes_yaml" in " ".join(entry["reads"]) and "§" in " ".join(entry["reads"])
    assert any("text of an entry" in item for item in entry["never_reads"])
    assert {"0.21.3", "0.21.6"} <= set(entry["verified"])
