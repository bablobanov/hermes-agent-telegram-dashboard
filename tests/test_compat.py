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
    """0.8.0: the external limit sources are engine-independent and say so; the Codex plan is a
    field of the facade the limits row already calls, read in the engine source."""
    sources = load_matrix()["sources"]
    external = sources["external_limits"]
    codex = sources["codex_plan"]

    assert "limits_sources" in external["probe"] and "loopback" in external["probe"]
    assert "_BOT_TOKEN" in external["probe"]
    assert "contract 1" in external["contract"] and "25 s" in external["contract"]
    assert "no engine import" in external["caveat"]
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


def test_the_cron_sources_and_the_traffic_probe_are_rows_of_the_matrix() -> None:
    """0.9.0: the cron line reads the engine's cron files and its run history read-only; the
    deaf verdict reads four private counters off the live adapter as a capability. Each row
    says what is read, what never is, and on which engine source it was read."""
    matrix = load_matrix()
    sources = matrix["sources"]
    cron = sources["cron"]
    runs = sources["cron_runs"]
    traffic = sources["telegram_traffic"]

    assert "jobs.json" in cron["probe"] and "first token" in cron["probe"]
    assert {"prompt", "script", "deliver"} <= set(cron["never_reads"])
    assert "200" in cron["contract"] and "15 min" in cron["contract"]
    assert "error_kind" in cron["contract"] or "kind of the error" in cron["contract"]
    assert verification_for(matrix, "cron", "0.21.3").startswith("verified on 0.21.3")
    assert "main" in cron["verified"] and "6ec05205a9" in cron["verified"]["main"]

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
