"""Run one reproducible Silver stage against one committed Bronze snapshot."""

from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import os
import sys
from collections import Counter
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

from pipeline.silver.about import ABOUT_VERSION, transform


def read_bronze(run_id, allow_smoke):
    url = os.environ["SUPABASE_URL"].rstrip("/")
    key = os.environ["SUPABASE_SECRET_KEY"]
    request = Request(
        f"{url}/rest/v1/rpc/read_bronze_for_silver",
        data=json.dumps({"p_run_id": run_id, "p_allow_smoke": allow_smoke}).encode(),
        headers={"apikey": key, "Content-Type": "application/json"},
    )
    if not key.startswith("sb_secret_"):
        request.add_header("Authorization", f"Bearer {key}")
    try:
        with urlopen(request, timeout=45) as response:
            snapshot = json.load(response)
    except HTTPError as error:
        raise ValueError(f"Bronze snapshot read returned HTTP {error.code}") from None
    except URLError:
        raise ValueError("Could not connect to the Bronze project") from None
    rows = snapshot.get("rows")
    if (snapshot.get("run_id") != run_id or not isinstance(rows, list) or
            not rows or len(rows) != snapshot.get("row_count") or
            snapshot.get("mode") not in ({"weekly", "smoke"} if allow_smoke else {"weekly"})):
        raise ValueError("Bronze response did not contain the requested complete snapshot")
    return urlsplit(url).hostname, snapshot


def read_csv_baseline(path):
    raw = Path(path).read_bytes()
    rows = []
    ids = set()
    with io.StringIO(raw.decode("utf-8-sig"), newline="") as stream:
        for row in csv.DictReader(stream):
            place_id = row["place_id"].strip()
            if not place_id or place_id in ids:
                raise ValueError("Baseline place IDs must be nonempty and unique")
            ids.add(place_id)
            rows.append({"source_key": f"place_id:{place_id}", "place_id": place_id,
                         "about": row["about"], "images": row.get("images")})
    if not rows:
        raise ValueError("Baseline CSV is empty")
    return f"csv-sha256:{hashlib.sha256(raw).hexdigest()}", {
        "run_id": 0, "mode": "csv_baseline", "collected_at": None, "rows": rows,
    }


def begin_run(db, project, snapshot, stage, version):
    with db.transaction():
        args = (project, snapshot["run_id"], stage, version)
        db.execute("select pg_advisory_xact_lock(hashtextextended(%s, 0))", (json.dumps(args),))
        db.execute("""
            insert into public.silver_run
                (source_project, bronze_run_id, stage, transform_version, source_mode, collected_at)
            values (%s, %s, %s, %s, %s, %s)
            on conflict (source_project, bronze_run_id, stage, transform_version) do nothing
        """, (*args, snapshot["mode"], snapshot["collected_at"]))
        run_id, status = db.execute("""
            select silver_run_id, status from public.silver_run
            where source_project = %s and bronze_run_id = %s and stage = %s and transform_version = %s
        """, args).fetchone()
    return run_id, status


def finish_run(db, run_id, status, counts):
    from psycopg.types.json import Jsonb

    db.execute("""
        update public.silver_run set status = %s, finished_at = clock_timestamp(), counts = %s
        where silver_run_id = %s and status <> 'succeeded'
    """, (status, Jsonb(dict(counts)), run_id))


def run_about(db, run_id, rows):
    from psycopg.types.json import Jsonb

    counts = Counter(shops=len(rows))
    records = []
    for row in rows:
        for feature in transform(row.get("about")):
            reason = feature["reason"]
            records.append((run_id, row["source_key"], row.get("place_id"), feature["feature_key"],
                            feature["value"], reason, Jsonb(feature["evidence"])))
            counts[reason] += 1
    counts["features"] = len(records)
    with db.transaction(), db.cursor() as cursor:
        cursor.executemany("""
            insert into public.shop_about_feature
                (silver_run_id, source_key, place_id, feature_key, value, reason, evidence)
            values (%s, %s, %s, %s, %s, %s, %s)
            on conflict (silver_run_id, source_key, feature_key) do nothing
        """, records)
        finish_run(db, run_id, "succeeded", counts)
    return counts


def save_observation(db, run_id, row, photo, status, items, digest=None, version=None, boxes=None):
    from psycopg.types.json import Jsonb

    with db.transaction():
        legacy = db.execute("""
            select image_id from public.menu_image where place_id = %s and url_sha1 = %s
        """, (row.get("place_id"), hashlib.sha1(photo["url"].encode()).hexdigest())).fetchone()
        if boxes is not None:
            db.execute("""
                insert into public.silver_ocr_cache (content_sha256, ocr_version, boxes)
                values (%s, %s, %s) on conflict do nothing
            """, (digest, version, Jsonb(boxes)))
        db.execute("""
            insert into public.silver_menu_observation as saved
                (silver_run_id, source_key, place_id, image_url, source_label, source_photo_date,
                 content_sha256, ocr_version, status, items, legacy_image_id)
            values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            on conflict (silver_run_id, source_key, image_url) do update
            set attempted_at = clock_timestamp(), status = excluded.status, items = excluded.items,
                content_sha256 = excluded.content_sha256, ocr_version = excluded.ocr_version,
                legacy_image_id = excluded.legacy_image_id
            where saved.status in ('download_failed', 'ocr_failed')
        """, (run_id, row["source_key"], row.get("place_id"), photo["url"], photo["label"],
              photo["photo_date"], digest, version, status, Jsonb(items), legacy[0] if legacy else None))


