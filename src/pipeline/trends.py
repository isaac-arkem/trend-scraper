"""Trend intelligence pipeline — scrape blowing-up short-form clips (TikTok +
IG Reels), capture + download their sound and video, cluster by sound, and rank
by velocity. Writes to the sounds / sound_snapshots / clips / trends tables and
stores audio+video in the dedicated 'trends' MinIO bucket."""
import os
import io
import re
import time
import unicodedata
from datetime import datetime, timezone

import httpx

from src.apify.client import run_actor
from src.ai.vision import analyze_image_bytes
from src.db.client import get_db, upsert
from src.storage.minio import get_minio
from src.utils.logger import get_logger

log = get_logger(__name__)

TRENDS_BUCKET = os.environ.get("TRENDS_BUCKET", "trends")
DEFAULT_THRESHOLD = 20_000      # min views/plays to ingest
DEFAULT_RECENCY_DAYS = 14       # only clips posted within this window

DANCE_TAGS = ["dancetok", "dance", "dancechallenge", "dancetrend", "dancechallenge2026",
              "newdancechallenge", "trendingdance", "brazildance", "dance2026"]
HOOK_TAGS = ["blowthisup", "tryit"]
IG_EXTRA_DANCE = ["reels", "reelsdance"]

_DL_HEADERS = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"}


# ── helpers ─────────────────────────────────────────────────────────────────
def _now():
    return datetime.now(timezone.utc)


def _hours_since(iso: str) -> float:
    if not iso:
        return 1e9
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
        return max(0.5, (_now() - dt).total_seconds() / 3600.0)
    except Exception:
        return 1e9


def _canonical(name: str, author: str) -> str:
    """Normalized key so the same track on TikTok and IG clusters into one trend."""
    s = f"{name or ''} {author or ''}".lower()
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode()
    s = re.sub(r"original sound|sonido original|son original", "", s)
    s = re.sub(r"[^a-z0-9]+", " ", s).strip()
    return re.sub(r"\s+", " ", s) or "unknown"


def _download(url: str) -> bytes | None:
    if not url:
        return None
    try:
        r = httpx.get(url, timeout=30, follow_redirects=True, headers=_DL_HEADERS)
        r.raise_for_status()
        return r.content
    except Exception as e:
        log.warning(f"trend download failed: {str(e)[:70]}")
        return None


def _put(path: str, data: bytes, content_type: str) -> str | None:
    try:
        get_minio().put_object(TRENDS_BUCKET, path, io.BytesIO(data), length=len(data),
                               content_type=content_type)
        return path
    except Exception as e:
        log.warning(f"MinIO put failed ({path}): {str(e)[:70]}")
        return None


def _exists(path: str) -> bool:
    try:
        get_minio().stat_object(TRENDS_BUCKET, path)
        return True
    except Exception:
        return False


def classify_subject(cover_url: str) -> str:
    """Run the clip's cover frame through gpt-4o vision to clean the feed to women.
    Returns one of: female / male / child / ad / none / unclear."""
    img = _download(cover_url)
    if not img:
        return "unclear"
    r = analyze_image_bytes(img)
    if not r:
        return "unclear"
    if not r.get("person_visible"):
        return "none"
    if r.get("is_ad_or_product") is True:
        return "ad"
    if r.get("is_child") is True:
        return "child"
    if r.get("person_is_female") is True:
        return "female"
    if r.get("person_is_female") is False:
        return "male"
    return "unclear"


# ── TikTok normalisation ────────────────────────────────────────────────────
def normalize_tiktok(i: dict, feed: str, seed: str, region: str = None, market: str = None) -> dict:
    author = i.get("authorMeta", {}) or {}
    m = i.get("musicMeta", {}) or {}
    vm = i.get("videoMeta", {}) or {}
    views = i.get("playCount") or 0
    posted = i.get("createTimeISO")
    music_id = str(m.get("musicId") or m.get("id") or "")
    return {
        "platform": "tiktok",
        "platform_post_id": str(i.get("id", "")),
        "video_url": i.get("webVideoUrl"),
        "creator_handle": (author.get("name") or author.get("uniqueId") or "").lower(),
        "caption": i.get("text", ""),
        "hashtags": [h.get("name") for h in (i.get("hashtags") or []) if h.get("name")],
        "views": views,
        "likes": i.get("diggCount") or 0,
        "comments": i.get("commentCount") or 0,
        "shares": i.get("shareCount") or 0,
        "saves": i.get("collectCount") or 0,
        "duration_sec": vm.get("duration"),
        "posted_at": posted,
        "velocity": round(views / _hours_since(posted), 2),
        "feed_source": feed,
        "seed_term": seed,
        "region": region,
        "market_code": market,
        "_cover_url": vm.get("coverUrl") or vm.get("originalCoverUrl"),
        "_video_dl": (i.get("mediaUrls") or [None])[0],   # populated when shouldDownloadVideos=True
        "_sound": {
            "platform": "tiktok",
            "platform_sound_id": music_id,
            "name": m.get("musicName"),
            "author": m.get("musicAuthor"),
            "play_url": m.get("playUrl"),
            "sound_page_url": f"https://www.tiktok.com/music/x-{music_id}" if music_id else None,
        },
    }


