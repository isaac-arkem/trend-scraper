"""Hashtag-led profile discovery — find the people behind a hashtag, per country,
then treat each one as a reference account.

The reference pipeline starts from handles somebody already curated. This one
starts from hashtags and a list of countries, works out who is posting under
them, and hands those handles to exactly the same downstream path. The niche is
decided up front (it is what you went looking for), not inferred afterwards.

    hashtags + countries
        -> geo-targeted hashtag search, no media downloaded  (per country)
        -> drop private / mega / brand-agency accounts
        -> reference_accounts rows, niche preset
        -> profile record + picture archived
        -> N posts per profile -> clips -> media in object storage
        -> manifest + per-country JSON

Everything lands where the reference pipeline already puts it, so the dashboard,
the boards and the archive keep working without knowing this ran:

    reference_accounts      one row per discovered profile, topic = the niche
    clips                   their posts, subject_type 'ref', feed_source NULL
    creators                name/followers/avatar, written by run_archive
    <runs>/…/manifest.json  settings and totals for the run
    <media>/profiles/…jpg   profile pictures on the dashboard's own path

Discovery must NOT download video. src.pipeline.trends.scrape_tiktok_feed
hardcodes shouldDownloadVideos=True, which is right for a trends harvest and
pure waste here — discovery only needs the handle. This module calls the actor
itself with downloads off rather than changing trends.py, which other
pipelines depend on.

Country is a search hint, not a verified address. TikTok's region parameter
targets where the search runs, and authorMeta.region came back empty on every
result in testing — so attribution leans on the tags you supply and the bio,
and a per-country count is an indication, not a census.

Example
-------
    python scrape_hashtag_profiles.py \\
        --title "Gulf beauty — western sweep" \\
        --countries "GB,US,AE" \\
        --niche gulf_beauty \\
        --hashtags "grwm,softglam,dubaibeauty" \\
        --posts-per-profile 10 \\
        --max-profiles 25
"""
import argparse
import json
import os
import re
import sys
import time
from datetime import datetime, timezone

from dotenv import load_dotenv

load_dotenv()

from src.apify.client import run_actor                 # noqa: E402
from src.db.client import get_db                       # noqa: E402
from src.pipeline import trends as T                   # noqa: E402
import src.run_archive as A                            # noqa: E402
from src.utils.logger import get_logger                # noqa: E402
from scrape_reference_accounts import (                 # noqa: E402
    aggregate_appearance,
    register_accounts,
    set_request_status,
)

log = get_logger(__name__)

POSTS_PER_PROFILE = 10
MAX_PROFILES      = 25      # per country
PER_TAG           = 30      # posts pulled per hashtag before filtering
RECENCY_DAYS      = 90
MIN_VIEWS         = 300
MAX_FOLLOWERS     = 300_000  # above this it is a brand or a managed influencer
APPEARANCE_SAMPLE = 5

# Generic drop list — brands / agencies / booking desks, niche-agnostic.
NOT_A_PERSON = ["brand:", "brand :", "management", "talent", "casting", "agency",
                "booking", "pr:", "collab", "business inquir", "for business",
                "sponsor", "mcn", "media group", "official account", "fans account",
                "粉丝账号", "商务"]


def now():
    return datetime.now(timezone.utc)


def parse_args():
    p = argparse.ArgumentParser(
        description="Discover profiles from hashtags per country, save as reference accounts.")
    p.add_argument("--title", default="", help="Run name, shown in the request row and manifest")
    p.add_argument("--hashtags", required=True,
                   help="Comma-separated hashtags to search (required — no built-in defaults)")
    p.add_argument("--countries", required=True,
                   help="Comma-separated ISO codes, e.g. GB,US,CA (required)")
    p.add_argument("--niche", required=True,
                   help="Niche assigned to every profile found (required)")
    p.add_argument("--posts-per-profile", type=int, default=POSTS_PER_PROFILE)
    p.add_argument("--max-profiles", type=int, default=MAX_PROFILES,
                   help="Cap on profiles kept per country")
    p.add_argument("--per-tag", type=int, default=PER_TAG,
                   help="Posts fetched per hashtag before filtering")
    p.add_argument("--recency-days", type=int, default=RECENCY_DAYS)
    p.add_argument("--min-views", type=int, default=MIN_VIEWS)
    p.add_argument("--max-followers", type=int, default=MAX_FOLLOWERS,
                   help="Skip accounts above this — brands and managed influencers")
    p.add_argument("--platform", default="both", choices=["tiktok", "instagram", "both"],
                   help="Only TikTok has geo-targeted hashtag search; Instagram is "
                        "global, so its country attribution rests on the tag and bio alone")
    p.add_argument("--no-bio-filter", action="store_true",
                   help="Keep every creator found — skip private / follower / brand-agency drops")
    p.add_argument("--skip-appearance", action="store_true",
                   help="Skip the vision pass — the per-profile cost driver")
    p.add_argument("--discover-only", action="store_true",
                   help="Find and register profiles, don't scrape their posts yet")
    return p.parse_args()