def ocr_photo(db, run_id, row, photo, engine, aliases):
    from pipeline.silver.ocr import ImageDownloadError, OCRSetupError, fetch_image, parse_observations

    try:
        digest, data = fetch_image(photo["url"])
        cached = db.execute("""
            select boxes from public.silver_ocr_cache where content_sha256 = %s and ocr_version = %s
        """, (digest, engine.version)).fetchone()
        if cached:
            boxes = cached[0]
        else:
            try:
                boxes = engine.recognize(data)
            except (ImageDownloadError, OCRSetupError):
                raise
            except Exception as error:
                print(f"OCR inference failed ({type(error).__name__})", file=sys.stderr)
                save_observation(db, run_id, row, photo, "ocr_failed", [])
                return "ocr_failed"
    except ImageDownloadError:
        save_observation(db, run_id, row, photo, "download_failed", [])
        return "download_failed"

    status, items = parse_observations(boxes, aliases)
    save_observation(db, run_id, row, photo, status, items, digest, engine.version,
                     None if cached else boxes)
    return status


def run_ocr(db, run_id, rows, engine, aliases):
    from pipeline.silver.ocr import OCRSetupError, menu_images

    counts = Counter(shops=len(rows), labeled_photos=0)
    for row in rows:
        photos = menu_images(row.get("images"))
        counts["labeled_photos"] += len(photos)
        if not photos:
            counts["shops_without_labeled_photos"] += 1
        for photo in photos:
            existing = db.execute("""
                select status from public.silver_menu_observation
                where silver_run_id = %s and source_key = %s and image_url = %s
            """, (run_id, row["source_key"], photo["url"])).fetchone()
            if existing and existing[0] in {"ok", "not_menu"}:
                counts[existing[0]] += 1
                continue
            try:
                counts[ocr_photo(db, run_id, row, photo, engine, aliases)] += 1
            except OCRSetupError:
                save_observation(db, run_id, row, photo, "ocr_failed", [])
                counts["ocr_failed"] += 1
                finish_run(db, run_id, "failed", counts)
                raise
    if not counts["labeled_photos"]:
        print("No images carry a supported menu label in this Bronze snapshot.")
    status = "failed" if counts["download_failed"] or counts["ocr_failed"] else "succeeded"
    finish_run(db, run_id, status, counts)
    return counts, status


def main():
    import psycopg

    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--bronze-run-id", type=int)
    source.add_argument("--csv-baseline")
    parser.add_argument("--stage", choices=("about", "menu_ocr"), required=True)
    parser.add_argument("--allow-smoke", action="store_true")
    parser.add_argument("--device", choices=("cpu", "gpu:0"), default="cpu")
    args = parser.parse_args()
    if args.csv_baseline and args.stage != "about":
        parser.error("CSV baseline is available only for About")
    if args.bronze_run_id is not None and args.bronze_run_id < 1:
        parser.error("Bronze run ID must be positive")
    if args.csv_baseline:
        project, snapshot = read_csv_baseline(args.csv_baseline)
    else:
        project, snapshot = read_bronze(args.bronze_run_id, args.allow_smoke)

    db_url = os.environ["SILVER_DATABASE_URL"]
    with psycopg.connect(db_url, sslmode="require", connect_timeout=12, autocommit=True,
                         prepare_threshold=None) as db:
        if args.stage == "about":
            version = ABOUT_VERSION
        else:
            from pipeline.silver.ocr import PARSER_VERSION, PaddleEngine

            engine = PaddleEngine(args.device)
            aliases = dict(db.execute("select alias_norm, canonical_id from public.canonical_alias"))
            alias_version = hashlib.sha256(json.dumps(aliases, sort_keys=True).encode()).hexdigest()[:16]
            version = f"{PARSER_VERSION}:{engine.version}:aliases={alias_version}"
        run_id, status = begin_run(db, project, snapshot, args.stage, version)
        # Same run ID cannot be written concurrently, including from Colab and Actions.
        locked = db.execute("select pg_try_advisory_lock(%s)", (-run_id,)).fetchone()[0]
        if not locked:
            raise ValueError(f"Silver run {run_id} is currently active")
        status = db.execute("select status from public.silver_run where silver_run_id = %s",
                            (run_id,)).fetchone()[0]
        if status == "succeeded":
            print(f"Silver run {run_id} already completed")
            return 0
        db.execute("""
            update public.silver_run set status = 'running', finished_at = null where silver_run_id = %s
        """, (run_id,))
        try:
            if args.stage == "about":
                counts, status = run_about(db, run_id, snapshot["rows"]), "succeeded"
            else:
                counts, status = run_ocr(db, run_id, snapshot["rows"], engine, aliases)
        except Exception:
            current = db.execute("select status from public.silver_run where silver_run_id = %s",
                                 (run_id,)).fetchone()[0]
            if current == "running":
                finish_run(db, run_id, "failed", {})
            raise
        print(f"Silver run {run_id} {status}: {json.dumps(dict(counts), sort_keys=True)}")
        return 0 if status == "succeeded" else 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as error:
        # DB and HTTP errors can contain connection information. Emit only safe diagnostics.
        detail = str(error) if type(error) is ValueError else type(error).__name__
        cause = getattr(error, "__cause__", None)
        if cause is not None:
            detail += f" ({type(cause).__name__})"
        print(f"Silver failed: {detail}", file=sys.stderr)
        sys.exit(1)
