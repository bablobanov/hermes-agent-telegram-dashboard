# hermes-agent-telegram-dashboard

**English** · [简体中文](README.zh-Hans.md)

[![CI](https://github.com/bablobanov/hermes-agent-telegram-dashboard/actions/workflows/ci.yml/badge.svg)](https://github.com/bablobanov/hermes-agent-telegram-dashboard/actions/workflows/ci.yml)

![Hermes Agent Telegram Dashboard](docs/social-preview.jpg)

Read-only status dashboard for [Hermes Agent](https://github.com/NousResearch/hermes-agent) as one
pinned Telegram message, edited in place on a schedule. No Mini App, no LLM call.

The updater lives **inside the gateway as a plugin** (`plugin/`): the bot token never leaves the
engine, the message is edited through the engine's own Telegram adapter. A cron tick with a
separate bot (`python -m telegram_dashboard`) remains as the fallback for installations without
the plugin API.

## Install

The plugin is listed in the [Hermes Plugin Catalog](https://hermes-agent.nousresearch.com/docs/plugins/telegram_dashboard_probe)
under its manifest name; the catalog pins the reviewed commit, and the install checks out exactly
that commit into the gateway's plugin directory:

```bash
hermes plugins install telegram_dashboard_probe
```

The plugin folder `plugin/telegram_dashboard_probe/` is self-contained: the `telegram_dashboard`
package lives inside it (module paths below, like `telegram_dashboard/grok.py`, are relative to
that folder). Without the catalog, one line puts the folder in place from a clone:

```bash
git clone https://github.com/bablobanov/hermes-agent-telegram-dashboard.git && cd hermes-agent-telegram-dashboard && install -d "${HERMES_HOME:-$HOME/.hermes}/plugins" && cp -r plugin/telegram_dashboard_probe "${HERMES_HOME:-$HOME/.hermes}/plugins/"
```

Either way, then enable it in `config.yaml` (`plugins.enabled`, the `chat_id` setting; the table
under "Deploying on a gateway" below), restart the gateway, pin the message it sends. Nothing is
installed into the engine's environment; `SHA256SUMS` in the repository root lists every file of
the plugin folder, so `sha256sum -c SHA256SUMS` run from `plugin/` (or from the gateway's
`plugins/` directory) verifies a copy.

## What the message shows

One phone screen in blocks, a thin line between two, then two folds that Telegram shows
collapsed. The example is the rich view (`rich: true`) as 1.0.0 draws it for the showcase state
(`python -m telegram_dashboard --demo 14` prints its plain form); the classic view, the default,
says the same in text (below). The width is a guide, not a law: a phone sets a proportional
font, so a line fits or wraps by what is on it (an emoji or a Cyrillic letter is wider than a
Latin one). An event names a cron job by its label in full and a long one wraps; the time of a
limit is never rounded for the width:

```
🟢 Healthy · updated Sep 26 21:00 UTC    the status and when the data was read; the
                                        pinned bar shows this line (under the
                                        freshness banner when that is up), and
                                        Telegram may say "3 minutes ago" instead of
                                        the date
✓ Gateway ✓ Telegram ✓ Backup ✓ Drift ✓ Cron
                                        what works, in one row: the gateway process,
                                        the Telegram channel, the last state.db backup,
                                        config drift, the cron ticker and its jobs
🧠 Limits used                           a table: the spent share and the time to its
Provider  Used · resets in              reset of every window, in the provider's own
Claude    42% · 2h10m    67% · 3d       order; ⚠️ from 90% spent; a provider without
Codex     ⚠️ 93% · 1h20m 58% · 5d       a number never shows a zero, the reason is in
Gemini    no data                       Details
Grok      31% · 6d
Kimi      8% · 3h40m     46% · 21d
🤖 Hermes 0.21.3 → 0.21.5                the version the gateway runs → the latest
                                        upstream release; 🆕 and its age for a day
                                        after a release came out
▸ 🕒 Cron · 27 active · 3 paused · 2 done
                                        collapsed: every job in one table, by how
                                        often it runs (see "The cron line")
▸ Details                               collapsed, a section per source: the plans,
                                        logins and Gemini refusals; the agent's rules;
                                        its memory; the Telegram channel; backup and
                                        drift; the Hermes version; every "no data"
                                        with its reason; then the screen's time, the
                                        confirmation, the odd data minute, the period,
                                        the coverage, and
  Dashboard 1.0.0                       the dashboard's own version, last
```

When something is off, 🛠 Needs attention comes between the top line and the ✓ row, and
❔ Not checked after the row, before the limits (the state `--demo 21`, here with no Gemini log
on the installation and the cron history not answering):

```
🟡 Warning · updated Sep 26 21:00 UTC
🛠 Needs attention                       up to five events in the collectors' order,
- Cron run failed: daily-digest         then the line of every item of the row that
Cron ⚠️ 1 of 27 failing                 is not well, even one an event already names
✓ Gateway ✓ Telegram ✓ Backup ✓ Drift   the row keeps what is well
❔ Not checked: Gemini log (not on this installation), cron history (unavailable)
                                        what the screen could not check: a source not
                                        on this installation, one that did not answer
                                        or is stale, profiles not seen
```

The classic view (`rich: false`, the default) is the same blocks as text, a blank line between
two: the limits a line per provider (`⚠️ Codex 93% (1h20m) · 58% (5d)`, `Gemini · no data`),
no fold for the cron jobs (their lines sit in the details with backup and drift, the job table
is the one part this view does not carry), and the details one collapsed quote. In HTML the top
line and the headings are bold; in plain text the headings are upper case (`🧠 LIMITS USED`) and
the details are shown in full. Both views come from the same snapshot.

The dashboard is one pinned message in the agent's private chat or in a topic of its
group. The plugin inside the gateway edits it in place every few minutes through the engine's
own Telegram adapter, so the pinned-message bar at the top of the chat always shows the current
top line (the freshness banner first, while it is up), and the message under it carries the
rest: gateway and Telegram state (and whether
the connected channel is deaf), the last backup, config drift, the cron ticker and its jobs,
whether the agent sees its rules (the context files in its system prompt), whether its
memory works (the notebooks against their limits, the writes waiting for approval), the
account limits of every provider with the time to each reset, the
Hermes version the gateway runs next to the latest upstream release, and a collapsed details
block with the reasons and the timestamps. Nothing to open, nothing to install
on the reader's side, no second bot, no LLM call: it is there every time the chat is opened.

<img src="docs/dashboard-preview.jpg" width="600" alt="An illustration of the pinned dashboard: status line, gateway, backup, drift, usage limits and details">

The picture is an illustration of what the message carries, drawn before 1.0.0; the message
itself is the screen above.

Up to five events that need attention come right after the top line, under 🛠 Needs attention.
Account limits: Claude and Codex from the Hermes usage facade, Grok's weekly pool from the
surface xAI serves its own Grok CLI, Kimi Code's windows from the surface the Kimi Code platform
serves its own clients (see "Limits" below), and any provider from a local process of your own
(see "External limit sources"). Gemini has no number without billing, never a zero: the last
time Google refused the engine a call is on its line while it is recent and always in the
details (see "Gemini refusals from the engine log"). Nothing is dropped from the old screen,
only moved: a line without a number still names its reason, in the details.

A source that cannot prove a value says `unknown`. A source that does not exist on this
installation says `unsupported`. Both lower coverage; neither turns green (one exception, the
cron run history: it only adds to a line that stands on its own file, see "The cron line").
Coverage itself (`Profiles 1/1 · sources 10/10`) lives in the details and comes up to the screen
only when it is incomplete, as one line:
`❔ Not checked: Gemini log (not on this installation), limits (stale), profiles 2/3` (and
`2 sources unavailable` for failed sources the line does not name). An item of the ✓ row with no
source on this installation, or no data from a source that line names, is left out of the row
and said by that line once. The details close with the dashboard's own version
(`Dashboard 1.0.0`), so the installed copy can be told from the pinned message at a glance.

**Rich view:** needs the setting `rich: true` (off by default), the engine's bot with the raw Bot
API call (read on Hermes v0.21.6 and v0.22.2) and a Telegram app that shows Rich Messages; remove
the setting or set `rich: false` to turn it off. Telegram documents no fallback text for a rich
message, so an app without Rich Messages may not show the screen at all.

With it on, where Telegram takes Rich Messages (Bot API 10.1+), the message is the rich view
above. How the plugin sends each view, and when it falls back to the classic one, is in "The
plugin" below.

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

The dashboard probes capabilities, never version numbers. The version line shows a number and
decides nothing by it (see "The Hermes version line"). `telegram_dashboard/compat_matrix.json`
records per source how it is probed and on which versions it was verified. A new release lowers
coverage and names what is missing instead of breaking.

What "verified" means here, honestly:

| Hermes | How |
|---|---|
| 0.21.1 (`2237be3559`) | the plugin path executed against the real engine objects (`tests/test_probe_plugin.py`) and a continuous pilot on a live gateway since 2026-09-11 |
| 0.21.3 (`v2026.9.14`) | the pilot gateway after its update; the Kimi credential resolver, registry row and the adapter's `_edit_text` read in the engine source; the plugin edits the pinned message as HTML through `_edit_text` on that gateway since 2026-09-25 (`screen_format: html` in its record, no fallback to plain taken); the public `edit_message` read in the source takes no parse mode, so the signature check picks `_edit_text` (`html_verb: _edit_text`); for 0.9.0 the cron files, the
ticker stamps, the run history's schema and the adapter's traffic counters read in the engine
source (`compat_matrix.json`, rows `cron`, `cron_runs`, `telegram_traffic`); for 0.10.0 the saved prompt in `state.db`, its context block and the runtime line naming the agent's directory read in the source, built by the engine's own functions and read back in `tests/test_context_files_engine.py` (row `context_files`) |
| 0.21.5 (`v2026.9.24`) | `_edit_text` and `edit_message` read in the engine source: same signatures and bodies as 0.21.1 and 0.21.3; for 0.10.0 the saved prompt's context block and runtime line read in the source and built by the engine's own functions in `tests/test_context_files_engine.py`, as on 0.21.3 (row `context_files`) |
| 0.21.6 (`v0.21.6`, 2026-10-08) | the whole suite against the installed engine on Python 3.14.6, the interpreter 0.21.6 requires; `hermes plugins validate`: `ok: true`, no warnings; every engine touchpoint of every source read against 0.21.5 (the matrix's `0.21.6` rows): the same or compatible, except three the plugin now reads as 0.21.6 does (the config through the engine's own YAML parser, the version without a constant, a context file's BOM); the plugin loaded by the engine's own plugin discovery in process, the default, and refused under `plugins.isolation: host` (below) |
| main (`485979ddf4`, 2026-09-28) | `edit_message` and `_edit_text` read in the engine source: same signatures, the public verb still without a parse mode |
| main (`6ec05205a9`, 2026-09-30) | the cron files (the heartbeat now carries the pid; the first token is read), the run history's schema and the adapter's traffic counters read in the engine source: the same fields and attributes as 0.21.3 |
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

**In process only.** Hermes 0.21.6 added `plugins.isolation`. Its default, `in_process`, loads
the plugin into the gateway as before. With `plugins.isolation: host` the engine runs
third-party plugins in a separate host process and refuses there the one call delivery rests
on, `register_platform_handler` (its factory receives the adapter's native SDK client): the
plugin does not load, and the engine says so (`ctx.register_platform_handler() cannot run in
the plugin host`; `hermes plugins validate` reports the verdict `in_process`). The plugin
cannot fix that on its side: the message is edited through the live Telegram adapter, which
stays in the gateway. A gateway under `host` has the fallback cron tick with its own bot token.

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
             → each tick: the traffic counters read off the adapter as attributes, then
               collect_all_async → render_dashboard → to_telegram_html,
               to_telegram_plain and to_telegram_rich, then adapter =
               runner.adapters["telegram"] if connected, the message created once
               (with rich: true sendRichMessage through the engine's bot, else
               adapter.send(plain)), then every tick, with rich: true, the rich form
               (_bot.do_api_request("editMessageText", ...)), and without it or on a
               refusal the HTML form through the verb the adapter has for it:
               edit_message(html, parse_mode="HTML") when its signature takes a parse mode,
               else the adapter's own _edit_text(html, "HTML"), and
               adapter.edit_message(plain) in the same tick when the HTML edit is refused
```

Two traps found by reading engine 0.21.1 and built into the plugin: `connect()` runs the factory
again on every reconnect with a new adapter instance, and `TelegramAdapter.edit_message` does not
forward to the replacement adapter (`send` does). A loop that keeps its first adapter would report
"Not connected" forever while looking alive. `tests/test_probe_plugin.py` executes the chain
against the real engine objects with the Telegram `Bot` mocked and simulates the reconnect.

**Two views of one screen.** Every form is built from the same blocks (`render_parts`) of one
snapshot. The rich view (`rich: true`, `rich.py`) is a Rich Message in sections: the top line,
Needs attention, the ✓ row, Not checked, the limits as a table and the Hermes version, a thin line
between two, then two collapsed `<details>`: the cron jobs as one table by how often they run, and
Details with a titled section per source (the plans, logins and Gemini refusals, the agent's
rules, its memory, the Telegram channel, backup and drift, the Hermes version, every "no data"),
the screen's own stamps and the dashboard's version closing it. The classic view (`rich: false`,
the default) is the same blocks as text: HTML when the adapter has a verb for it (bold headings,
the details in one `<blockquote expandable>`), else plain (upper-case headings, the details in
full); the limits are a line per provider, the cron lines sit in the details with backup and
drift, and no job table is drawn. The top line is bold in the HTML and the rich forms and carries
its time as `<tg-time unix="…" format="r">Sep 26 21:00 UTC</tg-time>`: Telegram shows "18 minutes
ago" where the client supports it (Telegram Desktop) and the words inside otherwise (Android), so
the words are the full date with the zone; the plain form writes them as they are.

**The screen is HTML when the adapter has a verb for it, plain text otherwise.** Which verb is
read off the adapter object on every tick, never off a version number, in this order:

1. the public `edit_message` when its signature has a parameter named `parse_mode`
   (positional-or-keyword or keyword-only; `**kwargs` does not count, a verb that swallowed the
   keyword would send the markup as plain text and answer success). Called with
   `parse_mode="HTML"`; a `SendResult` with `success=False` is a refusal of the form
2. otherwise the adapter's own `_edit_text(chat, id, text, parse_mode)`, the same on 0.21.1,
   0.21.3, 0.21.5 and main, which takes a parse mode and raises on refusal
3. otherwise plain, with `html_error: no HTML verb (edit_message without parse_mode, no
   _edit_text)`

On every engine known today the public verb takes no parse mode: without `finalize` it sends
plain text (the engine builds its PTB application without `Defaults`), and its `finalize=True`
path converts to MarkdownV2 and, when the escaped payload exceeds 4096 UTF-16 units, splits it
into NEW continuation messages, which a pinned dashboard must never do. So today the form goes
through `_edit_text`; the day the public verb takes a parse mode, the plugin switches to it
without a release. The HTML form is bold headings and the details in one
`<blockquote expandable>`, both Bot API 7.4 features that any bot may use, and the top line's
`<tg-time>` (above). On any refusal but
"not modified" the same tick edits plain through the public verb. A plain edit that succeeds
right after a failed HTML one means the form was refused: the screen stays plain (upper-case
headings, the details shown in full), the record says `screen_format: plain` with the reason in
`html_error` (the exception class, or `edit_message: <its error>`), one warning goes to the
journal, and HTML is tried again twelve ticks later. The record also names the verb that carries
the HTML while it does (`html_verb: edit_message` or `_edit_text`, `null` while plain). The
private method is a capability, never a promise (`compat_matrix.json`, row `screen_html`).
Without the rich form (below: off by default), the one `send` that creates the message (and a
recreation after a loss) goes through the adapter's own markdown conversion as plain text; the
next tick edits it into the HTML form. The cron path sends the HTML form with its own bot, rich
or not.

**Before HTML, the rich form** (since 0.12.0, only with `rich: true`). Where the engine's bot has the
raw Bot API call the engine sends its own rich messages with (`adapter._bot.do_api_request`, a
coroutine function: the engine's own test, `_bot_supports_rich`), every tick edits the message as
a Rich Message first: `editMessageText` with `rich_message: {html}`, the chat and the id as numbers, no topic,
no text and no parse mode, a 30 s deadline; "not modified" confirms. No verb of the adapter
carries it, and the public `edit_message(finalize=True)` splits into new messages, so the call is
the engine's bot itself: the bot token stays inside the engine and the plugin passes no
credential. The message asks Telegram to find no entity in it (`skip_entity_detection`), a second
line behind `<code>`. A private attribute, read as a capability on every tick, never a promise
(`compat_matrix.json`, row `screen_rich`). Any other answer: the same tick edits HTML, then
plain, as above, and the verdict follows the HTML rule. The next edit landed, so rich was
refused: `screen_format` names the form that landed, `rich_error` the exception class, one
warning goes to the journal, and rich is asked again twelve ticks later; without the call the
record says `rich_error: no rich call (adapter without _bot.do_api_request)`. A call that could
not be made (a method the Bot API server or the library lacks, a wrong argument) never reached
Telegram and counts as a refusal, as the engine counts it. Two rules keep the message single. On
a message the record says is rich, only the rich call's own answer may say the message is lost:
an edit of a rich message in another form may be refused in the words of a lost one, so an HTML
or plain answer counts as a loss only when it says the message is not there at all
(`message to edit not found`), and any other failure ends the tick as `edit_failed`. And a timeout
or a network error of the rich edit there ends the tick without an HTML or plain edit; the next
tick asks rich again. A new message is sent rich (`sendRichMessage` into the topic, none for
General, as the engine routes a send), since the engine notes that Telegram refuses a rich edit
of a plain message (its streamed previews in private topics): Telegram's refusal sends plain in
the same tick, a timeout sends nothing more in this tick; the next tick sends again, the same risk of
a second message as after a plain send that timed out. A message created before 0.12.0 is
therefore likely to stay HTML, with `rich_error` saying why; a new one, after a loss or in a new
chat, is rich. Over Telegram's ceilings for one rich message (32,768 characters, 500 blocks), or
when the rich form fails to build, the tick edits the HTML form, and a message that is rich
already gets the plain words as one rich paragraph, the loud notice too; the record says why
(`rich_render_error`), the journal once. A full form Telegram refuses on a rich message is followed
in the same tick by its plain words as one rich paragraph (`rich_error` says why, the journal
once), since an HTML or plain edit of a rich message may be refused for good. Without
`rich: true` (the default) the form is off: new messages are plain, then HTML, and a message that
is rich already keeps getting its plain words as one rich paragraph (delete it, and the next one
is HTML). A rich message carries no fallback text (`InputRichMessage` has no field for one), so
the form waits for an explicit yes.

**A tick that cannot build the screen still edits the message.** Whatever fails while composing
(a collector, the render, the package import), the message gets a loud one-screen notice with
the exception class and the time, and the record carries `last_render_error`. Yesterday's text
on a pinned dashboard looks exactly like a calm system, which is the failure this project exists
to prevent. Inside the gateway the drift command, the usage facade and the read of the engine's
error log run in worker threads with deadlines (`collect_all_async`), so a slow provider or a
hung disk never stalls the Telegram event loop.

Deploying on a gateway (0.21.x; 0.20.x has no `register_platform_handler`):

1. `hermes plugins install telegram_dashboard_probe` (the catalog pin), or copy
   `plugin/telegram_dashboard_probe/` to `$HERMES_HOME/plugins/telegram_dashboard_probe/`,
   with the `telegram_dashboard/` package inside it as the repository keeps it. The engine loads a
   directory plugin as a package with `__path__`, so the package beside the plugin is imported as
   a relative package and nothing is installed into the engine's environment. A
   `telegram_dashboard` installed in the interpreter is the fallback; with neither the message
   says so instead of showing a screen
2. in `config.yaml`: add `telegram_dashboard_probe` to `plugins.enabled` and set
   `plugins.entries.telegram_dashboard_probe.settings`:

   | setting | env fallback | meaning |
   |---|---|---|
   | `chat_id` | `HERMES_DASHBOARD_PROBE_CHAT` | required; no chat, no handler |
   | `thread_id` | `HERMES_DASHBOARD_PROBE_THREAD` | forum topic; omit for General |
   | `period_seconds` | `HERMES_DASHBOARD_PROBE_PERIOD` | default 60; finite, clamped to [0.01, 86400]; while the Telegram adapter is not connected yet (right after a start) the next try comes in 30 s, not a period later |
   | `hermes_home` | `HERMES_HOME` | default `~/.hermes`; where `gateway_state.json` lives, and the engine's `logs/errors.log`, which the Gemini line reads |
   | `drift_report` | `HERMES_DASHBOARD_PROBE_DRIFT_REPORT` | JSON with `checked_at`, `exit_code`, `stdout` |
   | `drift_command` | (config only, a list) | argv of `check_drift.py`; run off-loop on EVERY tick, 30 s limit, never two at once |
   | `limits_enabled` | `HERMES_DASHBOARD_PROBE_LIMITS` | default on; `0`/`false`/`no`/`off` turns it off |
   | `display_timezone` | `HERMES_DASHBOARD_PROBE_TZ` | IANA name; unknown degrades to UTC |
   | `limits_refresh_seconds` | `HERMES_DASHBOARD_PROBE_LIMITS_REFRESH` | default 900, floor 60; how often Grok, Kimi and the external sources are asked (not every tick) |
   | `limits_sources` | (config only, a list) | up to 4 local sources of limits: `url` on loopback, optional `key_env` and `timeout_seconds`; see "External limit sources" |
   | `backup_status` | `HERMES_DASHBOARD_PROBE_BACKUP_STATUS` | JSON status of the last `state.db` backup (see "The backup line"); unset = the ❔ line says `backup (not on this installation)` |
   | `context_files` | `HERMES_DASHBOARD_PROBE_CONTEXT_FILES` | default on; `0`/`false`/`no`/`off` turns the rules off, no event and no details line per platform (the details then say `Rules: off in the dashboard settings`); see "The rules line" |
   | `rich` | `HERMES_DASHBOARD_PROBE_RICH` | default off; `1`/`true`/`yes`/`on` turns the rich form on (Telegram documents no fallback text for a rich message: an app without Rich Messages may not show it). Off, the screen is the classic view, HTML or plain, and a message that is rich already gets its plain words as one rich paragraph (the record says `rich_error: off in the dashboard settings`); see "The plugin" |
   | `memory` | `HERMES_DASHBOARD_PROBE_MEMORY` | default on; `0`/`false`/`no`/`off` turns the memory section off, no event and no details lines (the details then say `Memory: off in the dashboard settings`); see "The memory section" |
   | `cron_notes` | (config only, a mapping) | job id → label, the owner's name for a cron job: the label replaces the job's name in the cron table, under "Needs attention" and in the details. Sanitized like a name (a secret, a path or a chat id typed into it is masked) and whole up to 80 characters; an entry whose id or label is not a non-empty string is skipped, anything but a mapping is ignored with a warning; the fallback tick reads the same mapping from its JSON config; see "The cron line" |

   `cron_notes` takes the job's `id` from `cron/jobs.json`; quote an id YAML would read as a
   number:

   ```yaml
   cron_notes:                     # in plugins.entries.telegram_dashboard_probe.settings
     "05d7506c24ff": Checks the dashboard updates
     "42d769b28188": Sends Threads topics to SMM
   ```

   Neither drift source configured means `drift (not on this installation)` on the ❔ line,
   never zero. The drift reader takes numbers, never words: a `drift_command` prints a line
   `keys_changed=N keys_total=M` (positions that differ, keys in the baseline) and exits 0 when
   clean, 1 on drift, 2 when it could not check; our own `check_drift.py` prints it last. Output
   without that line falls back to numbered sections `[N] …: count` and has no total
   (`Drift ⚠️ 3 keys` under "Needs attention", `✓ Drift 0 keys · checked 08:00` in the details);
   output with neither is no data when the exit code says drift (`drift (unavailable)` on the
   ❔ line, `Drift: drift present, output not parsed` in the details), and exit 0 without them
   still reads as clean (`✓ Drift`, `0 keys` in the details).
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
before Kimi (Grok's attempt at the top level) is moved under `grok` once. Beside it,
`release_cache` holds the once-a-day check of the latest Hermes release the same way
(`attempted_at` and its item), and `external_cache` the external limit sources: one entry per
source under the first eight hex digits of its URL's SHA-256 (the URL itself is not written),
with the provider, the plan and the login date it last named. `gemini_log_cache` keeps the last
Gemini 429 the engine's log showed (`last_429`: its time, limit, retry and model, never the
message; the limit and the retry are kept, not shown) and when the log was last read
(`checked_at`), so a 429 that rotated out of the log is still the last one. `cron_cache` holds
where the run-history read left off (`runs_checked_at`), the last tick that looked
(`checked_at`) and the failures the engine erased that the details still hold (`held`, by job
id: the kind, the time, the name, the kind of the error in a word, how many ticks shown).
`traffic_cache` holds the polling generation and the update counter last seen, the last tick
that saw the counter grow, the longest gap between updates per day of the last seven, the first
sighting of a blocked send path and the last send error the engine logged.

### The cron line

```
✓ Cron                              in the ✓ row: the ticker beats and no active job fails
Cron ⚠️ 2 of 27 failing             under Needs attention: a run failed, a result was not delivered, a job is blocked or overdue
Cron ⚠️ ticker silent 12 min        no heartbeat for longer than the engine's own threshold (200 s)
Cron ⚠️ ticks failing 2 h           the ticker beats, but every tick ends in an error
❔ Not checked: cron (unavailable)   the files could not be read; the reason in the details
❔ Not checked: cron (not on this installation)
                                    no cron directory, or no jobs.json, on this installation
```

The counts open the cron lines of the details: `Cron 27 active · 3 paused · ticker 20:58`, the
active jobs (enabled and without a pause marker: scheduled or running; or enabled and parked in
error), the paused ones and the ticker's last heartbeat.

The sources are the engine's own files under `HERMES_HOME/cron/`, read only: `jobs.json`
(every job with the outcome of its last run: `last_status`, `last_error`, `last_delivery_error`,
`failure_streak`, `next_run_at`, `last_run_at`, `state`, `paused_at`; since 0.12.0 also its
schedule in the engine's own words, `schedule_display`, else the stored schedule's own words in
the order `hermes cron list` falls back to them (`schedule.display`, `schedule.value`,
`schedule.expr`, `schedule.run_at`); since 1.0.0 also how often it runs, from `schedule.kind`
with `schedule.expr` or `schedule.minutes`, below), the ticker's stamps
(`ticker_heartbeat`, `ticker_last_success`, `ticker_last_error`; the first token of each is the
epoch, which is why main's `<epoch> <pid>` heartbeat reads the same), and `executions.db`, the
run history, opened `mode=ro` with `PRAGMA query_only` in a worker under the tick's deadline
(`cron_jobs.py`, `cron_runs.py`). Never read: `prompt`, `script`, `deliver` (chat ids live
there), `skills`, the model, the `error` column of the history, `state.db`.

What counts as a failure is the engine's own rule set (`hermes cron status`): a `last_status`
outside `ok`, `delivery_failed`, `delivery_queued` is a failed run (a literal the plugin does not
know is named as it is and is never green); a `last_delivery_error` is a run whose result never
reached its chat, a failure like any other (decision of 01.10: a job that ran but nobody heard is
not green); `blocked_config` is blocked; a `next_run_at` more than 15 minutes in the past while
the ticker lives is overdue. A failed run's streak is the engine's `failure_streak`; the delivery
streak the engine does not keep is counted from the history (`delivery_outcome = 'failed'` rows
in a row). A paused job carries no failure: its last run is history. A pause is read as the
ticker reads it (`is_job_runnable`): a job off, in the `paused` state or stamped `paused_at` is
paused, so an enabled record with the stamp alone (a half-pause the engine itself self-disables
on its next loop, and `hermes cron list` shows active until then) is never overdue here (0.9.1).
The fields are read as the ticker reads them (0.9.2): `enabled: null` is off, a number in
`paused_at` is a stamp, a `Paused` state written by hand is no pause marker, and a job the
ticker may fire is active whatever its state short of `completed`.
The engine's terminal `error` state is a failure on an enabled job whatever its stamps, counted
among the active. Only a job counted among the active carries a failure, so the line never reads
`1 of 0` (0.9.2): a job switched off carries none in any state and is counted paused (one that
completed in neither column), as `hermes cron status` does not read it.

A failure the next successful run erased from `jobs.json` between two ticks is still a failure
nobody saw: the history shows it, and the details keep it for two ticks with the run that
replaced it (`Cron daily-digest: run failed Sep 30 11:35, ok since 11:40`; `cron_cache.held` in
the record). A history that cannot be read, or one the installation does not keep, is one
source that did not answer: the line and the events stand on `jobs.json`, the delivery
streak reads `streak unknown`, the ❔ line says `cron history (unavailable)` (a `chronos` cron
provider keeps no history: `not on this installation`), and the status is left to the other
sources. The history is the one source that lowers the coverage and never the status: it only
adds to a line that stands on `jobs.json`, and an installation that keeps none would otherwise
never be green. The jobs file that cannot be read is `cron (unavailable)` on the ❔ line and
turns the status ⚪ like any source.

**The engine's error texts never reach the screen.** `last_error`, `last_delivery_error` and the
ticker's error are reduced to the kind of the error in a word, in the order the engine's own
health projection uses (`auth`, `rate limit`, `timeout`, `not connected`, `chat unavailable`,
`network`, `permission denied`, `fd exhaustion`, `stale code`, else `error`): `(rate limit)` in
the details, the text nowhere, not even in the record. The shared sanitizer, which since 0.9.0
also masks chat ids (`[id]`) and delivery targets (`[target]`), is the second line, not the
first.

Severity: a failed run, an undelivered result, a blocked or an overdue job is a warning; a dead
ticker, or one whose every tick fails, is critical, because nothing scheduled runs. At most
three cron events on the screen; the line carries the full count; the details name every job
with its time, its streak and the kind of its error. An event names the job by its label in
full, as the details and the cron table do: whole up to 80 characters, a long one wraps on the
phone (accepted on 23.10). The thresholds are the engine's own: 200 s
for the heartbeat (three ticks plus slack), 15 minutes for an overdue job. The layered order of
the checks (ticker alive, record consistent, run recorded, delivery confirmed) follows
[itpartypattaya/hermes-cron](https://github.com/itpartypattaya/hermes-cron) (MIT); no code is
taken from it. `python -m telegram_dashboard --demo 3`, `21`, `22` and `24` show the forms.

**How often a job runs** comes from its stored schedule as the engine keeps it (`cron/jobs.py`,
v0.22.2): `kind`, and the `expr` of a `cron` job or the `minutes` of an `interval` one; a `once`
job (a `run_at`) is `Once`. Never from the display words, which can disagree with the
expression (a job shown as "every monday 6am" runs `0 6 * * 1`; `cron_frequency.py`):

- `interval`: whole minutes, at most a year, in the largest unit that divides them
  (`Every 45 min`, `Every hour`, `Every 6 h`, `Daily`, `Every 3 days`), else in minutes
  (`Every 90 min`)
- `cron`, five fields with every month: a step of minutes that divides the hour, every other
  field open (`*/15 * * * *`: `Every 15 min`); a fixed minute every hour (`Every hour`), every N
  hours with N dividing the day (`0 */6 * * *`: `Every 6 h`) or at listed hours (`Daily`,
  `Twice a day`, `3 times a day`); one hour on listed weekdays (`Weekly`, `Twice a week`,
  `Weekdays` for Monday to Friday) or on one day of the month (`Monthly`)
- anything else is `Other`: a step that does not divide its cycle (`*/45` fires at :00 and
  :45), another shape, a malformed or a missing schedule

**The cron fold of the rich screen** (1.0.0) comes collapsed after the main screen, titled with
the counts (`🕒 Cron · 27 active · 3 paused · 2 done`; `done` only when a job completed). It
opens with the cron lines of the details (the counts and the ticker, every failure), then one
table in the look of the limits table:

- a shaded header row per frequency, ordered from the most frequent by the minutes between two
  runs (`Once` first, as a one-shot has no second run; `Other` last; one period with two
  labels, `Every 12 h` and `Twice a day`, by the label), and under it the active jobs of that
  frequency, what fails now first, then by the next run. Under `Other` a job's schedule follows its name in the
  engine's own words, set in `<code>` up to 24 characters (`*/30 9-17 * * 1-5`), since the header
  does not say how often it runs
- `⏸ Paused` and the paused jobs by their next run, then `✅ Done` and the completed ones, the
  latest run first
- the date right-aligned in one short form: an active job's next run as the time on the
  screen's day (`21:15`), the weekday and the time within the week ahead (`Sun 02:00`), else the
  day (`Oct 3`); a paused job's next run and a done job's last run as the day (`Sep 19`); `—`
  when there is none to read
- ⚠️ before a job that fails now (a failed or undelivered run, a blocked job) or is overdue, and
  a line under the table that says what the mark means

A paused job whose next run is already behind keeps that date, and one line under the table
says why that is no alarm: a paused job does not fire, its date is the one set before the pause
(decision of 22.10: a mark, never a failure; the line and the status do not change). A job is
named by its label from `cron_notes`, else by its name; both pass the shared sanitizer and are
whole up to 80 characters, a schedule up to 24, and a word where Telegram would find a mention,
a hashtag or a cashtag (`@ops`, `report-#weekly`, `$BTC`) goes to `<code>`. A long list is cut
with how many are left out (`… 5 more`; 60 active, 30 paused, 20 done). The classic view draws
no job table: its details carry the cron lines with backup and drift.

### Connected but deaf: the Telegram line

`✓ Telegram` in the ✓ row says the adapter is running: `is_connected` on the engine's adapter is
literally "the adapter was started", nothing about updates arriving or replies leaving. The
adapter keeps counters of its own for that, and the plugin reads four of them off the live
adapter object it already holds, as attributes, once per tick, calling nothing on it:
`_updates_received_total`, `_polling_generation`, `_polling_last_progress_monotonic` and
`_polling_generation_started_monotonic`, plus one public property beside them,
`send_path_degraded` (declared on `BasePlatformAdapter`, overridden by the Telegram adapter;
`telegram_traffic.py`).

Three verdicts take Telegram out of the row while the adapter says connected; its line goes under
"Needs attention", after the events:

```
Telegram ⚠️ no sends                the adapter's own send gate is closed past the 120 s reconnect
                                    grace: every reply fails while this message may still move (critical)
Telegram ⚠️ stalled                 no successful getUpdates round-trip for five minutes (critical)
Telegram ⚠️ quiet                   no update seen for longer than this installation usually waits (warning)
```

The quiet threshold is relative: twice the longest gap between updates seen in the last seven
days, at least 6 hours and at most 48, and never before the first update was seen at all. An
absolute threshold lies at night on a one-person bot; a relative one without a floor shouts on a
chatty group at lunch; one without a ceiling never surfaces a dead bot on a quiet installation.
A new polling generation (a reconnect) resets the counter: a lower number is a reset, not
silence, and the first update of the new generation counts.

The details always carry the times: `✓ Telegram last update seen Sep 30 12:30 · polling ok 12:30`
(the mark is the Telegram item's: ✓ in the row, ⚠️ under "Needs attention", none when it is not
checked; plus `sends blocked since …` and `quiet 9 h, usual gap up to 4 h` when they apply) and
`Telegram send errors: last Sep 30 11:58` or `none in the log`. "Last update seen" is the tick
that saw the counter grow, not the update's own time. Failed sends are the engine's own
`Failed to send Telegram message` ERROR lines in `logs/errors.log` (the same tail the Gemini
line reads, in its own worker); three in an hour are an event. Successful sends are not logged
at that level: the last successful send the plugin can vouch for is its own confirmed edit
(`Confirmed` in the details). Without the counters (an adapter of another shape, or the cron
path, which holds no adapter) Telegram keeps the verdict of the gateway state, the ❔ line names
`Telegram traffic`, and the details say `Telegram traffic: …` with the reason. `python -m telegram_dashboard --demo 23` shows the form.

**Four private attributes, read as a capability, not a promise; one public property.** The send
gate, `send_path_degraded`, is the adapter's public API since 0.21.1 and is read as such. The
four polling attributes are not part of the engine's public contract: like `_edit_text` for the
HTML form, a future adapter that renames or retypes them degrades the line to no data with the
reason and never raises inside the adapter (`tests/test_invariants.py` pins that nothing is
called on it). The plugin's catalog entry discloses the property and the four attributes by
name, and a public
snapshot of the polling state on the adapter is the upstream change that would turn the
capability into a contract.

### The rules line: does the agent see its context files

An `AGENTS.md` the agent never sees looks exactly like one it follows: nothing fails, the
answers are just worse. Hermes looks for its project context files (`.hermes.md` or `HERMES.md`
up to the git root, the `AGENTS.override.md` / `AGENTS.md` / `agents.md` chain from the git root
down, `CLAUDE.md` or `claude.md`, `.cursorrules` and `.cursor/rules/*.mdc`) from the agent's
working directory, and for a gateway with `terminal.cwd: .` (or unset) and the local backend
that directory is the service user's home,
not the service's `WorkingDirectory` (`hermes gateway install` sets that to `HERMES_HOME`). A
file put next to the service, or next to `SOUL.md`, is silently never loaded. The dashboard
judges it per platform of the gateway, from the system prompt the engine saved for that
platform's latest session, and comes up to the screen only when an agent does not see its rules,
as one event per platform under "Needs attention" that turns the status to a warning (the
healthy screen gets no line for them; there is no separate rules line beside the event).
Of the five events the screen shows, one place is kept for the rules when other sources fill
them: those sources have lines of their own, the rules do not:

```
- Telegram: rules not loaded        no file in the prompt, while one is there for it
- Telegram: rules outdated          the prompt holds another text than the file: /new
- Telegram: rules truncated         over context_file_max_chars, the middle cut out
- Telegram: rules blocked           the engine's injection scan refused the file
```

The details always carry one line per platform:
`✓ Rules Telegram: AGENTS.md · 11,162 chars · session Oct 7 14:32` when the agent sees them,
otherwise the files and what to do (`AGENTS.md only in the gateway's directory, the agent works
in another`, `only in HERMES_HOME`, `changed …, after session …: /new`, `in the agent's
directory, not in the prompt of session …`, `not UTF-8, the engine skips it in silence`, `in
the prompt, no such file now`, `differ from the prompt of session …`), and also `no context
file where the agent works` (never an alarm), `context files off for it in the engine's
config`, `the agent's directory not known here` (a sandbox prompt with no host directory to
compare), or the reason of a no data (the ❔ line names that source on the screen).
A `/new` there is for the chat of the platform it names, sent by hand: in the HTML message the
file names and `/new` are code, which Telegram turns into neither a link (`AGENTS.md` ends in a
top-level domain) nor a command a tap would send to the dashboard's own chat.

- **how it judges**: the files are found on disk by the engine's own rules, never by headings
  read out of the prompt: in the agent's directory, the nearest `.hermes.md` or `HERMES.md`
  there or in a parent up to the git root, else the `AGENTS.md` chain from the git root down
  (the first of `AGENTS.override.md`, `AGENTS.md`, `agents.md` in each directory), else
  `CLAUDE.md` (or `claude.md`), else `.cursorrules` and `.cursor/rules/*.mdc`; the first kind
  found wins, and a link is followed as the engine follows it. They are rendered the way
  the engine renders them and compared with the prompt right after its `# Project Context`
  header: the same text is loaded, the engine's own cut of that text (its marker with this
  text's length, the head and the tail it kept) is truncated, the scan's notice under the
  file's heading is blocked, anything else outdated. The agent's directory is the one the
  prompt's runtime block names, read back the way the engine reads it to check a stored prompt
- **a snapshot, not the files this minute**: the engine builds the prompt on a session's first
  turn and keeps it until a compression, a model switch or `/new`. When the prompt holds the
  current text, its end is proven by the engine's next part right after it (the skills index,
  the workspace snapshot, the memory block, the timestamp line or the runtime block): then the
  file's time does not matter, a `touch` or a rebuild after an edit reads loaded. Without that
  proof a file changed after the session started is outdated until `/new`: a saved prompt cannot
  otherwise say where an older, longer text ended, so a rule removed from the end must not read
  as loaded. The change time is the newest of the file's and its link's modification times; a
  time ahead of the clock is no time. One case slips through: a copy that keeps an old time
  (`rsync -a`, `cp -p`), made after the session started, that removed a rule from the very end
  of the file, with no known engine part after the text in the prompt
- **where else rules may wait**: only `.hermes.md`, `HERMES.md` and the AGENTS names
  (`AGENTS.override.md`, `AGENTS.md`, `agents.md`) count in the gateway process's working
  directory and in `HERMES_HOME`, where the agent does not look; a
  `CLAUDE.md` or `.cursorrules` there is taken to be another tool's (an `AGENTS.md` kept there
  for another tool raises the alarm too; turn the line off for such a gateway). A file of
  whitespace is no file, as for the engine; a file that is not UTF-8 (a Windows editor's ANSI or
  "Unicode") the engine skips in silence, and the line says so. The Hermes source tree is never
  taken for a place of rules (its contributor `AGENTS.md` is not loaded for a messaging platform
  on purpose)
- **which platforms**: the entries of `gateway_state.json` that the running gateway wrote
  (their writer stamps are the record's own pid and start, the way the engine's status tells
  live from preserved), so a platform switched off long ago is not judged forever; another
  profile's entries belong to its own `state.db`; Telegram right after a start and when there is
  no record. A platform
  whose `gateway.platforms.<name>.skip_context_files` is set is off, not an alarm
- **a sandbox backend** (docker and the like) names no host directory in the prompt; discovery
  then ran in the engine's `TERMINAL_CWD` when that exists on the host, else in the process's
  own directory, and so does the verdict; with neither (the cron tick without `gateway_dir`)
  rules in the prompt are `unknown`, never an alarm
- **what is read**: `HERMES_HOME/state.db`, opened `mode=ro` with `PRAGMA query_only` (the same
  read-only connection as the cron history), two short statements per platform: the newest
  session row with a saved prompt (its start and the prompt's hash, by the source index), then
  that one prompt. No other session's prompt is read, and the connection is closed before any
  file is, so a database in rollback-journal mode holds its writer back for milliseconds. In
  the prompt: the header, the block right after it and the runtime block's working directory.
  On disk: the context files by the names the engine loads, in the directory the prompt names
  (else the engine's `TERMINAL_CWD` or the gateway's working directory, either of which may be
  outside `HERMES_HOME`), and the `.hermes.md`, `HERMES.md` and AGENTS names also in the
  gateway's working directory and `HERMES_HOME`: their text (up to 4 MB, to compare) and change
  time; a leading BOM is read the way the running engine reads it (0.21.6 drops it before
  `strip`, earlier versions after it; told by the default `encoding` in the signature of the
  engine's private `agent.prompt_builder._read_text_with_timeout`, looked up in the module the
  gateway already imported and never called, both ways tried where no engine is loaded). In
  `config.yaml`: only
  `gateway.platforms.<name>.skip_context_files`, through the engine's own YAML parser
  (`hermes_yaml` from 0.21.6, else PyYAML); a file that is there and cannot be read makes a
  missing block `unknown`, never an alarm. In the gateway process: the engine's `TERMINAL_CWD`.
  All in a worker under the tick's deadline
- **what is never shown or logged**: the prompt and the files' text, session ids, paths. The
  screen carries file names as the engine labels them, lengths, times and verdicts; nothing of the line
  is kept in the plugin's record; a failure is the exception's class only
  (`tests/test_context_files.py` plants a phrase in every file and prompt and counts it in the
  screen, the snapshot and the log: zero)
- **what is never read**: messages, any other column of `sessions`, other sessions' prompts,
  any other key of `config.yaml`, `.env`, `auth.json`. Nothing is written anywhere
- **only the latest session per platform**: after an edit and `/new` in one chat the line is
  calm while another chat of the same platform still runs on the old text
- **off**: `context_files: false` hides the line and drops it from the coverage; the details
  say `Rules: off in the dashboard settings`, so off by choice never looks like a line that
  vanished

`tests/test_context_files_engine.py` builds the context and runtime blocks with the installed
engine's own functions and reads them back (0.21.3, 0.21.5 and 0.21.6: every kind of file, a
BOM-only file and a BOM before blank lines, a chain in
a repository, a chain with a cut file, a chain cut twice, a cut cursor bundle, an override file,
a directory reached through a link); it skips where Hermes is not importable.

### The memory section: does the agent keep what it writes

Hermes keeps a profile's built-in memory in two notebooks, `MEMORY.md` (the agent's notes) and
`USER.md` (what it knows of the user), each held to a character limit (2,200 and 1,375 by
default). With `memory.write_approval` on (`skills.write_approval` for skills), a write from the
gateway, a cron job or the background review waits as a file in `pending/` until someone sends
`/memory approve`, and nothing tells anyone; a background review's replace or remove waits there
even with the gate off. A queue can stand for weeks while the agent believes it remembered. Like
the rules, the memory has no line of its own on the screen, and a place of its own among the five
events; it becomes an event when it fails:

- `Memory: 58 writes stuck 24d` (`Skills: …` likewise): the oldest waiting write is older than
  3 days. Approval and rejection both delete a write's file, so a write that old has had neither,
  whatever landed beside it
- `Memory: USER.md over limit`: more text than the limit, the engine's own warning on load (every
  add refused until the agent frees room); `not UTF-8`: a file the engine cannot decode (it loads
  none of it and refuses every write); `bad limit`: a limit no add can pass (zero, below zero, or
  not a number). A notebook near its limit is no event: the engine runs notebooks close to their
  cap, and a refused add asks the model to consolidate in the same turn
- `Memory provider: unavailable`: the engine said in this gateway process that the configured
  provider reports unavailable (0.21.6 says so once per process, so it stands until the next
  start); `Memory provider: 3 errors`: the engine's warnings about the provider in the errors log
  over the last day

The numbers are in the details, always:

```
> Memory: MEMORY.md 1,934/2,200 (87%) · USER.md 1,301/1,375 (94%, one entry 71%)
> Memory queue: 58 waiting since Sep 2 · last write Aug 30 16:05 · approval on
> Waiting adds: USER.md 6,410 chars, 74 free: they do not fit
> Skills queue: 12 waiting since Sep 10 · approval on
> Memory provider honcho: no errors logged in 24 h, last Sep 24 10:12
```

- **counted the engine's way**: the entries split on the line holding `§`, stripped, empty and
  repeated ones dropped, joined with the delimiter; the engine's own `MemoryStore` gives the same
  count (`tests/test_memory.py`, on 0.21.3 and 0.21.6). The share of one entry shows when it
  holds half the limit or more
- **the last write** is the notebooks' modification time: the engine keeps no journal of applied
  writes, so a hand edit moves it too; for skills, the curator's ledger. A waiting write's time
  is its file's, which nothing rewrites
- **the waiting adds** are measured, not replayed: the characters each add would write against
  the room left, at most (an add the engine finds already there writes nothing); a replace or a
  remove is not counted, nor an add to a notebook the engine has switched off
- **a provider** leaves no status, only warnings in `logs/errors.log`; no warning is not proof it
  works (0.21.3 logs a provider it cannot find at debug level only)
- **what is read**: `config.yaml` (`memory.*` limits, switches, the gate and the provider's name,
  `skills.write_approval`) through the engine's own YAML parser, each value as the engine takes it
  (`write_mode` of old configs is not read at run time, so neither here; the managed overlay and
  `${VAR}` references are not applied), unread = limits and gate unknown, never guessed; the two
  notebooks, `memories/MEMORY.md` and `memories/USER.md` (up to 4 MB each), decoded as strictly as
  the engine decodes them; the names and times of every waiting file, `pending/memory/*.json` and
  `pending/skills/*.json`, and of the first 2,000 memory ones (up to 256 KB each) the action, the
  target and the length of each add; the time of `skills/.curator_ledger.jsonl`; the last 256 KB
  of `logs/errors.log` for the provider's warnings, their time and whether one says unavailable.
  All under `HERMES_HOME`, in a worker under the tick's deadline. Nothing is written anywhere
  (`tests/test_invariants.py`, 13)
- **what is never shown or logged**: the text of a notebook, an entry, a waiting write or its
  summary, a log line, a path. `tests/test_memory.py` plants a phrase in every one of them and
  counts it in the screen, the HTML, the snapshot and the log: zero
- **one profile**: the gateway's own `HERMES_HOME`; an empty home (no config, notebook or queue)
  is `unsupported`
- **off**: `memory: false` hides the section and drops it from the coverage; the details say
  `Memory: off in the dashboard settings`

### The backup line

`✓ Backup` sits in the ✓ row while the last run is good and younger than 26 h, and the details
carry the backup in every state: a screen silent about the norm makes silence indistinguishable
from confirmation. The details give the absolute time, the age and the integrity verdict
(`✓ Backup Sep 24 05:31 · 6 h ago · integrity ok`). The source is a
JSON status a backup job writes on every run (`ok`, `phase`, `reason`, `integrity`,
`finished_epoch` or `finished_at`); the dashboard reads that file only and never opens the
database or the copy. A failed run is loud: its line goes under "Needs attention"
(`Backup ⚠️ failed 6 h ago`, the `<phase>: <reason>` in the details) with an event; a status
older than 26 h likewise (`Backup ✓ 30 h ago ⚠️ older than 26 h`) with an event, and the
❔ line says `backup (stale)`; a missing or
unreadable file is `backup (unavailable)` on the ❔ line, the reason in the details, and an
event; no `backup_status` configured is `backup (not on this installation)` there. The status shape is
the one our own timer writes (`state_db_publish.py` in the operator repository); any writer that
produces the same keys works.

### The Hermes version line

`🤖 Hermes 0.21.3 → 0.21.5` sits after the limits: the version the running gateway serves, then
the release upstream (`NousResearch/hermes-agent`) marks Latest. The same release reads
`🤖 Hermes 0.21.5 ✓`; no answer from upstream reads `🤖 Hermes 0.21.3 · no data`, with the reason
in the details. The details carry both release dates, how many releases lie between them and
when upstream was last checked (`Hermes 0.21.3 of Sep 14, latest 0.21.5 of Sep 24`,
`2 releases behind · checked Sep 25 16:40`). For a day after Latest came out the line carries
`🆕` and the release's age (`🤖 Hermes 0.21.5 → 0.21.6 🆕 3h`), and the details the time it came
out (`New release: Hermes 0.21.6 out Oct 8 11:51`). The mark goes with Latest: a line too narrow
for Latest beside a long version of ours leaves both to the details, so the mark never dates ours.

The line informs, nothing more. Updating Hermes is a process, not a restart: the line carries no
threshold, no button, no command and no advice to update, and the new-release mark says only
that a release is new. Update by your own process.

- **The version** is the one of the code the gateway imported at start-up, looked up in
  `sys.modules`: a capability, not a version gate. Up to 0.21.5 that is the constant
  `hermes_cli.__version__`. From 0.21.6 there is none: the module reads its install stamp from
  disk on every access, so after an update on disk it names the new code, not the running one,
  and without a stamp it says `0.0.0`. There the identity the gateway resolved at start-up comes
  first (the private `hermes_cli.version_info._cached_version_info`, read as process state from
  the module the gateway already imported; nothing on it is called or changed), unless git gave
  it for another tree (no stamp and no `.git` of its own: 0.21.6 then asks the process home's
  `hermes-agent` clone, whatever that holds); the stamp last; `0.0.0` reads
  `no version stamp on this installation`, never a version, and an error of the engine's lookup
  `version lookup failed`. Not `importlib.metadata` (an editable install keeps the dist-info of
  install time), not the engine's `build_info.get_code_identity(refresh=True)` (inside the
  gateway it would restamp the gateway's own `code_sha`)
- **The latest release** comes from unauthenticated GETs to `api.github.com`: `releases/latest`
  with the last answer's ETag (a stored one a header cannot carry is dropped), and `releases?per_page=100` (about 1 MB, the list carries every
  release's notes) only when Latest names another version than the last answer. A new ETag alone
  is no new release: upstream edits a release's notes after publication. The version is the one
  in the release name (`Hermes Agent v0.21.6`, `Hermes Agent v0.21.5 (v2026.9.24)`), else a tag in
  the release scheme (`v0.21.6`, used since 0.21.6), never a date tag (`v2026.9.24`, the scheme
  before it); releases behind are positions on upstream's list, never arithmetic on version
  numbers; drafts and pre-releases are not counted. Of each release only its version and
  publication time are kept, and the check downloads, installs or replaces nothing. The
  engine's own update check (`check_for_updates` in `hermes_cli/banner.py`) counts commits
  behind `main` and is not used
- **No LLM, no agent.** The request is plain stdlib `urllib`, made by the plugin itself in its
  own worker thread, under Grok's single-flight deadline (`Flights`, 25 s; Kimi's is 15 s, for
  one request) and the cache policy Grok and Kimi share. The agent, its sessions and its tools
  take no part. A GitHub that hangs costs this line its answer for the tick
  (`no answer within 25 s`) and nothing else: the check starts with the tick and runs beside
  every other source, the gateway's event loop keeps running, the tick waits for it no longer
  than that deadline, and a hung request is not started a second time.
  `tests/test_hermes_version.py` pins each of these; a crash of the check waits for the next
  interval like a failed answer, so a bug cannot ask GitHub on every tick
- **At most every 15 minutes; a failure keeps the last answer.** The last answer and attempt
  live in the record under `release_cache` and survive a restart. A check goes out at most once
  per 15 minutes, whatever the tick's period: every tick at a period of 15 minutes or more, so a
  new release is on the screen within one tick of a 30-minute gateway, and within 15 minutes at
  the 60-second default. Without a token GitHub allows an address 60 requests an hour, and a
  304 counts too (measured): 4 an hour plus one list per new release leave the rest to whatever
  else asks from the address, the engine included; fewer than 10 left in GitHub's window, or
  none, waits for its reset (`Retry-After` first, an hour at most). A failed check
  (`GitHub rate limit`, `HTTP 503`, `request failed: URLError`, `answer shape: …`) keeps the last
  answer on the line, and the details say since when it stands
  (`Hermes latest as of Oct 8 12:10: GitHub rate limit · checked Oct 8 12:40`); with no answer
  yet the line is `no data`. A reason never quotes the answer: GitHub's rate-limit message
  carries the caller's IP
- **Not a source.** The line moves neither the status nor the coverage, so a GitHub outage does
  not turn the screen ⚪. A crash of the collector itself is still an event, like any
  collector's
- The cron fallback tick (`python -m telegram_dashboard`) has no version line: it runs outside
  the gateway, where there is no gateway version to read, and keeps no durable cache

### Limits: three kinds that never mix

Every limit line is one of three things, and the caption says which: an **official quota**
(a number the provider itself reports, with its own reset date), **local accounting** (tokens
this installation counted; it never knows the remaining quota, nor spend outside the agent), or
**"no data" with a reason**. "Official" means *the number came from the provider*, not
*the surface is in the provider's docs*: the engine's own Anthropic and Codex fetchers read
undocumented surfaces (`api/oauth/usage`, the ChatGPT backend), and so does the Grok reader.
A documented surface would be preferable; an undocumented provider number is still the
provider's number, and a local count is not.

**A refusal is an event, not a number.** Google gives a free-tier Gemini key no quota figure on
any surface, so the Gemini line has no number and never a zero. What the installation does have
is the HTTP 429 the engine logs when Google turns a call down, and it is said as what it is, in
plain words: a per-minute refusal is `⚠️ Gemini hit limit 5 min ago` on the line, which moves
no status, like the mark from 90%; a daily one is `⚠️ Gemini paused till 23:00` and an event
until the reset; the last one is always in the details (see "Gemini refusals from the engine
log").

An official line is one line: `⚠️ Codex 98% (1h21m)`. Every window the provider reports is on
it, in the provider's own order, as `N% (time to reset)`: the spent share (every percent on the
screen is spent, never remaining, from every source; the heading says `🧠 Limits used`) and the
time to that window's reset, never earlier than the real one: the time is when the quota is
back. Under a day it goes to the minute, rounded up (`45m`, `1h21m`, `23h59m`); from a day on to
the hour, the hour rounded up (`1d`, `1d5h`, `4d3h` for four days, two hours and 37 minutes); a
zero second unit is not written (`3h`, `4d`); `(?)` when the reset date cannot be read. The time
of a limit is never rounded for the width: a line with two windows and the warning mark can pass
the phone line and wraps on a narrow phone (`⚠️ Claude 29% (3h34m) · 87% (4d)`), a number and a
time stay whole. The mark comes from 90% spent in any window, or from a window the provider
itself calls `warning` or `critical`. A window carries no length label: the share and its reset
are what the reader acts on, the plan
(where the provider names it) is in the details, and a length the source does not state would
be a guess (the engine names Codex's windows `Session` and `Weekly` by position, and on some
plans the first one is the week). A limit for one model keeps its scope
(`Claude 37% (3h) · 12% (4d) · Opus 5% (4d)`). A model's own limit from an external source gets
a line of its own under the account's line, `Claude Fable 9%`, without the time when it resets
together with the account's window of the same label (see "External limit sources"). A state the provider reports in words is never
turned into a number or a zero (`Grok · usage not started`). The plans the providers name are
one details line, `Plans: Claude Max 5x · Codex Prolite · Grok SuperGrok`; a plan nobody
reports is not guessed. The rich view writes the same lines as a table (`Provider`,
`Used · resets in`): a row per line, a cell per window (`⚠️ 93% · 1h20m`), a state in words
across the row (`no data`), and a model's own limit in a row of its own with its reset spelled.
There is no bar: Telegram draws the block glyphs from a fallback font, and a bar by fifths says
less than the number after it; Telegram has no text colour either, so the mark is the only
emphasis. The countdown counts from the data time on the first line: a message that stopped
updating is the freshness banner's business, not the countdown's. A number read on its own
cadence must not borrow the screen's stamp: the details line `Data 12:21 · Kimi 12:16` names
every number read at another minute than the screen, and a cached number is never shown older
than its refresh interval (`quota_cache.py`), after two intervals the ❔ line says so
(`Kimi quota (stale)`). A stale number under a fresh stamp looks like knowledge, which is the one thing a limits
block must never do.

**Grok** (`telegram_dashboard/grok.py`): the weekly pool of the SuperGrok subscription, read
from `https://cli-chat-proxy.grok.com/v1/billing?format=credits` with the token the engine
already holds for `xai-oauth`, obtained through the engine's own resolver
(`hermes_cli.auth_xai.resolve_xai_oauth_runtime_credentials`, so the screen shows the quota
of exactly the grant inference uses) and the client header the Grok CLI sends. The plan name
comes from `…/v1/settings` (`subscription_tier_display`), optional, and shows in the details
(`Plans: … Grok SuperGrok`). The resolver is Hermes' own auth code and acts as it does for any
Hermes request to xAI: a token close to expiry is refreshed at xAI's token endpoint and saved to
`auth.json`, and a refresh xAI refuses for good clears the dead token there. The plugin's own
code writes neither. Neither request follows a redirect: urllib would carry the token on to
whatever host the redirect names, so a redirect is `no data` with its status (`HTTP 302`).
Probed on 2026-09-12: the inference host `api.x.ai` answers 404 for this path, so the proxy host
is required. Policy: one attempt per `limits_refresh_seconds`, success or failure; a failed attempt is "no data"
with its reason until the next interval; a cached number older than the interval is not shown.
The shape is checked strictly (`currentPeriod.type == USAGE_PERIOD_TYPE_WEEKLY`, a finite
percent in 0..100, a readable end date); anything else is named, not guessed. One answer is a
state rather than a changed shape: right after the weekly reset the proxy leaves
`creditUsagePercent` out altogether until the first request of the new period (probed
2026-09-25). The line then reads `Grok · usage not started`, never a zero, and only while the
period is the current one and on-demand spend is an explicit zero; a percent missing under any
other conditions is named (`no creditUsagePercent`). Grok is its own source of the coverage
(`Grok quota` on the ❔ line).

**Kimi** (`telegram_dashboard/kimi.py`): the windows of the Kimi Code subscription, read from
`<base URL>/v1/usages` on the very host the engine uses for Kimi inference. The key and the base
URL come from the engine's own resolver
(`hermes_cli.auth.resolve_api_key_provider_credentials("kimi-coding")`: `.env` first, then the
credential pool, then the key-prefix redirect), so the screen shows the quota of exactly the
credential inference uses, and the request carries the client header the engine sends to that
host. Without a usable key in `.env` or the gateway's environment that resolver loads Hermes'
credential pool, and loading saves the pool back to `auth.json` when it changes something, for
example: a placeholder key in `.env` (`changeme`, `***`) seeded into the pool, a row an older
Hermes wrote with the secret in it, an emptied `auth_type`. Hermes makes the same save itself the
first time it loads that pool (`hermes auth list`, an agent run on Kimi); a settled pool is not
written, and the plugin's own code writes nothing there. The request follows no redirect, like
Grok's. The shape is the one the official client parses (`@moonshot-ai/kimi-code-oauth`,
`managed-usage.ts`): `usages.limit_7d` (legacy plans) and `usages.limit_month_total` (new
plans), each with `used_ratio` in 0..1 and a `reset_time`, every window on the line with its own
reset (`Kimi 3% (29d)`). `usages.limit_5h` is not shown for now: on the pilot account it has read
0 at every reading while `limits[]` beside it reported 55 of 100 for a 300-minute window with the
same reset, and the plan shows no five-hour limit; a probe decides what either field is. Same
policy and cache as Grok; the request never goes through the credential pool's rotation, so a
failed request cannot mark the pool exhausted. Not in Kimi's docs; a changed shape is named,
not guessed. Kimi is its own source of the coverage (`Kimi quota` on the ❔ line).

