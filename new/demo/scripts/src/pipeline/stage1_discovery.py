"""Stage 1: Hashtag/keyword sweep → ranked list of creator usernames."""
from src.apify.instagram import scrape_hashtag as ig_hashtag
from src.apify.tiktok import scrape_hashtag as tt_hashtag
from src.utils.scoring import post_score
from src.utils.dedupe import dedupe_by_username
from src.utils.logger import get_logger

log = get_logger(__name__)


def run(market: dict, platform: str, limit_per_hashtag: int = 100) -> list[dict]:
    """
    Returns a ranked list of raw post dicts, each with a 'username' field.
    """
    hashtags = market["hashtags"].get(platform, [])
    if not hashtags:
        log.warning(f"No hashtags configured for {platform} in {market['name']}")
        return []

    log.info(f"[Stage 1] {platform.upper()} hashtag sweep for {market['name']} — {len(hashtags)} tags")

    posts = []
    if platform == "instagram":
        posts = ig_hashtag(hashtags, limit_per_hashtag=limit_per_hashtag)
    elif platform == "tiktok":
        region = market.get("apify_region_code", "")
        posts = tt_hashtag(hashtags, region=region, limit=limit_per_hashtag)

    # Score and rank
    for p in posts:
        p["_post_score"] = post_score(p.get("likes", 0), p.get("comments_count", 0), p.get("views", 0))

    posts.sort(key=lambda x: x["_post_score"], reverse=True)

    log.info(f"[Stage 1] Found {len(posts)} posts from hashtag sweep")

    observed = _observed_engagement(posts)

    # Extract unique creator usernames in ranked order
    seen = set()
    ranked_creators = []
    for p in posts:
        u = p.get("username", "").lower().strip()
        if u and u not in seen:
            seen.add(u)
            obs = observed.get(u, {})
            ranked_creators.append({
                "username": u,
                "platform": platform,
                "source_type": "hashtag_discovery",
                # Carried so Stage 2 can score on real engagement instead of a
                # placeholder. Dropping these was what made engagement_score the
                # constant 0.03 for every creator.
                "observed_avg_likes": obs.get("avg_likes"),
                "observed_posts": obs.get("posts", 0),
            })

    log.info(f"[Stage 1] Unique creators discovered: {len(ranked_creators)}")
    return ranked_creators, posts


def _observed_engagement(posts: list[dict]) -> dict:
    """{username: {avg_likes, posts}} from the posts already in hand.

    These are the posts that surfaced each creator, so their likes are real
    measurements — no extra scrape needed to compute them."""
    likes: dict[str, list] = {}
    for p in posts:
        u = (p.get("username") or "").lower().strip()
        if u:
            likes.setdefault(u, []).append(p.get("likes") or 0)
    return {u: {"avg_likes": sum(v) / len(v), "posts": len(v)}
            for u, v in likes.items() if v}
