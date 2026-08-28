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
import re
import time
import logging
from collections import Counter
from datetime import datetime, timezone

for _n in ("httpx", "httpcore", "urllib3", "apify_client", "openai"):
    logging.getLogger(_n).setLevel(logging.WARNING)

from src.db.client import get_db, upsert
from src.pipeline import trends as T
from src.ai.vision import analyze_image_bytes, QuotaExceededError

PER_ACCOUNT = 25        # recent posts per account. 15 was below a stable median,
                        # which limited every lift number computed downstream.
                        # Costs Apify results only — clips are pre-tagged
                        # subject_type='ref' below so they skip per-clip vision,
                        # and appearance stays capped at APPEARANCE_SAMPLE.
RECENCY_DAYS = 365      # reference accounts: their content regardless of "trend" recency
APPEARANCE_SAMPLE = 5   # cover frames to vision-analyze for the appearance profile

SINGLE = ["skin_tone", "hair_color", "hair_length", "hair_texture", "body_frame",
          "body_shape", "eye_color", "makeup_style"]


def log(m):
    print(f"[{time.strftime('%H:%M:%S')}] {m}", flush=True)


STAGES = [
    (1, "sources",  "Checking sources"),
    (2, "posts",    "Collecting posts"),
    (3, "media",    "Saving media"),
    (4, "results",  "Building your results"),
]
STAGE_TOTAL = len(STAGES)


def stage(key, detail=""):
    for n, k, text in STAGES:
        if k == key:
            print(f"STAGE|{n}|{STAGE_TOTAL}|{k}|{detail or text}", flush=True)
            return


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


def parse_account(raw):
    """A pasted profile URL or bare handle -> (platform, handle).

    The console lets people paste a mix of TikTok and Instagram links in one go,
    so platform is read off the URL rather than asked for separately. A bare
    handle has no platform in it and defaults to tiktok."""
    raw = (raw or "").strip().rstrip("/")
    if not raw:
        return None
    m = re.search(r"(?:https?://)?(?:www\.)?(tiktok|instagram)\.com/(?:@)?([A-Za-z0-9._-]+)", raw, re.I)
    if m:
        return m.group(1).lower(), m.group(2).lower()
    h = raw.lstrip("@")
    return ("tiktok", h.lower()) if re.fullmatch(r"[A-Za-z0-9._-]+", h or "") else None


def register_accounts(db, spec, region=None):
    """Add pasted accounts to reference_accounts before scraping them.

    Without this the script silently did nothing for a new account: it only ever
    read rows that already existed, so anything the operator typed in was never
    picked up. Each entry is 'url_or_handle=niche'.

    An account that already exists is NEVER rewritten. A blind upsert here
    overwrote a curated account's niche because the operator happened to paste a
    handle we already tracked — the row then looked like it belonged to the new
    niche, and a later cleanup keyed on that niche removed it. Existing accounts
    are reported and scraped, but their niche and region are left exactly as the
    person who curated them set them.
    """
    parsed_items, seen = [], []
    for item in [x for x in re.split(r"[,\n]+", spec) if x.strip()]:
        raw, _, niche = item.partition("=")
        parsed = parse_account(raw)
        if not parsed:
            log(f"   ! could not read account: {raw.strip()[:40]}")
            continue
        plat, handle = parsed
        niche = (niche or "").strip().lower().replace(" ", "_") or None
        parsed_items.append((plat, handle, niche))
        seen.append(handle)

    if not parsed_items:
        return []

    # which of these do we already hold?
    existing = {}
    handles = [h for _, h, _ in parsed_items]
    for i in range(0, len(handles), 100):
        for r in (db.table("reference_accounts")
                  .select("platform,handle,topic,region")
                  .in_("handle", handles[i:i + 100]).execute().data or []):
            existing[(r["platform"], (r["handle"] or "").lower())] = r

    new_rows, kept = [], []
    for plat, handle, niche in parsed_items:
        cur = existing.get((plat, handle))
        if cur:
            kept.append((handle, cur.get("topic")))
            if niche and niche != cur.get("topic"):
                log(f"   · @{handle} is already tracked under '{cur.get('topic')}' — "
                    f"keeping that, ignoring '{niche}'")
            continue
        if not niche:
            log(f"   ! @{handle} has no niche — it will not be grouped")
        new_rows.append({"platform": plat, "handle": handle, "topic": niche,
                         "region": region, "active": True})

    if new_rows:
        upsert("reference_accounts", new_rows, on_conflict="platform,handle")
        log(f"added {len(new_rows)} new account(s): "
            + ", ".join(f"{'TT' if r['platform']=='tiktok' else 'IG'}@{r['handle']}→{r['topic'] or '?'}"
                        for r in new_rows[:6])
            + (" …" if len(new_rows) > 6 else ""))
    if kept:
        log(f"{len(kept)} already tracked, left unchanged: "
            + ", ".join(f"@{h}({t or '?'})" for h, t in kept[:6])
            + (" …" if len(kept) > 6 else ""))
    return seen