**Codex** comes from the facade like Claude, and so does its plan (`plan_type`, title-cased:
`Prolite`), in the details beside the others. The engine names Codex's first window `Session`
whatever its length, so the line shows the share and the reset only.

**Claude on an installation without an Anthropic credential**: the line says `no data` and the
details say `no account token`, a reason, never a zero. A local source that answers for Claude
takes the line's place (see "External limit sources").

The check for the plugin path is the same `--check`, pointed at that file:

```json
{ "plugin_state_path": "~/.hermes/plugin-data/<namespace>/state.json", "period_seconds": 300 }
```

`period_seconds` here is the tolerance the check grants, not the plugin's period: with the
plugin editing every 60 s, 300 means five missed ticks before the first alert (`confirmed` up to
one period plus slack, `lagging` up to two, `stale` past that). The record also carries
`tick_failed` when a tick raised, so a loop that dies every tick does not keep an old `edited`
on disk.

### External limit sources

A provider the plugin has no reader for, or one this installation reads some other way, can be
answered by a local process of your own. `limits_sources` lists up to 4 sources; each is a URL on
loopback that answers one GET with a small JSON document, contract 1 below. The plugin knows
nothing about who answers.

```yaml
limits_sources:                   # in plugins.entries.telegram_dashboard_probe.settings
  - url: http://127.0.0.1:8080/v1/usage
    key_env: USAGE_SOURCE_KEY     # optional: sent as "Authorization: Bearer <its value>"
    timeout_seconds: 90           # optional: default 20, from 1 to 120
```

