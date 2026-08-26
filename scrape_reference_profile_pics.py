#!/usr/bin/env python3
"""
Scrape profile pics (+ followers, full_name, platform_user_id) for the reference
accounts and mirror the pics to MinIO — exactly like the creator profile pics.
Stored at profiles/<platform>/<platform_user_id>.jpg in the social-intel bucket.
Run AFTER the ALTER:  .venv/bin/python scrape_reference_profile_pics.py
"""
import io
import os
import time
import logging

import httpx

for _n in ("httpx", "httpcore", "urllib3", "apify_client"):
    logging.getLogger(_n).setLevel(logging.WARNING)

from src.db.client import get_db
from src.storage.minio import get_minio, profile_pic_path, resolve, MEDIA_LOGICAL
from src.apify import instagram as ig, tiktok as tt

# Routed through resolve() so this follows OBJECT_STORE like everything else.
# Addressing MINIO_BUCKET directly kept writing to whatever bucket that env var
# named, which after the Wasabi move was either the wrong store or a bucket the
# key cannot write to — failing silently in the same way the clip uploads did.
BUCKET = MEDIA_LOGICAL
BATCH = 100
DL_HEADERS = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
              "Referer": "https://www.instagram.com/"}


def log(m):
    print(f"[{time.strftime('%H:%M:%S')}] {m}", flush=True)


def download(url):
    try:
        r = httpx.get(url, timeout=20, follow_redirects=True, headers=DL_HEADERS)
        r.raise_for_status()
        if r.headers.get("content-type", "").startswith("image"):
            return r.content
    except Exception:
        pass
    return None


def main():
    db, mc = get_db(), get_minio()
    accts = db.table("reference_accounts").select("id,platform,handle").execute().data or []

    for platform in ("tiktok", "instagram"):
        subset = [a for a in accts if a["platform"] == platform]
        handles = [a["handle"] for a in subset]
        if not handles:
            continue
        prof = {}
        for i in range(0, len(handles), BATCH):
            b = handles[i:i + BATCH]
            profs = tt.scrape_profiles(b) if platform == "tiktok" else ig.scrape_profiles(b)
            for p in profs:
                if p.get("username"):
                    prof[p["username"].lower()] = p

        saved = missing = 0
        for a in subset:
            p = prof.get(a["handle"].lower())
            upd = {}
            if p:
                upd = {"platform_user_id": p.get("platform_user_id"),
                       "followers": p.get("followers"),
                       "full_name": p.get("full_name"),
                       "profile_pic_url": p.get("profile_pic_url")}
                url = p.get("profile_pic_url")
                if url:
                    img = download(url)
                    if img:
                        key = p.get("platform_user_id") or a["handle"]
                        _b, _k = resolve(BUCKET, profile_pic_path(platform, key))
                        mc.put_object(_b, _k,
                                      io.BytesIO(img), length=len(img), content_type="image/jpeg")
                        saved += 1
                    else:
                        missing += 1
                else:
                    missing += 1
                db.table("reference_accounts").update(upd).eq("id", a["id"]).execute()
            else:
                missing += 1
        log(f"{platform}: {saved} profile pics mirrored to MinIO, {missing} unavailable")

    log("DONE")


if __name__ == "__main__":
    main()
