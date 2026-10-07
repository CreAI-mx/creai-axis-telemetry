# Tasks: axis-usage-telemetry

Order is contract-first: the backend accepts events before any dev can send them.

## 0. Decisions
- [x] Repo: `CreAI-mx/creai-axis-telemetry`, holding the plugin, backend and dashboard (2026-10-07)
- [ ] Backend host: Supabase in creai's org (as built) or AWS (design § Open questions 4)
- [ ] Windows invocation settled (§ Open questions 1)
- [ ] Dashboard host chosen (§ Open questions 2)
- [ ] Privacy-notice owner confirms opt-in wording and 13-month retention
- [ ] Token issuer named (§ Open questions 3); put the name in README § Admin
- [ ] Jira: TR-356 (research topic)

## 1. Backend (`supabase/`)
- [ ] Create the Supabase project in creai's org; enable the Entra ID (or Google) provider
- [ ] `supabase link`, then `supabase db push` to apply `migrations/20261007000000_axis_usage.sql`
- [ ] `supabase functions deploy ingest` (`verify_jwt = false` is set in `config.toml`)
- [ ] Smoke test: a valid token gets 200, a revoked token 401, a duplicate id is accepted and ignored, a bad slug is rejected
- [ ] Monthly `pg_cron` retention job
- [ ] Admin script to issue and revoke tokens (replaces manual SQL)

## 2. Collector (`plugins/creai-telemetry/`)
- [x] Collector, hooks, `/creai-telemetry:creai-usage` skill, 10 tests, CI
- [ ] Windows launcher, per the decision in 0
- [ ] Install test on a clean machine: `claude plugin marketplace add CreAI-mx/creai-axis-telemetry`, then install `creai-telemetry`

## 3. Dashboard (`dashboard/`)
- [ ] Deploy behind SSO with a `config.js` (URL and anon key)
- [ ] Verify that a non-`@creai.mx` account sees nothing

## 4. Pilot
- [ ] 3 devs install, opt in and backfill; compare dashboard counts with their own `extract` output
- [ ] Announce to the team; issue tokens on request
- [ ] Review after 4 weeks: is the funnel definition useful, and is anything missing?