The answer is `200` with a JSON object of at most 64 KB:

```json
{
  "contract": 1,
  "provider": "Claude",
  "state": "ok",
  "reason": null,
  "plan": "Max 5x",
  "login_expires_at": "2026-10-27T21:00:00Z",
  "fetched_at": "2026-09-28T18:16:00Z",
  "windows": [
    {"label": "session", "used_percent": 42, "resets_at": "2026-09-28T21:00:00Z", "scope": null, "severity": "normal"},
    {"label": "week", "used_percent": 86, "resets_at": "2026-10-01T23:00:00Z", "scope": null, "severity": "warning"},
    {"label": "week", "used_percent": 100, "resets_at": "2026-10-01T23:00:00Z", "scope": "Fable", "severity": "critical"}
  ]
}
```

- `contract` is `1`; any other version is `no data` with `contract N not supported`
- `provider`, up to 24 characters, names the line. A name the screen already has (Claude, Codex,
  Grok, Kimi) takes that line's place; any other gets a line of its own before Gemini
- `state`: `ok` puts the windows on the screen; `login_expired` reads `Claude · login expired`
  and raises the event `Claude login expired`; `unavailable` is `no data` with `reason` (up to 60
  characters) in the details
- `windows`, up to 6, in the order the screen shows them: `used_percent` from 0 to 100 or `null`;
  `resets_at` or `null`; `scope`, the model a limit applies to, `null` for the account's own
  limit; `severity`, one of `normal`, `warning`, `critical`, or `null`; `label` is optional and
  not shown (windows carry no length label). The account's windows share one line. Every
  model's limit gets a line of its own after it (`⚠️ Claude Fable 100%`); the time to its reset is
  left out when the account's window with the same `label` resets within the same minute, and
  said otherwise (`Claude Fable 9% (2h)`). `warning` and `critical` put the mark on the line as
  90% does