def main():
    import os
    args = sys.argv[1:]
    def opt(name):
        return args[args.index(name) + 1] if name in args else None

    handles = [h.strip() for h in opt("--handles").split(",")] if opt("--handles") else None
    add      = opt("--add")           # "url=niche,url=niche" - new accounts
    region   = opt("--region")
    title    = opt("--title")         # names the run in reference_scrape_requests
    plat_only = (opt("--platform") or "").lower()   # tiktok | instagram | both
    # Depth and recency were module constants, so the console could show the
    # operator a "25 posts, last 14 days" choice that the script then ignored.
    per_account   = int(opt("--posts-per-account") or PER_ACCOUNT)
    recency_days  = int(opt("--recency-days") or RECENCY_DAYS)
    request_id = os.environ.get("REQUEST_ID")

    db = get_db()

    set_request_status(db, request_id, "running")

    stage("sources")

    # anything pasted in gets created first, then scraped in the same run
    if add:
        added = register_accounts(db, add, region)
        handles = list(dict.fromkeys((handles or []) + added))

    q = db.table("reference_accounts").select("*").eq("active", True)
    if handles:
        q = q.in_("handle", handles)
    if plat_only in ("tiktok", "instagram"):
        q = q.eq("platform", plat_only)
    accts = q.order("id").execute().data or []
    log((f"[{title}] " if title else "")
        + f"scraping {len(accts)} reference accounts"
        + (f" (handles={','.join(handles)})" if handles else " (all active)")
        + f" | {per_account} posts each, last {recency_days}d"
        + (f" | {plat_only} only" if plat_only in ("tiktok", "instagram") else ""))

    t0 = time.time()
    done = 0
    appearance_on = True   # flips off if OpenAI quota runs out (content+voice still scrape)
    try:
      for a in accts:
        handle, plat = a["handle"], a["platform"]
        log(f"── [{done+1}/{len(accts)}] @{handle} ({plat}) | topic={a.get('topic')} region={a.get('region')}")

        if done == 0:
            stage("posts", f"Collecting posts from {len(accts)} account(s)")
        log(f"   Fetching latest {per_account} posts from {plat}…")
        try:
            if plat == "tiktok":
                clips = T.scrape_tiktok_watchlist([handle], per_handle=per_account, recency_days=recency_days)
            else:
                clips = T.scrape_ig_watchlist([handle], per_handle=per_account, recency_days=recency_days)
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
            stage("media", f"Saving media for @{handle}")
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

      stage("results")
      log(f"━━ COMPLETE — {done}/{len(accts)} reference accounts scraped in {(time.time()-t0)/60:.1f}m ━━")
      set_request_status(db, request_id, "success")

    except Exception as e:
        msg = str(e)
        log(f"✗ FATAL — {msg}")
        set_request_status(db, request_id, "failed", error_message=msg)
        raise


if __name__ == "__main__":
    main()
