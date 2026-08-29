"""Hashtag-led profile discovery — find the people behind a hashtag, per country,
then treat each one as a reference account.

The reference pipeline starts from handles somebody already curated. This one
starts from hashtags and a list of countries, works out who is posting under
them, and hands those handles to exactly the same downstream path. The niche is
decided up front (it is what you went looking for), not inferred afterwards.

    hashtags + countries
        -> geo-targeted hashtag search, no media downloaded  (per country)
        -> filter to people who actually match the niche
        -> reference_accounts rows, niche preset
        -> profile record + picture archived
        -> N posts per profile (TT videos; IG Reels + photo/carousel) -> clips
           -> media in object storage (Wasabi by default)
        -> manifest + per-country JSON

Everything lands where the reference pipeline already puts it, so the dashboard,
the boards and the archive keep working without knowing this ran:

    reference_accounts      one row per discovered profile, topic = the niche
    clips                   their posts, subject_type 'ref', feed_source NULL
    creators                name/followers/avatar, written by run_archive
    <runs>/…/manifest.json  settings and totals for the run
    <media>/profiles/…jpg   profile pictures on the dashboard's own path

Two things learned from the first UK probe, both of which cost money to find out:

* Discovery must NOT download video. src.pipeline.trends.scrape_tiktok_feed
  hardcodes shouldDownloadVideos=True, which is right for a trends harvest and
  pure waste here — discovery only needs the handle. This module calls the actor
  itself with downloads off rather than changing trends.py, which other
  pipelines depend on.

* A hashtag alone does not identify the niche. 120 posts under four tags gave
  108 creators, of which a large share were brand accounts, agency-managed
  influencers and people studying abroad who were not Chinese students at all.
  So a bio filter runs before anything is registered or scraped, and generic
  tags like #studyabroad are deliberately not in the defaults.

Country is a search hint, not a verified address. TikTok's region parameter
targets where the search runs, and authorMeta.region came back empty on every
result in testing — so attribution leans on the country-specific tag and the
bio, and a per-country count is an indication, not a census. That caveat has to
travel with any number used to make a decision.

Example
-------
    python creator_intelligence.py \\
        --title "Egyptian creators" \\
        --hashtags "egyptgirls,cairocreators" \\
        --countries "EG" \\
        --niche egypt_creators \\
        --posts-per-profile 10 \\
        --max-profiles 25
"""
from typing import Optional
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
from src.storage.minio import get_minio                # noqa: E402
from src.utils.logger import get_logger                # noqa: E402
from scrape_reference_accounts import (                 # noqa: E402
    aggregate_appearance,
    register_accounts,
    set_request_status,
)

log = get_logger(__name__)

POSTS_PER_PROFILE = 20   # per creator, raised from 10
MAX_PROFILES      = 25      # per country
PER_TAG           = 30      # posts pulled per hashtag before filtering
RECENCY_DAYS      = 90
MIN_VIEWS         = 300
MAX_FOLLOWERS     = 300_000  # above this it is a brand or a managed influencer
APPEARANCE_SAMPLE = 5

# No hardcoded hashtag lists — hashtags are always passed from the UI via
# --hashtags. If none are given the run errors out early rather than silently
# searching for a niche the operator never asked for.
DEFAULT_COUNTRIES = ["GB", "US", "CA", "AU", "NZ", "IE", "DE", "NL"]

# Brand, agency and management accounts — not real creators, whatever they tag.
NOT_A_PERSON = ["brand:", "brand :", "management", "talent", "casting", "agency",
                "booking", "pr:", "collab", "business inquir", "for business",
                "sponsor", "mcn", "media group", "official account", "fans account",
                "scholarships matched", "study abroad made"]



# ── stage markers ───────────────────────────────────────────────────────────
# Emitted on their own line in a fixed format so a consumer can read progress
# without parsing prose that will change. api.py turns the most recent one into
# the `stage` field on GET /runs/{id}, which is what the console polls.
#
#   STAGE|<n>|<total>|<key>|<human text>
#
# Printed rather than logged: the logger wraps and colourises long lines, which
# would break a parser on exactly the runs that matter most — the long ones.
STAGES = [
    (1, "discover", "Searching hashtags and finding creators"),
    (2, "profiles", "Fetching profiles and avatars"),
    (3, "posts",    "Collecting posts"),
    (4, "media",    "Downloading videos and images"),
    (5, "vision",   "Analysing appearance"),
    (6, "done",     "Finished"),
]
STAGE_TOTAL = len(STAGES)


