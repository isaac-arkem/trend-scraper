#!/usr/bin/env python3
"""
Drive Stage 5 (AI vision analysis) across ALL markets / platforms, serially.
Resumable: only ever processes media with analysis_status='pending', so a
restart simply continues. Run:  .venv/bin/python run_stage5_all.py
"""
import sys
import time
from src.db.client import get_db
from src.pipeline import stage5_analysis

CHUNK = 1000  # assets per stage5 call — bounds memory, gives progress logging

REGION_ORDER = {"META": 0, "LATAM": 1, "INDOPAC": 2}


def log(msg: str):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def creator_ids(db, market_id, platform):
    rows = db.table("creators").select("id").eq("market_id", market_id)\
        .eq("platform", platform).execute().data or []
    return [c["id"] for c in rows]


def pending_count(db, cids):
    total = 0
    for i in range(0, len(cids), 100):
        b = cids[i:i+100]
        total += db.table("media_assets").select("id", count="exact")\
            .eq("analysis_status", "pending")\
            .in_("asset_type", ["image", "thumbnail", "frame", "video"])\
            .in_("creator_id", b).limit(1).execute().count or 0
    return total


def main():
    db = get_db()
    markets = db.table("markets").select("id,country_code,region,platform").execute().data
    markets.sort(key=lambda r: (REGION_ORDER.get(r["region"], 9), r["country_code"], r["platform"]))

    grand_done = 0
    t0 = time.time()
    for idx, m in enumerate(markets, 1):
        tag = f"{m['region']}/{m['country_code']}/{m['platform']}"
        cids = creator_ids(db, m["id"], m["platform"])
        if not cids:
            log(f"({idx}/{len(markets)}) {tag}: no creators, skip")
            continue
        pend = pending_count(db, cids)
        log(f"({idx}/{len(markets)}) {tag}: {pend} pending")
        if pend == 0:
            continue

        market_done = 0
        while True:
            try:
                results = stage5_analysis.run(market_id=m["id"], platform=m["platform"], max_assets=CHUNK)
            except Exception as e:
                log(f"  !! {tag} chunk error: {e} — moving on")
                break
            n = len(results or [])
            market_done += n
            grand_done += n
            remaining = pending_count(db, cids)
            log(f"  {tag}: +{n} (market {market_done}, total {grand_done}) | {remaining} left | {(time.time()-t0)/60:.1f}m")
            # stop when nothing pending remains, or a chunk produced 0 saves AND
            # nothing is left to attempt (avoids spinning on all-failed assets)
            if remaining == 0:
                break
            if n == 0:
                log(f"  {tag}: chunk saved 0 but {remaining} still pending (likely fetch errors) — moving on")
                break
        log(f"== {tag} DONE: {market_done} analyzed this run ==")

    log(f"ALL MARKETS COMPLETE — {grand_done} analyzed this run in {(time.time()-t0)/60:.1f}m")


if __name__ == "__main__":
    main()
