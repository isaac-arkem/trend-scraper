"""Per-run archive in MinIO — every scrape leaves a full paper trail.

If a run goes wrong, or a column turns out to be parsed badly, or someone asks
"where did this number come from six weeks ago", the answer has to be on disk.
Re-scraping to find out is the expensive mistake: the API charges again and the
platform has already moved on, so the old answer is simply gone.

Layout, dated so a run is findable without knowing its key:

    trends/runs/<platform>/<YYYY>/<MM>/<DD>/<COUNTRY>/
        manifest.json               what ran, with what settings, and the totals
        raw-hashtag-scrape.json     untouched actor output — the recovery copy
        clips.json                  normalised rows as written to clips
        board.json                  the trend_signals rows for this country
        profiles/<handle>.json      one file per creator profile fetched

Profile PICTURES stay on the existing convention instead —
    social-intel/profiles/<platform>/<platform_user_id>.jpg
— because the dashboard already builds exactly that path to display them
(src/storage/minio.py:profile_pic_path). A second location would mean the images
exist but never render.
"""
import io
import json
import os
from datetime import datetime, timezone

import httpx

from src.storage.minio import get_minio, profile_pic_path
from src.utils.logger import get_logger

log = get_logger(__name__)

RUNS_BUCKET  = os.environ.get("TRENDS_BUCKET", "trends")
MEDIA_BUCKET = os.environ.get("MINIO_BUCKET", "social-intel")

_DL_HEADERS = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                             "AppleWebKit/537.36"}


def run_prefix(platform: str, country: str, when: datetime = None) -> str:
    d = when or datetime.now(timezone.utc)
    return f"runs/{platform}/{d:%Y/%m/%d}/{(country or 'XX').upper()}"


def _ensure(bucket: str) -> None:
    mc = get_minio()
    try:
        if not mc.bucket_exists(bucket):
            mc.make_bucket(bucket)
            log.info(f"created MinIO bucket '{bucket}'")
    except Exception as e:
        log.warning(f"bucket check failed for {bucket}: {str(e)[:80]}")


def put_json(path: str, obj, bucket: str = None) -> str:
    """Write a JSON document. default=str so datetimes never break an archive."""
    bucket = bucket or RUNS_BUCKET
    _ensure(bucket)
    data = json.dumps(obj, ensure_ascii=False, indent=2, default=str).encode("utf-8")
    try:
        get_minio().put_object(bucket, path, io.BytesIO(data), length=len(data),
                               content_type="application/json")
        return path
    except Exception as e:
        log.warning(f"archive write failed ({path}): {str(e)[:90]}")
        return None


def put_bytes(path: str, data: bytes, content_type: str, bucket: str = None) -> str:
    bucket = bucket or MEDIA_BUCKET
    _ensure(bucket)
    try:
        get_minio().put_object(bucket, path, io.BytesIO(data), length=len(data),
                               content_type=content_type)
        return path
    except Exception as e:
        log.warning(f"upload failed ({path}): {str(e)[:90]}")
        return None


def exists(path: str, bucket: str) -> bool:
    try:
        get_minio().stat_object(bucket, path)
        return True
    except Exception:
        return False


def download(url: str) -> bytes:
    if not url:
        return None
    try:
        r = httpx.get(url, timeout=30, follow_redirects=True, headers=_DL_HEADERS)
        r.raise_for_status()
        return r.content
    except Exception as e:
        log.debug(f"download failed: {str(e)[:70]}")
        return None


# ── creator profiles ────────────────────────────────────────────────────────

def fetch_and_store_profiles(handles, platform, country, run_key, when=None,
                             batch=50, skip_existing=True):
    """Fetch creator profiles, mirror the picture to MinIO, archive the JSON.

    Pictures are keyed by platform_user_id so they survive a handle change, and
    saved to the same path the dashboard already reads. Profiles we already hold
    a picture for are skipped by default — this is per-result billed, and paying
    twice for the same face is the easiest waste to avoid.
    """
    from src.apify.tiktok import scrape_profiles as tt_profiles
    from src.apify.instagram import scrape_profiles as ig_profiles

    handles = [h for h in {(h or "").lower().lstrip("@") for h in handles} if h]
    if not handles:
        return {"requested": 0, "fetched": 0, "pics": 0, "skipped": 0}

    prefix = run_prefix(platform, country, when)
    scraper = tt_profiles if platform == "tiktok" else ig_profiles
    stats = {"requested": len(handles), "fetched": 0, "pics": 0, "skipped": 0}
    got = []

    for i in range(0, len(handles), batch):
        chunk = handles[i:i + batch]
        try:
            got += scraper(chunk)
        except Exception as e:
            log.warning(f"profile scrape failed ({platform} {chunk[:2]}…): {str(e)[:90]}")

    for p in got:
        uname = (p.get("username") or "").lower()
        if not uname:
            continue
        stats["fetched"] += 1

        # the raw profile record, one file each — this is the recovery copy
        put_json(f"{prefix}/profiles/{uname}.json",
                 {"run_key": run_key, "platform": platform, "country": country,
                  "fetched_at": datetime.now(timezone.utc).isoformat(), "profile": p})

        key = p.get("platform_user_id") or uname
        path = profile_pic_path(platform, key)
        if skip_existing and exists(path, MEDIA_BUCKET):
            stats["skipped"] += 1
            continue
        img = download(p.get("profile_pic") or p.get("profile_pic_url"))
        if img and put_bytes(path, img, "image/jpeg", MEDIA_BUCKET):
            stats["pics"] += 1

    # ── straight into the database, in the same pass.
    # Archiving alone left the platform with a leaderboard of bare handles: the
    # names, follower counts and avatar paths existed only as JSON in MinIO, so
    # nothing could be rendered or queried. Write them where they are needed.
    rows = []
    for p in got:
        uname = (p.get("username") or "").lower()
        if not uname:
            continue
        uid = p.get("platform_user_id") or uname
        rows.append({
            "platform": platform,
            "username": uname,
            "platform_user_id": p.get("platform_user_id"),
            "display_name": p.get("full_name"),
            "bio": p.get("bio"),
            "followers": p.get("followers"),
            "following": p.get("following"),
            "post_count": p.get("post_count"),
            "is_verified": bool(p.get("is_verified")),
            "profile_url": p.get("profile_url") or f"https://www.tiktok.com/@{uname}",
            "profile_pic_url": p.get("profile_pic_url"),
            "avatar_path": profile_pic_path(platform, uid),
            "country_code": country,
            "last_seen_at": datetime.now(timezone.utc).isoformat(),
        })
    if rows:
        try:
            from src.db.client import upsert
            for i in range(0, len(rows), 200):
                upsert("trend_creators", rows[i:i + 200],
                       on_conflict="platform,username")
            stats["stored"] = len(rows)
        except Exception as e:
            log.warning(f"trend_creators upsert failed: {str(e)[:100]}")
            stats["stored"] = 0

    return stats


# ── run manifest ────────────────────────────────────────────────────────────

def write_manifest(platform, country, run_key, settings, totals, when=None):
    prefix = run_prefix(platform, country, when)
    return put_json(f"{prefix}/manifest.json", {
        "run_key": run_key,
        "platform": platform,
        "country": country,
        "written_at": datetime.now(timezone.utc).isoformat(),
        "settings": settings,
        "totals": totals,
        "paths": {
            "raw":      f"{prefix}/raw-hashtag-scrape.json",
            "clips":    f"{prefix}/clips.json",
            "board":    f"{prefix}/board.json",
            "profiles": f"{prefix}/profiles/",
        },
    })
