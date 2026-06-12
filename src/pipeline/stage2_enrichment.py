"""Stage 2: Scrape full profiles, filter, score, save to DB."""
from src.apify.instagram import scrape_profiles as ig_profiles
from src.apify.tiktok import scrape_profiles as tt_profiles
from src.utils.scoring import creator_score, meets_follower_filter
from src.utils.dedupe import dedupe_by_username
from src.db.client import upsert
from src.utils.logger import get_logger

log = get_logger(__name__)

try:
    from langdetect import detect as _detect
    def detect_lang(text: str) -> str:
        try:
            return _detect(text) if text and len(text) > 10 else "unknown"
        except Exception:
            return "unknown"
except ImportError:
    def detect_lang(text: str) -> str:
        return "unknown"


def run(
    candidates: list[dict],
    market: dict,
    market_id: str,
    run_id: str,
    platform: str,
    seed_profiles: list[str] = None,
    min_followers: int = 10_000,
    max_followers: int = 2_000_000,
) -> list[dict]:
    seed_profiles = [u.lower() for u in (seed_profiles or [])]
    usernames = list({c["username"] for c in candidates if c.get("username")})
    usernames = list(set(usernames + seed_profiles))

    log.info(f"[Stage 2] Enriching {len(usernames)} profiles on {platform.upper()}")

    if platform == "instagram":
        raw_profiles = ig_profiles(usernames)
    else:
        raw_profiles = tt_profiles(usernames)

    enriched = []
    filtered_out = 0

    for p in raw_profiles:
        followers = p.get("followers") or 0
        if not meets_follower_filter(followers, min_followers, max_followers):
            filtered_out += 1
            continue

        bio = p.get("bio") or ""
        lang = detect_lang(bio)
        market_langs = market.get("languages", [])
        lang_match = lang in market_langs if market_langs else True

        scores = creator_score(
            followers=followers,
            avg_likes=followers * 0.03,  # rough estimate before we have post data
            community_connections=0,
            language_match=lang_match,
            hashtag_match=1,
        )

        # Basic male/brand filter — skip obviously non-female accounts
        username_lower = (p.get("username") or "").lower()
        bio_lower = (bio or "").lower()
        male_signals = ["official", "motors", "cars", "auto", "jewelry", "jewellery",
                        "restaurant", "food", "hotel", "real estate", "property",
                        "photography", "photographer"]
        if any(s in username_lower or s in bio_lower for s in male_signals):
            filtered_out += 1
            continue

        source = "seed" if p.get("username") in seed_profiles else "hashtag_discovery"

        row = {
            "platform": platform,
            "platform_user_id": p.get("platform_user_id"),
            "username": p.get("username"),
            "full_name": p.get("full_name"),
            "bio": bio,
            "profile_url": p.get("profile_url"),
            "profile_pic_url": p.get("profile_pic_url"),
            "followers": followers,
            "following": p.get("following"),
            "post_count": p.get("post_count"),
            "is_verified": p.get("is_verified", False),
            "market_id": market_id,
            "run_id": run_id,
            "source_type": source,
            "tier": 1,
            "language_detected": lang,
            **scores,
        }

        enriched.append({**row, "_related_profiles": p.get("related_profiles", [])})

    log.info(f"[Stage 2] {len(enriched)} passed filter, {filtered_out} filtered out")

    if enriched:
        db_rows = [{k: v for k, v in r.items() if not k.startswith("_")} for r in enriched]
        upsert("creators", db_rows, on_conflict="platform,username")

        # Save raw Apify profile JSON to MinIO
        try:
            from src.storage.minio import upload_bytes, build_path
            import json as _json
            country_iso = market.get("country_code", "XX")
            for r in enriched:
                uid = r.get("platform_user_id") or r.get("username")
                raw_path = f"{platform}/{country_iso}/{uid}/raw_profile.json"
                upload_bytes(
                    _json.dumps(r, ensure_ascii=False, default=str).encode("utf-8"),
                    raw_path, content_type="application/json"
                )
        except Exception as e:
            log.warning(f"Failed to save raw profile JSON to MinIO: {e}")

    return enriched
