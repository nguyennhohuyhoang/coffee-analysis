begin;

create role coffee_silver_runner nologin noinherit;

-- Keep existing quota callers working while excluding new database logins.
-- These functions run as their owner and must not be reachable via PUBLIC.
do $$
declare
    existing_role name;
    quota_function regprocedure;
begin
    for quota_function in
        select oid from pg_proc
        where oid in (to_regprocedure('public.check_and_consume_ai_quota(text,integer)'),
                      to_regprocedure('public.get_ai_quota_status(text,integer)'))
    loop
        for existing_role in
            select rolname from pg_roles
            where left(rolname, 3) <> 'pg_' and rolname <> 'coffee_silver_runner'
              and has_function_privilege(oid, quota_function, 'EXECUTE')
        loop
            execute format('grant execute on function %s to %I', quota_function, existing_role);
        end loop;
        execute format('revoke execute on function %s from public', quota_function);
    end loop;
end
$$;

grant usage on schema public to coffee_silver_runner;
grant select (alias_norm, canonical_id) on public.canonical_alias to coffee_silver_runner;
grant select (image_id, place_id, url_sha1) on public.menu_image to coffee_silver_runner;

grant select (silver_run_id, source_project, bronze_run_id, stage, transform_version, status),
    insert (source_project, bronze_run_id, source_mode, collected_at, stage, transform_version),
    update (status, finished_at, counts)
    on public.silver_run to coffee_silver_runner;
create policy silver_runner_read on public.silver_run
    for select to coffee_silver_runner using (true);
create policy silver_runner_insert on public.silver_run
    for insert to coffee_silver_runner with check (true);
create policy silver_runner_retry on public.silver_run
    for update to coffee_silver_runner using (status <> 'succeeded') with check (true);

grant select (silver_run_id, source_key, feature_key),
    insert (silver_run_id, source_key, place_id, feature_key, value, reason, evidence)
    on public.shop_about_feature to coffee_silver_runner;
create policy silver_runner_read on public.shop_about_feature
    for select to coffee_silver_runner using (true);
create policy silver_runner_insert on public.shop_about_feature
    for insert to coffee_silver_runner with check (true);

grant select (content_sha256, ocr_version, boxes), insert (content_sha256, ocr_version, boxes)
    on public.silver_ocr_cache to coffee_silver_runner;
create policy silver_runner_read on public.silver_ocr_cache
    for select to coffee_silver_runner using (true);
create policy silver_runner_insert on public.silver_ocr_cache
    for insert to coffee_silver_runner with check (true);

grant select (silver_run_id, source_key, image_url, status, items, content_sha256, ocr_version, legacy_image_id),
    insert (silver_run_id, source_key, place_id, image_url, source_label, source_photo_date,
            content_sha256, ocr_version, status, items, legacy_image_id),
    update (attempted_at, status, items, content_sha256, ocr_version, legacy_image_id)
    on public.silver_menu_observation to coffee_silver_runner;
create policy silver_runner_read on public.silver_menu_observation
    for select to coffee_silver_runner using (true);
create policy silver_runner_insert on public.silver_menu_observation
    for insert to coffee_silver_runner with check (true);
create policy silver_runner_retry on public.silver_menu_observation
    for update to coffee_silver_runner using (status in ('download_failed', 'ocr_failed')) with check (true);

commit;
