-- Retention: a function that deletes events older than 13 months.
-- ASSUMPTION: 13 months until the privacy-notice owner confirms it (tasks.md § 0). Nothing calls this
-- function on a schedule yet: deletion is permanent, so the monthly pg_cron job is added in a follow-up
-- migration once the period is confirmed (design.md, Privacy). The local smoke test runs it directly.

create or replace function public.axis_usage_purge_expired() returns bigint
language sql set search_path = '' as $$
  with gone as (
    delete from public.axis_usage_events where ts < now() - interval '13 months' returning 1
  )
  select count(*) from gone
$$;

-- Clients never call it: anon/authenticated get no execute right.
revoke all on function public.axis_usage_purge_expired() from public, anon, authenticated;