- `plan`, optional, up to 24 characters: the details line `Plans: …`
- `login_expires_at`, optional: when the login behind the numbers ends. The details say
  `Claude login until Oct 27`; from three days before it the event
  `Claude login expires in 2 days`, counted from the last date the source gave, so a source that
  fails in those days still warns
- `fetched_at`, optional: when the source read its numbers; the details name it when it differs
  from the screen's minute (`Data 07:21 · Claude 06:51`). A stamp ahead of the clock is replaced
  by the time of reading
- unknown keys are ignored, and every string is cleaned like any other text on the screen

The guards, and why:

- the URL is `http` on `127.0.0.1`, `localhost` or `[::1]`, without credentials in it. Any other
  entry is refused when the settings are read: the plugin's log names it at start, its line says
  `no data` with the reason, and nothing is requested. The request ignores proxies from the
  environment and follows no redirect: a redirect would carry the key to wherever it points. An
  answer other than 200, over 64 KB or not JSON is `no data` with the reason; the body of an
  error answer is not read
- `key_env` names a variable in the gateway's environment, never a bot token.
  `TELEGRAM_BOT_TOKEN` (the engine's Telegram adapter reads it), `HERMES_DASHBOARD_BOT_TOKEN` (the
  fallback tick's token) and any name ending in `_BOT_TOKEN`, in any case, are refused and never
  read: the plugin does not read the bot token itself, and a setting must not become a way to
  send it to a local port. The variable is read in `telegram_dashboard/external.py` and nowhere
  else
- the tick waits for a source no longer than 25 s, Grok's deadline (Kimi's is 15 s, for one
  request). The request itself runs on in its worker with the source's own `timeout_seconds`,
  never two at once for one source. On a miss the screen keeps the source's last line with that
  line's own stamp in the details while it is younger than two ticks or two
  `limits_refresh_seconds`, whichever is longer; the late answer is the next tick's line. A slow
  source never holds the tick back
