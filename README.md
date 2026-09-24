# hermes-agent-telegram-dashboard

[![CI](https://github.com/bablobanov/hermes-agent-telegram-dashboard/actions/workflows/ci.yml/badge.svg)](https://github.com/bablobanov/hermes-agent-telegram-dashboard/actions/workflows/ci.yml)

![Hermes Agent Telegram Dashboard](docs/social-preview.jpg)

Read-only status dashboard for [Hermes Agent](https://github.com/NousResearch/hermes-agent) as one
pinned Telegram message, edited in place on a schedule. No Mini App, no LLM call.

The updater lives **inside the gateway as a plugin** (`plugin/`): the bot token never leaves the
engine, the message is edited through the engine's own Telegram adapter. A cron tick with a
separate bot (`python -m telegram_dashboard`) remains as the fallback for installations without
the plugin API.

## Install

One line puts the plugin and its vendored package into the gateway's plugin directory:

```bash
git clone https://github.com/bablobanov/hermes-agent-telegram-dashboard.git && cd hermes-agent-telegram-dashboard && install -d "${HERMES_HOME:-$HOME/.hermes}/plugins/telegram_dashboard_probe" && cp -r plugin/telegram_dashboard_probe/. telegram_dashboard "${HERMES_HOME:-$HOME/.hermes}/plugins/telegram_dashboard_probe/"
```

Then enable it in `config.yaml` (`plugins.enabled`, the `chat_id` setting; the table under
"Deploying on a gateway" below), restart the gateway, pin the message it sends. Nothing is
installed into the engine's environment; `SHA256SUMS` in the repository root lists the files as
the plugin directory sees them, so `sha256sum -c` verifies a copy.

## What the message shows

1. Top line: overall status, data time, message confirmation time, source coverage, gateway
   and Telegram state, and the last `state.db` backup (time, age in words, integrity verdict)
2. Account limits: Claude and Codex from the Hermes usage facade, Grok's weekly pool from the
   surface xAI serves its own Grok CLI, Kimi Code's windows from the surface the Kimi Code
   platform serves its own clients (see "Limits" below); Gemini is shown as "no confirmed
   source", never as zero. A line without a number always names its reason
3. Config drift: number of keys that differ from the approved baseline
4. Up to five events that need attention

A source that cannot prove a value says `unknown`. A source that does not exist on this
installation says `unsupported`. Both lower coverage; neither turns green.

## Freshness is a load-bearing requirement

A pinned dashboard with a dead updater looks exactly like a calm system. Therefore:

- the message always carries the absolute data time and the last Telegram confirmation time
- a message not confirmed for two periods gets a loud banner on its first line; a confirmation
  within one period plus a little slack (`min(60 s, period/2)`, timer jitter of an updater that
  reads its own record one period later) still counts as confirmed
- `python -m telegram_dashboard --config c.json --check` reports message freshness with exit code
  0 (confirmed), 1 (lagging), 2 (stale, lost, never) for an external watchdog. A confirmation
  time ahead of the clock by more than a minute is `stale`, not "very fresh"
- the check reads the record of whichever path delivers: `plugin_state_path` for the plugin
  (the engine's `plugin-data/<namespace>/state.json`), otherwise `state_path` of the cron tick.
  The two paths never write the same file, and a check pointed at the wrong one says `never`

`message is not modified` from Telegram is treated as a confirmation, not a failure. A missing
message is logged at ERROR, recorded, recreated once, and announced in the new message.

## Compatibility across Hermes versions

The dashboard probes capabilities, never version numbers. `telegram_dashboard/compat_matrix.json`
records per source how it is probed and on which versions it was verified. A new release lowers
coverage and names what is missing instead of breaking.

What "verified" means here, honestly:

| Hermes | How |
|---|---|
| 0.21.1 (`2237be3559`) | the plugin path executed against the real engine objects (`tests/test_probe_plugin.py`) and a continuous pilot on a live gateway since 2026-09-11 |
| 0.21.3 (`v2026.9.14`) | the pilot gateway after its update; the Kimi credential resolver and registry row read in the engine source |
| 0.20.5 | read in the source of a desktop install: the sources degrade, the plugin API is absent (see the floor below) |

Anything else is not verified. Above these versions the sources are probed at runtime and
degrade to `unsupported`; nothing here promises "works on all versions".

**Delivery has a floor: Hermes 0.21.1.** The plugin path needs
`PluginContext.register_platform_handler` and the adapters' `_wire_plugin_handlers`, and both
are absent in 0.20.x (checked on a 0.20.5 install: not present anywhere in the tree; `spawn_task`
and `PluginState` exist there but nothing would ever call the factory). On 0.20.x the plugin
does not load a handler and nothing is delivered; the only path there is the fallback cron tick
with its own bot token. The compatibility principle still holds for the *sources* (gateway
state, limits, drift degrade per version); it does not extend to delivery, and this README
does not promise otherwise.

Three prohibitions, learned from a predecessor that died of them: no imports of engine
**internals**, no replacement of core files, no post-merge hooks or `assume-unchanged`.

One named exception: account limits come from `agent.account_usage.fetch_account_usage`, a public
function with the same signature on every verified version, called exactly the way the gateway's
own `/usage` calls it (worker thread, deadline, failures never propagate; `limits.py`). An import
failure makes the source `unsupported`. The exception is a row in the matrix, not a habit.

## The plugin: the screen inside the gateway

`plugin/telegram_dashboard_probe/` is the plugin. Every tick it collects, renders the first screen
and edits the same message; the counter text of the vertical slice is gone.

```
register(ctx) → ctx.register_platform_handler("telegram", wire)
             → adapter.connect() calls wire(app, adapter)
             → ctx.spawn_task(tick loop)            (exactly once; reconnects do not add loops)
             → each tick: collect_all_async → render_dashboard → to_telegram_plain, then
               adapter = runner.adapters["telegram"] if connected,
               adapter.send(...) once, adapter.edit_message(...) afterwards
```

Two traps found by reading engine 0.21.1 and built into the plugin: `connect()` runs the factory
again on every reconnect with a new adapter instance, and `TelegramAdapter.edit_message` does not
forward to the replacement adapter (`send` does). A loop that keeps its first adapter would report
"Not connected" forever while looking alive. `tests/test_probe_plugin.py` executes the chain
against the real engine objects with the Telegram `Bot` mocked and simulates the reconnect.

**The screen is plain text, on purpose.** `edit_message` without `finalize` sets no parse mode
(and the engine builds its PTB application without `Defaults`, so nothing is applied behind its
back). Its `finalize=True` path converts to MarkdownV2 and, when the escaped payload exceeds
4096 UTF-16 units, splits it into NEW continuation messages: a pinned dashboard must never do
that, and the dashboard cannot know how many `\` the engine will add. Headings are upper-case
lines (`to_telegram_plain`); the cron path keeps the HTML form for its own bot. The one
`send` that creates the message (and a recreation after a loss) goes through the adapter's
own markdown conversion; the screen has no markdown constructs, so it renders the same.

**A tick that cannot build the screen still edits the message.** Whatever fails while composing
(a collector, the render, the package import), the message gets a loud one-screen notice with
the exception class and the time, and the record carries `last_render_error`. Yesterday's text
on a pinned dashboard looks exactly like a calm system, which is the failure this project exists
to prevent. Inside the gateway the drift command and the usage facade run in worker threads with
deadlines (`collect_all_async`), so a slow provider never stalls the Telegram event loop.

Deploying on a gateway (0.21.x; 0.20.x has no `register_platform_handler`):

1. copy `plugin/telegram_dashboard_probe/` to `$HERMES_HOME/plugins/telegram_dashboard_probe/`
   **and** `telegram_dashboard/` into that same folder
   (`$HERMES_HOME/plugins/telegram_dashboard_probe/telegram_dashboard/`). The engine loads a
   directory plugin as a package with `__path__`, so the copy beside the plugin is imported as a
   relative package and nothing is installed into the engine's environment. A `telegram_dashboard`
   installed in the interpreter is the fallback; with neither the message says so instead of
   showing a screen
2. in `config.yaml`: add `telegram_dashboard_probe` to `plugins.enabled` and set
   `plugins.entries.telegram_dashboard_probe.settings`:

   | setting | env fallback | meaning |
   |---|---|---|
   | `chat_id` | `HERMES_DASHBOARD_PROBE_CHAT` | required; no chat, no handler |
   | `thread_id` | `HERMES_DASHBOARD_PROBE_THREAD` | forum topic; omit for General |
   | `period_seconds` | `HERMES_DASHBOARD_PROBE_PERIOD` | default 60; finite, clamped to [0.01, 86400] |
   | `hermes_home` | `HERMES_HOME` | default `~/.hermes`; where `gateway_state.json` lives |
   | `drift_report` | `HERMES_DASHBOARD_PROBE_DRIFT_REPORT` | JSON with `checked_at`, `exit_code`, `stdout` |
   | `drift_command` | (config only, a list) | argv of `check_drift.py`; run off-loop on EVERY tick, 30 s limit, never two at once |
   | `limits_enabled` | `HERMES_DASHBOARD_PROBE_LIMITS` | default on; `0`/`false`/`no`/`off` turns it off |
   | `display_timezone` | `HERMES_DASHBOARD_PROBE_TZ` | IANA name; unknown degrades to UTC |
   | `limits_refresh_seconds` | `HERMES_DASHBOARD_PROBE_LIMITS_REFRESH` | default 900, floor 60; how often Grok and Kimi are asked (not every tick) |
   | `backup_status` | `HERMES_DASHBOARD_PROBE_BACKUP_STATUS` | JSON status of the last `state.db` backup (see "The backup line"); unset = the line says "не наблюдается" |

   Neither drift source configured means drift is `unsupported` on the screen, never zero.
   A source that hangs is not started again until its worker returns (one worker per
   source across ticks), so a stuck facade or command cannot fill the gateway's executor.
3. restart the gateway; the message appears in the configured chat and its screen moves

Use a chat you can sacrifice. The plugin writes its delivery record to the plugin state
(`$HERMES_HOME/plugin-data/<namespace>/state.json`, one key `probe`) so a gateway restart edits
the same message. The directory name carries a digest of the plugin id
(`agent-plugin-telegram_dashboard_probe-<8 hex>`); the plugin logs the exact path at start
(`probe: delivery record for --check plugin_state_path: ...`) at INFO, which a gateway that
journals WARNING and above never shows: take the path from disk
(`ls $HERMES_HOME/plugin-data/ | grep probe`). The plugin's observability rests on that state
file, not on the journal; an empty `grep probe:` in the journal means nothing.

The record also carries `limits_cache`, one entry per provider read on its own cadence
(`grok`, `kimi`), each with the last attempt (`attempted_at`) and its item, so the interval
survives a restart and the state file shows when the provider was last asked. A record from
before Kimi (Grok's attempt at the top level) is moved under `grok` once.

### The backup line

`Бэкап: 24.09 05:31 +05 · 6 ч назад · integrity ok` sits in the top block, beside the gateway
line, in every state: a screen silent about the norm makes silence indistinguishable from
confirmation. The source is a JSON status a backup job writes on every run (`ok`, `phase`,
`reason`, `integrity`, `finished_epoch` or `finished_at`); the dashboard reads that file only
and never opens the database or the copy. A failed run is loud on the line
(`⚠️ не состоялся … · <phase>: <reason>`) and an event; a status older than 26 h is marked
(`⚠️ старше 26 ч`) and an event; a missing or unreadable file is `нет данных (<reason>)` and an
event; no `backup_status` configured is `не наблюдается`. The status shape is the one our own
timer writes (`state_db_publish.py` in the operator repository); any writer that produces the
same keys works.

### Limits: three kinds that never mix

Every limit line is one of three things, and the caption says which: an **official quota**
(a number the provider itself reports, with its own reset date), **local accounting** (tokens
this installation counted; it never knows the remaining quota, nor spend outside the agent), or
**"нет данных" with a reason**. "Official" means *the number came from the provider*, not
*the surface is in the provider's docs*: the engine's own Anthropic and Codex fetchers read
undocumented surfaces (`api/oauth/usage`, the ChatGPT backend), and so does the Grok reader.
A documented surface would be preferable; an undocumented provider number is still the
provider's number, and a local count is not.

Each official line carries its own stamp (`· данные HH:MM`): a number read on its own cadence
must not borrow the screen's `Обновлено`. A stale number under a fresh stamp looks like
knowledge, which is the one thing a limits block must never do.

**Grok** (`telegram_dashboard/grok.py`): the weekly pool of the SuperGrok subscription, read
from `https://cli-chat-proxy.grok.com/v1/billing?format=credits` with the token the engine
already holds for `xai-oauth`, obtained through the engine's own resolver
(`hermes_cli.auth_xai.resolve_xai_oauth_runtime_credentials`, so the screen shows the quota
of exactly the grant inference uses) and the client header the Grok CLI sends. The plan name
comes from `…/v1/settings` (`subscription_tier_display`), optional. Probed on 2026-09-12: the
inference host `api.x.ai` answers 404 for this path, so the proxy host is required. Policy:
one attempt per `limits_refresh_seconds`, success or failure; a failed attempt is "нет данных"
with its reason until the next interval; a cached number older than the interval is not shown.
The shape is checked strictly (`currentPeriod.type == USAGE_PERIOD_TYPE_WEEKLY`, a finite
percent in 0..100, a readable end date); anything else is named, not guessed. Grok is its own
source on the coverage line (`квота Grok`).

**Kimi** (`telegram_dashboard/kimi.py`): the windows of the Kimi Code subscription, read from
`<base URL>/v1/usages` on the very host the engine uses for Kimi inference. The key and the base
URL come from the engine's own resolver
(`hermes_cli.auth.resolve_api_key_provider_credentials("kimi-coding")`: `.env` first, then the
credential pool, then the key-prefix redirect), so the screen shows the quota of exactly the
credential inference uses, and the request carries the client header the engine sends to that
host. The shape is the one the official client parses (`@moonshot-ai/kimi-code-oauth`,
`managed-usage.ts`): `usages.limit_5h`, `usages.limit_7d` (legacy plans), `usages.limit_month_total`
(new plans), each with `used_ratio` in 0..1 and a `reset_time`; every window keeps its own reset
on the line (`5 ч 12% (сброс 25.09 03:10) · неделя 40% (сброс 26.09 17:32)`). Same policy and
cache as Grok; the request never goes through the credential pool's rotation, so a failed
request cannot mark the pool exhausted. Not in Kimi's docs; a changed shape is named, not
guessed. Kimi is its own source on the coverage line (`квота Kimi`).

**Claude on an installation without an Anthropic credential**: the line says
`нет данных (у сервера нет учётного токена)`, a reason, never a zero.

The check for the plugin path is the same `--check`, pointed at that file:

```json
{ "plugin_state_path": "~/.hermes/plugin-data/<namespace>/state.json", "period_seconds": 300 }
```

`period_seconds` here is the tolerance the check grants, not the plugin's period: with the
plugin editing every 60 s, 300 means five missed ticks before the first alert (`confirmed` up to
one period plus slack, `lagging` up to two, `stale` past that). The record also carries
`tick_failed` when a tick raised, so a loop that dies every tick does not keep an old `edited`
on disk.

### The watchdog: `watchdog/dashboard_probe_check.py`

A dead plugin cannot edit the message to say it is dead, so the check must run outside the
plugin. `watchdog/dashboard_probe_check.py` is that check in the shape of a Hermes cron job with
`--no-agent --script`: the scheduler runs it as `python <path>` from `$HERMES_HOME/scripts/`
(scripts are accepted from nowhere else), delivers whatever it prints, and treats empty stdout
as silence. Cron passes no arguments, so the config is `dashboard_probe_check.json` next to the
script (`plugin_state_path`, `period_seconds`, `check_interval_seconds`; a path as the first
argument serves manual runs, `--verbose` prints the state line even when silent).

```bash
hermes cron create "*/5 * * * *" --name dashboard-probe-check --no-agent   --script dashboard_probe_check.py --deliver telegram:<chat_id>:<alert_topic>
```

Repeat policy, and why: the durable state lives in the message itself, whose freshness stamp is
what the dashboard is for; an alert is an interruption, not an indicator, and twelve an hour
teach the reader to stop reading the watchdog. So `confirmed` is silence; `lagging` and `stale`
speak on the first tick past the confirmation threshold and then once an hour; `never` and
`lost` speak every tick, because there is no message to carry the state and silence there would
be indistinguishable from health. The repeat is stateless (counted from the confirmation stamp,
the watchdog stays read-only) and its hourly window is exactly one tick wide, so a tick the
scheduler skipped swallows that hour's alert; the next hour speaks again. Exit code is 0 in
every judged state; non-zero only when the watchdog itself is broken (config or package
missing), which the scheduler reports as a failed script.

Deployment ships a **cut** of the package next to the script, not the whole thing:
`telegram_dashboard/` with `__init__.py`, `schema.py`, `freshness.py`, `timeparse.py` only
(`PACKAGE_MODULES` in the script). `tests/test_watchdog_script.py` runs the script in a
subprocess with `-I` against a directory holding exactly that cut, so an import creeping past it
fails the test, not the server. The plugin and the cut are two artifacts of one commit; nothing
but their sha256 ties them together on the server, so check both at delivery.

Coverage boundary: the job runs inside the same gateway as the plugin and alerts through the
same channel. It covers "plugin dead, gateway alive" and **not** "gateway dead": there both are
silent, and that silence looks exactly like health. Acceptance therefore needs one real run from
the scheduler with the alert actually arriving (`hermes cron run` bypasses the scheduler's
dispatch), not only a manual run that prints.

## Running the fallback tick

```bash
python -m telegram_dashboard --demo 2                 # render static verification state 2
python -m telegram_dashboard --config c.json --dry-run
python -m telegram_dashboard --config c.json          # edit the pinned message
python -m telegram_dashboard --config c.json --check  # freshness of the message itself
```

Config is JSON. Every deployment-specific value (chat id, topic id, paths, token variable name)
lives there, not in code:

```json
{
  "hermes_home": "~/.hermes",
  "chat_id": "-1001234567890",
  "thread_id": "17585",
  "period_seconds": 300,
  "state_path": "~/.hermes/scripts/dashboard-delivery.json",
  "token_env": "HERMES_DASHBOARD_BOT_TOKEN",
  "drift_command": ["python3", "/path/to/check_drift.py"],
  "backup_status": "/path/to/daily-status.json",
  "display_timezone": "UTC",
  "limits_enabled": true,
  "recreate_on_loss": true
}
```

Limits are only available when the tick runs in an interpreter that can import the engine (the
gateway's own); elsewhere they degrade to `unsupported`.

## Tests

```bash
PYTHONPATH=. python -m pytest tests -q          # plus `pip install tzdata` on Windows
ruff check . && ruff format --check . && mypy --strict telegram_dashboard
```

Every run prints `telegram_dashboard.__file__` in the header: a green run that does not say which
tree it tested proves nothing. No test reaches a provider: the Grok and Kimi attempts are stubbed
by an autouse fixture in `tests/conftest.py` unless a test passes its own fake. `tests/test_probe_plugin.py` skips unless the Hermes engine is
importable; to run it, use an interpreter with the engine and `python-telegram-bot` installed
(for example `uv sync --extra messaging` in an engine checkout with `UV_PROJECT_ENVIRONMENT`
pointing outside the checkout, then that venv's `python -m pytest tests/test_probe_plugin.py`).

## License

MIT.
