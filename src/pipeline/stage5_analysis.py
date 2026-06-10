"""Stage 5: AI vision analysis of images and video frames."""
from src.ai.vision import analyze_image_url, analyze_image_bytes
from src.ai.frames import download_video_and_extract
from src.db.client import get_db, insert, update
from src.utils.logger import get_logger

log = get_logger(__name__)


def run(market_id: str, platform: str, max_assets: int = 500) -> list[dict]:
    db = get_db()

    # Fetch pending assets for this market
    pending = (
        db.table("media_assets")
        .select("*, posts(creator_id, creators(market_id))")
        .eq("analysis_status", "pending")
        .in_("asset_type", ["image", "thumbnail", "frame"])
        .limit(max_assets)
        .execute()
        .data
    )

    # Filter to this market only
    pending = [
        a for a in pending
        if a.get("posts", {}).get("creators", {}).get("market_id") == market_id
    ]

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
            frames = download_video_and_extract(original_url, max_frames=6)
            for frame_bytes in frames:
                result = analyze_image_bytes(frame_bytes)
                if result and result.get("person_visible"):
                    break
        else:
            # Download bytes first — Instagram CDN URLs expire and can't be
            # passed directly to OpenAI. Sending as base64 always works.
            try:
                import httpx
                r = httpx.get(original_url, follow_redirects=True, timeout=20,
                              headers={"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"})
                r.raise_for_status()
                result = analyze_image_bytes(r.content)
            except Exception as e:
                log.warning(f"Failed to download image for analysis: {e}")
                result = None

        if not result:
            update("media_assets", {"id": asset_id}, {"analysis_status": "failed"})
            continue

        if not result.get("person_visible"):
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