def stage(key: str, detail: str = "") -> None:
    for n, k, text in STAGES:
        if k == key:
            print(f"STAGE|{n}|{STAGE_TOTAL}|{k}|{detail or text}", flush=True)
            return


def now():
    return datetime.now(timezone.utc)


def parse_args():
    p = argparse.ArgumentParser(
        description="Discover profiles from hashtags per country, save as reference accounts.")
    p.add_argument("--title", default="", help="Run name, shown in the request row and manifest")
    p.add_argument("--hashtags", default="",
                   help="Comma-separated hashtags to search. REQUIRED — no built-in "
                        "fallback tags exist.")
    p.add_argument("--countries", default=",".join(DEFAULT_COUNTRIES),
                   help="Comma-separated ISO codes, e.g. GB,US,CA")
    p.add_argument("--niche", required=True,
                   help="REQUIRED. Niche slug every profile and clip is filed under, "
                        "e.g. egypt_creators. Created if it doesn't exist yet.")
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
                   help="Keep every creator found. Only for widening a sweep that "
                        "came back empty — it lets non-students through.")
    p.add_argument("--skip-appearance", action="store_true",
                   help="Skip the appearance pass entirely — no OpenAI spend at all")
    p.add_argument("--vision-budget-eur", type=float, default=44.0,
                   help="Hard ceiling for the WHOLE run, every country included. "
                        "The per-creator image allowance is derived from it.")
    p.add_argument("--max-images-per-creator", type=int, default=3,
                   help="Never analyse more than this per creator, budget allowing")
    p.add_argument("--discover-only", action="store_true",
                   help="Find and register profiles, don't scrape their posts yet")
    return p.parse_args()


def tags_for(iso: str, override: str) -> list:
    if not override:
        raise ValueError("--hashtags is required: no built-in fallback tags exist")
    return [t.strip().lstrip("#").lower() for t in override.split(",") if t.strip()]


def _signal(bio: str, followers: int, private: bool, max_followers: int):
    """Generic creator filter — rejects brands/agencies and private/empty accounts,
    accepts everyone else. Niche relevance comes from the hashtags searched, not
    from bio keywords."""
    bio = (bio or "").lower()
    if private:
        return False, "private account"
    if (followers or 0) > max_followers:
        return False, f"{followers:,} followers — brand/influencer"
    if any(k in bio for k in NOT_A_PERSON):
        return False, "brand/agency bio"
    return True, "accepted"


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


def save_creators(profiles: list, niche_id: Optional[int], niche: str,
                  country: str) -> int:
    """Discovered people go in `creators`. That is the creator table.

    They were being written only to reference_accounts, which is for accounts a
    human curated by hand — so creator intelligence output was landing in the
    wrong place and could not be queried alongside the 2,885 creators already
    there. reference_accounts still gets a row because the scrape loop reads its
    handles, but `creators` is the record.

    Upserted on (platform, username) so a re-run refreshes rather than
    duplicating — which is what makes "Re-run" safe.
    """
    if not profiles:
        return 0
    db = get_db()
    rows = []
    for p in profiles:
        uname = (p.get("username") or p.get("handle") or "").lower().lstrip("@")
        if not uname:
            continue
        rows.append({
            "platform": p.get("platform") or "tiktok",
            "username": uname,
            "platform_user_id": p.get("platform_user_id"),
            "full_name": p.get("full_name") or p.get("display_name"),
            "bio": p.get("bio"),
            "followers": p.get("followers"),
            "following": p.get("following"),
            "post_count": p.get("post_count"),
            "is_verified": bool(p.get("is_verified")),
            "profile_url": p.get("profile_url"),
            "profile_pic_url": p.get("profile_pic_url") or p.get("profile_pic"),
            "niche_id": niche_id,
            # Says how this creator was found, so hashtag discovery can be told
            # apart from a curated add later on.
            "source_type": "hashtag_discovery",
            "scraped_at": now().isoformat(),
        })
    saved = 0
    for i in range(0, len(rows), 100):
        try:
            r = db.table("creators").upsert(rows[i:i + 100],
                                            on_conflict="platform,username").execute()
            saved += len(r.data or [])
        except Exception as e:
            log.warning(f"[creators] upsert failed — {str(e)[:120]}")
            break
    log.info(f"[creators] {saved} rows in `creators` for {country} (niche {niche_id})")
    return saved


