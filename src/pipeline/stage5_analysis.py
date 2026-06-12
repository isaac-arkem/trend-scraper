"""Stage 5: AI vision analysis of images and video frames."""
from src.ai.vision import analyze_image_url, analyze_image_bytes
from src.ai.frames import download_video_and_extract
from src.db.client import get_db, insert, update
from src.utils.logger import get_logger

log = get_logger(__name__)


def run(market_id: str, platform: str, max_assets: int = 500) -> list[dict]:
    db = get_db()

    # Get creator_ids for this market directly (no complex join)
    creator_rows = db.table("creators").select("id").eq("market_id", market_id).eq("platform", platform).execute().data
    creator_ids = [c["id"] for c in creator_rows]
    if not creator_ids:
        log.warning(f"[Stage 5] No creators found for market {market_id}")
        return []

    # Fetch pending assets for these creators in batches
    pending = []
    for i in range(0, len(creator_ids), 100):
        batch = creator_ids[i:i+100]
        rows = db.table("media_assets").select("*").eq("analysis_status", "pending")\
            .in_("asset_type", ["image", "thumbnail", "frame", "video"])\
            .in_("creator_id", batch).limit(max_assets - len(pending)).execute().data or []
        pending.extend(rows)
        if len(pending) >= max_assets:
            break

    if not pending:
        log.info("[Stage 5] No pending media assets to analyze")
        return []

    log.info(f"[Stage 5] Analyzing {len(pending)} media assets")

    results = []
    for asset in pending:
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
                try:
                    import httpx as _httpx
                    r = _httpx.get(original_url, follow_redirects=True, timeout=20,
                                   headers={"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
                                            "Referer": "https://www.instagram.com/"})
                    r.raise_for_status()
                    image_bytes = r.content
                except Exception as e:
                    log.warning(f"Failed to download image: {e}")
            if image_bytes:
                result = analyze_image_bytes(image_bytes)
            else:
                result = None

        if not result:
            update("media_assets", {"id": asset_id}, {"analysis_status": "failed"})
            continue

        if not result.get("person_visible"):
            update("media_assets", {"id": asset_id}, {"analysis_status": "skipped"})
            continue

        # Skip non-female, children, and ads — female only
        if result.get("person_is_female") is not True:
            update("media_assets", {"id": asset_id}, {"analysis_status": "skipped"})
            continue
        if result.get("is_child") is True or result.get("is_ad_or_product") is True:
            update("media_assets", {"id": asset_id}, {"analysis_status": "skipped"})
            continue

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

        if saved:
            results.append(saved[0])

    log.info(f"[Stage 5] Analysis complete — {len(results)} results saved")
    return results