def tags_for(hashtags: str) -> list:
    tags = [t.strip().lstrip("#").lower() for t in (hashtags or "").split(",") if t.strip()]
    if not tags:
        raise SystemExit("--hashtags is required (comma-separated, no built-in defaults)")
    return tags


def _signal(bio: str, followers: int, private: bool, max_followers: int):
    """Generic quality gate — not niche-specific. Niche comes from the tags you searched."""
    bio = (bio or "").lower()
    if private:
        return False, "private account"
    if (followers or 0) > max_followers:
        return False, f"{followers:,} followers — brand/influencer"
    if any(k in bio for k in NOT_A_PERSON):
        return False, "brand/agency bio"
    return True, "passed quality filter"


def matches_niche(author: dict, max_followers: int):
    """TikTok: authorMeta rides along on every hashtag result, so this is free."""
    return _signal(f"{author.get('signature') or ''} {author.get('nickName') or ''}",
                   author.get("fans") or 0, bool(author.get("privateAccount")),
                   max_followers)


def matches_profile(p: dict, max_followers: int):
    """Instagram: hashtag results carry only ownerUsername, so the bio has to be
    fetched first. src.apify.* normalise both platforms to the same shape."""
    return _signal(f"{p.get('bio') or ''} {p.get('full_name') or ''}",
                   p.get("followers") or 0, False, max_followers)


def discover_tiktok(tags, iso, a):
    """Geo-targeted hashtag search -> creators that match the niche.

    Calls the actor directly so shouldDownloadVideos stays OFF: discovery needs
    the handle and the bio, never the video file.
    """
    raw = run_actor(
        "clockworks/tiktok-scraper",
        {"hashtags": tags,
         "resultsPerPage": a.per_tag,
         "shouldDownloadVideos": False,
         "shouldDownloadCovers": False,
         "shouldDownloadSlideshowImages": False,
         **({"region": iso} if iso else {})},
        max_items=a.per_tag * len(tags),
        label=f"discover:tt:{iso}",
    )

    seen, kept, rejected = {}, {}, {}
    for i in raw:
        if (i.get("playCount") or 0) < a.min_views:
            continue
        if T._hours_since(i.get("createTimeISO")) > a.recency_days * 24:
            continue
        au = i.get("authorMeta") or {}
        h = (au.get("name") or au.get("uniqueId") or "").lower()
        if not h:
            continue
        if h in seen:
            seen[h]["posts"] += 1
            continue
        ok, why = (True, "filter off") if a.no_bio_filter else matches_niche(au, a.max_followers)
        rec = {"handle": h, "platform": "tiktok", "posts": 1,
               "followers": au.get("fans") or 0, "nick": au.get("nickName") or "",
               "bio": (au.get("signature") or "")[:120], "reason": why}
        seen[h] = rec
        (kept if ok else rejected)[h] = rec

    log.info(f"  tiktok: {len(raw)} posts -> {len(seen)} creators -> {len(kept)} match")
    return list(kept.values()), list(rejected.values())