# ── persistence ─────────────────────────────────────────────────────────────
def _upsert_sound(s: dict) -> str | None:
    """Upsert a sound row, download its audio to MinIO if not already there,
    and write a usage snapshot. Returns the sound row id."""
    if not s.get("platform_sound_id"):
        return None
    db = get_db()
    canonical = _canonical(s.get("name"), s.get("author"))
    audio_path = f"{s['platform']}/sound/{s['platform_sound_id']}.mp3"
    if s.get("play_url") and not _exists(audio_path):
        audio = _download(s["play_url"])
        if audio:
            _put(audio_path, audio, "audio/mpeg")
        else:
            audio_path = None
    elif not _exists(audio_path):
        audio_path = None

    row = {
        "platform": s["platform"],
        "platform_sound_id": s["platform_sound_id"],
        "name": s.get("name"),
        "author": s.get("author"),
        "sound_page_url": s.get("sound_page_url"),
        "canonical_key": canonical,
        "last_seen_at": _now().isoformat(),
    }
    if audio_path:
        row["audio_minio_path"] = audio_path
    res = upsert("sounds", row, on_conflict="platform,platform_sound_id")
    return res[0]["id"] if res else None


def _save_clip(clip: dict, sound_id: str | None) -> dict | None:
    db = get_db()
    video_path = None
    if clip.get("_video_dl"):
        vpath = f"{clip['platform']}/clip/{clip['platform_post_id']}.mp4"
        if not _exists(vpath):
            vid = _download(clip["_video_dl"])
            if vid:
                video_path = _put(vpath, vid, "video/mp4")
        else:
            video_path = vpath

    row = {k: clip[k] for k in (
        "platform", "platform_post_id", "video_url", "creator_handle", "caption",
        "hashtags", "views", "likes", "comments", "shares", "saves", "duration_sec",
        "posted_at", "velocity", "feed_source", "seed_term")}
    row["sound_id"] = sound_id
    if clip.get("region"):
        row["region"] = clip["region"]
    if clip.get("market_code"):
        row["market_code"] = clip["market_code"]
    if clip.get("topic"):
        row["topic"] = clip["topic"]
    if video_path:
        row["video_minio_path"] = video_path
    if clip.get("subject_type"):
        row["subject_type"] = clip["subject_type"]
        row["vision_checked"] = True
        row["suitability_flag"] = clip["subject_type"] == "female"  # single female subject = priority
    res = upsert("clips", row, on_conflict="platform,platform_post_id")
    return res[0] if res else None


def _process_one(c: dict) -> dict | None:
    """Full per-clip work (thread-safe: clients are thread-local): women-filter via
    vision → upsert sound (+download audio) → save clip (+download video).
    If subject_type is already set on the clip (e.g. derived from the creator's
    known appearance), keep it and skip the vision call."""
    if not c.get("subject_type"):
        c["subject_type"] = classify_subject(c.get("_cover_url"))
    s = c.get("_sound") or {}
    sound_id = _upsert_sound(s) if s.get("platform_sound_id") else None
    return _save_clip(c, sound_id)


def process_clips(clips: list[dict], workers: int = 12) -> int:
    """Persist clips concurrently: sound (+audio) → clip (+video), then snapshot
    sound usage counts. Parallel across clips — each does its own vision call +
    MinIO downloads."""
    from concurrent.futures import ThreadPoolExecutor, as_completed
    saved = 0
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = [ex.submit(_process_one, c) for c in clips]
        for f in as_completed(futs):
            try:
                if f.result():
                    saved += 1
            except Exception as e:
                log.warning(f"clip process error: {str(e)[:80]}")
    _snapshot_sound_counts(list({(c.get("_sound") or {}).get("platform_sound_id")
                                 for c in clips if (c.get("_sound") or {}).get("platform_sound_id")}))
    return saved


