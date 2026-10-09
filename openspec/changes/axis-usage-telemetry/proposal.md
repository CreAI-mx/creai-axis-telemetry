# Proposal: Opt-in usage telemetry and an adoption dashboard for creai-axis

**Change slug:** `axis-usage-telemetry`
**Jira:** [TR-356](https://creai.atlassian.net/browse/TR-356) (Technical Research, monthly topic, Demo Day 2026-10-26)
**Author:** Miguel Arévalo · 2026-10-07
**Repo:** [CreAI-mx/creai-axis-telemetry](https://github.com/CreAI-mx/creai-axis-telemetry), which holds the
collector plugin, the Supabase backend and the dashboard. `creai-axis` itself is unchanged (see design § Where things live).
**Status:** collector with tests, schema, ingest function and dashboard (demo data) are built; nothing is deployed yet.

## Problem

creai-axis is meant to be the standard way every creai developer works, but nobody can tell
whether it is used. Today there is no answer to:

1. **Who uses it?** How many devs ran any creai-axis skill this week, and who installed it but stopped.
2. **How far down the pipeline do tickets go?** Do branches that start with `/creai-design-feature`
   reach `/creai-create-pr` and `/creai-archive`, or does the pipeline get abandoned midway?
3. **Which skills earn their keep?** Usage per skill tells maintainers where to invest and what to retire.
4. **Are people on current versions?** A fix in `creai-common` only helps devs who updated.

None of the existing sources answers these. Claude Code's built-in OpenTelemetry does not emit an
event when a dev *types* a plugin slash command, which is how the pipeline is mostly driven. The org
analytics APIs stop at plugin-level counts. (Both per a docs check on 2026-10-07; see design § Sources
considered.)

## Goals

- A dashboard creai leadership and maintainers can open any time, showing active devs per week, the
  pipeline funnel, skill usage, a dev × skill matrix, and installed versions. Every number shows
  when its data is from.
- Collection is **opt-in per developer**, **metadata-only**, and costs the dev nothing after a
  one-time setup: no new commands to remember, and never a slower or blocked session.
- Works on macOS, Linux and Windows for any dev who installs the `creai-telemetry` plugin. Windows
  needs one open question settled first (design § Open questions).
- One-time backfill, so the dashboard starts with each opted-in dev's history instead of empty.

## Non-goals

- Productivity scoring or ranking of individuals. The dashboard shows adoption, never output, lines
  of code or time.
- Capturing prompts, skill arguments, code, or anything from plugins outside creai-axis.
- Mandatory rollout. If leadership later wants it mandatory, that becomes a separate decision through
  managed settings, with HR/legal sign-off.
- Phase 2 outcome signals from GitHub/Bitbucket (OpenSpec changes, ADRs, PR conventions) are
  sketched in the design but not part of this change.

## What changes

Everything is new, in `creai-axis-telemetry`:

| Where | What |
|---|---|
| `.claude-plugin/marketplace.json` | A one-plugin marketplace, so devs install with `claude plugin marketplace add CreAI-mx/creai-axis-telemetry`. |
| `plugins/creai-telemetry/hooks/usage-collector.py` | Stdlib-only Python. It stays inert until opt-in. At `SessionEnd` it parses that session's transcript (and its subagents) from a byte cursor and queues one event per creai-axis skill, then sends the queue. At `SessionStart` it collects sessions changed since opt-in that still hold unread bytes (Claude Code killed or crashed, so `SessionEnd` never ran), then retries anything still queued. It always exits 0. |
| `plugins/creai-telemetry/hooks/hooks.json` | `SessionStart` and `SessionEnd` blocks only. |
| `plugins/creai-telemetry/skills/creai-usage/` | `/creai-telemetry:creai-usage`: opt in (with explicit consent and the token kept out of the chat), backfill, status, opt out. |
| `tests/collector/`, `supabase/functions/ingest/handler.test.ts` | Collector unit tests: parsing, privacy (arguments, text and OS user name never captured), cursors, the hook's time budget, concurrent sessions, send and retry, opt-in file permissions. Ingest tests: auth, malformed bodies, validation. Run in CI on macOS and Linux, Python 3.9 and 3.12, and Deno. |
| `supabase/` | Migration (append-only events, opt-in devs, RLS, views) and the `ingest` Edge Function. |
| `dashboard/` | Static page; demo data until `config.js` points it at Supabase. |

## Risks

- **Trust.** Named, per-dev data can read as surveillance. Mitigations: opt-in only, announce it
  before launch, publish exactly what is sent (it is in the skill), give everyone at creai the same
  view, and don't show any output or productivity metric.
- **Transcript format is not a public contract.** The collector depends on how Claude Code writes
  `~/.claude/projects/*.jsonl`. A format change makes it quietly send nothing; it never breaks a
  session. The dashboard's "last event" column per dev makes a silent stop visible within days.
- **A second install step.** Devs add this marketplace and plugin on top of creai-axis, so reach
  depends on the announcement. If installs stall, creai-axis can link it from its MANUAL, or managed
  settings can pre-install it; it is still inert until each dev opts in. Installed but not opted in,
  it reads one missing file and exits, at a measured cost of about 50 ms of Python start-up per
  session start and end.
