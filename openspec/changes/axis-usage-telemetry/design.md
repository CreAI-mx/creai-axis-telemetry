# Design: axis-usage-telemetry

## Sources considered

| Source | Typed `/creai-*` commands | Skills Claude invokes | Per dev | Verdict |
|---|---|---|---|---|
| Claude Code OpenTelemetry (`CLAUDE_CODE_ENABLE_TELEMETRY`) | No event found in docs | Reportedly yes, skill name unmasked only with `OTEL_LOG_TOOL_DETAILS=1` | `user.email` | Misses the main usage path. A plugin cannot turn it on: it needs managed or user settings `env`. |
| Org analytics APIs (Console / Enterprise) | No | Plugin-level counts at most | Yes | Too coarse. |
| Hook `PreToolUse` matcher `Skill` | No | Yes | Needs identity | Half the picture. |
| Hook `UserPromptSubmit` | Undocumented whether it sees the raw `/plugin:cmd` text | No | Needs identity | Unreliable. |
| **Local session transcripts** | **Yes**, as `<command-name>/plugin:skill</command-name>` | **Yes**, as a `tool_use` of `Skill` with `input.skill` | Via ingest token | **Chosen.** One parser, both paths, and backfill comes free. |
| Git hosting (PRs, `openspec/changes/`, ADRs) | Indirect | Indirect | By author | Phase 2: outcomes, not usage. Needs no install. |

The OTel and analytics rows come from a docs read on 2026-10-07 and haven't been tested on our
tenant. ASSUMPTION: if a later Claude Code version adds a slash-command event to OTel, the collector
can be retired in favour of managed-settings OTel without changing the dashboard, since the event
contract below stays the same.

## Event contract

One row per skill invocation. Field by field, this is everything that leaves the machine:

| Field | Example | Source in transcript record |
|---|---|---|
| `id` | sha1 hex | sha1(sessionId, record uuid or tool_use id, plugin, skill, trigger). It's stable, so resends are idempotent. |
| `ts` | `2026-10-05T18:33:29Z` | `timestamp` |
| `session` | uuid | `sessionId` |
| `repo` | `agrizar` | Name of the git repo root containing `cwd` (so subfolder sessions roll up), else the `cwd` folder name. Never searched above the home folder. `null` when that name is the home folder or matches the OS user name. Never the full path, which contains the OS username. |
| `branch` | `feature/DAIL-256-axis-v2` | `gitBranch` |
| `cc_version` | `2.1.290` | `version` |
| `plugin`, `plugin_version` | `creai-common`, `0.15.0` | Resolved via `installed_plugins.json`, `@creai-axis` entries only |
| `skill` | `creai-implement` | Command name or `Skill` input. A bare `/creai-implement` resolves to its plugin by scanning installed skill folders. |
| `trigger` | `slash` / `model` | Which of the two record shapes matched |

The dev's identity is never in the payload: the server stamps it from the ingest token.

## Collector lifecycle

```mermaid
sequenceDiagram
  participant CC as Claude Code
  participant C as usage-collector.py
  participant Q as ~/.config/creai-axis (outbox, cursors)
  participant F as Supabase Edge Function ingest
  participant DB as Postgres (append-only)
  CC->>C: SessionEnd {transcript_path}
  alt not opted in
    C-->>CC: exit 0 (no files touched)
  else opted in
    C->>Q: parse new bytes since cursor (main + subagents), append events
    C->>F: POST batch ≤500, Bearer token, 5 s timeout
    F->>F: sha256(token) → dev row, validate, clip fields
    F->>DB: upsert ignoreDuplicates on id
    F-->>C: {accepted, rejected}
    C->>Q: drop sent events; on failure keep them
    C-->>CC: exit 0 always
  end
  CC->>C: SessionStart (next session)
  C->>Q: collect transcripts changed since opt-in with unread bytes
  C->>F: send them, and retry whatever is still queued
```