def _snapshot_sound_counts(platform_sound_ids: list[str]) -> None:
    """Record current observed clip-count per sound (time-series for usage velocity)."""
    if not platform_sound_ids:
        return
    db = get_db()
    sounds = db.table("sounds").select("id").in_("platform_sound_id", platform_sound_ids).execute().data or []
    for s in sounds:
        cnt = db.table("clips").select("id", count="exact").eq("sound_id", s["id"]).limit(1).execute().count or 0
        db.table("sounds").update({"clip_count": cnt}).eq("id", s["id"]).execute()
        db.table("sound_snapshots").insert({"sound_id": s["id"], "clip_count": cnt}).execute()


# ── trend clustering ─────────────────────────────────────────────────────────
def rebuild_trends() -> int:
    """Cluster clips by sound canonical_key (cross-platform) into trends, pick the
    fastest example as the reference clip, and recompute velocity/totals."""
    db = get_db()
    sounds = db.table("sounds").select("id,canonical_key").execute().data or []
    by_key = {}
    for s in sounds:
        by_key.setdefault(s["canonical_key"], []).append(s["id"])

    n = 0
    for key, sound_ids in by_key.items():
        if not key or key == "unknown":
            continue
        clips = []
        for i in range(0, len(sound_ids), 50):
            clips += db.table("clips").select("id,views,velocity,caption,subject_type")\
                .in_("sound_id", sound_ids[i:i+50]).eq("subject_type", "female")\
                .is_("topic", "null").execute().data or []   # exclude reference-account clips
        if not clips:   # no women's clips on this sound → not a trend we surface
            continue
        ref = max(clips, key=lambda c: c.get("velocity") or 0)
        upsert("trends", {
            "canonical_key": key,
            "title": (ref.get("caption") or key)[:80],
            "reference_clip_id": ref["id"],
            "velocity": round(sum(c.get("velocity") or 0 for c in clips), 2),
            "total_views": sum(c.get("views") or 0 for c in clips),
            "total_clips": len(clips),
            "last_seen_at": _now().isoformat(),
        }, on_conflict="canonical_key")
        n += 1

    # Sound-less female clips (e.g. IG hashtag reels — no audio metadata) surface
    # each as its own single-clip trend so they still appear in the Recreate lane.
    noaudio = db.table("clips").select("id,views,velocity,caption")\
        .is_("sound_id", "null").eq("subject_type", "female")\
        .is_("topic", "null").limit(2000).execute().data or []   # exclude reference-account clips
    for c in noaudio:
        upsert("trends", {
            "canonical_key": f"clip:{c['id']}",
            "title": (c.get("caption") or "reel")[:80],
            "reference_clip_id": c["id"],
            "velocity": c.get("velocity") or 0,
            "total_views": c.get("views") or 0,
            "total_clips": 1,
            "last_seen_at": _now().isoformat(),
        }, on_conflict="canonical_key")
        n += 1
    return n


# ── feeds ────────────────────────────────────────────────────────────────────
def scrape_tiktok_feed(terms: list[str], feed: str, region_code: str = "",
                       region_label: str = None, market_code: str = None,
                       per_term: int = 50, threshold: int = DEFAULT_THRESHOLD,
                       recency_days: int = DEFAULT_RECENCY_DAYS) -> list[dict]:
    """Search TikTok by hashtags (optionally geo-targeted via region_code), keep
    clips above the view threshold + recency, tagged with region/market."""
    raw = run_actor("clockworks/tiktok-scraper",
                    {"hashtags": terms, "resultsPerPage": per_term,
                     "shouldDownloadVideos": True, **({"region": region_code} if region_code else {})},
                    label=f"TT:{feed}:{market_code or 'global'}")
    out = []
    for i in raw:
        if (i.get("playCount") or 0) < threshold:
            continue
        if _hours_since(i.get("createTimeISO")) > recency_days * 24:
            continue
        seed = i.get("searchHashtag") or (terms[0] if terms else "")
        out.append(normalize_tiktok(i, feed, str(seed), region=region_label, market=market_code))
    log.info(f"[trends] TikTok {feed} [{market_code or 'global'}]: {len(out)}/{len(raw)} passed threshold")
    return out


