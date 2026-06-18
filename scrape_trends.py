#!/usr/bin/env python3
"""
Run the trend feeds PER REGION across every market we already have
(MENA / LATAM / INDOPAC), plus the cross-platform watchlist.

Per market: TikTok dance + hook feeds, geo-targeted via the market's region code,
filtered to >=20k views / <=14d, women-only (vision), audio+video → MinIO,
clustered by sound, ranked by velocity. Processed in parallel; trends rebuild
after every feed so the dashboard fills progressively.

Run:  .venv/bin/python scrape_trends.py
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
DANCE_PER = 40
HOOK_PER = 20


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


def main():
    db = get_db()
    markets = load_markets()
    only = [a.upper() for a in sys.argv[1:] if not a.startswith("--")]  # optional market codes to limit to
    if only:
        markets = [m for m in markets if m[0] in only]
    wl = db.table("watchlist_accounts").select("platform,handle").eq("active", True).execute().data or []
    tt_handles = [w["handle"] for w in wl if w["platform"] == "tiktok"]
    ig_handles = [w["handle"] for w in wl if w["platform"] == "instagram"]

    t0 = time.time()
    total = 0
    log(f"Sweeping {len(markets)} markets" + (f" {only}" if only else "") + " + watchlist")

    def do_feed(terms, feed, code, region_code, region_label, per):
        clips = T.scrape_tiktok_feed(terms, feed, region_code=region_code,
                                     region_label=region_label, market_code=code, per_term=per)
        saved = T.process_clips(clips, workers=12)
        n = T.rebuild_trends()
        log(f"✓ {region_label}/{code} {feed}: +{saved} | {n} trends live | {(time.time()-t0)/60:.1f}m")
        return saved

    for code, region_code, region_label in markets:
        for feed, terms, per in [("dance", T.DANCE_TAGS, DANCE_PER), ("hook", T.HOOK_TAGS, HOOK_PER)]:
            log(f"▶ {region_label}/{code} {feed}: scraping (region={region_code})…")
            total += safe(f"{region_label}/{code} {feed}",
                          lambda t=terms, f=feed, c=code, rc=region_code, rl=region_label, p=per:
                          do_feed(t, f, c, rc, rl, p)) or 0

    log(f"▶ TikTok watchlist ({len(tt_handles)} accounts)…")
    total += safe("TT watchlist", lambda: (T.process_clips(T.scrape_tiktok_watchlist(tt_handles, per_handle=10), workers=12), T.rebuild_trends())[0]) or 0
    log(f"▶ IG Reels watchlist ({len(ig_handles)} accounts)…")
    total += safe("IG watchlist", lambda: (T.process_clips(T.scrape_ig_watchlist(ig_handles, per_handle=10), workers=12), T.rebuild_trends())[0]) or 0

    log(f"ALL DONE — {total} clips saved in {(time.time()-t0)/60:.1f}m")


if __name__ == "__main__":
    main()
