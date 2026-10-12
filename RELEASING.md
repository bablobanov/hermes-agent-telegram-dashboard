# Releasing

What a release changes besides the code. Tests and CI hold the first three; the rest is by hand.

- the version in `plugin/telegram_dashboard_probe/plugin.yaml`,
  `telegram_dashboard/__init__.py` (`__version__`) and `pyproject.toml` is the same
  (`tests/test_dashboard_version.py`)
- `SHA256SUMS` lists every file of the plugin folder (CI: "SHA256SUMS matches
  the plugin folder")
- the suite, `ruff` and `mypy --strict` pass in CI
- `README.md`: the example `Dashboard X.Y.Z` in "What the message shows" names the
  new version
- `README.zh-Hans.md`: translate the changed sections again and move the commit in
  its header, or keep the header's commit on purpose. The tests read only
  `README.md`, so nothing else notices a translation that lags