def discover_instagram(tags, iso, a):
    """IG has no geo parameter, and its hashtag results carry only a username —
    no bio, no follower count. So candidates are collected first and their
    profiles fetched in ONE batched call, then filtered. Fetching per-candidate
    instead would multiply the bill by the number of people we end up rejecting,
    and the probe suggests that is most of them.
    """
    from src.apify.instagram import scrape_profiles as ig_profiles

    # Not T.scrape_ig_feed: that keeps only type == "Video", and the hashtag
    # actor returns overwhelmingly Images and Sidecars — 20 items under
    # #中国留学生 came back as 19 Image + 1 Sidecar, so the video filter threw
    # away every candidate. Discovery only needs ownerUsername, so take them all.
    posts = run_actor("apify/instagram-hashtag-scraper",
                      {"hashtags": tags, "resultsLimit": a.per_tag},
                      label=f"discover:ig:{iso}")
    counts = {}
    for c in posts:
        h = (c.get("ownerUsername") or "").lower()
        if h:
            counts[h] = counts.get(h, 0) + 1
    if not counts:
        log.info("  instagram: no candidates")
        return [], []

    candidates = sorted(counts, key=lambda h: -counts[h])[:a.per_tag * 2]
    log.info(f"  instagram: {len(posts)} posts -> {len(counts)} creators "
             f"-> fetching {len(candidates)} bios")
    profiles = ig_profiles(candidates)

    kept, rejected = [], []
    for p in profiles:
        u = (p.get("username") or "").lower()
        if not u:
            continue
        ok, why = (True, "filter off") if a.no_bio_filter else matches_profile(p, a.max_followers)
        rec = {"handle": u, "platform": "instagram", "posts": counts.get(u, 1),
               "followers": p.get("followers") or 0, "nick": p.get("full_name") or "",
               "bio": (p.get("bio") or "")[:120], "reason": why}
        (kept if ok else rejected).append(rec)
    log.info(f"  instagram: {len(kept)} match the niche")
    return kept, rejected


def discover(tags, iso, a):
    """Both platforms, ranked together. More posts under a niche tag is a better
    signal of belonging than one viral hit."""
    kept, rejected = [], []
    if a.platform in ("tiktok", "both"):
        k, r = discover_tiktok(tags, iso, a)
        kept += k
        rejected += r
    if a.platform in ("instagram", "both"):
        try:
            k, r = discover_instagram(tags, iso, a)
            kept += k
            rejected += r
        except Exception as e:
            log.warning(f"  instagram discovery failed — {str(e)[:110]}")
    for r in rejected[:3]:
        log.info(f"     rejected @{r['handle']}: {r['reason']}")
    ranked = sorted(kept, key=lambda r: (-r["posts"], -min(r["followers"], 50_000)))
    return ranked, rejected, []


