-- Run in a disposable database after 001_bronze.sql and both Bronze migrations.
begin;

do $$
declare
    stamp constant timestamptz := '2026-11-13 02:00:00+00';
    rows constant jsonb := '[
        {"source_key":"place_id:ChIJexample", "name":"Menu café", "images":[{"about":"Menu"}]},
        {"source_key":"data_id:0x123:0x456", "about":[]}
    ]';
    weekly bigint;
    smoke bigint;
    snapshot jsonb;
    repeated_attempt jsonb;
begin
    weekly := (public.begin_bronze_collection(
        '2026-11-09', 'weekly', 'test', 2, 'test/silver-export', 1, 1, 'test', 'test', stamp
    )->>'run_id')::bigint;
    begin
        perform public.read_bronze_for_silver(weekly);
        raise exception 'running snapshot was exported';
    exception when sqlstate 'PT404' then
        null;
    end;

    perform public.finish_bronze_collection(weekly, rows, 2, stamp, null);
    snapshot := public.read_bronze_for_silver(weekly);
    if (snapshot->>'run_id')::bigint is distinct from weekly
        or snapshot->>'mode' is distinct from 'weekly'
        or (snapshot->>'collected_at')::timestamptz is distinct from stamp
        or (snapshot->>'row_count')::integer is distinct from 2
        or snapshot->'rows' is distinct from '[
            {"source_key":"data_id:0x123:0x456", "place_id":null, "name":null, "about":[], "images":null},
            {"source_key":"place_id:ChIJexample", "place_id":"ChIJexample", "name":"Menu café", "about":null, "images":[{"about":"Menu"}]}
        ]'::jsonb then
        raise exception 'weekly export differs from expected payload';
    end if;

    repeated_attempt := public.begin_bronze_collection(
        '2026-11-09', 'weekly', 'test', 2, 'test/silver-export', 1, 2, 'test', 'test', stamp
    );
    if repeated_attempt->>'status' is distinct from 'skipped'
        or (public.published_bronze_run((repeated_attempt->>'run_id')::bigint)->>'run_id')::bigint
           is distinct from weekly then
        raise exception 'weekly retry did not resolve to the original published snapshot';
    end if;

    smoke := (public.begin_bronze_collection(
        '2026-11-09', 'smoke', 'test', 2, 'test/silver-export', 2, 1, 'test', 'test', stamp
    )->>'run_id')::bigint;
    perform public.finish_bronze_collection(smoke, rows, 2, stamp, null);
    begin
        perform public.read_bronze_for_silver(smoke);
        raise exception 'smoke snapshot exported without opt-in';
    exception when sqlstate 'PT404' then
        null;
    end;
    if public.read_bronze_for_silver(smoke, true)->>'mode' is distinct from 'smoke' then
        raise exception 'explicit smoke export failed';
    end if;

    if not has_function_privilege('service_role', 'public.read_bronze_for_silver(bigint, boolean)', 'execute')
        or has_function_privilege('anon', 'public.read_bronze_for_silver(bigint, boolean)', 'execute')
        or has_function_privilege('authenticated', 'public.read_bronze_for_silver(bigint, boolean)', 'execute')
        or has_table_privilege('service_role', 'public.cafe_snapshots', 'select') then
        raise exception 'Bronze export permissions are incorrect';
    end if;

    update public.collection_runs set row_count = 1 where run_id = weekly;
    begin
        perform public.read_bronze_for_silver(weekly);
        raise exception 'incomplete snapshot was exported';
    exception when raise_exception then
        if sqlerrm <> 'Bronze snapshot count differs from published count' then
            raise;
        end if;
    end;
end;
$$;

rollback;
