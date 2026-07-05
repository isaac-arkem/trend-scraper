#!/usr/bin/env python3
"""
Schedule runner — checks dance_scrape_schedules and reference_scrape_schedules
for due jobs and triggers the Jenkins scraper pipeline for each one.

Run by Jenkinsfile.scheduler on a cron trigger (every minute).

Jenkins env vars required:
  JENKINS_URL       — base URL of the Jenkins server (e.g. http://localhost:8080)
  JENKINS_USER      — Jenkins username
  JENKINS_API_TOKEN — Jenkins API token
  JENKINS_JOB_NAME  — job path to trigger (e.g. trend-scraper)
"""
import os
import time
import base64
import logging
from datetime import datetime, timezone

from croniter import croniter
from src.db.client import get_db

logging.basicConfig(level=logging.INFO, format="[%(asctime)s] %(message)s", datefmt="%H:%M:%S")
log = logging.getLogger(__name__)


def now_utc():
    return datetime.now(timezone.utc)


def trigger_jenkins(jenkins_base, auth_header, job_name, params):
    url = f"{jenkins_base}/job/{job_name}/buildWithParameters"
    import urllib.request, urllib.parse
    data = urllib.parse.urlencode(params).encode()
    req = urllib.request.Request(url, data=data, method="POST")
    req.add_header("Authorization", f"Basic {auth_header}")
    req.add_header("Content-Type", "application/x-www-form-urlencoded")
    try:
        with urllib.request.urlopen(req) as res:
            return res.status, res.headers.get("Location")
    except urllib.error.HTTPError as e:
        # Jenkins often returns 201 or redirects — treat those as success
        if e.code in (201, 302, 303):
            return e.code, e.headers.get("Location")
        raise


def next_run_from_cron(cron_expr, after):
    """Compute the next run time after `after` using the cron expression."""
    it = croniter(cron_expr, after)
    return it.get_next(datetime).replace(tzinfo=timezone.utc)


def process_dance_schedules(db, jenkins_base, auth_header, job_name):
    now = now_utc()
    rows = (
        db.table("dance_scrape_schedules")
        .select("*")
        .eq("active", True)
        .lte("next_run_at", now.isoformat())
        .execute()
        .data or []
    )

    log.info(f"Dance schedules due: {len(rows)}")

    for row in rows:
        schedule_id = row["id"]
        log.info(f"  Triggering dance schedule {row['name']} ({schedule_id})")

        # insert a pending request row
        req = (
            db.table("dance_scrape_requests")
            .insert({
                "status": "pending",
                "schedule_id": schedule_id,
                "markets": row.get("markets") or [],
                "feed_ids": row.get("feed_ids") or [],
                "min_views": row.get("min_views") or 20000,
                "recency_days": row.get("recency_days") or 14,
            })
            .select("id")
            .single()
            .execute()
            .data
        )
        request_id = req["id"]

        # resolve feed slugs + tags from dance_feeds
        feed_ids = row.get("feed_ids") or []
        feeds = []
        if feed_ids:
            feeds = db.table("dance_feeds").select("slug,tags").in_("id", feed_ids).execute().data or []
        feed_slugs = ",".join(f["slug"] for f in feeds)
        all_tags = ",".join(t for f in feeds for t in (f.get("tags") or []))

        params = {
            "SCRAPER_TYPE": "dance_trends",
            "REQUEST_ID": request_id,
            "MARKETS": ",".join(row.get("markets") or []),
            "FEEDS": feed_slugs,
            "TAGS": all_tags,
            "MIN_VIEWS": str(row.get("min_views") or 20000),
            "RECENCY_DAYS": str(row.get("recency_days") or 14),
        }

        try:
            status, location = trigger_jenkins(jenkins_base, auth_header, job_name, params)
            log.info(f"    Jenkins responded {status} location={location}")
            db.table("dance_scrape_requests").update({
                "status": "triggered",
                "build_url": location,
                "triggered_at": now_utc().isoformat(),
            }).eq("id", request_id).execute()
        except Exception as e:
            log.error(f"    Failed to trigger Jenkins: {e}")
            db.table("dance_scrape_requests").update({
                "status": "failed",
                "error_message": str(e)[:500],
            }).eq("id", request_id).execute()

        # update next_run_at or deactivate
        _advance_schedule(db, "dance_scrape_schedules", row)


