# hermes-agent-telegram-dashboard

**English** · [简体中文](README.zh-Hans.md)

[![CI](https://github.com/bablobanov/hermes-agent-telegram-dashboard/actions/workflows/ci.yml/badge.svg)](https://github.com/bablobanov/hermes-agent-telegram-dashboard/actions/workflows/ci.yml)

![Two screens of the dashboard 1.1.1: a healthy installation, and one whose disk runs low](docs/hero-1.1.1.jpg)

*1.1.1 on Telegram for Android, demo data. The time in the top line is Telegram's own, written in
the language of the client (see [Screenshots](#screenshots)).*

Read-only status dashboard for [Hermes Agent](https://github.com/NousResearch/hermes-agent) as one
pinned Telegram message, edited in place by a plugin inside the gateway. No Mini App, no LLM call,
nothing to press: the pinned bar shows the status line every time the chat is opened.

**Preview, 1.2.0-beta.1 (outside the catalog):** the same screen as one pinned Telegram HTML
widget in your private chat with the bot, with tabs for Status, Cron and Details, a Refresh button
and a theme button, beside the message in the topic. Off unless you turn it on; see "The widget
(preview, 1.2.0-beta.1)" in the plugin's README.

**The full documentation is the plugin's own README:
[`plugin/telegram_dashboard_probe/README.md`](plugin/telegram_dashboard_probe/README.md).** It says
how every line is read, what the plugin reads, writes and sends (its Disclosure section), the
settings, compatibility and the tests. The catalog page shows the same file.

## What the screen shows

- the status and when the data was read, in the top line the pinned bar carries
- one row of what works: the gateway, the Telegram channel (and whether it is deaf), the last
  backup, config drift, the cron ticker and its jobs
- up to five events that need attention, and what the screen could not check
- the account limits of each provider with the time to every reset (Claude and Codex through the
  Hermes usage facade, others from a local source of your own)
- the Hermes version the gateway runs against the latest upstream release
- folded, the details: whether the agent sees its rules, whether its memory keeps what it
  writes, the cron counts, the ticker and every failing job, the free space on the disk
  `HERMES_HOME` lies on (an event when it runs low), the reasons and the times

## Screenshots

Demo data, rendered by the plugin's own code at 1.1.1 and sent to a test topic; Telegram for Android,
the details open on Telegram Desktop. The time in the top line is a Telegram time entity: each client
writes it itself, in its own language (Russian here). 1.1.0 also drew the limits as a table and the
cron jobs in a section of their own (the rich view): it sent that through a private call of the
engine's Telegram adapter, which the catalog review asked to remove, and it comes back once Hermes
has a public way to send a pre-built rich message
([NousResearch/hermes-agent#151881](https://github.com/NousResearch/hermes-agent/issues/151881)).

| | | |
|---|---|---|
| <img src="docs/screenshots/111-01-healthy.jpg" width="260" alt="A healthy installation, the details folded"> | <img src="docs/screenshots/111-02-details-open.jpg" width="260" alt="The details open: rules, memory, Telegram channel, backup, drift, the free space of the disk, cron, version"> | <img src="docs/screenshots/111-03-cron-failed.jpg" width="260" alt="A cron run failed three times in a row"> |
| A healthy installation | The details, the disk among them | A cron run failed three times in a row |
| <img src="docs/screenshots/111-04-memory.jpg" width="260" alt="Memory and skills writes wait for approval"> | <img src="docs/screenshots/111-05-rules.jpg" width="260" alt="The agent's rules file changed after its session started"> | <img src="docs/screenshots/111-06-disk-low.jpg" width="260" alt="The disk HERMES_HOME lies on has 1.6 GB free of 80"> |
| Memory writes stuck waiting for approval | The rules changed after the session started | The disk under its floor |

## Install

Hermes 0.22.2 or newer. From the [Hermes Plugin Catalog](https://hermes-agent.nousresearch.com/docs/plugins/telegram_dashboard_probe),
which pins the reviewed commit:

```bash
hermes plugins install telegram_dashboard_probe
```

Or from a clone, one line puts the self-contained plugin folder in place:

```bash
git clone https://github.com/bablobanov/hermes-agent-telegram-dashboard.git && cd hermes-agent-telegram-dashboard && install -d "${HERMES_HOME:-$HOME/.hermes}/plugins" && cp -r plugin/telegram_dashboard_probe "${HERMES_HOME:-$HOME/.hermes}/plugins/"
```

Then enable it in `config.yaml` (`plugins.enabled` and the `chat_id` setting), restart the gateway
and pin the message it sends. Nothing is installed into the engine's environment; `SHA256SUMS`
lists every file of the plugin folder (`sha256sum -c SHA256SUMS` from `plugin/`). The settings
("Deploying on a gateway") and what to do after an update from 1.0.0 (restart the gateway once)
are in the plugin's README.

## In short, before you install

- reads Hermes' own files without changing them, the agent's context files wherever its
  working directory is, and the free space of the disk `HERMES_HOME` lies on; writes only its
  own record through the engine's plugin state
- sends and edits the one message through the engine's Telegram adapter; the plugin never reads
  the bot token
- asks the Hermes usage facade for Claude and Codex limits every 5 minutes by default; to answer,
  Hermes may read the Claude Code or Codex CLI login on the host and refresh and save its OAuth
  token
- asks GitHub for the latest Hermes release without a token at most once per 15 minutes
- touches some engine internals by name and keeps a registry of its own in `sys.modules`; the
  full list is the [Disclosure](plugin/telegram_dashboard_probe/README.md#disclosure-what-the-plugin-reads-writes-and-sends)
  section

## License

MIT.