- a source is asked once per `limits_refresh_seconds`, like Grok and Kimi, and is a source of
  its own in the coverage (`Claude limits` on the ❔ line): `ok` and `login_expired` are answers;
  `unavailable`, silence and a shape the contract does not allow are `unavailable`
- `limits_enabled` off turns the sources off too. The fallback tick reads the same list from its
  JSON config and asks each source on every run, one after another, within its own timeout; it
  needs no engine for them

`python -m telegram_dashboard --demo 17` shows a source with a model's limit, the plans and a
login that ends in two days; `--demo 18` the same source after the login expired.

### Gemini refusals from the engine log

Google reports Gemini quota only to a project with billing enabled (Cloud Monitoring answers 403
without it), the Rate Limit page of AI Studio needs a browser login, and the engine reads no
limit by key. The one trace a free-tier key leaves is the HTTP 429 the engine writes to its own
log when Google refuses a call, and that refusal is what the Gemini line shows, in plain words
(`telegram_dashboard/gemini_log.py`).

- **what is read**: the last 256 KB of `$HERMES_HOME/logs/errors.log` (WARNING and above,
  rotated by the engine at 2 MB), from the first whole line to the last. Until the first
  successful read (the record then holds `last_429`, null when nothing was seen) the whole file
  is read, so a 429 from before the plugin arrived is the last one at once; a file over 4 MB is
  not the engine's rotation and is read by its tail even then, as is a first read that cannot be
  decoded. An entry counts when its first line holds `Gemini` and `HTTP 429`: the text-to-speech
  tool's error, the native Gemini adapter's summary and a failed streamed call all do, a 400 or a
  403 does not. From it the plugin keeps four things: the time, and the `limit`, the `model` and
  the seconds to retry when Google's message names them. The screen shows the time, the model
  and the kind of limit the retry tells, never the limit's number or the retry itself.
  The engine logs one refusal up to three times within a second, the later copies cut short;
  entries within ten seconds of the newest are one refusal and lend each other the fields
