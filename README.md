# creai-axis-telemetry

Measures how much creai-axis is used across creai's developers. It has three parts:

- **`creai-telemetry` plugin**: a Claude Code plugin whose session hook finds creai-axis skill
  invocations in the dev's local transcripts and sends one metadata-only event per invocation. It
  is **inert until the dev opts in**.
- **Backend** (`supabase/`): Postgres with append-only events and Row Level Security, plus an
  `ingest` Edge Function that authenticates each dev by a personal ingest token.
- **Dashboard** (`dashboard/`): a static page showing active devs per week, the pipeline funnel,
  skill usage, a dev × skill matrix and installed versions.

Read why and how in [`openspec/changes/axis-usage-telemetry/`](openspec/changes/axis-usage-telemetry/proposal.md):
`proposal.md`, then `design.md` and `tasks.md`. Jira: [TR-356](https://creai.atlassian.net/browse/TR-356).

## For developers: opt in

```bash
claude plugin marketplace add CreAI-mx/creai-axis-telemetry
claude plugin install creai-telemetry@creai-axis-telemetry
```

Then, in Claude Code, run `/creai-telemetry:creai-usage`. It shows exactly what is sent, asks for
your consent, and walks you through entering your ingest token. The token never goes into the chat.
The same skill shows status, backfills past sessions, or opts you out.

**What is sent:** per creai-axis skill invocation, the timestamp, session id, repo folder name (never your home folder or user name), git
branch, Claude Code version, plugin and version, skill name, and whether you typed it or Claude
invoked it. **Never sent:** prompts, arguments, code, file contents, tool output, or anything about
other plugins.

## Admin

<!-- ASSUMPTION: owner not named yet; see tasks.md § 0 -->
Ingest tokens are issued by: **TBD**. Ask in the team channel until this is filled in.

Issuing a token in v1 means inserting into `axis_usage_devs` the email, display name and
`sha256(token)`, then giving the token to the dev privately. Revoking means setting `revoked_at`.

## Layout

| Path | What |
|---|---|
| `.claude-plugin/marketplace.json` | One-plugin marketplace |
| `plugins/creai-telemetry/hooks/usage-collector.py` | Collector: stdlib-only Python 3.9+, always exits 0 |
| `plugins/creai-telemetry/hooks/hooks.json` | `SessionStart` (retry queue) and `SessionEnd` (collect + send) |
| `plugins/creai-telemetry/skills/creai-usage/SKILL.md` | Opt in, backfill, status, opt out |
| `tests/collector/` | Collector unit tests |
| `supabase/migrations/`, `supabase/functions/ingest/` | Schema, RLS, views; ingest endpoint (`handler.ts` holds the logic and its tests) |
| `dashboard/index.html`, `dashboard/config.example.js` | Dashboard; demo data until `config.js` exists |

## Develop

```bash
python3 -m unittest discover -s tests/collector                        # collector tests, stdlib only
python3 plugins/creai-telemetry/hooks/usage-collector.py extract ~/.claude/projects/*/*.jsonl   # what would be sent; sends nothing
deno check supabase/functions/ingest/ && deno test --no-lock supabase/functions/ingest/   # or via npx -y deno
python3 -m http.server -d dashboard 8000                               # dashboard with demo data
```

Use `$CLAUDE_CONFIG_DIR/projects` instead of `~/.claude/projects` if you set `CLAUDE_CONFIG_DIR`.

Deploy the backend (once a project exists in creai's Supabase org):

```bash
supabase link --project-ref <ref>
supabase db push
supabase functions deploy ingest
```

Dashboard prototype with demo data: https://claude.ai/artifact/3oGAkFBUDnTqU3mDGmv5ae (private).