def get_or_create_niche(slug: str, hashtags: list, countries: list) -> Optional[int]:
    """The niche row this run belongs to, created on first use.

    A niche is picked or invented by the operator; a hashtag is one of several
    ways to search for it. Keeping them in separate places is the whole point —
    running #chinesestudentsuk and #英国留学生 must land in ONE niche, not two.
    Returns the id, or None if the table isn't there yet (older database), which
    must not stop the run.
    """
    slug = (slug or "").strip().lower().replace(" ", "_")
    if not slug:
        return None
    db = get_db()
    try:
        found = db.table("niches").select("id").eq("slug", slug).limit(1).execute().data
        if found:
            return found[0]["id"]
        row = db.table("niches").insert({
            "slug": slug,
            "label": slug.replace("_", " ").title(),
            "hashtags": hashtags or [],
            "countries": countries or [],
            "created_by": "creator_intelligence",
        }).execute().data
        nid = row[0]["id"] if row else None
        log.info(f"[niche] created '{slug}' (id {nid})")
        return nid
    except Exception as e:
        log.warning(f"[niche] unavailable, continuing without it — {str(e)[:90]}")
        return None


def tag_niche(niche_id: Optional[int], post_ids: list, handles: list) -> None:
    """Stamp niche_id onto everything this run produced.

    Done as a pass at the end rather than inline: the clips are written by
    shared code that has no concept of a niche, and this file is the only place
    that knows which niche the run was for.
    """
    if not niche_id:
        return
    db = get_db()
    for i in range(0, len(post_ids), 100):
        try:
            db.table("clips").update({"niche_id": niche_id})\
              .in_("platform_post_id", post_ids[i:i + 100]).execute()
        except Exception as e:
            log.warning(f"[niche] clip tagging failed — {str(e)[:80]}")
            break
    for i in range(0, len(handles), 100):
        try:
            db.table("reference_accounts").update({"niche_id": niche_id})\
              .in_("handle", handles[i:i + 100]).execute()
        except Exception as e:
            log.warning(f"[niche] account tagging failed — {str(e)[:80]}")
            break
    log.info(f"[niche] tagged {len(post_ids)} clips, {len(handles)} accounts -> niche {niche_id}")


def save_cover_images(pending: list) -> dict:
    """Download every clip's cover frame into object storage.

    Only the mp4 was ever stored. An image post therefore left no media at all,
    the console had no thumbnail, and anything wanting to look at the picture had
    to go back to the platform CDN — where the URL carries an expiry and starts
    returning 403 within days. Stored next to the video, same bucket.
    """
    if not pending:
        return {"attempted": 0, "saved": 0, "already_held": 0, "failed": 0}

    db = get_db()
    stats = {"attempted": len(pending), "saved": 0, "already_held": 0, "failed": 0}
    log.info(f"[media] {len(pending)} cover images")

    def one(item):
        path = f"{item['platform']}/cover/{item['post_id']}.jpg"
        try:
            if A.exists(path, A.RUNS_BUCKET):
                return "already_held", item, path
            img = A.download(item["cover"])
            if not img:
                return "failed", item, None
            if not A.put_bytes(path, img, "image/jpeg", A.RUNS_BUCKET):
                return "failed", item, None
            return "saved", item, path
        except Exception as e:
            log.debug(f"  cover failed {item['post_id']}: {str(e)[:60]}")
            return "failed", item, None

    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=8) as ex:
        for outcome, item, path in ex.map(one, pending):
            stats[outcome] += 1
            if path:
                item["image_path"] = path
                try:
                    db.table("clips").update({"image_minio_path": path})\
                      .eq("platform_post_id", item["post_id"]).execute()
                except Exception:
                    # The column may not exist yet on an un-migrated database.
                    # The image is still in storage and the path is deterministic,
                    # so this is recoverable — it must not fail the run.
                    pass
    log.info(f"[media] saved {stats['saved']}, already held {stats['already_held']}, "
             f"failed {stats['failed']}")
    return stats


