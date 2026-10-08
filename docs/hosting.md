# Hosting

Decision (2026-10-07): run on **Supabase** for now, **in Docker on one machine for Demo Day
(2026-10-26)**, and keep the system ready to move to **AWS** if creai standardizes there. This page
is the runbook for the first and the plan for the second.

## Docker (Demo Day)

`supabase start` runs the whole backend as Docker containers: Postgres with the migrations applied,
Auth, the REST API, the `ingest` function, Studio and a local mail viewer (Mailpit) for sign-in
links. `deploy/docker-compose.yml` adds the dashboard as an nginx container. Everything listens on
`127.0.0.1` only.

**Needs:** Docker Desktop running, the Supabase CLI (`brew install supabase/tap/supabase`),
Python 3.9+. The dashboard loads supabase-js and its font from a CDN, so the machine needs internet.

```bash
scripts/demo-up.sh                 # starts everything, writes dashboard/config.js, prints the URLs
python3 scripts/smoke_ingest.py    # end-to-end checks against the running stack; cleans up after itself
scripts/demo-down.sh               # stops it; data is kept for the next demo-up
```

| What | URL |
|---|---|
| Dashboard | http://localhost:8080 |
| Ingest endpoint | http://127.0.0.1:54321/functions/v1/ingest |
| Mail viewer (sign-in links) | http://127.0.0.1:54324 |
| Studio (tables, SQL) | http://127.0.0.1:54323 |

**Opt yourself in.** Issue a token to a private file, then run the opt-in yourself so the token never
goes through a chat:

```bash
python3 scripts/axis_admin.py issue you@creai.mx "Your Name" --out ~/axis-token
python3 plugins/creai-telemetry/hooks/usage-collector.py optin \
  --endpoint http://127.0.0.1:54321/functions/v1/ingest < ~/axis-token && rm ~/axis-token
python3 plugins/creai-telemetry/hooks/usage-collector.py backfill
```

Then open the dashboard, enter your `@creai.mx` address, and open the sign-in link from the mail
viewer. Row Level Security admits only `@creai.mx` accounts.

**Limits of this setup**

- Only this machine can send events. The collector accepts plain `http://` only for `localhost`,
  `127.0.0.1` and `::1`, so a token never crosses a network in clear text. Other devs need an
  `https://` endpoint: a hosted project, or a TLS tunnel to this machine.
- While the stack is down, the hook's send fails fast and events stay queued; the next session start
  after `demo-up` sends them.
- Sign-in is by magic link, because no Entra ID app is registered for a local stack. The hosted
  dashboard uses `authProvider: "azure"`.
- `supabase start` is a development stack (default keys, no backups). Fine for a demo, not for the
  pilot's data.

## Moving to AWS

The collector and the event contract don't change. What changes is the box behind the endpoint.

| Piece | Now (Supabase) | AWS |
|---|---|---|
| Database | Supabase Postgres | Amazon RDS for PostgreSQL (or Aurora PostgreSQL). The tables, views and the retention function carry over; a few Supabase-specific parts need changes (see *Migrations on RDS* below). |
| Ingest | Edge Function (`index.ts` wires `handler.ts` to supabase-js) | Lambda behind API Gateway or a function URL, reusing `handler.ts` unchanged with a `Store` written against Postgres. |
| Dashboard reads | Browser → Supabase REST, filtered by RLS | A small read API (Lambda) that checks the Entra ID token and returns the same rows; only `liveData()` in `index.html` changes. |
| Dashboard hosting | nginx container / static host | S3 + CloudFront |
| Reader sign-in | Supabase Auth | Entra ID directly, or Cognito federated with Entra ID |
| Admin | `scripts/axis_admin.py` (psql) | The same script with `AXIS_DB_URL` pointing at RDS |

ASSUMPTION: RDS rather than the DynamoDB option in the design, because the dashboard views and the
funnel are SQL and move as they are.

**Migrations on RDS.** Both migrations are written for Supabase. On RDS:

