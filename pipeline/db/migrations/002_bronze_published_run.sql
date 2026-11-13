begin;

create or replace function public.published_bronze_run(p_run_id bigint)
returns jsonb
language sql
stable
security definer
set search_path = ''
as $$
    select jsonb_build_object('run_id', published.run_id, 'status', published.status,
                              'row_count', published.row_count)
    from public.collection_runs attempt
    join public.collection_runs published
      on published.github_repository = attempt.github_repository
     and published.mode = attempt.mode
     and published.status = 'succeeded'
     and ((attempt.mode = 'weekly' and published.week_start = attempt.week_start)
       or (attempt.mode = 'smoke' and published.github_run_id = attempt.github_run_id))
    where attempt.run_id = p_run_id
      and attempt.status in ('succeeded', 'skipped');
$$;

revoke all on function public.published_bronze_run(bigint)
    from public, anon, authenticated, service_role;
grant execute on function public.published_bronze_run(bigint) to service_role;

notify pgrst, 'reload schema';

commit;
