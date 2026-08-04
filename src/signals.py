"""Signals — one ranked view over clips, creators, hashtags and sounds.

Answers "how well is this doing?" rather than "how big is this?".

Everything here ranks by LIFT: how far something beat its own normal, not how
many views it got in absolute terms. A 1M-view clip from an account that
normally does 900K is unremarkable; a 1M-view clip from an account that
normally does 10K is the thing worth copying.

    clip lift    = clip views / that creator's median views
    bucket lift  = median clip lift of every clip in the bucket
                   (bucket = a hashtag, a sound, a creator, a niche)

The three source tables label the same places differently — clips.region holds
'LATAM' from the pipeline and 'latam' from the reference scraper, markets holds
'UAE' where every other row is ISO-2. Rather than rewrite live data, everything
is normalised on read here. Change ALIASES, not the database.
"""
from collections import defaultdict
from datetime import datetime, timezone, timedelta
import re
import statistics
import threading
import time

# ── label normalisation ─────────────────────────────────────────────────────
# The database is not wrong so much as it was written by three different
# processes that never agreed a vocabulary. This is the agreement.

COUNTRY_ALIASES = {
    "uae": "AE",              # markets.country_code holds the YAML key, not the ISO code
    "united states": "US",
    "usa": "US",
    "us": "US",
    "armenia": "AM",
    "italy": "IT",
}

# region = a group of countries. Lowercase reference-scraper values and
# uppercase pipeline values collapse into one bucket each.
REGION_ALIASES = {
    "latam": "LATAM",
    "mena": "MENA",
    "mideast": "MENA",
    "indopac": "INDOPAC",
    "armenia": "CIS",
    "russian": "CIS",
    "cis": "CIS",
    "united states": "US",
    "usa": "US",
    "us": "US",
    "misc": "MISC",
}

# niche = what the content is about, with geography stripped out. The five
# Armenian names had a country baked into the label, so they carry a country
# tag instead. Case-only duplicates collapse.
NICHE_ALIASES = {
    "maga": "maga",
    "cooking": "cooking_mum",
    "yerevan_lifestyle": "lifestyle",
    "comedy_skits": "comedy_skits",
    "heritage_diaspora": "heritage_diaspora",
    "music_dance": "music_dance",
    "fashion_beauty": "fashion_beauty",
}

# niches whose original label implied a country
NICHE_IMPLIED_COUNTRY = {
    "yerevan_lifestyle": "AM",
    "comedy_skits": "AM",
    "heritage_diaspora": "AM",
    "music_dance": "AM",
    "fashion_beauty": "AM",
}

SUBJECT_HIDDEN = {"child", "ad"}   # never shown, in any mode


def norm_country(raw, region_raw=None, niche_raw=None):
    """Best country code we can resolve, or None."""
    if raw:
        k = str(raw).strip()
        low = k.lower()
        if low in COUNTRY_ALIASES:
            return COUNTRY_ALIASES[low]
        if len(k) == 2 and k.isalpha():
            return k.upper()
    if niche_raw:
        implied = NICHE_IMPLIED_COUNTRY.get(str(niche_raw).strip().lower())
        if implied:
            return implied
    if region_raw:
        low = str(region_raw).strip().lower()
        if low in COUNTRY_ALIASES:
            return COUNTRY_ALIASES[low]
    return None


def norm_region(raw):
    if not raw:
        return None
    return REGION_ALIASES.get(str(raw).strip().lower(), str(raw).strip().upper())


def norm_niche(raw):
    if not raw:
        return None
    low = str(raw).strip().lower()
    return NICHE_ALIASES.get(low, low)


# ── ad detection ────────────────────────────────────────────────────────────
# The vision read carries is_ad_or_product, but it is only populated on clips
# scraped since the persist-vision change, so caption matching carries the rest.
_AD_PAT = re.compile(
    r"(^|\s)#(ad|advert|sponsored|partner|paidpartnership)\b|#\w*partner\b|paid partnership",
    re.I,
)


