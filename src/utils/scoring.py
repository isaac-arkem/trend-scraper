import math


def post_score(likes: int, comments: int, views: int = 0) -> float:
    return (likes or 0) + (comments or 0) * 5 + (views or 0) * 0.01


def engagement_rate(avg_likes: float, followers: int) -> float:
    if not followers or followers == 0:
        return 0.0
    return min(avg_likes / followers, 1.0)


def follower_score(followers: int, min_f: int = 10_000, max_f: int = 2_000_000) -> float:
    if not followers or followers < min_f:
        return 0.0
    if followers > max_f:
        return 0.0
    # Log-normalized between 0 and 1 within the acceptable range
    return math.log(followers - min_f + 1) / math.log(max_f - min_f + 1)


def creator_score(
    followers: int,
    avg_likes: float,
    community_connections: int = 0,
    language_match: bool = True,
    hashtag_match: int = 0,
) -> dict:
    f_score = follower_score(followers)
    e_score = engagement_rate(avg_likes, followers)
    c_score = min(community_connections / 10, 1.0)
    l_score = (1.0 if language_match else 0.3) * min(hashtag_match / 3, 1.0)

    total = (
        f_score * 0.30
        + e_score * 0.35
        + c_score * 0.25
        + l_score * 0.10
    )

    return {
        "relevance_score": round(l_score, 4),
        "engagement_score": round(e_score, 4),
        "community_score": round(c_score, 4),
        "total_score": round(total, 4),
    }


def meets_follower_filter(followers: int, min_f: int = 10_000, max_f: int = 2_000_000) -> bool:
    return followers is not None and min_f <= followers <= max_f