def run_appearance(pending: list, a) -> dict:
    """Two-stage appearance analysis over the whole run, inside one budget.

    Stage 1 is a cheap model answering only 'is there an adult person here'.
    Only images that pass reach the expensive full read. Children and adverts
    are rejected at the gate and their images are never sent onward — that is a
    safeguarding rule, so it is enforced in code, not requested in a prompt.

    The budget covers the entire run, every country in it. Because the scrape is
    already finished, the creator count is exact, and the per-creator image
    allowance is derived from it before any spending starts.
    """
    from collections import defaultdict
    from src.ai.appearance import (Budget, analyse_creator, eur_to_usd,
                                   plan_images_per_creator)

    with_images = [p for p in pending if p.get("image_path")]
    if not with_images:
        return {"skipped": True, "reason": "no stored images to analyse"}

    by_creator = defaultdict(list)
    for p in with_images:
        by_creator[p["handle"]].append(p)

    budget = Budget(eur_to_usd(a.vision_budget_eur))
    per_creator = plan_images_per_creator(len(by_creator), budget,
                                          max_per_creator=a.max_images_per_creator)
    log.info(f"[vision] {len(by_creator)} creators, budget €{a.vision_budget_eur:.0f} "
             f"(${budget.cap:.2f}) → {per_creator} image(s) each")

    db = get_db()
    counts = defaultdict(int)
    analysed_creators = 0

    for handle, items in by_creator.items():
        if budget.remaining() <= 0:
            budget.mark_stopped()
            break
        images = []
        for it in items[:per_creator]:
            try:
                bucket, key = _resolve_media(it["image_path"])
                images.append((it["post_id"], get_minio().get_object(bucket, key).read()))
            except Exception:
                continue
        if not images:
            continue

        for v in analyse_creator(images, budget, per_creator):
            counts[v["stage"]] += 1
            row = {"vision_checked": True, "vision_stage": v["stage"]}
            if v["stage"] == "analysed":
                row["appearance"] = v["appearance"]
                row["subject_type"] = T.subject_from_vision(v["appearance"])
                row["suitability_flag"] = row["subject_type"] in T.SUBJECT_TYPES
            try:
                db.table("clips").update(row).eq("platform_post_id", v["clip_id"]).execute()
            except Exception as e:
                log.debug(f"  clip update failed: {str(e)[:60]}")
        analysed_creators += 1

    out = {
        "creators_seen": analysed_creators,
        "images_per_creator": per_creator,
        "spent_usd": round(budget.spent, 4),
        "spent_eur": round(budget.spent / float(os.environ.get("EUR_USD_RATE", "1.08")), 2),
        "budget_eur": a.vision_budget_eur,
        "stopped_on_budget": budget.stopped_early,
        "by_outcome": dict(counts),
    }
    log.info(f"[vision] {dict(counts)} · spent €{out['spent_eur']:.2f} of "
             f"€{a.vision_budget_eur:.0f}" + ("  (CAP REACHED)" if budget.stopped_early else ""))
    return out


def _resolve_media(path: str):
    from src.storage.minio import resolve
    return resolve(A.RUNS_BUCKET, path)


