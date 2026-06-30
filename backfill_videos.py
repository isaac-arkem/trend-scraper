#!/usr/bin/env python3
"""
Backfill the missing TikTok clip videos: for every clip with no video_minio_path,
re-fetch the downloadable file via the actor (postURLs + shouldDownloadVideos),
download it, and upload to the trends MinIO bucket so it plays inline.
Slideshows (no video) are skipped. Resumable: only touches clips still missing video.
Run:  .venv/bin/python backfill_videos.py            (all)
      .venv/bin/python backfill_videos.py --limit 5  (test)
"""
import sys
import logging

for _n in ("httpx", "httpcore", "urllib3", "apify_client"):
    logging.getLogger(_n).setLevel(logging.WARNING)

import time
from src.db.client import get_db
from src.apify.client import run_actor
from src.pipeline.trends import _download, _put

BATCH = 50


def log(m):
    print(f"[{time.strftime('%H:%M:%S')}] {m}", flush=True)


def main():
    limit = None
    if "--limit" in sys.argv:
        limit = int(sys.argv[sys.argv.index("--limit") + 1])

    db = get_db()
    rows, start = [], 0
    while True:
        pg = db.table("clips").select("id,platform_post_id,video_url")\
            .eq("platform", "tiktok").is_("video_minio_path", "null")\
            .order("id").range(start, start + 999).execute().data or []
        rows += pg
        if len(pg) < 1000:
            break
        start += 1000
    rows = [r for r in rows if r.get("video_url")]
    if limit:
        rows = rows[:limit]
    by_post = {r["platform_post_id"]: r for r in rows}
    log(f"clips missing video: {len(rows)}")

    saved = slideshow = nofile = 0
    for i in range(0, len(rows), BATCH):
        batch = rows[i:i + BATCH]
        items = run_actor("clockworks/tiktok-scraper",
                          {"postURLs": [r["video_url"] for r in batch], "shouldDownloadVideos": True},
                          label="backfill")
        for it in items:
            pid = str(it.get("id", ""))
            r = by_post.get(pid)
            if not r:
                continue
            if it.get("isSlideshow"):
                slideshow += 1
                continue
            dl = (it.get("mediaUrls") or [None])[0]
            if not dl:
                nofile += 1
                continue
            vid = _download(dl)
            if not vid:
                nofile += 1
                continue
            path = f"tiktok/clip/{pid}.mp4"
            if _put(path, vid, "video/mp4"):
                db.table("clips").update({"video_minio_path": path}).eq("id", r["id"]).execute()
                saved += 1
        log(f"  {min(i+BATCH,len(rows))}/{len(rows)} processed | +{saved} downloaded | {slideshow} slideshows | {nofile} no-file")
    log(f"DONE — {saved} videos backfilled, {slideshow} slideshows (no video), {nofile} unavailable")


if __name__ == "__main__":
    main()
