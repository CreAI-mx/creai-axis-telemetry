---
name: creai-usage
description: Opt in to (or out of) creai-axis usage telemetry, check its status, or backfill past usage. Invoke when the dev asks to "opt in to axis usage", "join the axis adoption dashboard", "stop sending usage", "what does the usage collector send", or pastes an ingest token. Never invoke on your own initiative — telemetry is strictly opt-in.
license: Apache-2.0
metadata:
  author: creai
  version: "0.1"
  model: sonnet
---

# creai-usage — opt-in usage telemetry for the creai-axis adoption dashboard

The `creai-telemetry` plugin (marketplace `creai-axis-telemetry`) ships a collector
(`hooks/usage-collector.py`) that stays **inert** until the dev opts in, even though the plugin is installed. Once opted in, at the end of every Claude Code session it sends one metadata-only event
per creai-axis skill used, and the team dashboard shows it.

The collector is `python3 "${CLAUDE_PLUGIN_ROOT}/hooks/usage-collector.py"`; call it `COLLECTOR` below.

## What is sent — read this to the dev before opting in

Per skill invocation: timestamp, session id, project name (the git repo's folder name, or the folder
Claude ran in; never the home folder or anything named like the OS user), git branch, Claude Code
version, plugin, plugin version, skill name, and whether the dev typed it (`slash`) or Claude invoked
it (`model`).
The dev's name and email come from their ingest token, server-side.

**Never sent:** prompts, skill arguments, code, file contents, tool output, or anything from
non-creai-axis plugins. Names are visible to everyone at creai who can open the dashboard.

## Opt in

1. Show the "What is sent" section above and ask for explicit consent. Stop if the answer is not a clear yes.
2. The dev needs an **ingest token** and the **endpoint URL** from the dashboard admin
   (the `creai-axis-telemetry` README, § Admin, names who that is). If they don't have one, tell them who to ask and stop.
3. Tell the dev to run this themselves with the `!` prefix so the token never enters the conversation:
   `! python3 "<COLLECTOR path, expanded>" optin --endpoint <URL>` — it prompts for the token
   without echoing it. **Never ask the dev to paste the token into the chat**, and if they do, tell
   them to ask the admin to revoke it and issue a new one.
4. Offer a one-time backfill of past sessions: `COLLECTOR backfill` (optionally `--since YYYY-MM-DD`).
   Run it only on a yes.
5. Run `COLLECTOR status` and report: opted in, pending events, last send result.

## Status

Run `COLLECTOR status` and summarize it in two lines. If `last_send.ok` is false with error `401`,
the token was revoked or mistyped: re-run opt-in with a new token.

## Opt out

Run `COLLECTOR optout`. It deletes the local config, queue and cursors, so nothing more is sent.
Tell the dev that events already sent stay on the server until the dashboard admin deletes them,
and that they can request that deletion.

## Never

- Never opt someone in without their explicit yes in this conversation.
- Never read, print or store the token yourself; it lives only in `~/.config/creai-axis/usage.json` (mode 600).
- Never edit the collector's queue or cursor files by hand; use `optout` to reset.
