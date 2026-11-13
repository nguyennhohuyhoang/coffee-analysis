"""Collect a Google Maps snapshot and publish it to Supabase Postgres."""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from http.client import HTTPException
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener
from zoneinfo import ZoneInfo

OMKAR_URL = "https://google-maps-scraper.omkar.cloud"
QUERY = "cafe in an thuong da nang"
LOCALE = {"language": "en", "country_code": "vn"}
LIMITS = {"weekly": 120, "smoke": 3}
DETAIL_BATCH_SIZE = 20
PHOTO_LIMIT = 15
VIETNAM = ZoneInfo("Asia/Ho_Chi_Minh")

# The desktop CSV uses kgmid for the selected id. The separate source_key
# allows replay-safe inserts even when that optional provider field is absent.
SOURCE_FIELDS = {
    "id": "kgmid",
    "name": "name",
    "main_category": "main_category",
    "address": "address",
    "coordinate": "coordinates",
    "rating": "rating",
    "review": "reviews",
    "price_range": "price_range",
    "website": "website",
    "phone": "phone",
    "link": "link",
    "images": "images",
    "about": "about",
    "featured_reviews": "featured_reviews",
    "review_keywords": "review_keywords",
    "review_per_rating": "reviews_per_rating",
}
NESTED_TYPES = {
    "coordinate": dict,
    "images": list,
    "about": list,
    "featured_reviews": list,
    "review_keywords": list,
    "review_per_rating": dict,
}
OMKAR_ERRORS = {
    "missing_api_key", "invalid_api_key", "account_suspended", "quota_exceeded",
    "rate_limited", "upstream_unavailable", "service_unavailable",
    "plan_required", "activation_required", "subscription_expired",
    "subscription_pending", "subscription_cancelled",
}


class CollectionError(Exception):
    """Use only fixed codes and HTTP statuses so error messages can be logged."""


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


OPENER = build_opener(NoRedirect())


def required(name):
    value = os.getenv(name, "").strip()
    if not value:
        raise CollectionError(f"missing_{name.lower()}")
    return value


def hosted_supabase_url(value):
    try:
        parts = urlsplit(value)
    except ValueError:
        raise CollectionError("invalid_supabase_url") from None
    host = parts.netloc.removesuffix(".supabase.co")
    if (parts.scheme != "https" or host == parts.netloc
            or not host.isascii() or not host.isalnum()
            or parts.path not in ("", "/") or parts.query or parts.fragment):
        raise CollectionError("invalid_supabase_url")
    return value.rstrip("/")


def retry_after(headers, attempt):
    value = headers.get("Retry-After")
    if value is not None:
        try:
            return max(0, int(value))
        except ValueError:
            try:
                when = parsedate_to_datetime(value)
                if when.tzinfo is not None:
                    return max(0, (when - datetime.now(timezone.utc)).total_seconds())
            except (TypeError, ValueError, OverflowError):
                pass
    return 2 ** (attempt + 1)


def request_json(service, url, headers, payload=None):
    body = None
    if payload is not None:
        try:
            body = json.dumps(payload, ensure_ascii=False, allow_nan=False).encode("utf-8")
        except (ValueError, UnicodeError):
            raise CollectionError(f"{service}_invalid_payload") from None

    request = Request(
        url, data=body,
        headers={"Accept": "application/json", "Content-Type": "application/json", **headers},
    )
    for attempt in range(4):
        try:
            with OPENER.open(request, timeout=60) as response:
                return json.load(response)
        except HTTPError as exc:
            status = exc.code
            code = f"{service}_http_{status}"
            delay = retry_after(exc.headers, attempt)
            with exc:
                if service == "omkar":
                    try:
                        error = json.loads(exc.read(8192))
                    except (ValueError, OSError, HTTPException):
                        error = None
                    if isinstance(error, dict):
                        provider_code = error.get("code")
                        if isinstance(provider_code, str) and provider_code in OMKAR_ERRORS:
                            code = f"omkar_{provider_code}"
            transient = (status in {408, 429} or 500 <= status <= 599) and code != "omkar_quota_exceeded"
        except (URLError, OSError, HTTPException):
            code, transient, delay = f"{service}_network_error", True, 2 ** (attempt + 1)
        except (ValueError, UnicodeError):
            raise CollectionError(f"{service}_invalid_json") from None

        if not transient or attempt == 3 or delay > 60:
            raise CollectionError(code)
        time.sleep(delay)


