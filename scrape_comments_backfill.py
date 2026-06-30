#!/usr/bin/env python3
"""
Backfill the comments table with each creator's OWN replies (as decided: 5 posts
per creator, only comments where the commenter == the creator). Instagram only
(TikTok needs a different actor). Batches many post URLs per actor run for speed.
Run:  .venv/bin/python scrape_comments_backfill.py            (all IG creators)
      .venv/bin/python scrape_comments_backfill.py --limit 20 (test)
"""
import sys
import time
import logging

for _n in ("httpx", "httpcore", "urllib3", "apify_client"):
    logging.getLogger(_n).setLevel(logging.WARNING)

from src.db.client import get_db
from src.apify.client import run_actor

POSTS_PER_CREATOR = 5
CHUNK = 100             # post URLs per comment-scraper run
RESULTS_PER_POST = 40   # max comments pulled per post


def log(m):
    print(f"[{time.strftime('%H:%M:%S')}] {m}", flush=True)


def main():
    limit = None
    if "--limit" in sys.argv:
        limit = int(sys.argv[sys.argv.index("--limit") + 1])

    db = get_db()
    creators, start = [], 0
    while True:
        pg = db.table("creators").select("id,username").eq("platform", "instagram")\
            .order("id").range(start, start + 999).execute().data or []
        creators += pg
        if len(pg) < 1000:
            break
        start += 1000
    if limit:
        creators = creators[:limit]

    # build post list: 5 recent posts per creator, mapped back to post_id + creator handle
    url_meta = {}   # post_url -> (post_id, creator_username)
    urls = []
    for c in creators:
        posts = db.table("posts").select("id,post_url").eq("creator_id", c["id"])\
            .not_.is_("post_url", "null").order("posted_at", desc=True)\
            .limit(POSTS_PER_CREATOR).execute().data or []
        for p in posts:
            if p["post_url"] not in url_meta:
                url_meta[p["post_url"]] = (p["id"], (c["username"] or "").lower())
                urls.append(p["post_url"])
    log(f"{len(creators)} creators, {len(urls)} posts to scan for creator-replies")

    saved = scanned = 0
    seen = set()
    for i in range(0, len(urls), CHUNK):
        batch = urls[i:i + CHUNK]
        try:
            items = run_actor("apify/instagram-comment-scraper",
                              {"directUrls": batch, "resultsLimit": RESULTS_PER_POST},
                              label=f"comments {i//CHUNK+1}")
        except Exception as e:
            log(f"  chunk error: {str(e)[:80]} — skip")
            continue
        rows = []
        for it in items:
            meta = url_meta.get(it.get("postUrl"))
            if not meta:
                continue
            post_id, cuser = meta
            if (it.get("ownerUsername") or "").lower() != cuser:   # only the creator's own comments
                continue
            if it.get("id") in seen:
                continue
            seen.add(it.get("id"))
            rows.append({
                "post_id": post_id,
                "commenter_username": (it.get("ownerUsername") or "").lower(),
                "commenter_platform_id": str((it.get("owner") or {}).get("id", "")),
                "text": it.get("text", ""),
                "likes": it.get("likesCount") or 0,
            })
        for j in range(0, len(rows), 200):
            db.table("comments").insert(rows[j:j+200]).execute()
        saved += len(rows)
        scanned = min(i + CHUNK, len(urls))
        log(f"  {scanned}/{len(urls)} posts scanned | {saved} creator-replies stored")

    log(f"DONE — {saved} creator-replies stored from {len(urls)} posts across {len(creators)} creators")


if __name__ == "__main__":
    main()