def looks_like_ad(clip):
    ap = clip.get("appearance")
    if isinstance(ap, dict) and ap.get("is_ad_or_product") is True:
        return True
    return bool(_AD_PAT.search(clip.get("caption") or ""))


# ── lift ────────────────────────────────────────────────────────────────────

MIN_CLIPS_FOR_BASELINE = 5      # an account needs this many clips before its median means anything
MIN_BASELINE_VIEWS     = 1000   # and the median must clear this, or tiny accounts score infinity
MIN_CLIPS_PER_BUCKET   = 8      # a hashtag/sound needs this many SCORED clips to be ranked
                                # 8, not 5: at 5 the top hashtag was a 5-clip spike scoring 30x;
                                # at 8 the board settles to repeatable results. Tunable in the UI.
BREAKOUT_AT            = 2.0    # a clip that did 2x its account's normal counts as a breakout

# Two different questions, so two different rankings.
#
# HASHTAG and SOUND buckets pull clips from many different accounts, so "median
# lift" is a real signal: it asks whether content carrying this tag typically
# beats the normal of whoever posted it.
#
# CREATOR and NICHE buckets do not work that way. A creator's clips measured
# against that same creator's own median must sit half above and half below —
# the median lift is ~1.0 by construction and says nothing. Niches inherit the
# same problem because they are made of whole accounts. Those two boards rank on
# BREAKOUT RATE instead: what share of the output actually broke out. That is a
# question the arithmetic can answer.

# Lift is a MEDIAN, never a mean. One freak clip in a three-clip hashtag was
# enough to top the board at 684x under a mean. best_lift is carried alongside
# as its own column so a single breakout stays visible without driving the rank.


def account_baselines(clips, min_clips=None, min_views=None):
    """Median views per creator handle, for accounts with enough clips.

    Both floors are tunable because neither has a defensible universal value —
    they trade coverage against trust. Lower them and more rows get scored on
    thinner evidence; raise them and the boards get quieter but every number
    means more."""
    min_clips = MIN_CLIPS_FOR_BASELINE if min_clips is None else min_clips
    min_views = MIN_BASELINE_VIEWS     if min_views is None else min_views
    by = defaultdict(list)
    for c in clips:
        h = (c.get("creator_handle") or "").lower()
        if h:
            by[h].append(c.get("views") or 0)
    out = {}
    for h, vals in by.items():
        if len(vals) < min_clips:
            continue
        med = statistics.median(vals)
        if med >= min_views:
            out[h] = med
    return out


def clip_lift(clip, baselines):
    h = (clip.get("creator_handle") or "").lower()
    med = baselines.get(h)
    if not med:
        return None
    return (clip.get("views") or 0) / med


# ── time series ─────────────────────────────────────────────────────────────

def _day(iso):
    if not iso:
        return None
    try:
        return datetime.fromisoformat(str(iso).replace("Z", "+00:00")).date()
    except Exception:
        return None


def sparkline(clips, days=30, buckets=12):
    """Views per time bucket over the window — the shape, not the numbers."""
    dated = [(d, c.get("views") or 0) for c in clips if (d := _day(c.get("posted_at")))]
    if not dated:
        return []
    end = max(d for d, _ in dated)
    start = end - timedelta(days=days)
    span = max((end - start).days, 1)
    out = [0] * buckets
    for d, v in dated:
        if d < start:
            continue
        idx = min(int((d - start).days / span * buckets), buckets - 1)
        out[idx] += v
    return out


# ── filtering ───────────────────────────────────────────────────────────────

def apply_filters(clips, country=None, region=None, niche=None,
                  subject="all", days=None, exclude_ads=True):
    now = datetime.now(timezone.utc).date()
    out = []
    for c in clips:
        st = c.get("subject_type")
        if st in SUBJECT_HIDDEN:
            continue
        if subject == "female" and st != "female":
            continue
        if subject == "male" and st != "male":
            continue
        if exclude_ads and looks_like_ad(c):
            continue
        if country and country != "ALL" and c["_country"] != country:
            continue
        if region and region != "ALL" and c["_region"] != region:
            continue
        if niche and niche != "ALL" and c["_niche"] != niche:
            continue
        if days:
            d = _day(c.get("posted_at"))
            if not d or (now - d).days > days:
                continue
        out.append(c)
    return out


