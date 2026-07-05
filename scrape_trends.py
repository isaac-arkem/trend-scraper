#!/usr/bin/env python3
"""
Run the trend feeds PER REGION across every market we already have
(MENA / LATAM / INDOPAC), plus the cross-platform watchlist.

Per market: TikTok dance + hook feeds, geo-targeted via the market's region code,
filtered to >=20k views / <=14d, women-only (vision), audio+video → MinIO,
clustered by sound, ranked by velocity. Processed in parallel; trends rebuild
after every feed so the dashboard fills progressively.

Run:  .venv/bin/python scrape_trends.py

Jenkins env vars:
  REQUEST_ID   — uuid of the dance_scrape_requests row to update with status
  MARKETS      — comma-separated market codes to scrape (e.g. AE,SA,NG)
  FEEDS        — comma-separated feed slugs (e.g. dance,hook)
  TAGS         — comma-separated hashtags to scrape
  MIN_VIEWS    — minimum view threshold (default 20000)
  RECENCY_DAYS — how many days back to look (default 14)
"""
import sys
import time
import logging
import yaml

for _n in ("httpx", "httpcore", "urllib3", "apify_client", "openai"):
    logging.getLogger(_n).setLevel(logging.WARNING)

from src.db.client import get_db
from src.pipeline import trends as T

REGION_LABEL = {"META": "MENA", "LATAM": "LATAM", "INDOPAC": "INDOPAC"}
CLIPS_PER_TERM = 40


def log(m):
    print(f"[{time.strftime('%H:%M:%S')}] {m}", flush=True)


def load_markets():
    m = yaml.safe_load(open("config/markets.yaml"))["markets"]
    return [(code, cfg.get("apify_region_code"), REGION_LABEL.get(cfg["region"], cfg["region"]))
            for code, cfg in m.items()]


def safe(label, fn):
    """Run a feed end-to-end; never let a network blip kill the whole sweep."""
    for attempt in range(3):
        try:
            return fn()
        except Exception as e:
            if attempt == 2:
                log(f"  ✗ {label} gave up: {str(e)[:90]}")
                return 0
            log(f"  {label} error ({type(e).__name__}) — retry {attempt+1}/3 in 10s")
            time.sleep(10)


def set_request_status(db, request_id, status, error_message=None):
    if not request_id:
        return
    from datetime import datetime, timezone
    update = {"status": status}
    if error_message:
        update["error_message"] = error_message[:500]
    if status in ("success", "failed"):
        update["completed_at"] = datetime.now(timezone.utc).isoformat()
    db.table("dance_scrape_requests").update(update).eq("id", request_id).execute()


def main():
    import os

    # ── params from Jenkins env vars ──────────────────────────────────────────
    request_id   = os.environ.get("REQUEST_ID")
    market_codes = [m.strip().upper() for m in os.environ["MARKETS"].split(",") if m.strip()] if os.environ.get("MARKETS") else None
    feed_slugs   = [f.strip() for f in os.environ["FEEDS"].split(",") if f.strip()] if os.environ.get("FEEDS") else None
    tags         = [t.strip() for t in os.environ["TAGS"].split(",") if t.strip()] if os.environ.get("TAGS") else None
    min_views    = int(os.environ["MIN_VIEWS"]) if os.environ.get("MIN_VIEWS") else 20000
    recency_days = int(os.environ["RECENCY_DAYS"]) if os.environ.get("RECENCY_DAYS") else 14

    db = get_db()
    set_request_status(db, request_id, "running")

    # ── markets ───────────────────────────────────────────────────────────────
    markets = load_markets()
    if market_codes:
        markets = [m for m in markets if m[0] in market_codes]

    if not markets:
        log("ERROR — no matching markets found for: " + str(market_codes))
        set_request_status(db, request_id, "failed", error_message="No matching markets")
        return

    # ── tags: use what came from Jenkins, fall back to hardcoded defaults ─────
    if not tags:
        log("WARNING — no TAGS env var, falling back to default DANCE_TAGS + HOOK_TAGS")
        tags = T.DANCE_TAGS + T.HOOK_TAGS

    feed_label = ",".join(feed_slugs) if feed_slugs else "all"
    log(f"Sweeping {len(markets)} markets | feeds={feed_label} | tags={len(tags)} | min_views={min_views} | recency={recency_days}d")

    wl = db.table("watchlist_accounts").select("platform,handle").eq("active", True).execute().data or []
    tt_handles = [w["handle"] for w in wl if w["platform"] == "tiktok"]
    ig_handles = [w["handle"] for w in wl if w["platform"] == "instagram"]

    t0 = time.time()
    total = 0

    def do_feed(terms, feed, code, region_code, region_label):
        clips = T.scrape_tiktok_feed(
            terms, feed, region_code=region_code,
            region_label=region_label, market_code=code,
            per_term=CLIPS_PER_TERM,
            threshold=min_views, recency_days=recency_days,
        )
        saved = T.process_clips(clips, workers=12)
        n = T.rebuild_trends()
        log(f"✓ {region_label}/{code} {feed}: +{saved} | {n} trends live | {(time.time()-t0)/60:.1f}m")
        return saved

    try:
        for code, region_code, region_label in markets:
            for slug in (feed_slugs or ["dance"]):
                log(f"▶ {region_label}/{code} {slug}: scraping (region={region_code})…")
                total += safe(
                    f"{region_label}/{code} {slug}",
                    lambda t=tags, f=slug, c=code, rc=region_code, rl=region_label:
                    do_feed(t, f, c, rc, rl),
                ) or 0

        log(f"▶ TikTok watchlist ({len(tt_handles)} accounts)…")
        total += safe("TT watchlist", lambda: (T.process_clips(T.scrape_tiktok_watchlist(tt_handles, per_handle=10), workers=12), T.rebuild_trends())[0]) or 0
        log(f"▶ IG Reels watchlist ({len(ig_handles)} accounts)…")
        total += safe("IG watchlist", lambda: (T.process_clips(T.scrape_ig_watchlist(ig_handles, per_handle=10), workers=12), T.rebuild_trends())[0]) or 0

        log(f"ALL DONE — {total} clips saved in {(time.time()-t0)/60:.1f}m")
        set_request_status(db, request_id, "success")

    except Exception as e:
        msg = str(e)
        log(f"FATAL — {msg}")
        set_request_status(db, request_id, "failed", error_message=msg)
        raise


if __name__ == "__main__":
    main()
