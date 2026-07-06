from typing import Union, Optional
"""Stage 5: AI vision analysis of images and video frames."""
import time
import httpx
from src.ai.vision import analyze_image_url, analyze_image_bytes
from src.ai.frames import download_video_and_extract
from src.db.client import get_db, insert, update
from src.utils.logger import get_logger

log = get_logger(__name__)

_DL_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
    "Referer": "https://www.instagram.com/",
}


def _download_image(url: str) -> Optional[bytes]:
    """Download an image, retrying only transient network errors (DNS/connect/
    timeout). HTTP status errors (404/410 — expired URLs) fail fast, no retry."""
    delay = 2
    for attempt in range(4):
        try:
            r = httpx.get(url, follow_redirects=True, timeout=20, headers=_DL_HEADERS)
            r.raise_for_status()
            return r.content
        except httpx.HTTPStatusError as e:
            log.warning(f"Image URL dead ({e.response.status_code}): {url[:60]}")
            return None
        except httpx.TransportError as e:
            if attempt == 3:
                log.warning(f"Image download failed after retries ({type(e).__name__}): {url[:60]}")
                return None
            time.sleep(delay)
            delay = min(delay * 2, 15)
        except Exception as e:
            log.warning(f"Failed to download image: {e}")
            return None


ANALYZABLE_TYPES = ["image", "thumbnail", "frame", "video"]

# Per-creator cost controls for AI analysis (scraping stays uncapped — we keep all
# images for the creation wizard; this only limits what's sent to the vision model):
#   CAP_PER_CREATOR — hard ceiling: never analyze more than this many images/creator.
#   GOOD_READS_TO_STOP — early stop: once we've got this many usable appearance reads
#       (a person is clearly visible, so the indicators are filled), stop analyzing
#       the rest of that creator's images. Most extra images add no new appearance
#       info, so this avoids paying for them.
CAP_PER_CREATOR = 20
GOOD_READS_TO_STOP = 2


def mark_capped(ids: list[str]) -> None:
    """Mark un-analyzed (over-cap / early-stopped) assets as 'skipped' so they're
    not analyzed, not charged, and the pending queue still drains to zero. We reuse
    the existing 'skipped' status because the DB CHECK constraint only allows
    (pending, analyzing, done, failed, skipped) — these have no analysis_results row,
    so they never show up in the dashboard's women/skipped galleries."""
    db = get_db()
    for i in range(0, len(ids), 200):
        db.table("media_assets").update({"analysis_status": "skipped"})\
            .in_("id", ids[i:i+200]).execute()


def _is_good_read(saved: Optional[dict]) -> bool:
    """A 'good read' = a saved result where a person is clearly visible, i.e. the
    appearance indicators got filled. Those are the images worth stopping on."""
    return bool(saved and saved.get("person_visible"))


def analyze_creator(creator_id: str, cap: int = CAP_PER_CREATOR,
                    good_reads_to_stop: int = GOOD_READS_TO_STOP) -> int:
    """Analyze ONE creator's images in order, with early-stop:
      • stop once `good_reads_to_stop` usable appearance reads are collected, OR
      • stop after `cap` images analyzed.
    Already-done images (and prior good reads) count toward both limits, so this is
    fully resumable. Remaining un-analyzed pending images are marked 'capped'.
    Returns the number of new results saved this call."""
    db = get_db()

    # Prior state for this creator (so resumes don't re-pay): how many analyzed,
    # and how many were already good reads.
    done_count = db.table("media_assets").select("id", count="exact")\
        .eq("creator_id", creator_id).eq("analysis_status", "done")\
        .in_("asset_type", ANALYZABLE_TYPES).limit(1).execute().count or 0
    good = db.table("analysis_results").select("id", count="exact")\
        .eq("creator_id", creator_id).eq("person_visible", True).limit(1).execute().count or 0

    # This creator's still-pending images, in stable order.
    pending = []
    start = 0
    while True:
        page = db.table("media_assets").select("*").eq("creator_id", creator_id)\
            .eq("analysis_status", "pending").in_("asset_type", ANALYZABLE_TYPES)\
            .order("id").range(start, start + 999).execute().data or []
        pending += page
        if len(page) < 1000:
            break
        start += 1000
    if not pending:
        return 0

    saved = 0
    analyzed = done_count
    leftover = []
    for asset in pending:
        if good >= good_reads_to_stop or analyzed >= cap:
            leftover.append(asset["id"])
            continue
        result = analyze_asset(asset)   # raises QuotaExceededError to halt the run
        analyzed += 1
        if result:
            saved += 1
            if _is_good_read(result):
                good += 1

    if leftover:
        mark_capped(leftover)
    return saved