def omkar_results(api_key, endpoint, params):
    data = request_json(
        "omkar", f"{OMKAR_URL}/{endpoint}?{urlencode(params | LOCALE)}", {"API-Key": api_key},
    )
    if not isinstance(data, dict) or not isinstance(data.get("results"), list):
        raise CollectionError(f"omkar_invalid_{endpoint.replace('-', '_')}_response")
    results = data["results"]
    if (type(data.get("count")) is not int or data["count"] != len(results)
            or any(not isinstance(row, dict) for row in results)):
        raise CollectionError(f"omkar_invalid_{endpoint.replace('-', '_')}_results")
    return results


def source_id(row, field):
    value = row.get(field)
    return value.strip() if isinstance(value, str) and value.strip() else None


def project_place(row, source_key):
    record = {"source_key": source_key}
    for column, source_field in SOURCE_FIELDS.items():
        value = row.get(source_field)
        if value is None:
            record[column] = None
            continue

        if column in NESTED_TYPES:
            valid = isinstance(value, NESTED_TYPES[column])
        elif column == "rating":
            valid = type(value) in (int, float) and 0 <= value <= 5 and math.isfinite(value)
        elif column == "review":
            valid = type(value) is int and 0 <= value < 2**63
        else:
            valid = isinstance(value, str)
        if not valid:
            raise CollectionError(f"omkar_invalid_field_{column}")
        record[column] = value
    return record


def details_ids(row):
    return {value for field in ("data_id", "place_id") if (value := source_id(row, field))}


def keep_search_image_labels(detail_images, search_images):
    if not isinstance(search_images, list):
        return detail_images
    if detail_images in (None, []):
        return search_images or detail_images
    if not isinstance(detail_images, list):
        return detail_images

    def labeled_url(photo):
        if not isinstance(photo, dict):
            return None
        link, label = photo.get("link"), photo.get("about")
        if isinstance(link, str) and link and isinstance(label, str) and label.strip():
            return link
        return None

    labels = {labeled_url(photo) for photo in detail_images}
    merged = list(detail_images)
    for photo in search_images:
        link = labeled_url(photo)
        if link and link not in labels:
            merged.append(photo)
            labels.add(link)
    return merged


def collect_details(api_key, search_rows):
    unique = {}
    seen_ids = set()
    for row in search_rows:
        lookup = source_id(row, "data_id") or source_id(row, "place_id")
        if not lookup or "," in lookup:
            raise CollectionError("omkar_missing_details_identity")
        place_id = source_id(row, "place_id")
        key = f"place_id:{place_id}" if place_id else f"data_id:{lookup}"
        aliases = details_ids(row)
        if key not in unique and not (aliases & seen_ids):
            unique[key] = (lookup, row)
        seen_ids.update(aliases)

    places = list(unique.items())
    for start in range(0, len(places), DETAIL_BATCH_SIZE):
        batch = places[start:start + DETAIL_BATCH_SIZE]
        by_lookup = {lookup: (key, row) for key, (lookup, row) in batch}
        aliases = {alias: lookup for lookup, (_, row) in by_lookup.items() for alias in details_ids(row)}
        details = omkar_results(api_key, "business-details", {"data_id": ",".join(by_lookup)})
        matched = {}
        for detail in details:
            matches = {aliases[alias] for alias in details_ids(detail) & aliases.keys()}
            if len(matches) != 1:
                raise CollectionError("omkar_unmatched_details")
            lookup = matches.pop()
            if lookup in matched:
                raise CollectionError("omkar_duplicate_details")
            matched[lookup] = detail
        if matched.keys() != by_lookup.keys():
            raise CollectionError("omkar_incomplete_details")
        for lookup, (key, search_row) in by_lookup.items():
            place = search_row | matched[lookup]
            place["images"] = keep_search_image_labels(place.get("images"), search_row.get("images"))
            image_count = place.get("image_count")
            if place.get("images") in (None, []) and type(image_count) is int and image_count > 0:
                # The details endpoint can report a gallery while leaving images empty.
                time.sleep(0.55)
                place["images"] = omkar_results(
                    api_key, "business-photos", {"data_id": lookup, "limit": PHOTO_LIMIT},
                )
                if len(place["images"]) > PHOTO_LIMIT:
                    raise CollectionError("omkar_photos_exceed_limit")
            yield project_place(place, key)


def github_run(mode):
    repository = required("GITHUB_REPOSITORY")
    run_id = int(required("GITHUB_RUN_ID"))
    response = request_json(
        "github", f"https://api.github.com/repos/{repository}/actions/runs/{run_id}",
        {"Authorization": f"Bearer {required('GITHUB_TOKEN')}", "X-GitHub-Api-Version": "2022-11-28"},
    )
    try:
        timestamp = response["created_at"]
        if not isinstance(timestamp, str):
            raise ValueError
        created_at = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
        if created_at.tzinfo is None:
            raise ValueError
    except (KeyError, TypeError, ValueError):
        raise CollectionError("github_invalid_run_timestamp") from None
    local_date = created_at.astimezone(VIETNAM).date()
    return {
        "p_week_start": (local_date - timedelta(days=local_date.weekday())).isoformat(),
        "p_mode": mode,
        "p_query": QUERY,
        "p_result_limit": LIMITS[mode],
        "p_github_repository": repository,
        "p_github_run_id": run_id,
        "p_github_run_attempt": int(required("GITHUB_RUN_ATTEMPT")),
        "p_github_sha": required("GITHUB_SHA"),
        "p_github_event": required("GITHUB_EVENT_NAME"),
        "p_workflow_created_at": created_at.isoformat(),
    }


