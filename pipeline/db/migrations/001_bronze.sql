begin;

create table if not exists public.collection_runs (
    run_id bigint generated always as identity primary key,
    week_start date not null check (extract(isodow from week_start) = 1),
    mode text not null check (mode in ('weekly', 'smoke')),
    source text not null default 'omkar_cloud_google_maps',
    schema_version integer not null default 1,
    search_query text not null check (btrim(search_query) <> ''),
    result_limit integer not null check (result_limit between 1 and 500),
    github_repository text not null,
    github_run_id bigint not null check (github_run_id > 0),
    github_run_attempt integer not null check (github_run_attempt > 0),
    github_sha text not null,
    github_event text not null,
    workflow_created_at timestamptz not null,
    started_at timestamptz not null default clock_timestamp(),
    collected_at timestamptz,
    finished_at timestamptz,
    status text not null default 'running',
    source_count integer check (source_count between 0 and result_limit),
    row_count integer not null default 0 check (row_count between 0 and result_limit),
    error_code text check (error_code ~ '^[a-z][a-z0-9_]{0,79}$'),
    unique (github_repository, github_run_id, github_run_attempt),
    check (
        (status = 'running' and finished_at is null and collected_at is null
            and source_count is null and row_count = 0 and error_code is null)
        or (status = 'succeeded' and finished_at is not null and collected_at is not null
            and source_count is not null and source_count >= row_count and row_count > 0
            and error_code is null)
        or (status = 'failed' and finished_at is not null and collected_at is null
            and row_count = 0 and error_code is not null)
        or (status = 'skipped' and finished_at is not null and collected_at is null
            and source_count is null and row_count = 0 and error_code is null)
    )
);

create unique index if not exists collection_runs_weekly_success
    on public.collection_runs (github_repository, week_start)
    where status = 'succeeded' and mode = 'weekly';

create unique index if not exists collection_runs_smoke_success
    on public.collection_runs (github_repository, github_run_id)
    where status = 'succeeded' and mode = 'smoke';

create table if not exists public.cafe_snapshots (
    run_id bigint not null references public.collection_runs (run_id),
    source_key text not null check (btrim(source_key) <> ''),
    id text,
    name text,
    main_category text,
    address text,
    coordinate jsonb check (jsonb_typeof(coordinate) = 'object'),
    rating numeric check (rating between 0 and 5),
    review bigint check (review >= 0),
    price_range text,
    website text,
    phone text,
    link text,
    images jsonb check (jsonb_typeof(images) = 'array'),
    about jsonb check (jsonb_typeof(about) = 'array'),
    featured_reviews jsonb check (jsonb_typeof(featured_reviews) = 'array'),
    review_keywords jsonb check (jsonb_typeof(review_keywords) = 'array'),
    review_per_rating jsonb check (jsonb_typeof(review_per_rating) = 'object'),
    primary key (run_id, source_key)
);

create index if not exists cafe_snapshots_source_history
    on public.cafe_snapshots (source_key, run_id);

alter table public.collection_runs enable row level security;
alter table public.cafe_snapshots enable row level security;
revoke all on table public.collection_runs, public.cafe_snapshots
    from public, anon, authenticated, service_role;

create or replace function public.begin_bronze_collection(
    p_week_start date,
    p_mode text,
    p_query text,
    p_result_limit integer,
    p_github_repository text,
    p_github_run_id bigint,
    p_github_run_attempt integer,
    p_github_sha text,
    p_github_event text,
    p_workflow_created_at timestamptz
) returns jsonb
language plpgsql
security definer
set search_path = ''
as $$
declare
    attempt public.collection_runs;
    already_collected boolean;
