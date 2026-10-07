-- creai-axis usage telemetry: opt-in developers, append-only skill events, dashboard views.
-- Design: openspec/changes/axis-usage-telemetry/design.md

-- Developers who opted in. A row is created by an admin when issuing an ingest token;
-- the token itself is shown once and only its SHA-256 is stored.
create table public.axis_usage_devs (
  id            uuid primary key default gen_random_uuid(),
  email         text not null unique check (email ~* '^[^@]+@creai\.mx$'),
  display_name  text not null,
  github_login  text,
  token_hash    text not null unique check (token_hash ~ '^[0-9a-f]{64}$'),
  opted_in_at   timestamptz not null default now(),
  revoked_at    timestamptz
);

-- One row per skill invocation. Append-only: no UPDATE/DELETE grants exist for any client role.
-- Only metadata travels; prompts, arguments and code never leave the developer's machine.
create table public.axis_usage_events (
  id              text primary key check (id ~ '^[0-9a-f]{40}$'),  -- client-side dedupe key (sha1)
  dev_id          uuid not null references public.axis_usage_devs(id),
  ts              timestamptz not null,
  received_at     timestamptz not null default now(),
  session_id      text,
  repo            text,           -- basename of the session cwd
  branch          text,
  cc_version      text,           -- Claude Code version
  plugin          text not null,  -- e.g. creai-common
  plugin_version  text,
  skill           text not null,  -- e.g. creai-implement
  trigger         text not null check (trigger in ('slash', 'model'))
);
create index axis_usage_events_ts_idx on public.axis_usage_events (ts);
create index axis_usage_events_dev_ts_idx on public.axis_usage_events (dev_id, ts);

alter table public.axis_usage_devs enable row level security;
alter table public.axis_usage_events enable row level security;

-- Readers: any signed-in @creai.mx account (Supabase Auth, Azure/Google SSO).
-- Writers: only the ingest Edge Function, which uses the service role and bypasses RLS.
create or replace function public.is_creai_reader() returns boolean
language sql stable as $$
  select coalesce(auth.jwt() ->> 'email', '') ~* '@creai\.mx$'
$$;

create policy devs_read on public.axis_usage_devs
  for select to authenticated using (public.is_creai_reader());
create policy events_read on public.axis_usage_events
  for select to authenticated using (public.is_creai_reader());

revoke all on public.axis_usage_devs, public.axis_usage_events from anon;
revoke insert, update, delete, truncate on public.axis_usage_devs, public.axis_usage_events from authenticated;
-- token_hash is never readable by the dashboard.
revoke select on public.axis_usage_devs from authenticated;
grant select (id, email, display_name, github_login, opted_in_at, revoked_at)
  on public.axis_usage_devs to authenticated;

-- The canonical pipeline order (docs/MANUAL.md). Funnel steps are counted per (dev, repo, branch),
-- because one ticket's pipeline usually spans several sessions.
create table public.axis_usage_pipeline_steps (
  step_order int primary key,
  skill      text not null unique
);
insert into public.axis_usage_pipeline_steps values
  (1, 'creai-refine-task'),
  (2, 'creai-design-feature'),
  (3, 'creai-estimate'),
  (4, 'creai-implement'),
  (5, 'creai-audit-pr'),
  (6, 'creai-create-pr'),
  (7, 'creai-archive');
alter table public.axis_usage_pipeline_steps enable row level security;
create policy steps_read on public.axis_usage_pipeline_steps
  for select to authenticated using (public.is_creai_reader());
grant select on public.axis_usage_pipeline_steps to authenticated;

-- Views run with the caller's rights so RLS above still applies.
create view public.axis_usage_v_events with (security_invoker = true) as
select e.*, d.display_name, d.email
from public.axis_usage_events e
join public.axis_usage_devs d on d.id = e.dev_id;

create view public.axis_usage_v_weekly with (security_invoker = true) as
select date_trunc('week', ts)::date as week,
       count(*)                     as events,
       count(distinct dev_id)       as active_devs,
       count(distinct skill)        as distinct_skills,
       max(received_at)             as data_as_of
from public.axis_usage_events
group by 1;

create view public.axis_usage_v_dev_skill_30d with (security_invoker = true) as
select d.display_name, e.plugin, e.skill, count(*) as events, max(e.ts) as last_used
from public.axis_usage_events e
join public.axis_usage_devs d on d.id = e.dev_id
where e.ts >= now() - interval '30 days'
group by 1, 2, 3;

create view public.axis_usage_v_funnel_30d with (security_invoker = true) as
with tickets as (
  select dev_id, repo, branch, skill
  from public.axis_usage_events
  where ts >= now() - interval '30 days'
    and branch is not null and branch not in ('HEAD', 'main', 'master', 'develop')
  group by 1, 2, 3, 4
)
select s.step_order, s.skill, count(t.skill) as branches_reached
from public.axis_usage_pipeline_steps s
left join tickets t on t.skill = s.skill
group by 1, 2
order by 1;

create view public.axis_usage_v_devs with (security_invoker = true) as
select d.display_name, d.github_login, d.opted_in_at, d.revoked_at,
       max(e.ts)                       as last_event,
       count(e.id) filter (where e.ts >= now() - interval '7 days')  as events_7d,
       count(e.id) filter (where e.ts >= now() - interval '30 days') as events_30d,
       (array_agg(e.plugin_version order by e.ts desc)
          filter (where e.plugin = 'creai-common'))[1] as creai_common_version,
       (array_agg(e.cc_version order by e.ts desc))[1] as cc_version
from public.axis_usage_devs d
left join public.axis_usage_events e on e.dev_id = d.id
group by d.id;

grant select on public.axis_usage_v_events, public.axis_usage_v_weekly, public.axis_usage_v_dev_skill_30d,
                public.axis_usage_v_funnel_30d, public.axis_usage_v_devs to authenticated;