# ── ranking ─────────────────────────────────────────────────────────────────

def _bucket_rows(clips, baselines, key_fn, days, limit, label_fn=None, min_bucket=None,
                 sort='lift', breakout_at=None):
    min_bucket  = min_bucket or MIN_CLIPS_PER_BUCKET
    breakout_at = BREAKOUT_AT if breakout_at is None else breakout_at
    buckets = defaultdict(list)
    for c in clips:
        for k in key_fn(c):
            if k:
                buckets[k].append(c)

    rows = []
    for key, items in buckets.items():
        lifts = [l for c in items if (l := clip_lift(c, baselines)) is not None]
        # gate on SCORED clips — a bucket of 40 clips where only 2 have a usable
        # baseline is 2 clips' worth of evidence, however big it looks
        if len(lifts) < min_bucket:
            continue
        views = sum(c.get("views") or 0 for c in items)
        handles = []
        for c in sorted(items, key=lambda x: -(x.get("views") or 0)):
            h = c.get("creator_handle")
            if h and h not in handles:
                handles.append(h)
            if len(handles) >= 3:
                break
        top = max(items, key=lambda c: clip_lift(c, baselines) or 0)
        rows.append({
            "key": key,
            "label": label_fn(key, items) if label_fn else key,
            "posts": len(items),
            "views": views,
            "lift": round(statistics.median(lifts), 2),
            "best_lift": round(max(lifts), 1),
            "breakout": round(sum(1 for l in lifts if l >= breakout_at) / len(lifts), 3),
            "scored": len(lifts),
            "creators": handles,
            "spark": sparkline(items, days=days or 90),
            "sample": {
                "caption": (top.get("caption") or "")[:90],
                "handle": top.get("creator_handle"),
                "views": top.get("views"),
                "video": top.get("video_minio_path"),
                "url": top.get("video_url"),
            },
        })
    rows.sort(key=lambda r: (-r["breakout"], -r["best_lift"]) if sort == "breakout"
              else -r["lift"])
    return rows[:limit]


def rank_hashtags(clips, baselines, days=None, limit=50, min_bucket=None, breakout_at=None):
    return _bucket_rows(
        clips, baselines,
        key_fn=lambda c: [f"#{h.lower()}" for h in (c.get("hashtags") or []) if h],
        days=days, limit=limit, min_bucket=min_bucket, breakout_at=breakout_at,
    )


def rank_sounds(clips, baselines, sound_names, days=None, limit=50, min_bucket=None, breakout_at=None):
    return _bucket_rows(
        clips, baselines,
        key_fn=lambda c: [c.get("sound_id")] if c.get("sound_id") else [],
        days=days, limit=limit, min_bucket=min_bucket, breakout_at=breakout_at,
        label_fn=lambda k, items: sound_names.get(k) or "unknown sound",
    )


def rank_creators(clips, baselines, days=None, limit=50, min_bucket=None, breakout_at=None):
    return _bucket_rows(
        clips, baselines,
        key_fn=lambda c: [(c.get("creator_handle") or "").lower()],
        days=days, limit=limit, min_bucket=min_bucket, breakout_at=breakout_at, sort="breakout",
        label_fn=lambda k, items: "@" + k,
    )


def rank_niches(clips, baselines, days=None, limit=50, min_bucket=None, breakout_at=None):
    return _bucket_rows(
        clips, baselines,
        key_fn=lambda c: [c.get("_niche")],
        days=days, limit=limit, min_bucket=min_bucket, breakout_at=breakout_at, sort="breakout",
    )