def normalize_ig(i: dict, feed: str, seed: str) -> dict:
    mi = i.get("musicInfo") or {}
    views = i.get("videoPlayCount") or i.get("videoViewCount") or 0
    posted = i.get("timestamp")
    audio_id = str(mi.get("audio_id") or "")
    return {
        "platform": "instagram",
        "platform_post_id": i.get("shortCode") or str(i.get("id", "")),
        "video_url": i.get("url"),
        "creator_handle": (i.get("ownerUsername") or "").lower(),
        "caption": i.get("caption", ""),
        "hashtags": i.get("hashtags") or [],
        "views": views,
        "likes": max(0, i.get("likesCount") or 0),   # IG returns -1 when hidden
        "comments": i.get("commentsCount") or 0,
        "shares": 0,
        "saves": 0,
        "duration_sec": i.get("videoDuration"),
        "posted_at": posted,
        "velocity": round(views / _hours_since(posted), 2),
        "feed_source": feed,
        "seed_term": seed,
        "_cover_url": i.get("displayUrl"),
        "_video_dl": i.get("videoUrl"),
        "_sound": {
            "platform": "instagram",
            "platform_sound_id": audio_id,
            "name": mi.get("song_name"),
            "author": mi.get("artist_name"),
            "play_url": i.get("audioUrl"),
            "sound_page_url": f"https://www.instagram.com/reels/audio/{audio_id}/" if audio_id else None,
        },
    }


def normalize_ig_hashtag(i: dict, feed: str, seed: str, region: str = None) -> dict:
    """instagram-hashtag-scraper output — has video + stats but NO sound metadata."""
    views = i.get("videoViewCount") or i.get("videoPlayCount") or 0
    posted = i.get("timestamp")
    return {
        "platform": "instagram",
        "platform_post_id": i.get("shortCode") or str(i.get("id", "")),
        "video_url": i.get("url"),
        "creator_handle": (i.get("ownerUsername") or "").lower(),
        "caption": i.get("caption", ""),
        "hashtags": i.get("hashtags") or [],
        "views": views,
        "likes": max(0, i.get("likesCount") or 0),
        "comments": i.get("commentsCount") or 0,
        "shares": 0,
        "saves": 0,
        "duration_sec": i.get("videoDuration"),
        "posted_at": posted,
        "velocity": round(views / _hours_since(posted), 2),
        "feed_source": feed,
        "seed_term": seed,
        "region": region,
        "market_code": None,
        "_cover_url": i.get("displayUrl"),
        "_video_dl": i.get("videoUrl"),
        "_sound": None,   # hashtag scraper returns no audio — paid actor needed for sound
    }


def scrape_ig_feed(terms: list[str], feed: str, region_label: str = None,
                   per_term: int = 50, threshold: int = DEFAULT_THRESHOLD,
                   recency_days: int = DEFAULT_RECENCY_DAYS) -> list[dict]:
    """Discover IG Reels by hashtag (free hashtag scraper; global — IG has no geo
    region param). Video + stats only, no sound."""
    raw = run_actor("apify/instagram-hashtag-scraper",
                    {"hashtags": terms, "resultsLimit": per_term}, label=f"IG:{feed}")
    out = []
    for i in raw:
        if not (i.get("type") == "Video" or i.get("videoUrl")):
            continue
        views = i.get("videoViewCount") or i.get("videoPlayCount") or 0
        if views < threshold:
            continue
        if _hours_since(i.get("timestamp")) > recency_days * 24:
            continue
        out.append(normalize_ig_hashtag(i, feed, terms[0] if terms else "", region_label))
    log.info(f"[trends] IG {feed}: {len(out)}/{len(raw)} passed threshold")
    return out


def scrape_ig_watchlist(handles: list[str], per_handle: int = 10,
                        recency_days: int = DEFAULT_RECENCY_DAYS) -> list[dict]:
    """Latest Reels from IG watchlist accounts (reel-scraper is username-only)."""
    if not handles:
        return []
    raw = run_actor("apify/instagram-reel-scraper",
                    {"username": handles, "resultsLimit": per_handle},
                    label="IG:watchlist")
    out = []
    for i in raw:
        if not i.get("videoUrl"):       # reels only
            continue
        if _hours_since(i.get("timestamp")) > recency_days * 24:
            continue
        out.append(normalize_ig(i, "watchlist", i.get("ownerUsername", "")))
    log.info(f"[trends] IG watchlist: {len(out)} recent reels")
    return out


