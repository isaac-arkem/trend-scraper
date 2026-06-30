#!/usr/bin/env python3
"""
Scrape the curated reference influencers (reference_accounts table):
  • content  → their recent videos/reels with sounds + engagement + captions(voice),
               stored in `clips` tagged with topic + region, feed_source=null
               (so they DON'T mix into the Dance Trends lane)
  • appearance → gpt-4o vision on cover frames → aggregated look per account
                 (skin/hair/body/makeup + %female), saved to reference_accounts.appearance

Run:  .venv/bin/python scrape_reference_accounts.py                  (all)
      .venv/bin/python scrape_reference_accounts.py --topic adhd_wellness
      .venv/bin/python scrape_reference_accounts.py --limit 3        (test)
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


def main():
    args = sys.argv[1:]
    topic = args[args.index("--topic") + 1] if "--topic" in args else None
    limit = int(args[args.index("--limit") + 1]) if "--limit" in args else None

    db = get_db()
    q = db.table("reference_accounts").select("*").eq("active", True)
    if topic:
        q = q.eq("topic", topic)
    accts = q.order("id").execute().data or []
    if limit:
        accts = accts[:limit]
    log(f"scraping {len(accts)} reference accounts" + (f" (topic={topic})" if topic else ""))

    t0 = time.time()
    done = 0
    appearance_on = True   # flips off if OpenAI quota runs out (content+voice still scrape)
    for a in accts:
        handle, plat = a["handle"], a["platform"]
        try:
            if plat == "tiktok":
                clips = T.scrape_tiktok_watchlist([handle], per_handle=PER_ACCOUNT, recency_days=RECENCY_DAYS)
            else:
                clips = T.scrape_ig_watchlist([handle], per_handle=PER_ACCOUNT, recency_days=RECENCY_DAYS)
        except Exception as e:
            log(f"  {handle}: scrape failed ({str(e)[:60]}) — skip")
            continue

        # tag as reference content: topic+region, no feed_source, skip the women-filter
        for c in clips:
            c["feed_source"] = None
            c["topic"] = a["topic"]
            c["region"] = a["region"]
            c["subject_type"] = "ref"      # pre-set so process_clips skips per-clip vision
        if clips:
            T.process_clips(clips, workers=8)

        appearance = None
        if appearance_on:
            try:
                appearance = aggregate_appearance([c.get("_cover_url") for c in clips if c.get("_cover_url")])
            except QuotaExceededError:
                log("  ⚠ OpenAI quota out — skipping appearance for remaining accounts (content+voice still scraping)")
                appearance_on = False
        update = {"scraped_at": datetime.now(timezone.utc).isoformat()}
        if appearance is not None:
            update["appearance"] = appearance
        db.table("reference_accounts").update(update).eq("id", a["id"]).execute()
        done += 1
        dom = (appearance or {}).get("dominant", {}) if appearance else {}
        log(f"  ✓ {a['topic']}/{a['region']} @{handle} ({plat}): {len(clips)} clips | "
            f"look={dom.get('hair_color')},{dom.get('skin_tone')} | {done}/{len(accts)} | {(time.time()-t0)/60:.1f}m")

    log(f"DONE — {done}/{len(accts)} reference accounts scraped in {(time.time()-t0)/60:.1f}m")


if __name__ == "__main__":
    main()
