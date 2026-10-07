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
  C->>F: retry whatever is still queued
```

**Time budget.** Claude Code kills the hook at 10 s, so the hook budgets 8 s. It scans until 2 s are
used, leaving room for one 5 s send. A file it didn't finish goes on an `incomplete` list in the cursor
file, and the next hook (any session's, start or end) resumes it. It only starts a batch whose 5 s HTTP
timeout still fits the budget; the rest stays queued. A last line still being written when the hook
runs also keeps the file on the `incomplete` list, so a later hook collects it once it's complete. A
line still cut off after a day is abandoned (its writer died).

**Concurrent sessions.** Several Claude sessions share one outbox and one cursor file. Every
read-modify-write of either one happens under a cross-process lock (`usage.lock`: `flock` on macOS and
Linux, `msvcrt.locking` on Windows). Those writes are appending events, renaming the outbox to a
claim, and saving cursors. The lock is never held while scanning or sending, and a hook that can't get
it within 2 s leaves state untouched for the next hook.
- **Sending.** A hook claims the outbox by renaming it under the lock and sends it unlocked. Events
  queued meanwhile go to a fresh outbox. Unsent events are re-appended before the claim is deleted: a
  crash in between duplicates events (the server ignores repeated ids) but never loses them. A claim
  untouched for 10 minutes belongs to a dead process, and the next flush adopts it.
- **Cursors.** A hook scans from a snapshot of the cursors, then re-reads them under the lock and
  updates only its own transcripts, so it never overwrites another hook's progress or its
  `incomplete` list.

## Identity and auth

- **Opt-in creates the identity.** An admin inserts an `axis_usage_devs` row (email, display name)
  holding the SHA-256 of a random 32+ char token, then hands the token to the dev once over a private
  channel. The dev runs `! python3 …/usage-collector.py optin --endpoint …`, which reads the token with
  `getpass`, so it never lands in shell history, argv or the Claude conversation. It's stored in
  `~/.config/creai-axis/usage.json` with mode 600.
- **No shared secret on laptops.** The Edge Function holds the service role, and each token can
  only write events for its own dev. Revoking means setting `revoked_at`.
- **Readers** sign in with Supabase Auth. ASSUMPTION: Microsoft Entra ID, since creai runs Microsoft
  365. RLS allows `select` to any authenticated `@creai.mx` email. The dashboard can never read
  `token_hash`, and no client role holds `insert`, `update` or `delete` grants.
- Deploy the function with `--no-verify-jwt`, since it authenticates with its own token.

## Privacy

- Opt-in per dev, named, with the same view for everyone at creai. Announce it in the team channel
  before the first token is issued.
- Opt-out deletes local state immediately, under the lock. A hook that was mid-send when the dev
  opted out drops its unsent events instead of requeueing them; only an empty `usage.lock` remains.
  Deleting server rows is an admin action on request; it's the one exception to append-only, and
  it's logged in the Jira ticket.
- ASSUMPTION: retention of 13 months, enough for a year-over-year view. A monthly `pg_cron` job would
  delete older rows. Confirm with whoever owns creai's privacy notice (LFPDPPP); employee data
  processing may need a line in the internal privacy notice.

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
2. **Dashboard hosting.** A static page behind Entra: Azure Static Web Apps, AWS S3 + CloudFront,
   Vercel with SSO, or Supabase Storage. A claude.ai artifact works for the demo but can't reach
   Supabase (its CSP blocks fetch).
3. **Who issues tokens**: the platform owner, or a small admin page. It's manual SQL in v1.
4. **Backend host.** Supabase in creai's org (as built, not a personal project), or AWS (API Gateway +
   Lambda for ingest, DynamoDB with TTL for retention, S3 + CloudFront for the dashboard) if creai
   standardizes there. The collector and the event contract don't change either way; only `supabase/`
   and the dashboard's data layer would be replaced.

## Phase 2 (not in this change)

A scheduled job (GitHub Actions in `creai-axis-telemetry`) walks the CreAI-mx repos and counts outcomes
per author and week: new `openspec/changes/*`, archive PRs, new ADRs, and PRs whose body follows the
`/creai-create-pr` template. These land in a second table, so the dashboard can set "used the skill"
next to "the artifact exists", including for devs who haven't opted in, at repo level only.