- **what is never read**: the message's text beyond those fields (it never reaches the screen),
  tracebacks, session ids, `agent.log`, `gateway.log`, `auth.json`, `state.db` and the rotated
  `errors.log.1` and `.2`. No network
- **seen by Hermes only**: a script that calls Gemini on its own, such as a skill's, is not in
  the engine's log. The details say so on every screen: `Gemini refusals seen by Hermes: …`
- **per minute or per day**: Gemini's free quotas are per minute and per day, and a per-minute
  window ends within a minute, so a retry longer than two minutes is the daily quota. A daily
  refusal is the event `Gemini daily limit used up` and `⚠️ Gemini paused till 23:00` on the
  line until the reset Google named, in the screen's zone; any other refusal, one without a
  retry among them, is the mark only, for an hour: `⚠️ Gemini hit limit 5 min ago`. After that
  the line says `Gemini · no data` again, and the details keep `last Sep 29 14:03, per-minute
  limit, model gemini-2.5-flash-preview-tts` (a daily one says `daily limit till …`, one
  without a retry names no kind). A daily quota spent just before Google's reset asks for a
  short retry and looks per-minute
- **memory**: the record keeps the last 429 (`gemini_log_cache`), so one that rotated out of the
  tail is still the last one. The fallback cron tick keeps no record, so each of its runs reads
  the whole file, up to 4 MB