- **pg_cron must be preloaded** for the monthly retention job (a follow-up migration, once the
  retention period is signed off): add `pg_cron` to `shared_preload_libraries` in the DB parameter
  group, reboot, and run that migration in the database named by `cron.database_name` (`postgres`
  unless changed).
- **The `anon` and `authenticated` roles don't exist.** Both migrations grant or revoke on them. Create
  them first (`create role anon nologin; create role authenticated nologin;`) so the migrations run
  unchanged, or remove those statements.
- **`auth.jwt()` doesn't exist**, and the RLS policies and `is_creai_reader()` call it. Leave out
  `is_creai_reader()` and the three `*_read` policies. RLS stays on, so with no policy every role is
  denied. The browser no longer reaches the database. Instead, two backend roles get explicit rights
  plus their own policies. The read API checks the Entra ID token and connects as `axis_reader`.
  ASSUMPTION: the read API is the only reader, as in the table above. The views run as the caller
  (`security_invoker`), so `axis_reader` needs the tables under them as well as the views.

```sql
-- Two login roles; their passwords live in AWS Secrets Manager, never in a migration.
create role axis_ingest login;
create role axis_reader login;
grant usage on schema public to axis_ingest, axis_reader;

-- Ingest Lambda: look up a token, append events. RLS stays on, so each right also needs a policy.
grant select (id, revoked_at, token_hash) on public.axis_usage_devs to axis_ingest;
grant insert on public.axis_usage_events to axis_ingest;
create policy devs_ingest on public.axis_usage_devs for select to axis_ingest using (true);
create policy events_ingest on public.axis_usage_events for insert to axis_ingest with check (true);

-- Read API: the views run as the caller (security_invoker), so it needs the tables underneath too.
-- Never token_hash.
grant select (id, email, display_name, github_login, opted_in_at, revoked_at)
  on public.axis_usage_devs to axis_reader;
grant select on public.axis_usage_events, public.axis_usage_pipeline_steps to axis_reader;
grant select on public.axis_usage_v_events, public.axis_usage_v_weekly, public.axis_usage_v_dev_skill_30d,
                public.axis_usage_v_funnel_30d, public.axis_usage_v_devs to axis_reader;
create policy devs_reader on public.axis_usage_devs for select to axis_reader using (true);
create policy events_reader on public.axis_usage_events for select to axis_reader using (true);
create policy steps_reader on public.axis_usage_pipeline_steps for select to axis_reader using (true);
```

The ingest Lambda's `Store` writes `insert … on conflict do nothing` with no conflict column: that
needs only `insert`. Naming the column (`on conflict (id)`, as supabase-js does) would also need
`select` on the events table. This block was checked on the local stack, acting as each role: the
ingest role finds a token and adds an event, but can't read or delete events. The reader role reads
all five views, but can't read `token_hash` or write anything.

**Moving data.** Copy two tables; nothing else holds state. The dump holds named usage data and
token hashes, so keep it owner-only and delete it after the import. Keep passwords off the command
line too (`ps` shows every argument to every user): put them in `~/.pgpass` (mode 600, one
`host:port:database:user:password` line per server) and connect without one.

```bash
chmod 600 ~/.pgpass
(umask 077 && pg_dump "host=<supabase-host> port=5432 dbname=postgres user=postgres" \
  --data-only -t public.axis_usage_devs -t public.axis_usage_events -f axis-usage.sql)
psql "host=<rds-endpoint> port=5432 dbname=<db> user=<user>" -v ON_ERROR_STOP=1 -f axis-usage.sql
rm axis-usage.sql
```

Token hashes move with `axis_usage_devs`, so every dev's token keeps working.

**Keep the endpoint stable.** Each dev's endpoint URL is saved in their local config at opt-in. Before
the pilot, put a name creai controls in front of the backend (ASSUMPTION: something like
`https://axis-usage.creai.mx/ingest`) and hand out that URL. Moving to AWS is then a DNS or proxy
change. Without it, every dev re-runs `optin` with the new URL (their token still works).