def surface_existing_ig_reels(max_age_days: int = 30, threshold: int = DEFAULT_THRESHOLD) -> list[dict]:
    """Mine IG video reels we ALREADY scraped (posts table) that crossed the view
    threshold and were posted recently — no new scraping / no paid actor. Tagged
    with the creator's region; women-filter applied downstream via process_clips.
    No sound (posts table has no audio metadata)."""
    db = get_db()
    # creator -> (username, region)
    creators, start = [], 0
    while True:
        pg = db.table("creators").select("id,username,market_id").eq("platform", "instagram")\
            .order("id").range(start, start + 999).execute().data or []
        creators += pg
        if len(pg) < 1000:
            break
        start += 1000
    markets = db.table("markets").select("id,region").execute().data or []
    mreg = {m["id"]: {"META": "MENA"}.get(m["region"], m["region"]) for m in markets}
    cmeta = {c["id"]: (c.get("username"), mreg.get(c.get("market_id"))) for c in creators}

    # Female-creator set from the appearance analysis we already ran (thumbnails are
    # expired, so we classify the reel by its creator's known gender, not a fresh call).
    from collections import Counter
    cids = [c["id"] for c in creators]
    fem, male = Counter(), Counter()
    for i in range(0, len(cids), 50):
        b = cids[i:i+50]
        st = 0
        while True:
            rows = db.table("analysis_results").select("creator_id,raw_json")\
                .in_("creator_id", b).order("id").range(st, st + 999).execute().data or []
            for r in rows:
                raw = r.get("raw_json") or {}
                if raw.get("person_is_female") is True or raw.get("subject_type") == "female":
                    fem[r["creator_id"]] += 1
                elif raw.get("person_is_female") is False or raw.get("subject_type") == "male":
                    male[r["creator_id"]] += 1
            if len(rows) < 1000:
                break
            st += 1000
    female_creators = {cid for cid in (set(fem) | set(male)) if fem[cid] >= 1 and fem[cid] >= male[cid]}
    log.info(f"[trends] {len(female_creators)} female IG creators identified for reel surfacing")

    posts, start = [], 0
    while True:
        pg = db.table("posts").select("creator_id,platform_post_id,post_url,caption,hashtags,"
                                      "likes,comments_count,views,media_url,thumbnail_url,posted_at")\
            .eq("platform", "instagram").eq("media_type", "video").gte("views", threshold)\
            .order("id").range(start, start + 999).execute().data or []
        posts += pg
        if len(pg) < 1000:
            break
        start += 1000

    cutoff_h = max_age_days * 24
    out = []
    for p in posts:
        if _hours_since(p.get("posted_at")) > cutoff_h:
            continue
        uname, region = cmeta.get(p.get("creator_id"), (None, None))
        views = p.get("views") or 0
        out.append({
            "subject_type": "female" if p.get("creator_id") in female_creators else "male",
            "platform": "instagram",
            "platform_post_id": p["platform_post_id"],
            "video_url": p.get("post_url"),
            "creator_handle": uname or "",
            "caption": p.get("caption", ""),
            "hashtags": p.get("hashtags") or [],
            "views": views,
            "likes": p.get("likes") or 0,
            "comments": p.get("comments_count") or 0,
            "shares": 0,
            "saves": 0,
            "duration_sec": None,
            "posted_at": p.get("posted_at"),
            "velocity": round(views / _hours_since(p.get("posted_at")), 2),
            "feed_source": None,          # not a dance/hook/watchlist feed — surfaced from owned data
            "seed_term": "existing_reels",
            "region": region,
            "market_code": None,
            "_cover_url": p.get("thumbnail_url"),
            "_video_dl": p.get("media_url"),
            "_sound": None,
        })
    log.info(f"[trends] existing IG reels: {len(out)} viral reels (<= {max_age_days}d) to surface")
    return out


def scrape_tiktok_watchlist(handles: list[str], per_handle: int = 10,
                            recency_days: int = DEFAULT_RECENCY_DAYS) -> list[dict]:
    """Latest posts from watchlist accounts (no view threshold — early signal)."""
    if not handles:
        return []
    raw = run_actor("clockworks/tiktok-scraper",
                    {"profiles": handles, "resultsPerPage": per_handle,
                     "shouldDownloadVideos": True},
                    label="TT:watchlist")
    out = []
    for i in raw:
        if _hours_since(i.get("createTimeISO")) > recency_days * 24:
            continue
        author = (i.get("authorMeta", {}) or {}).get("name", "")
        out.append(normalize_tiktok(i, "watchlist", author))
    log.info(f"[trends] TikTok watchlist: {len(out)} recent clips")
    return out