**Sessions that end without `SessionEnd`.** Claude Code doesn't run `SessionEnd` when it's killed or
crashes, or when the terminal is closed. So every `SessionStart` also lists the transcripts on the
machine and collects those changed since opt-in whose size differs from their cursor. "Since
opt-in" is the config file's write time, which is sub-second; `opted_in_at` is rounded to the
second and could let in a transcript written just before consent. Fully read files
cost one `stat` each (about 26 ms for 5,000 files) and are never opened. Files last changed before
opt-in are left alone: loading history stays the dev's choice through `backfill`. A session that is
still running elsewhere is read up to its last complete line, and the rest is collected later.

**Time budget.** Claude Code kills the hook at 10 s, so the hook budgets 8 s. It scans until 2 s are
used, leaving room for one 5 s send. A file it didn't finish goes on an `incomplete` list in the cursor
file, and the next hook (any session's, start or end) resumes it. It only starts a batch whose 5 s HTTP
timeout still fits the budget; the rest stays queued. A last line still being written when the hook
runs also keeps the file on the `incomplete` list, so a later hook collects it once it's complete. A
line still cut off after a day is abandoned (its writer died) and the cursor moves past it. Only lines containing `"Skill"` or
`<command-name>` are parsed as JSON; tool output, which can run to megabytes per line, is skipped
unparsed, and so is any line over 8 MB.

**Concurrent sessions.** Several Claude sessions share one outbox and one cursor file. Every
read-modify-write of either one happens under a cross-process lock (`usage.lock`: `flock` on macOS and
Linux, `msvcrt.locking` on Windows). Those writes are appending events, renaming the outbox to a
claim, and saving cursors. The lock is never held while scanning or sending, and a hook that can't get
it within 2 s leaves state untouched for the next hook. So that the next hook knows to rescan, it first
writes one marker file per transcript to `usage-rescan/`, which needs no lock.
- **Sending.** A hook claims the outbox by renaming it under the lock and sends it unlocked. Events
  queued meanwhile go to a fresh outbox. Unsent events are re-appended before the claim is deleted: a
  crash in between duplicates events (the server ignores repeated ids) but never loses them. A claim
  untouched for 10 minutes belongs to a dead process, and the next flush adopts it.
- **Cursors.** A hook scans from a snapshot of the cursors, then re-reads them under the lock and
  updates only its own transcripts, so it never overwrites another hook's progress or its
  `incomplete` list.

## Identity and auth

- **Opt-in creates the identity.** An admin runs `scripts/axis_admin.py issue`, which inserts an
  `axis_usage_devs` row (email, display name) holding the SHA-256 of a random 32+ char token, then hands
  the token to the dev once over a private channel. The dev runs `! python3 …/usage-collector.py optin --endpoint …`, which reads the token with
  `getpass`, so it never lands in shell history, argv or the Claude conversation. It's stored in
  `~/.config/creai-axis/usage.json` with mode 600.
- **No shared secret on laptops.** The Edge Function holds the service role, and each token can
  only write events for its own dev. Revoking means setting `revoked_at`.
- **Readers** sign in with Supabase Auth. ASSUMPTION: Microsoft Entra ID, since creai runs Microsoft
  365. The local Docker stack has no Entra app, so there the dashboard signs in by magic link
  (`authProvider: "email"`); the same RLS applies. RLS allows `select` to any authenticated `@creai.mx` email. The dashboard can never read
  `token_hash`, and no client role holds `insert`, `update` or `delete` grants.
- Deploy the function with `--no-verify-jwt`, since it authenticates with its own token.
- The collector only sends to `https://` endpoints, except plain `http://` to `localhost`, `127.0.0.1`
  or `::1` (the local Docker stack), so a token never crosses a network in clear text.
  It never follows a redirect: the token would go along to a URL `optin` never checked, so a
  redirect counts as a failed send and the events stay queued.
  A loopback send also ignores `http_proxy`, which would otherwise carry the clear-text token off the machine.

## Privacy

- Opt-in per dev, named, with the same view for everyone at creai. Announce it in the team channel
  before the first token is issued.
- Opt-out deletes local state immediately, under the lock. A hook that was mid-send when the dev
  opted out drops its unsent events instead of requeueing them; only an empty `usage.lock` remains.
  Deleting server rows is an admin action on request; it's the one exception to append-only, and
  it's logged in the Jira ticket.
- ASSUMPTION: retention of 13 months, enough for a year-over-year view. Migration `20261007200000`
  adds `axis_usage_purge_expired()`, which deletes older rows. It is not scheduled yet, because
  deletion is permanent and the period is unconfirmed. Once the owner of creai's privacy notice
  (LFPDPPP) confirms it, a follow-up migration runs it monthly:
  `create extension if not exists pg_cron;` then
  `select cron.schedule('axis-usage-retention', '15 3 1 * *', 'select public.axis_usage_purge_expired()');`.
  The earliest events date from October 2026, so no row can expire before November 2027. Employee
  data processing may also need a line in the internal privacy notice.

## Metrics (as the dashboard computes them)

- **Active dev**: at least one event in the window. Shown as `active / opted-in`.
- **Work branch**: a distinct (dev, repo, branch), excluding `main`, `master`, `develop` and `HEAD`. The
  pipeline spans sessions, so a branch is the unit of a ticket.
- **Funnel**: for each pipeline step (`refine-task → design-feature → estimate → implement → audit-pr
  → create-pr → archive`), the share of work branches that used it in the window.
- **Reached PR**: work branches that used `creai-create-pr`, out of those that used `design-feature`
  or `refine-task`.
- **Typed share**: `slash` events / all events. A high value means devs drive the pipeline on
  purpose rather than Claude picking skills up by itself.
- **Freshness**: the header shows the newest `received_at`, not the time the page loaded.

## Where things live

Everything lives in one repo, `CreAI-mx/creai-axis-telemetry`. It is its own one-plugin marketplace
(`creai-telemetry`), next to the Supabase project (migrations, function) and the dashboard. This
replaced an earlier plan to add the collector to `creai-common`. Shipping it separately means:

- `creai-axis` is untouched, so there is no `creai-common` bump, and its "all hooks are
  `PreToolUse(Bash)`" rule and "not an application" scope stay true.
- Only devs who install the plugin run the hook, which adds a second, explicit layer of consent.
- The collector, the event contract and the backend version together in one repo.

The collector still only counts plugins from the `creai-axis` marketplace (`MARKETPLACE` in the script).

## Open questions

1. **Windows invocation.** The hooks command is `python3 "${CLAUDE_PLUGIN_ROOT}/hooks/usage-collector.py" hook`,
   and native Windows often has only `python` or `py`. Options: a `.ps1` twin like creai-axis's hooks,
   or a launcher that tries `python3`, `python`, `py -3` in turn. Ask whoever owns Windows support for
   Axis v2 which one they'd accept.
2. **Dashboard hosting** after Demo Day (until then, an nginx container; see `docs/hosting.md`). A static page behind Entra: Azure Static Web Apps, AWS S3 + CloudFront,
   Vercel with SSO, or Supabase Storage. A claude.ai artifact works for the demo but can't reach
   Supabase (its CSP blocks fetch).
3. **Who issues tokens**: the platform owner, or a small admin page. In v1 it's `scripts/axis_admin.py`.
4. **Backend host.** Decided 2026-10-07: Supabase for now, run in Docker on one machine for Demo Day,
   kept ready to move to AWS. The collector and the event contract don't change either way. The AWS
   mapping (RDS PostgreSQL, Lambda reusing `handler.ts`, S3 + CloudFront) and the data move are in
   `docs/hosting.md`. Before the pilot, hand devs an endpoint on a name creai controls, so a move
   doesn't make everyone re-run `optin`.

## Phase 2 (not in this change)

A scheduled job (GitHub Actions in `creai-axis-telemetry`) walks the CreAI-mx repos and counts outcomes
per author and week: new `openspec/changes/*`, archive PRs, new ADRs, and PRs whose body follows the
`/creai-create-pr` template. These land in a second table, so the dashboard can set "used the skill"
next to "the artifact exists", including for devs who haven't opted in, at repo level only.
