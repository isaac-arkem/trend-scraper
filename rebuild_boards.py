"""Rebuild the country boards from clips already in the database.

Costs nothing — no Apify, no scraping. Reads clips, ranks them, writes
trend_signals. Deliberately split from the harvest because the two have very
different economics: harvesting is billed per result, ranking is free. Once a
week's clips are in, the board can be rebuilt as often as you like.

    weekly board  (last 7 days)
        .venv/bin/python rebuild_boards.py --board-days 7

    monthly board (last 30 days), stored under its own run key so it does not
    overwrite the weekly one
        .venv/bin/python rebuild_boards.py --board-days 30 --run-key 2026-08-MONTH

    one country
        .venv/bin/python rebuild_boards.py --markets AE --board-days 7
"""
import argparse
from datetime import datetime, timezone

import json

from dotenv import load_dotenv
load_dotenv()

from src.db.client import get_db
from src.utils.logger import get_logger
import src.signals as S
from scrape_weekly_trends import build_board, run_key

log = get_logger(__name__)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--markets", help="comma list of ISO codes; default all")
    ap.add_argument("--board-days", type=int, default=7,
                    help="7 for the weekly board, 30 for the monthly one")
    ap.add_argument("--run-key", help="defaults to the current ISO week")
    ap.add_argument("--top", type=int, default=50)
    ap.add_argument("--archive-date", help="YYYY/MM/DD to read membership from; default all runs")
    ap.add_argument("--since", help="only rank clips harvested on/after this date "
                                    "(YYYY-MM-DD) — excludes the old dance corpus")
    ap.add_argument("--from-market-code", action="store_true",
                    help="ignore the archive and trust clips.market_code (lossy)")
    a = ap.parse_args()

    db = get_db()
    rk = a.run_key or run_key()

    clips, sound_names = S.load_clips(db, force=True)
    log.info(f"{len(clips):,} clips loaded")

    # Country membership comes from the per-run archive, not clips.market_code.
    # clips is UNIQUE on platform_post_id and a viral video is harvested by many
    # countries, so the last upsert wins and market_code no longer says who found
    # it. The archive each run writes does say — clips.json per country.
    by_id = {c.get("platform_post_id"): c for c in clips}
    by_country = {}
    if not a.from_market_code:
        import src.run_archive as A
        from src.storage.minio import get_minio
        mc = get_minio()
        pref = f"runs/tiktok/{a.archive_date}/" if a.archive_date else "runs/tiktok/"
        # a run that crosses midnight lands under two dates; both are ours
        found = 0
        for o in mc.list_objects(A.RUNS_BUCKET, prefix=pref, recursive=True):
            if not o.object_name.endswith("/clips.json"):
                continue
            country = o.object_name.split("/")[-2].upper()
            try:
                rows = json.loads(mc.get_object(A.RUNS_BUCKET, o.object_name).read())
            except Exception as e:
                log.warning(f"  unreadable archive {o.object_name}: {str(e)[:60]}")
                continue
            bucket = by_country.setdefault(country, {})
            for r in rows:
                c = by_id.get(r.get("platform_post_id"))
                if c:
                    bucket[r.get("platform_post_id")] = c
            found += 1
        log.info(f"archive: {found} country files -> "
                 f"{ {k: len(v) for k, v in sorted(by_country.items())} }")

    # anything the archive did not cover falls back to the stored code
    for c in clips:
        cc = c.get("_country")
        if not cc:
            continue
        by_country.setdefault(cc, {}).setdefault(c.get("platform_post_id"), c)

    # keyed by post id while collecting, plain lists from here on
    by_country = {k: list(v.values()) for k, v in by_country.items()}

    wanted = None
    if a.markets:
        wanted = {x.strip().upper() for x in a.markets.split(",")}

    region_of = {}
    for m in (db.table("markets").select("country_code,region,apify_region_code")
              .execute().data or []):
        region_of[(m.get("apify_region_code") or m["country_code"]).upper()] = m["region"]

    log.info(f"run_key={rk}  window={a.board_days}d  countries={len(by_country)}")
    total = 0
    for country, sel in sorted(by_country.items(), key=lambda x: -len(x[1])):
        if wanted and country not in wanted:
            continue
        n, _ = build_board(db, country, region_of.get(country), rk, sel,
                           clips, sound_names, window_days=a.board_days,
                           harvested_since=a.since)
        log.info(f"  {country:4} {len(sel):>6,} clips in corpus -> {n:>4} board rows")
        total += n
    log.info(f"done — {total} rows written for run_key={rk}")


if __name__ == "__main__":
    main()
