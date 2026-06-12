"""Stage 4: Scrape posts, download media to MinIO, save to DB."""
from src.apify.instagram import scrape_posts as ig_posts
from src.apify.tiktok import scrape_hashtag as tt_posts_via_hashtag
from src.apify.client import run_actor
from src.storage.minio import upload_from_url, build_path
from src.db.client import upsert, insert, get_db
from src.utils.logger import get_logger

log = get_logger(__name__)

TIKTOK_PROFILE_ACTOR = "clockworks/tiktok-scraper"


def run(
    all_creators: list[dict],
    market: dict,
    platform: str,
    run_id: str,
    max_creators: int = 50,
    posts_per_creator: int = 5,
) -> tuple[list[dict], list[dict]]:
    db = get_db()

    # Re-fetch creators from DB sorted by score, get top N
    market_id = market.get("id")
    creator_rows = (
        db.table("creators")
        .select("*")
        .eq("market_id", market_id)
        .eq("platform", platform)
        .order("total_score", desc=True)
        .limit(max_creators)
        .execute()
        .data
    )

    if not creator_rows:
        log.warning("[Stage 4] No creators found in DB for harvest")
        return [], []

    log.info(f"[Stage 4] Harvesting {len(creator_rows)} creators on {platform.upper()}")

    usernames = [c["username"] for c in creator_rows]
    creator_id_map = {c["username"]: c["id"] for c in creator_rows}
    creator_user_id_map = {c["username"]: c.get("platform_user_id") or c["username"] for c in creator_rows}

    # Scrape posts
    raw_posts = []
    if platform == "instagram":
        raw_posts = ig_posts(usernames, posts_per_user=posts_per_creator)
    elif platform == "tiktok":
        raw_posts = _scrape_tiktok_user_posts(usernames, posts_per_creator)

    log.info(f"[Stage 4] Scraped {len(raw_posts)} posts")

    # Save posts and download media
    saved_posts = []
    saved_assets = []
    country_iso = market.get("country_code", "XX")

    for post in raw_posts:
        username = post.get("username", "").lower()
        creator_id = creator_id_map.get(username)
        if not creator_id:
            continue

        platform_user_id = creator_user_id_map.get(username, username)
        post_id = post.get("platform_post_id") or post.get("post_url", "").split("/")[-1] or "unknown"

        post_row = {
            "creator_id": creator_id,
            "platform": platform,
            "platform_post_id": post.get("platform_post_id"),
            "post_url": post.get("post_url"),
            "caption": post.get("caption"),
            "hashtags": post.get("hashtags", []),
            "likes": post.get("likes", 0),
            "comments_count": post.get("comments_count", 0),
            "views": post.get("views", 0),
            "shares": post.get("shares", 0),
            "media_type": post.get("media_type", "image"),
            "media_url": post.get("media_url"),
            "thumbnail_url": post.get("thumbnail_url"),
            "posted_at": post.get("posted_at"),
        }

        try:
            result = upsert("posts", post_row, on_conflict="platform,platform_post_id")
        except Exception as e:
            log.debug(f"Skipping post for {username} — creator removed: {e}")
            continue
        if not result:
            continue

        saved_post = result[0]
        saved_posts.append(saved_post)
        post_db_id = saved_post["id"]

        # Download and store media
        media_type = post.get("media_type", "image")
        thumbnail_url = post.get("thumbnail_url")
        video_url = post.get("video_url")
        all_images = post.get("all_images") or []

        assets_to_save = []  # list of (url, filename, asset_type)

        if platform == "tiktok":
            # TikTok: use cover image only (video URLs are webpage links)
            if thumbnail_url:
                assets_to_save.append((thumbnail_url, "cover.jpg", "image"))

        elif media_type == "carousel":
            # Instagram carousel: save every image
            for idx, img_url in enumerate(all_images[:20]):
                assets_to_save.append((img_url, f"image_{idx+1:02d}.jpg", "image"))

        elif media_type == "video":
            # Instagram video/reel: save thumbnail + video file
            if thumbnail_url:
                assets_to_save.append((thumbnail_url, "cover.jpg", "image"))
            if video_url:
                assets_to_save.append((video_url, "video.mp4", "video"))

        else:
            # Instagram image
            img_url = post.get("media_url") or thumbnail_url
            if img_url:
                assets_to_save.append((img_url, "image.jpg", "image"))

        for url, filename, asset_type in assets_to_save:
            if not url:
                continue
            minio_path = build_path(platform, country_iso, platform_user_id, post_id, filename)
            stored_path = upload_from_url(url, minio_path)
            if stored_path:
                asset_row = {
                    "post_id": post_db_id,
                    "creator_id": creator_id,
                    "asset_type": asset_type,
                    "minio_path": stored_path,
                    "original_url": url,
                    "analysis_status": "pending",
                }
                result = insert("media_assets", asset_row)
                if result:
                    saved_assets.extend(result)

    log.info(f"[Stage 4] Saved {len(saved_posts)} posts, {len(saved_assets)} media assets")

    # Save raw posts JSON per creator to MinIO
    try:
        from src.storage.minio import upload_bytes
        import json as _json
        from collections import defaultdict
        posts_by_creator = defaultdict(list)
        for p in raw_posts:
            posts_by_creator[p.get("username","unknown")].append(p)
        for username, creator_posts in posts_by_creator.items():
            uid = creator_user_id_map.get(username, username)
            raw_path = f"{platform}/{country_iso}/{uid}/raw_posts.json"
            upload_bytes(
                _json.dumps(creator_posts, ensure_ascii=False, default=str).encode("utf-8"),
                raw_path, content_type="application/json"
            )
    except Exception as e:
        log.warning(f"Failed to save raw posts JSON to MinIO: {e}")

    return saved_posts, saved_assets


def _scrape_tiktok_user_posts(usernames: list[str], limit: int) -> list[dict]:
    from src.apify.tiktok import _normalise_post
    items = run_actor(
        TIKTOK_PROFILE_ACTOR,
        {"profiles": usernames, "resultsPerPage": limit},
        label="TT:user_posts",
    )
    return [_normalise_post(i) for i in items if i.get("authorMeta")]
