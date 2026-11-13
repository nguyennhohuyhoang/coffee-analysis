begin;

create or replace function public.read_bronze_for_silver(
    p_run_id bigint,
    p_allow_smoke boolean default false
) returns jsonb
language plpgsql
stable
security definer
set search_path = ''
as $$
declare
    collection public.collection_runs;
    snapshots jsonb;
    snapshot_count integer;
begin
    select * into collection
    from public.collection_runs r
    where r.run_id = p_run_id
      and r.status = 'succeeded'
      and (r.mode = 'weekly' or (r.mode = 'smoke' and p_allow_smoke is true));

    if not found then
        raise exception 'published Bronze snapshot not found' using errcode = 'PT404';
    end if;

    select count(*), jsonb_agg(jsonb_build_object(
        'source_key', s.source_key,
        'place_id', case when starts_with(s.source_key, 'place_id:')
                         then nullif(substr(s.source_key, 10), '') end,
        'name', s.name,
        'about', s.about,
        'images', s.images
    ) order by s.source_key)
    into snapshot_count, snapshots
    from public.cafe_snapshots s
    where s.run_id = p_run_id;

    if snapshot_count <> collection.row_count then
        raise exception 'Bronze snapshot count differs from published count';
    end if;

    return jsonb_build_object('run_id', collection.run_id, 'mode', collection.mode,
                              'collected_at', collection.collected_at,
                              'row_count', snapshot_count, 'rows', snapshots);
end;
$$;

revoke all on function public.read_bronze_for_silver(bigint, boolean)
    from public, anon, authenticated, service_role;
grant execute on function public.read_bronze_for_silver(bigint, boolean) to service_role;

notify pgrst, 'reload schema';

commit;
