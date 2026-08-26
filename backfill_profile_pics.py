"""Fetch the missing profile pictures for reference accounts. Nothing else.

380 of 471 rows in reference_accounts have profile_pic_url NULL — mostly the
dance-trends accounts, which were merged in by handle and never profile-scraped.
This scrapes only those, mirrors the picture into object storage on the path the
dashboard already builds, and writes profile_pic_url + platform_user_id back to
the row.

Deliberately narrow: it does not touch posts, clips, niches, or any account that
already has a picture. Profiles are billed per result, so re-fetching a face we
already hold is the easiest money to waste.

    python backfill_profile_pics.py --dry-run
    python backfill_profile_pics.py --topic music_dance
    python backfill_profile_pics.py --limit 20      # a slice first
    python backfill_profile_pics.py                 # all 380
"""
import argparse
import collections
import json
import sys
import time
from datetime import datetime, timezone

from dotenv import load_dotenv

load_dotenv()

from src.apify.instagram import scrape_profiles as ig_profiles   # noqa: E402
from src.apify.tiktok import scrape_profiles as tt_profiles      # noqa: E402
from src.db.client import get_db                                 # noqa: E402
from src.storage.minio import profile_pic_path, upload_bytes     # noqa: E402
import src.run_archive as A                                      # noqa: E402
from src.utils.logger import get_logger                          # noqa: E402

log = get_logger(__name__)
BATCH = 25


def parse_args():
    p = argparse.ArgumentParser(description="Backfill missing reference-account profile pictures.")
    p.add_argument("--topic", default="", help="Only this niche (default: all)")
    p.add_argument("--platform", default="", choices=["", "tiktok", "instagram"])
    p.add_argument("--limit", type=int, default=0, help="Cap accounts processed (0 = no cap)")
    p.add_argument("--batch", type=int, default=BATCH, help="Handles per actor call")
    p.add_argument("--include-inactive", action="store_true")
    p.add_argument("--dry-run", action="store_true")
    return p.parse_args()


def main():
    a = parse_args()
    db = get_db()
    run_key = f"picbackfill-{datetime.now(timezone.utc):%Y%m%d-%H%M%S}"

    # "Missing" means no file in storage — not merely a null column. A row can
    # carry a profile_pic_url that 403s because TikTok's avatar links are signed
    # with an x-expires stamp; every one tested had already lapsed. Selecting on
    # the column alone skipped exactly the accounts that need re-fetching.
    q = (db.table("reference_accounts")
         .select("id,platform,handle,topic,region,active,profile_pic_url,platform_user_id"))
    if a.topic:
        q = q.eq("topic", a.topic)
    if a.platform:
        q = q.eq("platform", a.platform)
    if not a.include_inactive:
        q = q.eq("active", True)
    rows = q.order("id").execute().data or []

    from src.storage.minio import get_minio, resolve, MEDIA_LOGICAL
    mc = get_minio()
    bucket, prefix = resolve(MEDIA_LOGICAL, "profiles/")
    held = {o.object_name for o in mc.list_objects(bucket, prefix=prefix, recursive=True)}
    missing = [r for r in rows
               if not (r.get("platform_user_id")
                       and resolve(MEDIA_LOGICAL,
                                   profile_pic_path(r["platform"], r["platform_user_id"]))[1] in held)]
    log.info(f"  {len(rows)} accounts, {len(held)} pictures already in storage")

    log.info(f"[{run_key}] {len(missing)} accounts with no STORED picture")
    for t, n in collections.Counter(r.get("topic") or "?" for r in missing).most_common(12):
        log.info(f"    {t:22s} {n}")
    if a.limit:
        missing = missing[:a.limit]
        log.info(f"  limited to {len(missing)}")
    if a.dry_run:
        log.info("  dry run — nothing fetched")
        return

    by_plat = collections.defaultdict(list)
    for r in missing:
        by_plat[r["platform"]].append(r)

    t0 = time.time()
    totals = collections.Counter()

    for plat, rows in sorted(by_plat.items()):
        scraper = ig_profiles if plat == "instagram" else tt_profiles
        log.info(f"── {plat}: {len(rows)} accounts")
        for i in range(0, len(rows), a.batch):
            chunk = rows[i:i + a.batch]
            by_handle = {r["handle"].lower(): r for r in chunk}
            try:
                got = scraper(list(by_handle))
            except Exception as e:
                log.warning(f"   batch failed: {str(e)[:110]}")
                totals["failed"] += len(chunk)
                continue

            for p in got:
                uname = (p.get("username") or "").lower()
                row = by_handle.get(uname)
                if not row:
                    continue
                uid = p.get("platform_user_id") or uname
                src = p.get("profile_pic_url") or p.get("profile_pic")
                path = profile_pic_path(plat, uid)

                stored = None
                if src:
                    try:
                        img = A.download(src)
                        if img:
                            stored = upload_bytes(img, path, "image/jpeg")
                    except Exception as e:
                        log.warning(f"   {uname}: image fetch failed — {str(e)[:70]}")

                # The row is updated whether or not the image downloaded: the
                # source URL and the user id are both worth keeping, and a
                # retry can pick the image up later without re-scraping.
                upd = {"platform_user_id": uid}
                if src:
                    upd["profile_pic_url"] = src
                db.table("reference_accounts").update(upd).eq("id", row["id"]).execute()

                totals["updated"] += 1
                totals["stored"] += 1 if stored else 0
                if not src:
                    totals["no_pic_url"] += 1

            missed = set(by_handle) - {(p.get("username") or "").lower() for p in got}
            totals["not_returned"] += len(missed)
            log.info(f"   {min(i + a.batch, len(rows))}/{len(rows)} — "
                     f"{len(got)} profiles, {totals['stored']} pictures stored"
                     + (f", {len(missed)} not returned" if missed else ""))

    summary = {"run_key": run_key, "totals": dict(totals),
               "minutes": round((time.time() - t0) / 60, 1),
               "finished_at": datetime.now(timezone.utc).isoformat()}
    A.put_json(f"runs/backfill/{datetime.now(timezone.utc):%Y/%m/%d}/{run_key}.json", summary)
    with open(f"run-{run_key}.json", "w") as fh:
        json.dump(summary, fh, indent=2, ensure_ascii=False)

    log.info(f"━━ DONE — {totals['updated']} rows updated, {totals['stored']} pictures in storage, "
             f"{totals['not_returned']} not returned by the actor, {totals['failed']} failed "
             f"in {summary['minutes']}m ━━")


if __name__ == "__main__":
    main()