def main():
    a = parse_args()
    countries = [c.strip().upper() for c in a.countries.split(",") if c.strip()]
    if not countries:
        raise SystemExit("--countries is required (comma-separated ISO codes)")
    niche = a.niche.strip().lower().replace(" ", "_")
    if not niche:
        raise SystemExit("--niche is required")
    tags = tags_for(a.hashtags)
    request_id = os.environ.get("REQUEST_ID")
    run_key = f"{niche}-{now():%Y%m%d-%H%M%S}"

    db = get_db()
    set_request_status(db, request_id, "running")

    log.info(f"[{a.title or run_key}] {len(countries)} countries | niche={niche} "
             f"| tags={','.join(tags)} "
             f"| {a.posts_per_profile} posts/profile | max {a.max_profiles} profiles/country "
             f"| <= {a.max_followers:,} followers")

    t0 = time.time()
    seen_handles = set()
    summary = {"run_key": run_key, "title": a.title, "niche": niche,
               "hashtags": tags, "countries": {}, "started_at": now().isoformat()}
    appearance_on = not a.skip_appearance
    grand_profiles = grand_clips = 0

    try:
        for iso in countries:
            log.info(f"── {iso} — {', '.join('#' + t for t in tags)}")
            try:
                ranked, rejected, raw = discover(tags, iso, a)
            except Exception as e:
                log.warning(f"  {iso}: discovery failed — {str(e)[:120]}")
                summary["countries"][iso] = {"error": str(e)[:200]}
                continue

            fresh = [r for r in ranked if r["handle"] not in seen_handles][:a.max_profiles]
            log.info(f"  keeping {len(fresh)} (cap {a.max_profiles}, "
                     f"{len(ranked) - len(fresh)} dropped as duplicate or over cap)")

            prefix = A.run_prefix(a.platform, iso)
            A.put_json(f"{prefix}/discovered.json",
                       {"tags": tags, "kept": ranked, "rejected": rejected})

            if not fresh:
                summary["countries"][iso] = {"creators": len(ranked), "new": 0,
                                             "profiles": [], "clips": 0}
                continue

            handles = [r["handle"] for r in fresh]
            # register_accounts reads the platform off the URL; a bare handle
            # defaults to TikTok, so Instagram accounts must arrive as URLs or
            # they would all be registered on the wrong platform.
            spec = ",".join(
                (f"https://www.instagram.com/{r['handle']}/={niche}"
                 if r["platform"] == "instagram" else f"{r['handle']}={niche}")
                for r in fresh)
            register_accounts(db, spec, region=iso)
            seen_handles.update(handles)

            pstats = {}
            for plat in sorted({r["platform"] for r in fresh}):
                hs = [r["handle"] for r in fresh if r["platform"] == plat]
                try:
                    st = A.fetch_and_store_profiles(hs, plat, iso, run_key)
                    pstats[plat] = st
                    log.info(f"  {plat}: {st['fetched']} profiles, {st['pics']} pictures, "
                             f"{st['skipped']} already held")
                except Exception as e:
                    pstats[plat] = {"error": str(e)[:150]}
                    log.warning(f"  {plat} profile archive failed — {str(e)[:110]}")

            country_clips = 0
            per_profile = []
            if not a.discover_only:
                accts = (db.table("reference_accounts").select("*")
                         .eq("active", True).in_("handle", handles).execute().data or [])
                for n, acct in enumerate(accts, 1):
                    h, plat = acct["handle"], acct.get("platform") or "tiktok"
                    log.info(f"  [{n}/{len(accts)}] @{h} ({plat})")
                    try:
                        fetch = (T.scrape_ig_watchlist if plat == "instagram"
                                 else T.scrape_tiktok_watchlist)
                        clips = fetch([h], per_handle=a.posts_per_profile,
                                      recency_days=a.recency_days)
                    except Exception as e:
                        log.warning(f"     fetch failed: {str(e)[:80]}")
                        continue

                    for c in clips:
                        c["feed_source"] = None
                        c["topic"] = acct.get("topic") or niche
                        c["region"] = acct.get("region") or iso
                        c["subject_type"] = "ref"

                    saved = T.process_clips(clips, workers=8) if clips else 0
                    country_clips += saved
                    log.info(f"     {len(clips)} clips -> {saved} saved")

                    appearance = None
                    if appearance_on and clips:
                        covers = [c.get("_cover_url") for c in clips if c.get("_cover_url")]
                        try:
                            appearance = aggregate_appearance(covers[:APPEARANCE_SAMPLE])
                        except Exception as e:
                            log.warning(f"     appearance off for the rest — {str(e)[:70]}")
                            appearance_on = False

                    upd = {"scraped_at": now().isoformat()}
                    if appearance is not None:
                        upd["appearance"] = appearance
                    db.table("reference_accounts").update(upd).eq("id", acct["id"]).execute()
                    per_profile.append({"handle": h, "clips_saved": saved,
                                        "followers": next((r["followers"] for r in fresh
                                                           if r["handle"] == h), None)})

            summary["countries"][iso] = {
                "tags": tags, "creators": len(ranked), "new": len(fresh),
                "rejected": len(rejected),
                "profiles": per_profile or [{"handle": h} for h in handles],
                "clips": country_clips, "profile_archive": pstats,
            }
            grand_profiles += len(fresh)
            grand_clips += country_clips

            A.write_manifest(a.platform, iso, run_key,
                             settings={"hashtags": tags, "niche": niche,
                                       "posts_per_profile": a.posts_per_profile,
                                       "max_profiles": a.max_profiles,
                                       "max_followers": a.max_followers,
                                       "recency_days": a.recency_days,
                                       "min_views": a.min_views, "title": a.title},
                             totals={"creators": len(ranked), "registered": len(fresh),
                                     "rejected": len(rejected), "clips_saved": country_clips})

        summary["finished_at"] = now().isoformat()
        summary["minutes"] = round((time.time() - t0) / 60, 1)
        summary["totals"] = {"profiles": grand_profiles, "clips": grand_clips}
        summary["by_country"] = {k: v.get("new", 0) for k, v in summary["countries"].items()}

        A.put_json(f"runs/{a.platform}/{now():%Y/%m/%d}/{run_key}-summary.json", summary)
        out = f"run-{run_key}.json"
        with open(out, "w") as fh:
            json.dump(summary, fh, indent=2, ensure_ascii=False)

        log.info("━━ COMPLETE ━━")
        for iso, n in sorted(summary["by_country"].items(), key=lambda x: -x[1]):
            log.info(f"   {iso}: {n} profiles")
        log.info(f"   {grand_profiles} profiles, {grand_clips} clips, "
                 f"{summary['minutes']}m — written to {out}")
        set_request_status(db, request_id, "success")

    except Exception as e:
        log.error(f"FATAL — {e}")
        set_request_status(db, request_id, "failed", error_message=str(e))
        raise


if __name__ == "__main__":
    main()
