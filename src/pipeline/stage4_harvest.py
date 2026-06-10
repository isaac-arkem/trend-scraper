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

        result = upsert("posts", post_row, on_conflict="platform,platform_post_id")
        if not result:
            continue

        saved_post = result[0]
        saved_posts.append(saved_post)
        post_db_id = saved_post["id"]

        # Download and store media
        media_url = post.get("media_url") or post.get("thumbnail_url")
        if not media_url:
            continue

        media_type = post.get("media_type", "image")
        ext = "mp4" if media_type == "video" else "jpg"
        filename = f"original.{ext}"
        minio_path = build_path(platform, country_iso, platform_user_id, post_id, filename)

        stored_path = upload_from_url(media_url, minio_path)
        if stored_path:
            asset_type = "video" if media_type == "video" else "image"
            asset_row = {
                "post_id": post_db_id,
                "creator_id": creator_id,
                "asset_type": asset_type,
                "minio_path": stored_path,
                "original_url": media_url,
                "analysis_status": "pending",
            }
            assets = insert("media_assets", asset_row)
            if assets:
                saved_assets.extend(assets)

        # Also save thumbnail separately if video
        if media_type == "video" and post.get("thumbnail_url"):
            thumb_path = build_path(platform, country_iso, platform_user_id, post_id, "thumbnail.jpg")
            stored_thumb = upload_from_url(post["thumbnail_url"], thumb_path)
            if stored_thumb:
                thumb_row = {
                    "post_id": post_db_id,
                    "creator_id": creator_id,
                    "asset_type": "thumbnail",
                    "minio_path": stored_thumb,
                    "original_url": post["thumbnail_url"],
                    "analysis_status": "pending",
                }
                assets = insert("media_assets", thumb_row)
                if assets:
                    saved_assets.extend(assets)

    log.info(f"[Stage 4] Saved {len(saved_posts)} posts, {len(saved_assets)} media assets")
    return saved_posts, saved_assets


def _scrape_tiktok_user_posts(usernames: list[str], limit: int) -> list[dict]:
    from src.apify.tiktok import _normalise_post
    items = run_actor(
        TIKTOK_PROFILE_ACTOR,
        {"profiles": usernames, "resultsPerPage": limit},
        label="TT:user_posts",
    )
    return [_normalise_post(i) for i in items if i.get("authorMeta")]
