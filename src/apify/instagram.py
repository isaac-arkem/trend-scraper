from src.apify.client import run_actor
from src.utils.logger import get_logger

log = get_logger(__name__)

HASHTAG_ACTOR = "apify/instagram-hashtag-scraper"
PROFILE_ACTOR = "apify/instagram-profile-scraper"
POST_ACTOR = "apify/instagram-scraper"
COMMENTS_ACTOR = "apify/instagram-comment-scraper"


def scrape_hashtag(hashtags: list[str], limit_per_hashtag: int = 100) -> list[dict]:
    items = run_actor(
        HASHTAG_ACTOR,
        {"hashtags": hashtags, "resultsLimit": limit_per_hashtag},
        label="IG:hashtag",
    )
    return [_normalise_post(i) for i in items if i.get("ownerUsername")]


def scrape_profiles(usernames: list[str]) -> list[dict]:
    if not usernames:
        return []
    items = run_actor(
        PROFILE_ACTOR,
        {"usernames": usernames},
        label="IG:profiles",
    )
    return [_normalise_profile(i) for i in items if i.get("username")]


def scrape_posts(usernames: list[str], posts_per_user: int = 5) -> list[dict]:
    if not usernames:
        return []
    profile_urls = [f"https://www.instagram.com/{u.strip('/')}/" for u in usernames]
    items = run_actor(
        POST_ACTOR,
        {
            "directUrls": profile_urls,
            "resultsType": "posts",
            "resultsLimit": posts_per_user,
        },
        label="IG:posts",
    )
    return [_normalise_post(i) for i in items if i.get("ownerUsername") or i.get("shortCode")]


def scrape_comments(post_urls: list[str], limit: int = 200) -> list[dict]:
    if not post_urls:
        return []
    items = run_actor(
        COMMENTS_ACTOR,
        {"directUrls": post_urls, "resultsLimit": limit},
        label="IG:comments",
    )
    return [_normalise_comment(i) for i in items if i.get("ownerUsername")]


def _normalise_post(i: dict) -> dict:
    return {
        "platform": "instagram",
        "username": i.get("ownerUsername", "").lower().strip(),
        "platform_post_id": i.get("shortCode") or i.get("id"),
        "post_url": i.get("url"),
        "caption": i.get("caption", ""),
        "hashtags": i.get("hashtags", []),
        "likes": i.get("likesCount") or 0,
        "comments_count": i.get("commentsCount") or 0,
        "views": i.get("videoViewCount") or 0,
        "media_type": "video" if i.get("isVideo") else "image",
        "media_url": i.get("videoUrl") or i.get("displayUrl"),
        "thumbnail_url": i.get("displayUrl"),
        "posted_at": i.get("timestamp"),
    }


def _normalise_profile(i: dict) -> dict:
    return {
        "platform": "instagram",
        "platform_user_id": str(i.get("id", "")),
        "username": i.get("username", "").lower().strip(),
        "full_name": i.get("fullName"),
        "bio": i.get("biography"),
        "followers": i.get("followersCount"),
        "following": i.get("followsCount"),
        "post_count": i.get("postsCount"),
        "profile_url": i.get("url"),
        "profile_pic_url": i.get("profilePicUrl"),
        "is_verified": bool(i.get("verified")),
        "related_profiles": [
            r.get("username", "").lower()
            for r in (i.get("relatedProfiles") or [])
            if r.get("username")
        ],
    }


def _normalise_comment(i: dict) -> dict:
    return {
        "commenter_username": i.get("ownerUsername", "").lower().strip(),
        "commenter_platform_id": str(i.get("ownerId", "")),
        "text": i.get("text", ""),
        "likes": i.get("likesCount") or 0,
    }
