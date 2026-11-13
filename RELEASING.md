# Releasing

What a release changes besides the code. Tests and CI hold the first three; the rest is by hand.

- the version in `plugin/telegram_dashboard_probe/plugin.yaml`,
  `telegram_dashboard/__init__.py` (`__version__`) and `pyproject.toml` is the same
  (`tests/test_dashboard_version.py`)
- `SHA256SUMS` lists every file of the plugin folder (CI: "SHA256SUMS matches
  the plugin folder")
- the suite, `ruff` and `mypy --strict` pass in CI
- `plugin/telegram_dashboard_probe/README.md`: the example `Dashboard X.Y.Z` in "What
  the message shows" names the new version
- the widget mode (since 1.2.0-beta.1): `tests/test_widget_html.py` holds the sent HTML within
  4096 bytes after zlib-9 minus its margin, on the zlib of the interpreter that runs it: run the
  suite on the engine's Python too (zlib-ng compresses the HTML a few bytes worse); the README's
  byte and packet figures in "The widget" name the release's own; its look is checked by eye in
  Telegram Desktop and on a phone (the CI has no client)
- `README.zh-Hans.md`: translate the changed sections again and move the commit in
  its header, or keep the header's commit on purpose. The tests read only
  `plugin/telegram_dashboard_probe/README.md`, so nothing else notices a translation
  that lags
