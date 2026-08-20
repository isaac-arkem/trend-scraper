"""Weekly trend harvest — top posts per hashtag, per country, into clips +
a frozen board in trend_signals.

    seed hashtags  ->  20 posts each, geo-targeted  ->  clips  ->  board

Run:
    .venv/bin/python scrape_weekly_trends.py --markets AE --dry-run
    .venv/bin/python scrape_weekly_trends.py --markets AE,SA,BR
    .venv/bin/python scrape_weekly_trends.py                      # all 19

Deliberate cost choices, because this runs across 19 countries every week:

  * NO vision. process_clips() runs a gpt-4o call per clip unless subject_type
    is already set, so every clip is marked 'unscanned' here. At 19 countries x
    50 tags x 20 posts that call would dominate the entire budget. Scan the
    breakouts afterwards instead — they are the only ones anyone looks at.
  * NO video download. shouldDownloadVideos stays off; 8.7k clips already put
    44 GB in MinIO. Fetch video later, for clips that earned it.
  * Subtitles ARE on. TikTok's own captions are free with the scrape and cannot
    be recovered later without paying for the whole scrape again.
"""
import argparse
import os
import re
import sys
import time
from collections import Counter, defaultdict
from datetime import datetime, timezone, timedelta

from dotenv import load_dotenv
load_dotenv()

from src.apify.client import run_actor
from src.db.client import get_db, upsert
from src.pipeline import trends as T
from src.utils.logger import get_logger
import src.signals as S
import src.run_archive as A

log = get_logger(__name__)

POSTS_PER_TAG   = 20
TAGS_PER_MARKET = 40
RECENCY_DAYS    = 7
MIN_VIEWS       = 5_000
TOP_N           = 50          # rows per board

# scored clips a row needs, per board kind
# Lowered after BOARD_STOP_TAGS removed the generic tags: with #fyp and #dance
# stripped out, few genuine local hashtags reach 5 scored clips inside a 7-day
# window, and half the countries lost their hashtag board entirely.
MIN_BUCKET = {"hashtag": 3, "sound": 2, "creator": 2, "niche": 3}


def now():
    return datetime.now(timezone.utc)


# ── run status, so a caller can poll instead of tailing a log ───────────────
# The other two pipelines already report into a requests table; this one did
# not, so a UI could start a trends run and then had no way to know whether it
# was still going, finished, or had died. REQUEST_ID is set by whatever
# launches the script.

def set_status(db, request_id, status, error=None, progress=None):
    if not request_id:
        return
    upd = {"status": status}
    if error:
        upd["error_message"] = str(error)[:500]
    if progress is not None:
        upd["final_progress"] = progress
    if status in ("success", "failed"):
        upd["completed_at"] = now().isoformat()
    try:
        db.table("dance_scrape_requests").update(upd).eq("id", request_id).execute()
    except Exception as e:
        log.warning(f"status update failed: {str(e)[:80]}")


def run_key():
    y, w, _ = now().isocalendar()
    return f"{y}-W{w:02d}"


# ── seeds ───────────────────────────────────────────────────────────────────

STOP_TAGS = {
    # carried by everything, so they discriminate nothing
    "fyp", "foryou", "foryoupage", "viral", "trending", "trend", "tiktok",
    "fypage", "fyppppppppppppppppppppppp", "capcut", "capcutpioneer", "xyzbca",
    "viralvideo", "viraltiktok", "explore", "trendingnow", "foryoupageofficiall",
    # the legacy dance-scraper vocabulary. Seeded from dance_feeds on the first
    # run, these went out to all 17 countries and turned every board into the
    # same global dance report. Blacklisted so they can never lead again.
    "dance", "dancetok", "dancechallenge", "dancetrend", "dancechallenge2026",
    "newdancechallenge", "trendingdance", "brazildance", "dance2026",
    "dancer", "dancing", "dancevideo", "dancemoves", "braziliandance",
    "blowthisup", "tryit", "reels", "reelsdance",
}