def main():
    a = parse_args()
    countries = [c.strip().upper() for c in a.countries.split(",") if c.strip()]
    niche = a.niche.strip().lower().replace(" ", "_")
    request_id = os.environ.get("REQUEST_ID")
    run_key = f"{niche}-{now():%Y%m%d-%H%M%S}"

    db = get_db()
    set_request_status(db, request_id, "running")

    log.info(f"[{a.title or run_key}] {len(countries)} countries | niche={niche} "
             f"| {a.posts_per_profile} posts/profile | max {a.max_profiles} profiles/country "
             f"| <= {a.max_followers:,} followers")

    t0 = time.time()
    seen_handles = set()
    summary = {"run_key": run_key, "title": a.title, "niche": niche,
               "countries": {}, "started_at": now().isoformat()}
    appearance_on = not a.skip_appearance
    # Cover URLs collected across every country, for the media and appearance
    # phases that run once the whole scrape is done.
    pending_media = []
    all_handles = []
    # The niche exists before anything is scraped — everything this run produces
    # is filed under it, so it must not depend on the run succeeding.
    niche_id = get_or_create_niche(niche, [t for t in (a.hashtags or "").split(",") if t],
                                   [c for c in (a.countries or "").split(",") if c])
    summary["niche_id"] = niche_id
    grand_profiles = grand_clips = grand_creators = 0

    try:
        for iso in countries:
            tags = tags_for(iso, a.hashtags)
            log.info(f"── {iso} — {', '.join('#' + t for t in tags)}")
            stage("discover", f"Searching {len(tags)} hashtags in {iso}")
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
            # The creator table is `creators`. reference_accounts above is
            # pipeline plumbing — the scrape loop reads its handles — but the
            # record of who was found belongs with the other 2,885 creators,
            # not in the table meant for hand-curated accounts.
            grand_creators += save_creators(fresh, niche_id, niche, iso)
            seen_handles.update(handles)

            stage("profiles", f"Fetching {len(fresh)} profiles in {iso}")
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

            stage("posts", f"Collecting posts for {len(fresh)} creators in {iso}")
            country_clips = 0
            all_handles.extend(handles)
            per_profile = []
            if not a.discover_only:
                accts = (db.table("reference_accounts").select("*")
                         .eq("active", True).in_("handle", handles).execute().data or [])
                for n, acct in enumerate(accts, 1):
                    h, plat = acct["handle"], acct.get("platform") or "tiktok"
                    log.info(f"  [{n}/{len(accts)}] @{h} ({plat})")
                    try:
                        if plat == "instagram":
                            # Reels (video+sound) + photo/carousel posts — same
                            # split as scrape_reference_accounts. TikTok stays
                            # video-only.
                            clips = T.scrape_ig_watchlist(
                                [h], per_handle=a.posts_per_profile,
                                recency_days=a.recency_days)
                            try:
                                images = T.scrape_ig_images(
                                    [h], per_handle=a.posts_per_profile,
                                    recency_days=a.recency_days)
                            except Exception as e:
                                log.warning(f"     IG images failed: {str(e)[:80]}")
                                images = []
                            seen = {c.get("platform_post_id") for c in clips}
                            extra = [im for im in images
                                     if im.get("platform_post_id") not in seen]
                            if extra:
                                log.info(f"     + {len(extra)} photo/carousel posts")
                            clips = clips + extra
                        else:
                            clips = T.scrape_tiktok_watchlist(
                                [h], per_handle=a.posts_per_profile,
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
                    n_img = sum(1 for c in clips
                                if c.get("_image_dl") and not c.get("_video_dl"))
                    log.info(f"     {len(clips)} clips -> {saved} saved"
                             + (f" ({n_img} images)" if n_img else ""))

                    # Cover / photo URLs are held for the media + vision phases,
                    # which run once the whole scrape is finished. They are kept
                    # here because this is the only point they exist: the actor
                    # returns them, they are not always persisted yet, and they
                    # expire from the platform CDN within days.
                    for c in clips:
                        cover = c.get("_image_dl") or c.get("_cover_url")
                        if cover and c.get("platform_post_id"):
                            pending_media.append({
                                "post_id": c["platform_post_id"],
                                "platform": c["platform"],
                                "cover": cover,
                                "handle": h,
                                "country": iso,
                            })

                    db.table("reference_accounts").update(
                        {"scraped_at": now().isoformat()}).eq("id", acct["id"]).execute()
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

        tag_niche(niche_id, [m["post_id"] for m in pending_media], all_handles)

        # ── phase 2 · media ────────────────────────────────────────────────
        # Videos (and IG photo posts) are already in object storage via
        # process_clips. This pass mirrors any remaining cover/photo URLs so
        # appearance has a local jpg even if the earlier upload missed one.
        stage("media", f"Downloading {len(pending_media)} images")
        summary["media"] = save_cover_images(pending_media)

        # ── phase 3 · appearance ───────────────────────────────────────────
        # Deliberately after the whole scrape, not during it. The creator count
        # is only known once every country is done, and that count is what the
        # budget is divided by — starting earlier would mean guessing.
        stage("vision", "Analysing appearance")
        summary["vision"] = run_appearance(pending_media, a) if not a.skip_appearance else {
            "skipped": True, "reason": "--skip-appearance"}

        summary["finished_at"] = now().isoformat()
        summary["minutes"] = round((time.time() - t0) / 60, 1)
        summary["totals"] = {"profiles": grand_profiles, "clips": grand_clips,
                             "creators": grand_creators}
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
