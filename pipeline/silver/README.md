# Café Silver transformation

The [weekly Bronze workflow](../bronze/README.md) passes a completed collection ID to [Café Silver transformation](../../.github/workflows/silver.yml). The About and Menu OCR jobs run independently. Both read that collection through the scoped `read_bronze_for_silver` function and write their results to the Supabase PostgreSQL database that already serves the website.

## Set up

1. On the Bronze project, apply [001](../db/migrations/001_bronze.sql) if the Bronze tables are absent, then apply [002](../db/migrations/002_bronze_published_run.sql) and [003](../db/migrations/003_bronze_silver_export.sql). On the existing menu database, apply [004](../db/migrations/004_silver.sql) and apply [005](../db/migrations/005_silver_runner_access.sql) once to provision the restricted `coffee_silver_runner` role. Enable login with a generated password through your private admin connection. Keep the password outside migration files.
2. Set `SUPABASE_URL`, `SUPABASE_SECRET_KEY`, and `SILVER_DATABASE_URL` in [GitHub Actions secrets](https://github.com/hngnho/coffee-analysis/settings/secrets/actions). The first two point to the Bronze Data API. The third connects as `coffee_silver_runner` to the existing menu database. Use the Supabase **Session pooler** connection with `coffee_silver_runner.<project-ref>` as the username if the runner needs IPv4. [Supabase connection methods](https://supabase.com/docs/guides/database/connecting-to-postgres)
3. Enable Actions on the GitHub repository. The schedule and manual dispatch are available from the default branch once the workflow is merged.

## Run one snapshot manually

In [GitHub Actions](https://github.com/hngnho/coffee-analysis/actions), select **Café Silver transformation → Run workflow** and use the default branch. Enter the positive `run_id` printed by a successful Bronze run and choose `both`, `about`, or `menu_ocr`. To test a Bronze run collected in smoke mode, explicitly enable **Allow an explicitly selected Bronze smoke run**. Weekly collection calls both stages automatically.

To run from the repository root, set the same three connection values in your local environment and use a Python 3.12 environment. Install the PostgreSQL client and run the selected stage with the actual Bronze run ID.

```sh
python -m pip install 'psycopg[binary]==3.2.12'
python -m pipeline.silver.run --bronze-run-id 123 --stage about

python -m pip install -r pipeline/silver/requirements-ocr.txt
python -m pipeline.silver.run --bronze-run-id 123 --stage menu_ocr
```

Use `--allow-smoke` to run against a completed smoke collection. If an OCR attempt fails on a retryable image error, the job reports failure after saving its progress. Retry the same Bronze run ID to finish the remaining work.

## What is stored

- **About** stores 16 café features per snapshot row in `shop_about_feature`. Each value is `true` for explicit support, `false` for explicit negative evidence that covers the feature, or `NULL` for missing, malformed, partial, or conflicting evidence. A reason and the original option values are stored alongside the Bronze and Silver run references.
- **Menu OCR** uses only `images[*].about` labels that identify menus, such as `Menu`, `Menu photo`, `Thực đơn`, and `Bảng giá`. A collection with no labeled photos yields zero candidates. The hosted gallery response can contain unlabeled photos, so check the Bronze label count if that happens. The worker uses PaddleOCR on CPU and reuses the notebook parser and canonical item mappings. It stores candidate items, prices, and review flags in `silver_menu_observation` and reusable text boxes in `silver_ocr_cache`. Existing menu rows, prices, and human reviews are preserved. The existing web views continue to serve the website during processing.
