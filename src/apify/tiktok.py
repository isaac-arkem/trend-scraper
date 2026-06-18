from src.apify.client import run_actor
from src.utils.logger import get_logger

log = get_logger(__name__)

SEARCH_ACTOR = "clockworks/tiktok-scraper"
PROFILE_ACTOR = "clockworks/tiktok-profile-scraper"
FOLLOWING_ACTOR = "clockworks/tiktok-followers-scraper"


def scrape_hashtag(hashtags: list[str], region: str, limit: int = 100) -> list[dict]:
    items = run_actor(
        SEARCH_ACTOR,
        {
            "hashtags": hashtags,
            "region": region,
            "resultsPerPage": limit,
        },
        label="TT:hashtag",
    )
    return [_normalise_post(i) for i in items if _get_nested(i, "authorMeta", "name")]


def scrape_keyword(keywords: list[str], region: str, limit: int = 100) -> list[dict]:
    items = run_actor(
        SEARCH_ACTOR,
        {
            "keywords": keywords,
            "region": region,
            "resultsPerPage": limit,
        },
        label="TT:keyword",
    )
    return [_normalise_post(i) for i in items if _get_nested(i, "authorMeta", "name")]


def scrape_profiles(usernames: list[str], limit: int = 1) -> list[dict]:
    if not usernames:
        return []
    items = run_actor(
        PROFILE_ACTOR,
        {"profiles": usernames, "resultsPerPage": limit},
        label="TT:profiles",
    )
    return [_normalise_profile(i) for i in items]


def scrape_following(username: str, limit: int = 200, user_id: str = None) -> list[dict]:
    run_input = {"listType": "following", "maxItems": limit}
    if user_id:
        run_input["userId"] = user_id
    else:
        run_input["username"] = username
    items = run_actor(
        FOLLOWING_ACTOR,
        run_input,
        label=f"TT:following:{username}",
    )
    return [_normalise_follow(i) for i in items if i.get("uniqueId")]


def _get_nested(d: dict, *keys):
    for k in keys:
        if not isinstance(d, dict):
            return None
        d = d.get(k)
    return d


def _normalise_post(i: dict) -> dict:
    author = i.get("authorMeta", {})
    music = i.get("musicMeta", {})
    return {
        "platform": "tiktok",
        "username": (author.get("name") or author.get("uniqueId") or "").lower().strip(),
        "platform_user_id": str(author.get("id", "")),
        "platform_post_id": str(i.get("id", "")),
        "post_url": i.get("webVideoUrl"),
        "caption": i.get("text", ""),
        "hashtags": [h.get("name") for h in (i.get("hashtags") or []) if h.get("name")],
        "likes": i.get("diggCount") or 0,
        "comments_count": i.get("commentCount") or 0,
        "views": i.get("playCount") or 0,
        "shares": i.get("shareCount") or 0,
        "media_type": "video",
        "media_url": i.get("webVideoUrl"),
        "thumbnail_url": _get_nested(i, "videoMeta", "coverUrl") or _get_nested(i, "covers", 0),
        "posted_at": i.get("createTimeISO"),
        "music_id": str(music.get("musicId", "")),
    }


def _normalise_profile(i: dict) -> dict:
    # clockworks/tiktok-profile-scraper returns data under authorMeta
    author = i.get("authorMeta", {})
    # fallback for other actor formats that use userInfo.user/stats
    user  = _get_nested(i, "userInfo", "user") or {}
    stats = _get_nested(i, "userInfo", "stats") or {}
    return {
        "platform": "tiktok",
        "platform_user_id": str(author.get("id") or user.get("id") or ""),
        "username": (author.get("name") or user.get("uniqueId") or "").lower().strip(),
        "full_name": author.get("nickName") or user.get("nickname"),
        "bio": author.get("signature") or user.get("signature"),
        "followers": author.get("fans") or stats.get("followerCount"),
        "following": author.get("following") or stats.get("followingCount"),
        "post_count": author.get("video") or stats.get("videoCount"),
        "profile_pic_url": author.get("avatar") or user.get("avatarLarger"),
        "is_verified": bool(author.get("verified") or user.get("verified")),
        "profile_url": f"https://www.tiktok.com/@{(author.get('name') or user.get('uniqueId') or '').lower().strip()}",
    }


def _normalise_follow(i: dict) -> dict:
    return {
        "platform": "tiktok",
        "platform_user_id": str(i.get("id", "")),
        "username": (i.get("uniqueId") or "").lower().strip(),
        "full_name": i.get("nickname"),
        "followers": i.get("followerCount"),
    }
