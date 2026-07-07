#!/usr/bin/env python3
"""
Scrape the curated reference influencers (reference_accounts table):
  • content  → their recent videos/reels with sounds + engagement + captions(voice),
               stored in `clips` tagged with topic + region, feed_source=null
               (so they DON'T mix into the Dance Trends lane)
  • appearance → gpt-4o vision on cover frames → aggregated look per account
                 (skin/hair/body/makeup + %female), saved to reference_accounts.appearance

Run:  .venv/bin/python scrape_reference_accounts.py                              (all active)
      .venv/bin/python scrape_reference_accounts.py --handles alice,bob,carol     (specific)

Jenkins env vars (optional):
  REQUEST_ID  — uuid of the reference_scrape_requests row to update with status
"""
import sys
import time
import logging
from collections import Counter
from datetime import datetime, timezone

for _n in ("httpx", "httpcore", "urllib3", "apify_client", "openai"):
    logging.getLogger(_n).setLevel(logging.WARNING)

from src.db.client import get_db
from src.pipeline import trends as T
from src.ai.vision import analyze_image_bytes, QuotaExceededError

PER_ACCOUNT = 15        # recent posts to pull per account
RECENCY_DAYS = 365      # reference accounts: their content regardless of "trend" recency
APPEARANCE_SAMPLE = 5   # cover frames to vision-analyze for the appearance profile

SINGLE = ["skin_tone", "hair_color", "hair_length", "hair_texture", "body_frame",
          "body_shape", "eye_color", "makeup_style"]


def log(m):
    print(f"[{time.strftime('%H:%M:%S')}] {m}", flush=True)


def aggregate_appearance(cover_urls):
    singles = {f: Counter() for f in SINGLE}
    fashion, content = Counter(), Counter()
    n = person = female = 0
    for url in cover_urls[:APPEARANCE_SAMPLE]:
        img = T._download(url)
        if not img:
            continue
        r = analyze_image_bytes(img)
        if not r:
            continue
        n += 1
        if r.get("person_visible"):
            person += 1
        if r.get("person_is_female") is True:
            female += 1
        for f in SINGLE:
            v = r.get(f)
            if v and v != "unclear":
                singles[f][v] += 1
        for v in (r.get("fashion_style") or []):
            fashion[v] += 1
        for v in (r.get("content_style") or []):
            content[v] += 1
    if n == 0:
        return None
    top = lambda c: c.most_common(1)[0][0] if c else None
    return {
        "analyzed": n, "person_visible": person, "female": female,
        "dominant": {f: top(c) for f, c in singles.items()},
        "fashion_style": [k for k, _ in fashion.most_common(3)],
        "content_style": [k for k, _ in content.most_common(3)],
    }


def set_request_status(db, request_id, status, error_message=None):
    if not request_id:
        return
    update = {"status": status}
    if error_message:
        update["error_message"] = error_message[:500]
    if status in ("success", "failed"):
        update["completed_at"] = datetime.now(timezone.utc).isoformat()
    db.table("reference_scrape_requests").update(update).eq("id", request_id).execute()


def main():
    import os
    args = sys.argv[1:]
    handles = [h.strip() for h in args[args.index("--handles") + 1].split(",")] if "--handles" in args else None
    request_id = os.environ.get("REQUEST_ID")

    db = get_db()

    set_request_status(db, request_id, "running")

    q = db.table("reference_accounts").select("*").eq("active", True)
    if handles:
        q = q.in_("handle", handles)
    accts = q.order("id").execute().data or []
    log(f"scraping {len(accts)} reference accounts" + (f" (handles={','.join(handles)})" if handles else " (all active)"))

    t0 = time.time()
    done = 0
    appearance_on = True   # flips off if OpenAI quota runs out (content+voice still scrape)
    try:
      for a in accts:
        handle, plat = a["handle"], a["platform"]
        log(f"── [{done+1}/{len(accts)}] @{handle} ({plat}) | topic={a.get('topic')} region={a.get('region')}")

        log(f"   Fetching latest {PER_ACCOUNT} posts from {plat}…")
        try:
            if plat == "tiktok":
                clips = T.scrape_tiktok_watchlist([handle], per_handle=PER_ACCOUNT, recency_days=RECENCY_DAYS)
            else:
                clips = T.scrape_ig_watchlist([handle], per_handle=PER_ACCOUNT, recency_days=RECENCY_DAYS)
        except Exception as e:
            log(f"   ✗ Fetch failed: {str(e)[:80]} — skipping account")
            continue

        log(f"   {len(clips)} clips retrieved")

        # tag as reference content: topic+region, no feed_source, skip the women-filter
        for c in clips:
            c["feed_source"] = None
            c["topic"] = a["topic"]
            c["region"] = a["region"]
            c["subject_type"] = "ref"      # pre-set so process_clips skips per-clip vision

        if clips:
            log(f"   Processing {len(clips)} clips (downloading audio/video, saving to storage)…")
            saved = T.process_clips(clips, workers=8)
            log(f"   {saved} clips saved to storage ({len(clips) - saved} duplicates/skipped)")
        else:
            log(f"   No clips to process — account may be private or inactive")

        appearance = None
        if appearance_on and clips:
            cover_urls = [c.get("_cover_url") for c in clips if c.get("_cover_url")]
            log(f"   Analyzing appearance from {min(len(cover_urls), APPEARANCE_SAMPLE)} cover frames (vision AI)…")
            try:
                appearance = aggregate_appearance(cover_urls)
                if appearance:
                    dom = appearance.get("dominant", {})
                    log(f"   Appearance: skin={dom.get('skin_tone')} hair={dom.get('hair_color')}/{dom.get('hair_length')} "
                        f"makeup={dom.get('makeup_style')} body={dom.get('body_frame')}")
                else:
                    log(f"   No appearance data extracted (no visible person in frames)")
            except QuotaExceededError:
                log("   ⚠ OpenAI quota exceeded — skipping appearance analysis for remaining accounts")
                appearance_on = False

        update = {"scraped_at": datetime.now(timezone.utc).isoformat()}
        if appearance is not None:
            update["appearance"] = appearance
        db.table("reference_accounts").update(update).eq("id", a["id"]).execute()
        done += 1
        log(f"   ✓ Done — {done}/{len(accts)} accounts complete | elapsed {(time.time()-t0)/60:.1f}m")

      log(f"━━ COMPLETE — {done}/{len(accts)} reference accounts scraped in {(time.time()-t0)/60:.1f}m ━━")
      set_request_status(db, request_id, "success")

    except Exception as e:
        msg = str(e)
        log(f"✗ FATAL — {msg}")
        set_request_status(db, request_id, "failed", error_message=msg)
        raise


if __name__ == "__main__":
    main()