begin
    if p_week_start is distinct from
        date_trunc('week', p_workflow_created_at at time zone 'Asia/Ho_Chi_Minh')::date then
        raise exception 'week_start does not match workflow creation time' using errcode = '22023';
    end if;

    select exists (
        select 1 from public.collection_runs r
        where r.github_repository = p_github_repository
          and r.mode = p_mode and r.status = 'succeeded'
          and ((p_mode = 'weekly' and r.week_start = p_week_start)
            or (p_mode = 'smoke' and r.github_run_id = p_github_run_id))
    ) into already_collected;

    insert into public.collection_runs (
        week_start, mode, search_query, result_limit, github_repository, github_run_id,
        github_run_attempt, github_sha, github_event, workflow_created_at, status, finished_at
    ) values (
        p_week_start, p_mode, p_query, p_result_limit, p_github_repository, p_github_run_id,
        p_github_run_attempt, p_github_sha, p_github_event, p_workflow_created_at,
        case when already_collected then 'skipped' else 'running' end,
        case when already_collected then clock_timestamp() end
    ) on conflict (github_repository, github_run_id, github_run_attempt) do nothing
    returning * into attempt;

    if not found then
        select * into strict attempt from public.collection_runs r
        where r.github_repository = p_github_repository
          and r.github_run_id = p_github_run_id
          and r.github_run_attempt = p_github_run_attempt;
        if (attempt.week_start, attempt.mode, attempt.search_query, attempt.result_limit,
            attempt.github_sha, attempt.github_event, attempt.workflow_created_at)
            is distinct from
            (p_week_start, p_mode, p_query, p_result_limit,
             p_github_sha, p_github_event, p_workflow_created_at) then
            raise exception 'workflow attempt metadata changed' using errcode = '22023';
        end if;
    else
        -- GitHub can re-run an attempt only after the preceding attempt ends.
        update public.collection_runs r
        set status = 'failed', finished_at = clock_timestamp(), error_code = 'interrupted'
        where r.github_repository = p_github_repository
          and r.github_run_id = p_github_run_id
          and r.github_run_attempt < p_github_run_attempt
          and r.status = 'running';
    end if;

    return jsonb_build_object('run_id', attempt.run_id, 'status', attempt.status,
                              'row_count', attempt.row_count);
end;
$$;

create or replace function public.finish_bronze_collection(
    p_run_id bigint,
    p_records jsonb,
    p_source_count integer,
    p_collected_at timestamptz,
    p_error_code text
) returns jsonb
language plpgsql
security definer
set search_path = ''
set statement_timeout = '30s'
as $$
declare
    attempt public.collection_runs;
    inserted_count integer;
    slot text;
begin
    select * into strict attempt from public.collection_runs where run_id = p_run_id for update;
    if attempt.status = 'running' then
        if p_error_code is not null then
            if p_records is not null or p_collected_at is not null then
                raise exception 'failed run cannot include a snapshot' using errcode = '22023';
            end if;
            update public.collection_runs
            set status = 'failed', finished_at = clock_timestamp(),
                source_count = p_source_count, error_code = p_error_code
            where run_id = p_run_id returning * into attempt;
        else
            -- Serialize successful publications for the same slot across different attempts.
            slot := attempt.github_repository || '/' || attempt.mode || '/' ||
                case when attempt.mode = 'weekly' then attempt.week_start::text
                     else attempt.github_run_id::text end;
            perform pg_advisory_xact_lock(hashtextextended(slot, 0));

            if exists (
                select 1 from public.collection_runs r
                where r.github_repository = attempt.github_repository
                  and r.mode = attempt.mode and r.status = 'succeeded'
                  and ((attempt.mode = 'weekly' and r.week_start = attempt.week_start)
                    or (attempt.mode = 'smoke' and r.github_run_id = attempt.github_run_id))
            ) then
                update public.collection_runs set status = 'skipped', finished_at = clock_timestamp()
                where run_id = p_run_id returning * into attempt;
            else
                insert into public.cafe_snapshots (
                    run_id, source_key, id, name, main_category, address, coordinate, rating,
                    review, price_range, website, phone, link, images, about, featured_reviews,
                    review_keywords, review_per_rating
                )
                select p_run_id, r.source_key, r.id, r.name, r.main_category, r.address,
                       r.coordinate, r.rating, r.review, r.price_range, r.website, r.phone,
                       r.link, r.images, r.about, r.featured_reviews, r.review_keywords,
                       r.review_per_rating
                from jsonb_populate_recordset(null::public.cafe_snapshots, p_records) as r;
                get diagnostics inserted_count = row_count;
                update public.collection_runs
                set status = 'succeeded', row_count = inserted_count, source_count = p_source_count,
                    collected_at = p_collected_at, finished_at = clock_timestamp()
                where run_id = p_run_id returning * into attempt;
            end if;
        end if;
    end if;

    return jsonb_build_object('run_id', attempt.run_id, 'status', attempt.status,
                              'row_count', attempt.row_count);
end;
$$;

revoke all on function
    public.begin_bronze_collection(date, text, text, integer, text, bigint, integer, text, text, timestamptz),
    public.finish_bronze_collection(bigint, jsonb, integer, timestamptz, text)
    from public, anon, authenticated, service_role;

grant execute on function
    public.begin_bronze_collection(date, text, text, integer, text, bigint, integer, text, text, timestamptz),
    public.finish_bronze_collection(bigint, jsonb, integer, timestamptz, text)
    to service_role;

notify pgrst, 'reload schema';

commit;
