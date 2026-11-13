# Weekly café snapshots in Supabase

GitHub Actions calls the hosted Omkar Cloud Google Maps Scraper every Friday at **09:00 in Da Nang** (02:00 UTC). The workflow searches for `cafe in an thuong da nang`, the exact query recorded in the 115-row example CSV. Weekly mode requests up to 120 results. It fetches details in batches of up to 20 to collect the fields missing from search, including featured reviews and reviews by rating. [Search Businesses](https://www.omkar.cloud/tools/google-maps-scraper) · [Business Details](https://www.omkar.cloud/tools/google-maps-scraper/business-details)

Supabase Postgres stores the run history and all snapshot rows. The collector keeps responses in memory until it publishes. GitHub Actions logs run IDs, counts, field names with no data, image label coverage, and sanitized error codes. The weekly workflow passes the ID of the committed snapshot to Silver. A retry passes the original snapshot ID when that week's collection has already succeeded.

## Set up Supabase

1. Open the Bronze Supabase project configured in the `SUPABASE_URL` GitHub Actions secret. Silver writes to the existing menu database using `SILVER_DATABASE_URL`.
2. Open the Bronze project's **SQL Editor** → **New query**. Run [pipeline/db/migrations/001_bronze.sql](../db/migrations/001_bronze.sql) if the Bronze tables are absent, then run [the Bronze handoff migration](../db/migrations/002_bronze_published_run.sql) and [the Silver export migration](../db/migrations/003_bronze_silver_export.sql). These scripts set up `public.collection_runs`, `public.cafe_snapshots`, and the functions used to publish, identify, or read a committed snapshot. You can verify the tables in **Table Editor**. The **Data API** must expose `public`. Check **Integrations → Data API** if needed. [Supabase database functions](https://supabase.com/docs/guides/database/functions) · [Data API](https://supabase.com/docs/guides/api)
3. Copy the **Project URL** from the **Connect** dialog or **Integrations → Data API**. Use the base URL in the form `https://<project-ref>.supabase.co`.
4. If the project needs a new key, open **Settings → API Keys → Publishable and secret API keys**. In **Secret keys**, create a dedicated key named `coffee-bronze-actions` and copy its `sb_secret_…` value. Keep the secret key in GitHub Actions Secrets. The SQL restricts the Bronze tables and allows the server role to run the collection, handoff, and export functions. The secret key has elevated permissions across the project. [Supabase API keys](https://supabase.com/docs/guides/getting-started/api-keys)

## Set up GitHub Actions

1. Sign in to Omkar Cloud. Copy the key from **My API Key** and confirm that the Google Maps Scraper API is active for your account. [Omkar Cloud API reference](https://www.omkar.cloud/docs/api)
2. In [coffee-analysis → Actions secrets](https://github.com/hngnho/coffee-analysis/settings/secrets/actions), click **New repository secret** for each entry.

   | Name | Value to paste |
   | --- | --- |
   | `OMKAR_API_KEY` | The Omkar Cloud API key |
   | `SUPABASE_URL` | The Supabase Project URL from step 3 |
   | `SUPABASE_SECRET_KEY` | The Supabase `sb_secret_…` key from step 4 |
   | `SILVER_DATABASE_URL` | A PostgreSQL connection string for the Silver destination |

   Enter each value directly in GitHub. [GitHub Actions Secrets](https://docs.github.com/en/actions/how-tos/write-workflows/choose-what-workflows-do/use-secrets)
3. Merge the pipeline files and [workflow](../../.github/workflows/weekly-bronze.yml) into the repository's default branch, currently `main`. GitHub reads the weekly schedule and enables manual runs from that branch. [GitHub scheduling and manual runs](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows#schedule)

## Test and verify

1. In [GitHub Actions](https://github.com/hngnho/coffee-analysis/actions), open **Weekly Bronze collection** → **Run workflow**, select `main` and **smoke**, and click **Run workflow**. Smoke mode requests up to three places and writes a run marked `smoke`. The successful job prints the Supabase run ID and stored row count. Only weekly mode automatically calls Silver.
2. In the Supabase SQL Editor, run these queries. Match the run ID to the GitHub log. A successful smoke run should show one to three rows, and the counts in both tables should agree.

   ```sql
   select run_id, week_start, mode, status, source_count, row_count,
          github_run_id, github_run_attempt, collected_at, error_code
   from public.collection_runs
   order by run_id desc
   limit 10;

   select r.run_id, r.mode, r.row_count, count(s.run_id) as stored_rows
   from public.collection_runs r
   left join public.cafe_snapshots s using (run_id)
   group by r.run_id
   order by r.run_id desc
   limit 10;
   ```

   Open `cafe_snapshots` in **Table Editor**, filter to the smoke run ID, and check the nested values, especially `images` and `featured_reviews`. If details return no images, the collector retains any search images or fetches the first page of up to 15 gallery photos when a positive image count is reported. Bronze keeps image objects as supplied, including any `about` label. When details and search both return images, Bronze also retains search entries whose original labels would otherwise be lost. The hosted details example shows empty `images` and `menu_photos` arrays, and the hosted gallery example supplies photo IDs and dimensions without `about` labels. Silver OCR selects only menu-labeled entries in `images`. A weekly snapshot with zero such entries produces no new OCR candidates. [Omkar Business Details](https://www.omkar.cloud/tools/google-maps-scraper/business-details) · [Omkar Business Photos](https://www.omkar.cloud/tools/google-maps-scraper/business-photos)
3. Open the **same** GitHub run and choose **Re-run all jobs**. A new attempt should report `skipped` with zero snapshot rows. A new **Run workflow** dispatch creates a new smoke test.
4. To collect the current week's full snapshot immediately, dispatch **weekly**. A second weekly run for that Vietnam week should report `skipped` and pass the original committed run ID to Silver. Future weeks append their own observations. [Manually running a GitHub workflow](https://docs.github.com/actions/managing-workflow-runs/manually-running-a-workflow)

## Fields and identity

The CSV stores coordinates and reviews per rating as JSON objects, four other nested fields as JSON arrays, rating as a number, and review count as an integer. Phones, price ranges, and IDs are text.

| Postgres type | Bronze fields |
| --- | --- |
| `text` | `id` (Omkar `kgmid`), `name`, `main_category`, `address`, `price_range`, `website`, `phone`, `link` |
| `numeric` | `rating` |
| `bigint` | `review` (Omkar `reviews`) |
| `jsonb` objects | `coordinate` (Omkar `coordinates`), `review_per_rating` (Omkar `reviews_per_rating`) |
| `jsonb` arrays | `images`, `about`, `featured_reviews`, `review_keywords` |

`run_id` links each snapshot to its collection. The additional `source_key` uses the Google Maps `place_id` or falls back to Omkar's `data_id`. Repeated search hits that share a provider ID use the first observation within that run. `source_count` is the number of search results before deduplication. The same café can appear in snapshots for many weeks.

The run records the source, query, limit, GitHub run and attempt IDs, commit SHA, event, timestamps, status, and counts. The collection week starts Monday in Vietnam and is calculated from the original GitHub workflow creation time. A rerun after Monday keeps the original week and records the actual later collection time. Snapshot inserts and success are committed in one transaction. Each repository can publish one successful weekly snapshot per week, with smoke tests tracked by their GitHub run ID.

## If a run fails

- `missing_omkar_api_key`, `omkar_invalid_api_key`, or `omkar_activation_required` means to check the GitHub secret and the Google Maps API subscription in Omkar Cloud. `omkar_quota_exceeded` means the plan quota is exhausted. Temporary errors retry with bounded backoff. [Omkar Cloud error codes](https://www.omkar.cloud/docs/api)
- A Supabase HTTP 401 or 403 usually points to the project URL, server key, or function grants. A 404 can indicate that the SQL has yet to run or the Data API cannot see the functions. Check the setup steps before retrying.
- An empty search, incomplete detail batch, invalid response, or failed database write fails the run without publishing a partial snapshot. Check the workflow log and `collection_runs.error_code`. If the runner is cancelled or loses database access, its row may remain `running`. Re-run that GitHub workflow to record the interruption and try again. If the first attempt had already committed, the rerun safely skips publication.

The CSV was used to verify the schema. A smoke or weekly run writes newly collected data.
