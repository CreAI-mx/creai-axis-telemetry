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

Tokens are issued with `scripts/axis_admin.py` (psql underneath; local Docker stack by default,
`AXIS_DB_URL=postgresql://…` for any other Postgres). Only `sha256(token)` is stored; give the token to the dev privately.

```bash
python3 scripts/axis_admin.py issue dev@creai.mx "Dev Name" --out ~/dev-token   # new file, mode 600; re-issue to rotate
python3 scripts/axis_admin.py revoke dev@creai.mx
python3 scripts/axis_admin.py forget dev@creai.mx   # deletes their events on request
python3 scripts/axis_admin.py list
```

## Layout

| Path | What |
|---|---|
| `.claude-plugin/marketplace.json` | One-plugin marketplace |
| `plugins/creai-telemetry/hooks/usage-collector.py` | Collector: stdlib-only Python 3.9+, always exits 0 |
| `plugins/creai-telemetry/hooks/hooks.json` | `SessionEnd` (collect + send) and `SessionStart` (catch up on sessions that ended without `SessionEnd`, retry queue) |
| `plugins/creai-telemetry/skills/creai-usage/SKILL.md` | Opt in, backfill, status, opt out |
| `tests/collector/` | Collector unit tests |
| `tests/scripts/` | Tests for the admin and smoke-test scripts (stand-in `psql`, no database) |
| `supabase/migrations/`, `supabase/functions/ingest/` | Schema, RLS, views; ingest endpoint (`handler.ts` holds the logic and its tests) |
| `dashboard/index.html`, `dashboard/config.example.js` | Dashboard; demo data until `config.js` exists |
| `deploy/docker-compose.yml`, `scripts/demo-up.sh`, `scripts/demo-down.sh` | Run everything in Docker on one machine |
| `scripts/axis_admin.py`, `scripts/smoke_ingest.py`, `scripts/seed_demo.py` | Token admin; end-to-end check of a running backend; fictitious demo data for the local stack |
| `docs/hosting.md` | Docker runbook (Demo Day) and the move to AWS |
| `docs/ideas.md` | Ideas for after Demo Day: telemetry features, decoupling from creai-axis, role packs beyond devs |

## Develop

```bash
python3 -m unittest discover -s tests/collector                        # collector tests, stdlib only
python3 -m unittest discover -s tests/scripts                          # admin and smoke-test scripts
python3 plugins/creai-telemetry/hooks/usage-collector.py extract ~/.claude/projects/*/*.jsonl   # what would be sent; sends nothing
deno check supabase/functions/ingest/ && deno test --no-lock supabase/functions/ingest/   # or via npx -y deno
python3 -m http.server -d dashboard 8000                               # dashboard with demo data
```

Use `$CLAUDE_CONFIG_DIR/projects` instead of `~/.claude/projects` if you set `CLAUDE_CONFIG_DIR`.

Run everything in Docker on this machine (Docker Desktop and the Supabase CLI needed; see
[`docs/hosting.md`](docs/hosting.md)):

```bash
scripts/demo-up.sh && python3 scripts/smoke_ingest.py   # dashboard on http://localhost:8080
```

Deploy the backend to a hosted project (once one exists in creai's Supabase org):

```bash
supabase link --project-ref <ref>
supabase db push
supabase functions deploy ingest
```

Dashboard prototype with demo data: https://claude.ai/artifact/3oGAkFBUDnTqU3mDGmv5ae (private).