def run(market_id: str, platform: str, cap: int = CAP_PER_CREATOR,
        good_reads_to_stop: int = GOOD_READS_TO_STOP, max_assets: int = None) -> int:
    db = get_db()

    creator_rows = db.table("creators").select("id").eq("market_id", market_id).eq("platform", platform).execute().data
    creator_ids = [c["id"] for c in creator_rows]
    if not creator_ids:
        log.warning(f"[Stage 5] No creators found for market {market_id}")
        return 0

    log.info(f"[Stage 5] Analyzing {len(creator_ids)} creators "
             f"(cap {cap}/creator, stop after {good_reads_to_stop} good reads)")
    total = 0
    for cid in creator_ids:
        total += analyze_creator(cid, cap, good_reads_to_stop)

    log.info(f"[Stage 5] Analysis complete — {total} results saved")
    return total


def analyze_asset(asset: dict) -> Optional[dict]:
    """Analyze a single media asset end-to-end: mark analyzing → fetch image/frames →
    vision analyze → save result + mark done (or failed). Thread-safe; returns the
    saved analysis_results row, or None on failure/no-result."""
    asset_id = asset["id"]
    creator_id = asset.get("creator_id") or asset.get("posts", {}).get("creator_id")
    original_url = asset.get("original_url")
    asset_type = asset.get("asset_type")

    # Mark as analyzing
    update("media_assets", {"id": asset_id}, {"analysis_status": "analyzing"})

    result = None

    if asset_type == "video":
        # Real video file — extract frames and analyze best one
        frames = download_video_and_extract(original_url, max_frames=6)
        for frame_bytes in frames:
            r = analyze_image_bytes(frame_bytes)
            if r and r.get("person_visible"):
                result = r
                break
        if not result and frames:
            result = analyze_image_bytes(frames[0])
    else:
        # Image or carousel frame — try MinIO first, fall back to original URL
        image_bytes = None
        if asset.get("minio_path"):
            try:
                from src.storage.minio import get_minio
                import os as _os
                mc = get_minio()
                bucket = _os.environ.get("MINIO_BUCKET", "social-intel")
                obj = mc.get_object(bucket, asset["minio_path"])
                image_bytes = obj.read()
            except Exception:
                pass
        if not image_bytes and original_url:
            image_bytes = _download_image(original_url)
        if image_bytes:
            result = analyze_image_bytes(image_bytes)
        else:
            result = None

    if not result:
        update("media_assets", {"id": asset_id}, {"analysis_status": "failed"})
        return None

    # Categorise the subject — save EVERYTHING, tag what it is.
    # AI Analysis tab filters to female; skipped ones viewable separately.
    if not result.get("person_visible"):
        subject_type = "none"
    elif result.get("is_ad_or_product") is True:
        subject_type = "ad"
    elif result.get("is_child") is True:
        subject_type = "child"
    elif result.get("person_is_female") is True:
        subject_type = "female"
    elif result.get("person_is_female") is False:
        subject_type = "male"
    else:
        subject_type = "unclear"
    result["subject_type"] = subject_type

    # Save result
    analysis_row = {
        "media_asset_id": asset_id,
        "creator_id": creator_id,
        "person_visible": result.get("person_visible"),
        "body_frame": result.get("body_frame"),
        "body_shape": result.get("body_shape"),
        "skin_tone": result.get("skin_tone"),
        "eye_color": result.get("eye_color"),
        "hair_color": result.get("hair_color"),
        "hair_length": result.get("hair_length"),
        "hair_texture": result.get("hair_texture"),
        "makeup_style": result.get("makeup_style"),
        "fashion_style": result.get("fashion_style", []),
        "content_style": result.get("content_style", []),
        "image_quality": result.get("image_quality"),
        "confidence": result.get("confidence"),
        "notes": result.get("notes"),
        "raw_json": result,
    }

    saved = insert("analysis_results", analysis_row)
    update("media_assets", {"id": asset_id}, {"analysis_status": "done"})

    return saved[0] if saved else None