def rank_videos(clips, baselines, days=None, limit=50, breakout_at=None):
    breakout_at = BREAKOUT_AT if breakout_at is None else breakout_at
    rows = []
    for c in clips:
        lift = clip_lift(c, baselines)
        if lift is None:
            continue
        h = (c.get("creator_handle") or "").lower()
        rows.append({
            "key": c["id"],
            "label": (c.get("caption") or "").strip()[:110] or "(no caption)",
            "posts": 1,
            "views": c.get("views") or 0,
            "lift": round(lift, 2),
            "best_lift": round(lift, 1),
            "breakout": 1.0 if lift >= breakout_at else 0.0,
            "scored": 1,
            "creators": [c.get("creator_handle")] if c.get("creator_handle") else [],
            "spark": [],
            "baseline": int(baselines.get(h, 0)),
            "sample": {
                "caption": (c.get("caption") or "")[:90],
                "handle": c.get("creator_handle"),
                "views": c.get("views"),
                "video": c.get("video_minio_path"),
                "url": c.get("video_url"),
            },
        })
    rows.sort(key=lambda r: -r["lift"])
    return rows[:limit]


# ── source data, cached ─────────────────────────────────────────────────────

_cache = {"clips": None, "sounds": {}, "at": 0}
_lock = threading.Lock()
CACHE_TTL = 300


def load_clips(db, force=False):
    """Every clip, with normalised country / region / niche attached."""
    with _lock:
        if not force and _cache["clips"] is not None and time.time() - _cache["at"] < CACHE_TTL:
            return _cache["clips"], _cache["sounds"]

        cols = ("id,platform,platform_post_id,creator_handle,caption,hashtags,views,likes,"
                "comments,shares,saves,duration_sec,posted_at,sound_id,video_minio_path,"
                "video_url,velocity,subject_type,region,market_code,topic,appearance,transcript")
        clips, off = [], 0
        while True:
            page = (db.table("clips").select(cols)
                    .range(off, off + 999).execute().data or [])
            clips += page
            off += 1000
            if len(page) < 1000:
                break

        for c in clips:
            c["_country"] = norm_country(c.get("market_code"), c.get("region"), c.get("topic"))
            c["_region"]  = norm_region(c.get("region"))
            c["_niche"]   = norm_niche(c.get("topic"))

        sounds = {}
        off = 0
        while True:
            page = (db.table("sounds").select("id,name,author")
                    .range(off, off + 999).execute().data or [])
            for s in page:
                nm = (s.get("name") or "").strip()
                au = (s.get("author") or "").strip()
                sounds[s["id"]] = f"{nm} — {au}" if nm and au else (nm or au or "unknown sound")
            off += 1000
            if len(page) < 1000:
                break

        _cache.update({"clips": clips, "sounds": sounds, "at": time.time()})
        return clips, sounds


def facets(clips):
    """Every value the filters can take, already normalised."""
    def tally(attr):
        n = defaultdict(int)
        for c in clips:
            if c.get(attr):
                n[c[attr]] += 1
        return [{"value": k, "count": v} for k, v in sorted(n.items(), key=lambda x: -x[1])]
    return {
        "countries": tally("_country"),
        "regions":   tally("_region"),
        "niches":    tally("_niche"),
    }


def coverage(clips):
    """What the labels looked like before normalising — the mess, quantified."""
    raw_region, raw_market, raw_topic = defaultdict(int), defaultdict(int), defaultdict(int)
    for c in clips:
        raw_region[str(c.get("region"))] += 1
        raw_market[str(c.get("market_code"))] += 1
        raw_topic[str(c.get("topic"))] += 1
    return {
        "total": len(clips),
        "with_country": sum(1 for c in clips if c["_country"]),
        "with_region": sum(1 for c in clips if c["_region"]),
        "with_niche": sum(1 for c in clips if c["_niche"]),
        "raw_region": dict(sorted(raw_region.items(), key=lambda x: -x[1])),
        "raw_market": dict(sorted(raw_market.items(), key=lambda x: -x[1])),
        "raw_topic": dict(sorted(raw_topic.items(), key=lambda x: -x[1])),
    }
