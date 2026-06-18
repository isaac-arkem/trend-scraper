#!/usr/bin/env python3
"""
Re-scrape every creator's profile picture (Instagram + TikTok) and mirror it to
MinIO so it stays available after the original CDN URLs expire.

Stored at a deterministic path: profiles/<platform>/<platform_user_id>.jpg — the
dashboard builds the same path from the creator row, so no DB column is needed.

Resumable: skips any creator whose profile pic is already in MinIO.
Run:  .venv/bin/python scrape_profile_pics.py            (all platforms)
      .venv/bin/python scrape_profile_pics.py instagram  (one platform)
      .venv/bin/python scrape_profile_pics.py --limit 5  (small test batch)
"""
import io
import os
import sys
import time
import logging
import httpx

for _n in ("httpx", "httpcore", "urllib3", "apify_client"):
    logging.getLogger(_n).setLevel(logging.WARNING)

from src.db.client import get_db
from src.storage.minio import get_minio, profile_pic_path
from src.apify import instagram as ig, tiktok as tt

BUCKET = os.environ.get("MINIO_BUCKET", "social-intel")
BATCH = 100  # usernames per Apify actor run
DL_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
    "Referer": "https://www.instagram.com/",
}


def log(m):
    print(f"[{time.strftime('%H:%M:%S')}] {m}", flush=True)


def creator_key(c: dict) -> str:
    return c.get("platform_user_id") or c["username"]


def exists_in_minio(mc, path: str) -> bool:
    try:
        mc.stat_object(BUCKET, path)
        return True
    except Exception:
        return False


def download(url: str) -> bytes | None:
    try:
        r = httpx.get(url, timeout=20, follow_redirects=True, headers=DL_HEADERS)
        r.raise_for_status()
        if r.headers.get("content-type", "").startswith("image"):
            return r.content
    except Exception as e:
        log(f"  download failed: {str(e)[:70]}")
    return None


def load_creators(db, platform: str) -> list[dict]:
    out, start = [], 0
    while True:
        page = db.table("creators").select("id,platform,username,platform_user_id")\
            .eq("platform", platform).order("id").range(start, start + 999).execute().data or []
        out += page
        if len(page) < 1000:
            break
        start += 1000
    return out


def scrape_profiles(platform: str, usernames: list[str]) -> dict:
    profiles = ig.scrape_profiles(usernames) if platform == "instagram" else tt.scrape_profiles(usernames)
    return {p["username"]: p for p in profiles if p.get("username")}


def run_platform(db, mc, platform: str, limit: int = None):
    creators = load_creators(db, platform)
    todo = [c for c in creators
            if not exists_in_minio(mc, profile_pic_path(platform, creator_key(c)))]
    if limit:
        todo = todo[:limit]
    log(f"{platform}: {len(creators)} creators, {len(todo)} need a profile pic")

    saved = missing = 0
    for i in range(0, len(todo), BATCH):
        batch = todo[i:i + BATCH]
        by_user = scrape_profiles(platform, [c["username"] for c in batch])
        for c in batch:
            p = by_user.get(c["username"])
            url = p.get("profile_pic_url") if p else None
            if not url:
                missing += 1
                continue
            img = download(url)
            if not img:
                missing += 1
                continue
            path = profile_pic_path(platform, creator_key(c))
            mc.put_object(BUCKET, path, io.BytesIO(img), length=len(img), content_type="image/jpeg")
            db.table("creators").update({"profile_pic_url": url}).eq("id", c["id"]).execute()
            saved += 1
        log(f"  {platform}: {saved} saved, {missing} missing ({i+len(batch)}/{len(todo)} processed)")
    log(f"== {platform} DONE: {saved} profile pics mirrored, {missing} unavailable ==")


def main():
    limit = None
    argv = sys.argv[1:]
    if "--limit" in argv:
        idx = argv.index("--limit")
        limit = int(argv[idx + 1])
        del argv[idx:idx + 2]  # drop flag + its value so it's not read as a platform
    platforms = [a for a in argv if not a.startswith("--")] or ["instagram", "tiktok"]

    db, mc = get_db(), get_minio()
    for platform in platforms:
        run_platform(db, mc, platform, limit=limit)


if __name__ == "__main__":
    main()
