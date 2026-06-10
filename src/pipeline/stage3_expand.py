"""Stage 3: Snowball expansion via related profiles, following lists, mutual comments."""
from collections import Counter
from src.apify.instagram import scrape_comments, scrape_profiles as ig_profiles
from src.apify.tiktok import scrape_following, scrape_profiles as tt_profiles
from src.utils.scoring import creator_score, meets_follower_filter
from src.db.client import upsert, insert, get_db
from src.utils.logger import get_logger

log = get_logger(__name__)


def run(
    enriched_creators: list[dict],
    market: dict,
    market_id: str,
    run_id: str,
    platform: str,
    max_candidates: int = 200,
    min_followers: int = 10_000,
    max_followers: int = 2_000_000,
) -> list[dict]:
    log.info(f"[Stage 3] Expanding from {len(enriched_creators)} seed creators on {platform.upper()}")

    seed_usernames = {c["username"] for c in enriched_creators}
    candidate_pool: dict[str, dict] = {}

    if platform == "instagram":
        _expand_instagram(enriched_creators, candidate_pool, seed_usernames)
    elif platform == "tiktok":
        _expand_tiktok(enriched_creators, candidate_pool, seed_usernames, max_candidates)

    # Filter and score
    new_candidates = []
    for username, data in candidate_pool.items():
        if username in seed_usernames:
            continue
        followers = data.get("followers") or 0
        if not meets_follower_filter(followers, min_followers, max_followers):
            continue

        connections = data.get("_connections", 0)
        scores = creator_score(
            followers=followers,
            avg_likes=followers * 0.025,
            community_connections=connections,
            language_match=True,
            hashtag_match=1,
        )

        row = {
            "platform": platform,
            "username": username,
            "full_name": data.get("full_name"),
            "followers": followers,
            "market_id": market_id,
            "run_id": run_id,
            "source_type": data.get("source_type", "related_profile"),
            "tier": 2,
            **scores,
        }
        new_candidates.append({**row, "_connections": connections})

    new_candidates.sort(key=lambda x: x["total_score"], reverse=True)
    new_candidates = new_candidates[:max_candidates]

    log.info(f"[Stage 3] {len(new_candidates)} new candidates after expansion")

    if new_candidates:
        db_rows = [{k: v for k, v in r.items() if not k.startswith("_")} for r in new_candidates]
        upsert("creators", db_rows, on_conflict="platform,username")

        # Save cluster edges
        _save_edges(enriched_creators, new_candidates, run_id, platform)

    return enriched_creators + new_candidates


def _expand_instagram(seeds: list[dict], pool: dict, seed_usernames: set) -> None:
    # Edge 1: relatedProfiles from profile scraper results
    for creator in seeds:
        for related_username in creator.get("_related_profiles", []):
            u = related_username.lower().strip()
            if u and u not in seed_usernames:
                if u not in pool:
                    pool[u] = {"username": u, "platform": "instagram", "source_type": "related_profile", "_connections": 0}
                pool[u]["_connections"] += 1

    # Edge 2: mutual commenters (scrape comments on seed posts)
    post_urls = [c.get("profile_url") for c in seeds if c.get("profile_url")]
    if post_urls:
        comments = scrape_comments(post_urls[:10], limit=300)
        commenter_counts = Counter(c["commenter_username"] for c in comments if c.get("commenter_username"))
        for username, count in commenter_counts.items():
            u = username.lower().strip()
            if u and u not in seed_usernames and count >= 2:
                if u not in pool:
                    pool[u] = {"username": u, "platform": "instagram", "source_type": "mutual_comment", "_connections": 0}
                pool[u]["_connections"] += count


def _expand_tiktok(seeds: list[dict], pool: dict, seed_usernames: set, max_candidates: int) -> None:
    per_creator_limit = max(50, max_candidates // max(len(seeds), 1))
    for creator in seeds:
        username = creator.get("username")
        if not username:
            continue
        following = scrape_following(username, limit=per_creator_limit)
        for f in following:
            u = (f.get("username") or "").lower().strip()
            if u and u not in seed_usernames:
                if u not in pool:
                    pool[u] = {**f, "source_type": "following", "_connections": 0}
                pool[u]["_connections"] += 1


def _save_edges(sources: list[dict], targets: list[dict], run_id: str, platform: str) -> None:
    db = get_db()
    source_ids = {r["username"]: r.get("id") for r in sources}
    target_ids = {r["username"]: r.get("id") for r in targets}

    # Fetch actual DB IDs
    all_usernames = list(source_ids.keys()) + list(target_ids.keys())
    rows = db.table("creators").select("id,username").in_("username", all_usernames).execute().data
    id_map = {r["username"]: r["id"] for r in rows}

    edges = []
    for source in sources:
        src_id = id_map.get(source["username"])
        if not src_id:
            continue
        for target in targets:
            tgt_id = id_map.get(target["username"])
            if not tgt_id:
                continue
            edges.append({
                "source_creator_id": src_id,
                "target_creator_id": tgt_id,
                "edge_type": target.get("source_type", "related_profile"),
                "platform": platform,
                "weight": target.get("_connections", 1),
                "run_id": run_id,
            })

    if edges:
        upsert("cluster_edges", edges, on_conflict="source_creator_id,target_creator_id,edge_type")
        log.info(f"[Stage 3] Saved {len(edges)} cluster edges")
