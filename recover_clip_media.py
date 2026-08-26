"""Re-fetch the video and audio for clips whose media never uploaded.

A whole sweep wrote 419 clip rows with no media behind them: trends.py's _put()
addressed the bucket directly instead of going through the store router, so every
upload hit AccessDenied, was caught, logged as a warning, and the run carried on.
That is now fixed — this recovers what was lost.

Deliberately narrow, because the obvious tool for the job is not safe here:
scrape_reference_accounts.py selects EVERY active account when no --handles is
given, and runs vision on each with no way to switch it off. This touches only
the accounts you name, and never calls vision — the media is the point, not the
faces.

    python recover_clip_media.py --topic chinese_student --dry-run
    python recover_clip_media.py --topic chinese_student
"""
import argparse
import collections
import sys
import time
from datetime import datetime, timezone

from dotenv import load_dotenv

load_dotenv()

from src.db.client import get_db                  # noqa: E402
from src.pipeline import trends as T              # noqa: E402
from src.utils.logger import get_logger           # noqa: E402

log = get_logger(__name__)


def parse_args():
    p = argparse.ArgumentParser(description="Re-fetch missing clip media for reference accounts.")
    p.add_argument("--topic", default="", help="Only accounts in this niche")
    p.add_argument("--handles", default="", help="Comma-separated handles instead of a topic")
    p.add_argument("--posts", type=int, default=10, help="Posts per account")
    p.add_argument("--recency-days", type=int, default=90)
    p.add_argument("--limit", type=int, default=0)
    p.add_argument("--dry-run", action="store_true")
    return p.parse_args()


def main():
    a = parse_args()
    db = get_db()

    q = db.table("reference_accounts").select("id,platform,handle,topic,region").eq("active", True)
    if a.handles:
        q = q.in_("handle", [h.strip().lower().lstrip("@") for h in a.handles.split(",") if h.strip()])
    elif a.topic:
        q = q.eq("topic", a.topic)
    else:
        log.error("give --topic or --handles; refusing to scrape every account")
        sys.exit(1)
    accts = q.order("id").execute().data or []
    if a.limit:
        accts = accts[:a.limit]

    # How much media is actually missing, so the log states the gap not a guess.
    topic = a.topic or (accts[0].get("topic") if accts else "")
    if topic:
        miss = (db.table("clips").select("id", count="exact")
                .eq("topic", topic).is_("video_minio_path", "null").execute().count)
        tot = db.table("clips").select("id", count="exact").eq("topic", topic).execute().count
        log.info(f"  {topic}: {miss} of {tot} clips have no stored media")

    log.info(f"  {len(accts)} accounts | {a.posts} posts each | last {a.recency_days}d | NO vision")
    for p, n in collections.Counter(x["platform"] for x in accts).most_common():
        log.info(f"    {p}: {n}")
    if a.dry_run:
        log.info("  dry run — nothing fetched")
        return

    t0 = time.time()
    saved_total = 0
    for n, acct in enumerate(accts, 1):
        h, plat = acct["handle"], acct.get("platform") or "tiktok"
        log.info(f"[{n}/{len(accts)}] @{h} ({plat})")
        try:
            fetch = T.scrape_ig_watchlist if plat == "instagram" else T.scrape_tiktok_watchlist
            clips = fetch([h], per_handle=a.posts, recency_days=a.recency_days)
        except Exception as e:
            log.warning(f"   fetch failed: {str(e)[:90]}")
            continue

        # Same tagging as the reference pipeline. subject_type 'ref' is what makes
        # process_clips skip the per-clip vision call.
        for c in clips:
            c["feed_source"] = None
            c["topic"] = acct.get("topic")
            c["region"] = acct.get("region")
            c["subject_type"] = "ref"

        saved = T.process_clips(clips, workers=8) if clips else 0
        saved_total += saved
        log.info(f"   {len(clips)} clips -> {saved} saved")

    if topic:
        still = (db.table("clips").select("id", count="exact")
                 .eq("topic", topic).is_("video_minio_path", "null").execute().count)
        log.info(f"  clips still without media: {still}")
    log.info(f"━━ DONE — {saved_total} clips processed in "
             f"{(time.time() - t0) / 60:.1f}m ━━")


if __name__ == "__main__":
    main()
