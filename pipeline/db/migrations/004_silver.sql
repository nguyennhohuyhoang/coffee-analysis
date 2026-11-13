begin;

create table if not exists public.silver_run (
    silver_run_id bigint generated always as identity primary key,
    source_project text not null,
    bronze_run_id bigint not null,
    source_mode text not null check (source_mode in ('weekly', 'smoke', 'csv_baseline')),
    collected_at timestamptz,
    stage text not null check (stage in ('about', 'menu_ocr')),
    transform_version text not null,
    status text not null default 'running' check (status in ('running', 'succeeded', 'failed')),
    started_at timestamptz not null default clock_timestamp(),
    finished_at timestamptz,
    counts jsonb not null default '{}',
    unique (source_project, bronze_run_id, stage, transform_version)
);

create table if not exists public.shop_about_feature (
    silver_run_id bigint not null references public.silver_run (silver_run_id),
    source_key text not null,
    place_id text,
    feature_key text not null,
    value boolean,
    reason text not null,
    evidence jsonb not null check (jsonb_typeof(evidence) = 'array'),
    primary key (silver_run_id, source_key, feature_key),
    check ((value is true and reason = 'explicit_true') or
           (value is false and reason = 'explicit_false') or
           (value is null and reason not in ('explicit_true', 'explicit_false')))
);

create index if not exists shop_about_feature_place_idx
    on public.shop_about_feature (place_id, feature_key, silver_run_id);

create table if not exists public.silver_ocr_cache (
    content_sha256 text not null,
    ocr_version text not null,
    boxes jsonb not null check (jsonb_typeof(boxes) = 'array'),
    primary key (content_sha256, ocr_version)
);

create table if not exists public.silver_menu_observation (
    silver_run_id bigint not null references public.silver_run (silver_run_id),
    source_key text not null,
    place_id text,
    image_url text not null,
    source_label text not null,
    source_photo_date text,
    attempted_at timestamptz not null default clock_timestamp(),
    content_sha256 text,
    ocr_version text,
    status text not null check (status in ('ok', 'not_menu', 'download_failed', 'ocr_failed')),
    items jsonb not null check (jsonb_typeof(items) = 'array'),
    legacy_image_id bigint,
    primary key (silver_run_id, source_key, image_url),
    foreign key (content_sha256, ocr_version) references public.silver_ocr_cache (content_sha256, ocr_version)
);

alter table public.silver_run enable row level security;
alter table public.shop_about_feature enable row level security;
alter table public.silver_ocr_cache enable row level security;
alter table public.silver_menu_observation enable row level security;
revoke all on public.silver_run, public.shop_about_feature, public.silver_ocr_cache,
    public.silver_menu_observation from public, anon, authenticated, service_role;

commit;