def supabase_rpc(url, key, function, payload):
    result = request_json("supabase", f"{url}/rest/v1/rpc/{function}", {"apikey": key}, payload)
    if (not isinstance(result, dict) or type(result.get("run_id")) is not int
            or result["run_id"] <= 0
            or result.get("status") not in ("running", "succeeded", "failed", "skipped")
            or type(result.get("row_count")) is not int):
        raise CollectionError("supabase_invalid_rpc_response")
    return result


def emit_published_run(url, key, result):
    output_path = os.getenv("GITHUB_OUTPUT")
    if not output_path:
        return
    if result["status"] == "skipped":
        result = supabase_rpc(url, key, "published_bronze_run", {"p_run_id": result["run_id"]})
    if result["status"] != "succeeded" or result["row_count"] <= 0:
        raise CollectionError("supabase_missing_published_snapshot")
    with open(output_path, "a", encoding="utf-8") as output:
        output.write(f"bronze_run_id={result['run_id']}\n")


def run(mode):
    url = hosted_supabase_url(required("SUPABASE_URL"))
    key = required("SUPABASE_SECRET_KEY")
    if not key.startswith("sb_secret_"):
        raise CollectionError("invalid_supabase_secret_key")

    metadata = github_run(mode)
    attempt = supabase_rpc(url, key, "begin_bronze_collection", metadata)
    run_id = attempt["run_id"]
    if attempt["status"] != "running":
        print(f"Supabase Bronze run {run_id} is {attempt['status']}.", flush=True)
        if attempt["status"] == "failed":
            return 1
        if mode == "weekly":
            emit_published_run(url, key, attempt)
        return 0

    print(f"Supabase Bronze run {run_id} started for {metadata['p_week_start']} ({mode}).", flush=True)
    finish = {
        "p_run_id": run_id,
        "p_records": None,
        "p_source_count": None,
        "p_collected_at": None,
        "p_error_code": None,
    }
    records = []
    try:
        api_key = required("OMKAR_API_KEY")
        rows = omkar_results(api_key, "search", {"query": QUERY, "limit": LIMITS[mode]})
        if len(rows) > LIMITS[mode]:
            raise CollectionError("omkar_search_exceeds_limit")
        finish["p_source_count"] = len(rows)
        if not rows:
            raise CollectionError("omkar_empty_search")
        records = list(collect_details(api_key, rows))
        finish["p_records"] = records
        finish["p_collected_at"] = datetime.now(timezone.utc).isoformat()
        result = supabase_rpc(url, key, "finish_bronze_collection", finish)
    except Exception as exc:
        error = exc if isinstance(exc, CollectionError) else CollectionError("unexpected_error")
        finish.update(p_records=None, p_collected_at=None, p_error_code=str(error))
        try:
            result = supabase_rpc(url, key, "finish_bronze_collection", finish)
        except CollectionError as report_error:
            print(f"Could not confirm status for Supabase Bronze run {run_id}: {report_error}.", file=sys.stderr)
            raise error from None
        if result["status"] == "failed":
            raise error from None

    if result["status"] not in ("succeeded", "skipped"):
        raise CollectionError("supabase_unexpected_run_status")
    print(f"Supabase Bronze run {run_id} {result['status']} with {result['row_count']} snapshot rows.", flush=True)
    if result["status"] == "succeeded" and records:
        missing = [column for column in SOURCE_FIELDS if all(row[column] in (None, [], {}) for row in records)]
        if missing:
            print("Fields with no values in this run: " + ", ".join(missing), flush=True)
        photos = [photo for row in records for photo in row["images"] or []]
        labeled = sum(isinstance(photo, dict) and isinstance(photo.get("about"), str)
                      and bool(photo["about"].strip()) for photo in photos)
        print(f"Images with a source label: {labeled}/{len(photos)}.", flush=True)
    if mode == "weekly":
        emit_published_run(url, key, result)
    return 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=LIMITS, required=True)
    try:
        return run(parser.parse_args().mode)
    except CollectionError as exc:
        print(f"Bronze collection error: {exc}.", file=sys.stderr)
    except Exception:
        print("Bronze collection error: unexpected_error.", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
