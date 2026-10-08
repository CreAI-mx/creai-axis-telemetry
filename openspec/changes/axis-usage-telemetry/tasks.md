# Tasks: axis-usage-telemetry

Order is contract-first: the backend accepts events before any dev can send them.

## 0. Decisions
- [x] Repo: `CreAI-mx/creai-axis-telemetry`, holding the plugin, backend and dashboard (2026-10-07)
- [x] Backend host: Supabase for now, in Docker for Demo Day, ready to move to AWS (2026-10-07; `docs/hosting.md`)
- [ ] Windows invocation settled (§ Open questions 1)
- [ ] Dashboard host chosen (§ Open questions 2)
- [ ] Privacy-notice owner confirms opt-in wording and 13-month retention
- [ ] Token issuer named (§ Open questions 3); put the name in README § Admin
- [ ] Jira: TR-356 (research topic)

## 1. Backend (`supabase/`)
- [ ] Create the Supabase project in creai's org; enable the Entra ID (or Google) provider
- [ ] `supabase link`, then `supabase db push` to apply `migrations/20261007000000_axis_usage.sql`
- [ ] `supabase functions deploy ingest` (`verify_jwt = false` is set in `config.toml`)
- [x] Smoke test on the local stack: a valid token gets 200, a revoked token 401, a duplicate id is accepted and ignored, a bad slug is rejected
- [x] Monthly `pg_cron` retention job (`20261007200000_axis_usage_retention.sql`)
- [x] Admin script to issue, revoke and list tokens and forget a dev's events (`scripts/axis_admin.py`)
- [x] Local Docker stack: `scripts/demo-up.sh`, `scripts/demo-down.sh`, smoke test `scripts/smoke_ingest.py`
- [ ] Stable endpoint name in front of the backend before the pilot (`docs/hosting.md` § Keep the endpoint stable)

## 2. Collector (`plugins/creai-telemetry/`)
- [x] Collector, hooks, `/creai-telemetry:creai-usage` skill, tests, CI
- [x] Copilot review fixes: repo name never the OS user, hook time budget, concurrent-safe outbox (0.1.1)
- [ ] Windows launcher, per the decision in 0
- [ ] Install test on a clean machine: `claude plugin marketplace add CreAI-mx/creai-axis-telemetry`, then install `creai-telemetry`

## 3. Dashboard (`dashboard/`)
- [x] Demo Day: nginx container (`deploy/docker-compose.yml`), magic-link sign-in on the local stack
- [ ] Deploy behind SSO with a `config.js` (URL and anon key)
- [ ] Verify that a non-`@creai.mx` account sees nothing

## 4. Pilot
- [ ] 3 devs install, opt in and backfill; compare dashboard counts with their own `extract` output
- [ ] Announce to the team; issue tokens on request
- [ ] Review after 4 weeks: is the funnel definition useful, and is anything missing?