# TikTok returns only ~3 trending hashtags per country per industry, so sweeping
# a handful of industries is what turns 3 real tags into a usable seed list.
TRENDING_INDUSTRIES = ["All Industries", "Beauty & Personal Care", "Food & Beverage",
                       "Entertainment", "Health & Fitness", "Gaming", "Sports & Outdoor",
                       "Travel", "Apparel & Accessories", "Education"]


TRENDING_ACTOR = "khadinakbar/tiktok-trending-hashtags-scraper"

# Countries the trending actor covers. AM is not among them, so Armenia falls
# back to its configured market tags.
TRENDING_COUNTRIES = {"AE","KW","TR","ZA","EG","MA","SA","NG",
                      "BR","MX","CO","AR","IN","ID","PH","TH"}


def trending_hashtags(country, days=7, limit=40, industry="All Industries"):
    """TikTok's OWN trending hashtag board for a country.

    This is the seed source the whole design assumed existed. The obvious actor
    for it, clockworks/tiktok-trends-scraper, is broken — four attempts across
    five days, including with all-default inputs, every one SUCCEEDED with 0
    items. khadinakbar/tiktok-trending-hashtags-scraper returns the same board
    and works, so that is what we use.

    Returns [(hashtag, rank, posts, views, is_new), ...] in rank order.
    """
    if country not in TRENDING_COUNTRIES:
        return []
    period = "7" if days <= 7 else ("30" if days <= 30 else "90")
    try:
        rows = run_actor(TRENDING_ACTOR,
                         {"timePeriod": period, "country": country,
                          "industry": industry, "maxResults": limit},
                         label=f"trending:{country}")
    except Exception as e:
        log.warning(f"    trending fetch failed for {country}: {str(e)[:90]}")
        return []
    out = []
    for r in rows:
        h = (r.get("hashtag_name") or "").lower().lstrip("#")
        if h and h not in STOP_TAGS:
            out.append((h, r.get("rank"), r.get("post_count") or 0,
                        r.get("video_views") or 0, bool(r.get("is_new_to_top100"))))
    out.sort(key=lambda x: x[1] or 999)
    return out