- **time**: the engine stamps its log in the host's local time without an offset; the plugin
  runs in the same process on the same host and converts with the process's zone. The hour
  repeated when summer time ends adds an hour to the age of a 429 from that hour, once a year
- **source**: `Gemini log` on the ❔ line when it does not answer. No `logs/` directory is `not on this
  installation`; the directory without `errors.log` is a log with no 429 (the engine on Windows
  creates the file at the first warning); a file that cannot be read is `unavailable`, and the
  line keeps what the record remembers. The read runs in a worker under the tick's 25 s deadline,
  like an external source. `limits_enabled` off leaves the log unread
- an external source that answers for Gemini (contract 1) takes the line's place with its
  numbers; the 429 the log saw stays in the details and in the event

`python -m telegram_dashboard --demo 19` shows a per-minute 429, `--demo 20` a daily one.

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

The package runs from the plugin folder; point Python at it (or at a deployed copy):

```bash
export PYTHONPATH=plugin/telegram_dashboard_probe
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
  "context_files": true,
  "memory": true,
  "gateway_dir": "/path/the/gateway/runs/in",
  "recreate_on_loss": true
}
```

Limits are only available when the tick runs in an interpreter that can import the engine (the
gateway's own); elsewhere they degrade to `unsupported`. The cron tick runs in a directory of its
own, so the rules line looks for rules left beside the gateway only in the `gateway_dir` it is
given (the service's `WorkingDirectory`); `context_files: false` (or `"off"`) turns the line off,
`memory: false` the memory section.

## Tests

```bash
python -m pytest tests             # plus `pip install tzdata` on Windows
ruff check . && ruff format --check . && mypy --strict -p telegram_dashboard
```

`pyproject.toml` points pytest and mypy at the package inside the plugin folder. Every run prints
`telegram_dashboard.__file__` in the header: a green run that does not say which tree it tested
proves nothing. Run it without `-q`: pytest hides the header in quiet mode. No test reaches a provider: the Grok, Kimi and GitHub attempts are stubbed by an autouse
fixture in `tests/conftest.py` unless a test passes its own fake, and the running Hermes version
there is a fixed one; a plugin loaded by the test
helpers takes that same patched package, not a second copy of it under the plugin's name
(`tests/probe_fakes.py`, `load_plugin`). `tests/test_probe_plugin.py` skips unless the Hermes engine is
importable; to run it, use an interpreter with the engine and `python-telegram-bot` installed
(for example `uv sync --extra messaging` in an engine checkout with `UV_PROJECT_ENVIRONMENT`
pointing outside the checkout, then that venv's `python -m pytest tests/test_probe_plugin.py`).

`tests/test_invariants.py` reads the plugin's own source and pins thirteen properties a later change
cannot undo quietly: the bot token is never read, no port is listened on, the drift command
runs without a shell, state goes through the engine's plugin state only, the one task is
spawned through `ctx.spawn_task`, the run history and `state.db` are opened read-only and
query-only through one connection function (the only SQLite the plugin opens), nothing under
`HERMES_HOME/cron` is ever written and the cron and traffic modules import no network client,
the entry registers no hook, tool, middleware or command, the traffic probe reads the adapter's
counters without calling anything on it, and the rules line writes nothing, imports no network
client and logs no text, the memory section opens files to read bytes only, opens no database,
imports no network client and logs nothing, no file of the plugin folder carries an invisible character (the
catalog validator warns on one), and no word of any demo screen, sent as HTML or rich, becomes a
link, a command, a mention, a hashtag or a cashtag outside `<code>`: a word that looks like a file name
(`name.ext`, the extension starting with a letter) or a `/command` is code by its form, not by a
list. The plain fallback has no code: there Telegram may still link a file name and make `/new`
a command a tap would send to the dashboard's own chat.

## License

MIT.
