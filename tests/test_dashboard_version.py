"""The dashboard names its own version in the details (decision of 01.10): the package
constant, which the plugin manifest and the project metadata must agree with, so the line on
the screen is the version that was installed, never a stale ``0.1.0``."""

import re
from pathlib import Path

from probe_fakes import PLUGIN_DIR

import telegram_dashboard

ROOT = Path(__file__).resolve().parents[1]


def test_the_package_the_manifest_and_the_project_name_one_version() -> None:
    manifest = (PLUGIN_DIR / "plugin.yaml").read_text(encoding="utf-8")
    project = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    in_manifest = re.search(r"^version: (\S+)$", manifest, re.MULTILINE)
    in_project = re.search(r'^version = "([^"]+)"$', project, re.MULTILINE)

    assert in_manifest is not None and in_project is not None
    assert in_manifest.group(1) == telegram_dashboard.__version__
    assert in_project.group(1) == telegram_dashboard.__version__
    assert telegram_dashboard.__version__ != "0.1.0"