def seed_tags(db, market_code, region, limit=TAGS_PER_MARKET, market_row=None,
              country=None, days=7):
    """Hashtags to search for ONE country — TikTok's real trending board first.

    Order matters. The first run seeded from dance_feeds and every one of the 17
    countries ended up searching the same 32-37 of 40 tags (#dance, #fyp,
    #viral), so the boards were a global dance report rather than 17 local
    pictures. Trending first fixes that at the source.
    """
    out, seen, meta = [], set(), {}

    def add(t, src, rank=None):
        t = (t or "").lower().lstrip("#").strip()
        if t and t not in seen and t not in STOP_TAGS:
            seen.add(t); out.append(t); meta[t] = (src, rank)
            return True
        return False

    # 1. TikTok's own trending board, swept across industries
    for ind in TRENDING_INDUSTRIES:
        if len(out) >= limit:
            break
        for h, rank, posts, views, is_new in trending_hashtags(
                country or market_code, days=days, limit=40, industry=ind):
            if len(out) >= limit:
                break
            add(h, "trending", rank)
    n_trend = len(out)

    # 2. the market's configured local tags
    for t in ((market_row or {}).get("seed_hashtags") or []):
        if len(out) >= limit:
            break
        add(t, "market")
    n_market = len(out) - n_trend

    # 3. co-occurrence from this country's own recent clips
    if len(out) < limit:
        rows, off = [], 0
        while True:
            q = (db.table("clips").select("hashtags,posted_at,market_code")
                 .not_.is_("hashtags", "null"))
            if market_code:
                q = q.eq("market_code", market_code)
            page = q.range(off, off + 999).execute().data or []
            rows += page; off += 1000
            if len(page) < 1000 or off >= 3000:
                break
        cutoff = (now() - timedelta(days=45)).isoformat()
        freq = Counter()
        for r in rows:
            if (r.get("posted_at") or "") < cutoff:
                continue
            for h in (r.get("hashtags") or []):
                h = (h or "").lower().lstrip("#")
                if h and h not in STOP_TAGS:
                    freq[h] += 1
        room = min(limit, len(out) + max(6, limit // 4))
        for t, _ in freq.most_common(limit * 4):
            if len(out) >= room:
                break
            add(t, "observed")
    n_obs = len(out) - n_trend - n_market

    log.info(f"    seeds: {n_trend} trending + {n_market} market + {n_obs} observed "
             f"= {len(out)}   top: {out[:6]}")
    return out[:limit], meta


# ── harvest ─────────────────────────────────────────────────────────────────

def harvest(tags, market_code, region_code, region_label, feed="weekly",
            per_tag=POSTS_PER_TAG, recency_days=RECENCY_DAYS, min_views=MIN_VIEWS):
    """Posts for these hashtags, geo-targeted, recent only."""
    raw = run_actor(
        "clockworks/tiktok-scraper",
        {"hashtags": tags,
         "resultsPerPage": per_tag,
         "shouldDownloadVideos": False,          # see module docstring
         "shouldDownloadCovers": False,
         "downloadSubtitlesOptions": T.SUBTITLES_OPTION,
         **({"region": region_code} if region_code else {})},
        label=f"weekly:{market_code}",
    )
    kept, stale, thin = [], 0, 0
    for i in raw:
        if (i.get("playCount") or 0) < min_views:
            thin += 1; continue
        if T._hours_since(i.get("createTimeISO")) > recency_days * 24:
            stale += 1; continue
        seed = ""
        for h in (i.get("hashtags") or []):
            n = (h.get("name") or "").lower()
            if n in tags:
                seed = n; break
        c = T.normalize_tiktok(i, feed, seed or (tags[0] if tags else ""),
                               region_label, market_code)
        # skip the paid vision call — backfill on breakouts instead
        c["subject_type"] = "unscanned"
        kept.append(c)
    log.info(f"  {market_code}: {len(raw)} fetched -> {len(kept)} kept "
             f"({stale} older than {recency_days}d, {thin} under {min_views:,} views)")
    return kept, {"fetched": len(raw), "kept": len(kept), "stale": stale,
                  "thin": thin}, raw


# ── niche resolution ────────────────────────────────────────────────────────

def niche_resolver(db):
    """handle -> curated topic, and hashtag -> feed niche. NULL is a valid answer."""
    by_handle = {}
    for r in (db.table("reference_accounts").select("handle,topic").execute().data or []):
        if r.get("handle") and r.get("topic"):
            by_handle[r["handle"].lower()] = S.norm_niche(r["topic"])

    by_tag = {}
    for f in (db.table("dance_feeds").select("slug,tags").execute().data or []):
        n = S.norm_niche(f.get("slug"))
        for t in (f.get("tags") or []):
            by_tag.setdefault((t or "").lower().lstrip("#"), n)

    def resolve(clip):
        h = (clip.get("creator_handle") or "").lower()
        if h in by_handle:
            return by_handle[h], "creator"
        for t in (clip.get("hashtags") or []):
            t = (t or "").lower().lstrip("#")
            if t in by_tag:
                return by_tag[t], "hashtag"
        return None, None            # deliberately unassigned
    return resolve


# ── board ───────────────────────────────────────────────────────────────────

def build_board(db, country, region, rk, clips_for_country, all_clips, sound_names,
                window_days=7, harvested_since=None):
    """Rank hashtags / sounds / creators / videos for one country, freeze to
    trend_signals, and diff against the previous run.

    The board is a WINDOW, not the whole corpus. Weekly runs window to 7 days and
    monthly runs to 30 — "what is working right now" must not be answered with
    clips from two months ago just because they are still in the table.

    Baselines are deliberately computed from the FULL corpus, not the window: an
    account's normal is its normal, and measuring it against one week of its own
    output would make every account look average."""
    baselines = S.account_baselines(all_clips)

    if window_days:
        cutoff = (now() - timedelta(days=window_days)).isoformat()
        clips_for_country = [c for c in clips_for_country
                             if (c.get("posted_at") or "") >= cutoff]

    # Optionally restrict to clips WE harvested recently. posted_at alone is not
    # enough: ~9k of the corpus came from the old dance-seeded runs and plenty of
    # it was posted inside the last 7 days, so #dancestolearn kept surfacing on
    # boards that were otherwise correct.
    if harvested_since:
        clips_for_country = [c for c in clips_for_country
                             if (c.get("scraped_at") or "") >= harvested_since]

    prev = {}
    for r in (db.table("trend_signals").select("kind,key,rank,run_key")
              .eq("country_code", country).neq("run_key", rk)
              .order("captured_at", desc=True).limit(4000).execute().data or []):
        prev.setdefault((r["kind"], r["key"]), r["rank"])

    # Per-kind evidence floors. A hashtag needs several clips before "this tag
    # works" means anything — many accounts use it, so the sample is cheap. A
    # creator is the opposite: a breakout creator has ONE viral clip, not eight.
    # Using one floor for both left the creator board with 54 rows across 17
    # countries, 50 of them Armenia, purely because Armenia had old clips with
    # enough per-creator history.
    boards = {
        "hashtag": S.rank_hashtags(clips_for_country, baselines, limit=TOP_N,
                                   min_bucket=MIN_BUCKET["hashtag"]),
        "sound":   S.rank_sounds(clips_for_country, baselines, sound_names, limit=TOP_N,
                                 min_bucket=MIN_BUCKET["sound"]),
        "creator": S.rank_creators(clips_for_country, baselines, limit=TOP_N,
                                   min_bucket=MIN_BUCKET["creator"]),
        "video":   S.rank_videos(clips_for_country, baselines, limit=TOP_N),
    }

    rows = []
    for kind, board in boards.items():
        for i, r in enumerate(board, 1):
            was = prev.get((kind, str(r["key"])))
            rows.append({
                "run_key": rk, "country_code": country, "region": region,
                "kind": kind, "key": str(r["key"]), "label": str(r.get("label"))[:300],
                "rank": i,
                "rank_delta": (was - i) if was else None,
                "is_new": was is None,
                "posts": r.get("posts", 0), "views": r.get("views", 0),
                "lift": r.get("lift"), "best_lift": r.get("best_lift"),
                "breakout": r.get("breakout"), "scored": r.get("scored", 0),
                "sample_clip_id": r["key"] if kind == "video" else None,
                "metrics": {"creators": r.get("creators", [])[:3],
                            "spark": r.get("spark", [])},
            })
    # Clear this country's rows for this run first. Upsert alone only ADDS and
    # UPDATES: rows from an earlier build of the same run_key survive, so a tag
    # that no longer qualifies (a #fyp filtered out by BOARD_STOP_TAGS) stayed on
    # the board forever and the filter looked broken.
    try:
        db.table("trend_signals").delete()\
          .eq("run_key", rk).eq("country_code", country).execute()
    except Exception as e:
        log.warning(f"  {country}: could not clear old board rows: {str(e)[:80]}")

    # one row per (run_key, country, kind, key) — a duplicate inside a single
    # batch makes postgres reject the whole statement
    seen, deduped = set(), []
    for r in rows:
        sig = (r["run_key"], r["country_code"], r["kind"], r["key"])
        if sig in seen:
            continue
        seen.add(sig); deduped.append(r)
    rows = deduped

    for i in range(0, len(rows), 200):
        upsert("trend_signals", rows[i:i+200],
               on_conflict="run_key,country_code,kind,key")
    return len(rows), rows


# ── main ────────────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--markets", help="comma list of market codes, e.g. AE,SA,BR")
    ap.add_argument("--tags-per-market", type=int, default=TAGS_PER_MARKET)
    ap.add_argument("--posts-per-tag", type=int, default=POSTS_PER_TAG)
    ap.add_argument("--recency-days", type=int, default=RECENCY_DAYS)
    ap.add_argument("--min-views", type=int, default=MIN_VIEWS)
    ap.add_argument("--tags", help="comma list of hashtags to use instead of the "
                                   "trending board for this run")
    ap.add_argument("--title", help="names this run in the log")
    ap.add_argument("--board-days", type=int, default=7,
                    help="board window: 7 for the weekly run, 30 for the monthly one")
    ap.add_argument("--no-profiles", action="store_true",
                    help="skip creator profile fetch (saves per-result spend)")
    ap.add_argument("--dry-run", action="store_true",
                    help="show the seed tags and stop — no Apify spend")
    a = ap.parse_args()

    db = get_db()
    rk = run_key()
    request_id = os.environ.get("REQUEST_ID")
    set_status(db, request_id, "running")

    markets = db.table("markets").select("country_code,region,name,apify_region_code,seed_hashtags")\
                .eq("platform", "tiktok").execute().data or []
    seen, uniq = set(), []
    for m in markets:
        iso = (m.get("apify_region_code") or m["country_code"]).upper()
        if iso in seen:
            continue
        seen.add(iso)
        uniq.append({"iso": iso, "region": m["region"], "name": m["name"],
                     "raw": m["country_code"], "row": m})
    if a.markets:
        want = {x.strip().upper() for x in a.markets.split(",")}
        uniq = [m for m in uniq if m["iso"] in want or m["raw"].upper() in want]
    if not uniq:
        log.error("no matching markets"); sys.exit(1)

    # An operator can pin the hashtags for a run. Without this the console
    # offered a hashtag box whose contents were never reached — the seed list
    # always came from the trending board.
    forced_tags = None
    if a.tags:
        forced_tags = [t.strip().lstrip("#").lower() for t in a.tags.split(",") if t.strip()]
        log.info(f"using {len(forced_tags)} hashtag(s) from the command line, "
                 f"skipping trending discovery: {forced_tags[:6]}")

    log.info((f"[{a.title}] " if a.title else "")
             + f"run_key={rk}  markets={[m['iso'] for m in uniq]}")

    if a.dry_run:
        for m in uniq:
            tags = forced_tags or seed_tags(db, m["raw"], m["region"], a.tags_per_market,
                                            m["row"], country=m["iso"], days=a.board_days)[0]
            est = len(tags) * a.posts_per_tag
            log.info(f"  {m['iso']:4} {len(tags):3} tags -> ~{est:,} results  {tags[:10]}")
        log.info("dry run — nothing scraped, nothing written")
        return

    resolve = niche_resolver(db)
    t0 = time.time()
    totals = Counter()

    # Loaded once up front so each country can rank and WRITE its board the
    # moment it finishes. Doing boards in a second pass at the end meant a crash
    # or a closed laptop after 16 countries left 16 countries of paid scraping
    # with nothing to show for it.
    log.info("loading existing corpus for baselines…")
    all_clips, sound_names = S.load_clips(db, force=True)
    log.info(f"  {len(all_clips):,} clips in corpus")

    for m in uniq:
        if forced_tags:
            tags, seed_meta = forced_tags, {t: ("operator", None) for t in forced_tags}
        else:
            tags, seed_meta = seed_tags(db, m["raw"], m["region"], a.tags_per_market,
                                        m["row"], country=m["iso"], days=a.board_days)
        if not tags:
            log.warning(f"  {m['iso']}: no seed tags, skipping"); continue
        log.info(f"▶ {m['iso']} ({m['name']}) — {len(tags)} tags")
        try:
            clips, stats, raw = harvest(tags, m["iso"], m["iso"], m["region"],
                                        per_tag=a.posts_per_tag,
                                        recency_days=a.recency_days,
                                        min_views=a.min_views)
        except Exception as e:
            log.error(f"  {m['iso']} harvest failed: {str(e)[:120]}"); continue
        if not clips:
            log.warning(f"  {m['iso']}: nothing recent enough"); continue

        for c in clips:
            n, src = resolve(c)
            if n:
                c["niche"], c["niche_source"] = n, src

        saved = T.process_clips(clips, workers=8)
        totals["saved"] += saved
        totals["fetched"] += stats["fetched"]
        log.info(f"  {m['iso']}: {saved} clips saved")

        # ── archive: raw payload first, so a bad parse is always recoverable
        pre = A.run_prefix("tiktok", m["iso"])
        A.put_json(f"{pre}/raw-hashtag-scrape.json", raw)
        A.put_json(f"{pre}/clips.json",
                   [{k: v for k, v in c.items() if not k.startswith("_")} for c in clips])

        if not a.no_profiles:
            handles = {c.get("creator_handle") for c in clips if c.get("creator_handle")}
            pst = A.fetch_and_store_profiles(handles, "tiktok", m["iso"], rk)
            log.info(f"  {m['iso']}: profiles {pst['fetched']}/{pst['requested']} fetched, "
                     f"{pst['pics']} new pics, {pst['skipped']} already had one, "
                     f"{pst.get('stored', 0)} -> trend_creators")
            totals["pics"] += pst["pics"]

        A.write_manifest("tiktok", m["iso"], rk,
                         {"tags": tags, "seed_sources": {k: v[0] for k, v in seed_meta.items()},
                          "posts_per_tag": a.posts_per_tag,
                          "recency_days": a.recency_days, "min_views": a.min_views},
                         {**stats, "saved": saved})

        # ── board for THIS country, written now.
        #
        # Membership comes from what this country actually harvested, never from
        # clips.market_code. A viral video trends in many countries at once and
        # clips is UNIQUE on platform_post_id, so the last country to upsert
        # overwrites market_code — Brazil harvested 213 clips and only 37 still
        # said BR by the time Turkey had run. Ranking off that column would have
        # built Brazil's board from a sixth of its data.
        # Read the saved rows back. The in-memory dicts come from
        # normalize_tiktok() and carry no `id` — that is assigned on insert — and
        # rank_videos keys on it, so building straight off them threw KeyError
        # 'id' and lost every board for the run.
        seen_ids = {c.get("platform_post_id") for c in clips if c.get("platform_post_id")}
        mine = []
        ids = list(seen_ids)
        for i in range(0, len(ids), 100):
            mine += (db.table("clips").select(
                "id,platform,platform_post_id,creator_handle,caption,hashtags,views,"
                "likes,comments,shares,saves,duration_sec,posted_at,sound_id,"
                "video_minio_path,video_url,velocity,subject_type,region,market_code,"
                "topic,niche,appearance,transcript,scraped_at,feed_source")
                .in_("platform_post_id", ids[i:i+100]).execute().data or [])
        for c in mine:
            c["_country"] = m["iso"]
            c["_region"]  = m["region"]
            c["_niche"]   = S.norm_niche(c.get("niche") or c.get("topic"))
        # anything already in the corpus that this country previously owned
        mine += [c for c in all_clips
                 if c.get("_country") == m["iso"]
                 and c.get("platform_post_id") not in seen_ids]
        try:
            n, board_rows = build_board(db, m["iso"], m["region"], rk, mine,
                                        all_clips + clips, sound_names,
                                        window_days=a.board_days)
            A.put_json(f'{A.run_prefix("tiktok", m["iso"])}/board.json', board_rows)
            log.info(f"  {m['iso']}: {n} board rows written ({a.board_days}d window)")
            totals["board_rows"] += n
        except Exception as e:
            log.error(f"  {m['iso']} board failed: {str(e)[:120]}")

        all_clips += clips          # feeds the next country's baselines

    set_status(db, request_id, "success", progress={
        "fetched": totals["fetched"], "saved": totals["saved"],
        "profile_pics": totals["pics"], "board_rows": totals["board_rows"],
        "run_key": rk, "minutes": round((time.time() - t0) / 60, 1)})
    log.info(f"done in {(time.time()-t0)/60:.1f}m — {totals['fetched']} fetched, "
             f"{totals['saved']} saved, {totals['pics']} profile pics, "
             f"{totals['board_rows']} board rows, run_key={rk}")
    log.info(f"archive: {A.RUNS_BUCKET}/runs/tiktok/{now():%Y/%m/%d}/<COUNTRY>/")


if __name__ == "__main__":
    main()
