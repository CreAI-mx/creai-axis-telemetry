-- Retention: events older than 13 months are deleted once a month.
-- ASSUMPTION: 13 months until the privacy-notice owner confirms it (tasks.md § 0).
-- Plain Postgres + pg_cron, both of which Supabase and Amazon RDS for PostgreSQL provide.

create or replace function public.axis_usage_purge_expired() returns bigint
language sql set search_path = '' as $$
  with gone as (
    delete from public.axis_usage_events where ts < now() - interval '13 months' returning 1
  )
  select count(*) from gone
$$;

-- Clients never call it: anon/authenticated get no execute right.
revoke all on function public.axis_usage_purge_expired() from public, anon, authenticated;

create extension if not exists pg_cron;

-- 03:15 UTC on the 1st of every month. Scheduling the same name again replaces the job.
select cron.schedule('axis-usage-retention', '15 3 1 * *', 'select public.axis_usage_purge_expired()');
