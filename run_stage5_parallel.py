#!/usr/bin/env python3
"""
Stage 5 (AI vision analysis) across ALL markets — 16 concurrent workers.
Resumable: only processes analysis_status='pending', so a restart continues.
Run:  .venv/bin/python run_stage5_parallel.py
"""
import time
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed

# Quiet per-request HTTP noise — 16 workers would flood the log
for _n in ("httpx", "httpcore", "openai", "urllib3"):
    logging.getLogger(_n).setLevel(logging.WARNING)
from src.db.client import get_db
from src.pipeline import stage5_analysis
from src.ai.vision import QuotaExceededError

WORKERS = 16
CAP = stage5_analysis.CAP_PER_CREATOR        # max images analyzed per creator
GOOD = stage5_analysis.GOOD_READS_TO_STOP    # early-stop after this many good reads
REGION_ORDER = {"META": 0, "LATAM": 1, "INDOPAC": 2}


def log(m):
    print(f"[{time.strftime('%H:%M:%S')}] {m}", flush=True)


def db_retry(fn, *args, tries=10, **kwargs):
    """Run a main-loop DB call, retrying transient network errors with backoff so
    a single DNS/connection blip can't crash the whole run."""
    delay = 3
    for i in range(tries):
        try:
            return fn(*args, **kwargs)
        except Exception as e:
            if i == tries - 1:
                raise
            log(f"  network/DB error ({type(e).__name__}: {str(e)[:80]}) — retry {i+1}/{tries} in {delay}s")
            time.sleep(delay)
            delay = min(delay * 2, 60)


def reset_stale_analyzing(db):
    """Any asset stuck in 'analyzing' is orphaned (no concurrent run but us) — reset."""
    r = db.table("media_assets").update({"analysis_status": "pending"})\
        .eq("analysis_status", "analyzing").execute()
    n = len(r.data or [])
    if n:
        log(f"Reset {n} stale 'analyzing' assets back to pending")


def creator_ids(db, market_id, platform):
    rows = db.table("creators").select("id").eq("market_id", market_id)\
        .eq("platform", platform).execute().data or []
    return [c["id"] for c in rows]


def main():
    db = get_db()
    db_retry(reset_stale_analyzing, db)

    markets = db_retry(lambda: db.table("markets").select("id,country_code,region,platform").execute().data)
    markets.sort(key=lambda r: (REGION_ORDER.get(r["region"], 9), r["country_code"], r["platform"]))

    t0 = time.time()
    grand = 0
    log(f"Cap {CAP} images/creator, early-stop after {GOOD} good reads")
    for idx, m in enumerate(markets, 1):
        tag = f"{m['region']}/{m['country_code']}/{m['platform']}"
        cids = db_retry(creator_ids, db, m["id"], m["platform"])
        if not cids:
            log(f"({idx}/{len(markets)}) {tag}: no creators, skip")
            continue

        # One task per creator — each runs that creator's capped, early-stopping
        # analysis sequence. Parallelism is across creators.
        market_done = 0
        with ThreadPoolExecutor(max_workers=WORKERS) as ex:
            futs = [ex.submit(stage5_analysis.analyze_creator, cid, CAP, GOOD) for cid in cids]
            for f in as_completed(futs):
                try:
                    market_done += f.result() or 0
                except QuotaExceededError:
                    log("!! OpenAI quota exceeded — halting. Top up billing, then "
                        "re-run this script to resume.")
                    ex.shutdown(wait=False, cancel_futures=True)
                    return
                except Exception as e:
                    log(f"  creator error: {e}")
        grand += market_done
        rate = grand / max(1e-9, (time.time() - t0) / 3600)
        log(f"== ({idx}/{len(markets)}) {tag} DONE: +{market_done} (total {grand}) "
            f"| {rate:.0f}/hr | {(time.time()-t0)/60:.1f}m ==")

    log(f"ALL MARKETS COMPLETE — {grand} analyzed this run in {(time.time()-t0)/60:.1f}m")


if __name__ == "__main__":
    main()
