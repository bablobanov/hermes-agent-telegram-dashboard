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