def process_reference_schedules(db, jenkins_base, auth_header, job_name):
    now = now_utc()
    rows = (
        db.table("reference_scrape_schedules")
        .select("*")
        .eq("active", True)
        .lte("next_run_at", now.isoformat())
        .execute()
        .data or []
    )

    log.info(f"Reference schedules due: {len(rows)}")

    for row in rows:
        schedule_id = row["id"]
        log.info(f"  Triggering reference schedule {row['name']} ({schedule_id})")

        # resolve handles from account_ids
        account_ids = row.get("account_ids") or []
        handles = []
        if account_ids:
            accts = db.table("reference_accounts").select("handle").in_("id", account_ids).execute().data or []
            handles = [a["handle"] for a in accts]

        req = (
            db.table("reference_scrape_requests")
            .insert({
                "status": "pending",
                "schedule_id": schedule_id,
                "handles": handles or None,
            })
            .select("id")
            .single()
            .execute()
            .data
        )
        request_id = req["id"]

        params = {
            "SCRAPER_TYPE": "reference_profiles",
            "REQUEST_ID": request_id,
        }
        if handles:
            params["HANDLES"] = ",".join(handles)

        try:
            status, location = trigger_jenkins(jenkins_base, auth_header, job_name, params)
            log.info(f"    Jenkins responded {status} location={location}")
            db.table("reference_scrape_requests").update({
                "status": "triggered",
                "build_url": location,
                "triggered_at": now_utc().isoformat(),
            }).eq("id", request_id).execute()
        except Exception as e:
            log.error(f"    Failed to trigger Jenkins: {e}")
            db.table("reference_scrape_requests").update({
                "status": "failed",
                "error_message": str(e)[:500],
            }).eq("id", request_id).execute()

        _advance_schedule(db, "reference_scrape_schedules", row)


def _advance_schedule(db, table, row):
    """Move next_run_at forward for recurring, deactivate for one_time."""
    if row.get("schedule_type") == "one_time":
        db.table(table).update({"active": False}).eq("id", row["id"]).execute()
        log.info(f"    One-time schedule {row['id']} deactivated.")
        return

    cron = row.get("cron")
    if not cron:
        log.warning(f"    Recurring schedule {row['id']} has no cron — skipping advance.")
        return

    try:
        next_run = next_run_from_cron(cron, now_utc())
        db.table(table).update({
            "next_run_at": next_run.isoformat(),
            "last_run_at": now_utc().isoformat(),
        }).eq("id", row["id"]).execute()
        log.info(f"    Next run scheduled for {next_run.isoformat()}")
    except Exception as e:
        log.error(f"    Failed to advance schedule {row['id']}: {e}")


def main():
    jenkins_url   = os.environ["JENKINS_URL"].rstrip("/")
    jenkins_user  = os.environ["JENKINS_USER"]
    jenkins_token = os.environ["JENKINS_API_TOKEN"]
    job_name      = os.environ["JENKINS_JOB_NAME"].strip("/")

    auth_header = base64.b64encode(f"{jenkins_user}:{jenkins_token}".encode()).decode()

    db = get_db()

    t0 = time.time()
    log.info("Scheduler tick started")

    process_dance_schedules(db, jenkins_url, auth_header, job_name)
    process_reference_schedules(db, jenkins_url, auth_header, job_name)

    log.info(f"Scheduler tick done in {time.time() - t0:.1f}s")


if __name__ == "__main__":
    main()
