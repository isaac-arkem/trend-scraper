"""
Community Mapper — Flask server
Serves React dashboard + image proxy + data API
Run: uv run python dashboard.py → http://localhost:8050
"""
import os
from dotenv import load_dotenv
load_dotenv()

from flask import Flask, send_file, request, Response, jsonify
import httpx
from supabase import create_client

app = Flask(__name__, static_folder="static")
db  = create_client(os.environ["SUPABASE_URL"], os.environ["SUPABASE_SECRET_KEY"])

# ── MinIO image serve ─────────────────────────────────────────────────────────
@app.route("/img-minio")
def serve_minio():
    path = request.args.get("p", "")
    if not path:
        return _placeholder()
    try:
        from src.storage.minio import get_minio
        import os
        mc = get_minio()
        bucket = os.environ.get("MINIO_BUCKET", "social-intel")
        obj = mc.get_object(bucket, path)
        data = obj.read()
        ct = obj.headers.get("content-type", "image/jpeg")
        return Response(data, content_type=ct,
                        headers={"Cache-Control": "public,max-age=86400"})
    except Exception:
        return _placeholder()

# ── image proxy (bypasses CORS / Instagram auth) ──────────────────────────────
@app.route("/img")
def proxy_img():
    url = request.args.get("u", "")
    if not url:
        return _placeholder()
    try:
        r = httpx.get(url, follow_redirects=True, timeout=12, headers={
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
            "Referer": "https://www.instagram.com/",
        })
        ct = r.headers.get("content-type", "image/jpeg")
        return Response(r.content, content_type=ct,
                        headers={"Cache-Control": "public,max-age=3600"})
    except Exception:
        return _placeholder()

def _placeholder():
    svg = b'<svg xmlns="http://www.w3.org/2000/svg" width="1" height="1"><rect fill="#1A1A1A" width="1" height="1"/></svg>'
    return Response(svg, content_type="image/svg+xml")

# ── trends bucket serve (audio / video) ────────────────────────────────────
@app.route("/trend-file")
def trend_file():
    import re
    path = request.args.get("p", "")
    if not path:
        return _placeholder()
    from src.storage.minio import get_minio
    mc = get_minio()
    bucket = os.environ.get("TRENDS_BUCKET", "trends")
    try:
        st = mc.stat_object(bucket, path)
        size = st.size
        ct = st.content_type or "application/octet-stream"
    except Exception:
        return _placeholder()

    rng = request.headers.get("Range")
    try:
        if rng:
            # browsers need 206/partial-content to render + seek video
            m = re.match(r"bytes=(\d+)-(\d*)", rng)
            start = int(m.group(1))
            end = int(m.group(2)) if m.group(2) else size - 1
            end = min(end, size - 1)
            length = end - start + 1
            data = mc.get_object(bucket, path, offset=start, length=length).read()
            resp = Response(data, status=206, content_type=ct)
            resp.headers["Content-Range"] = f"bytes {start}-{end}/{size}"
            resp.headers["Accept-Ranges"] = "bytes"
            resp.headers["Content-Length"] = str(length)
            return resp
        data = mc.get_object(bucket, path).read()
        return Response(data, content_type=ct,
                        headers={"Accept-Ranges": "bytes", "Content-Length": str(size),
                                 "Cache-Control": "public,max-age=86400"})
    except Exception:
        return _placeholder()

# ── data API ─────────────────────────────────────────────────────────────────
@app.route("/api/markets")
def api_markets():
    rows = db.table("markets").select("*").execute().data or []
    seen, out = set(), []
    for r in rows:
        k = r["country_code"]
        if k not in seen:
            seen.add(k)
            region = "MENA" if r["region"] == "META" else r["region"]
            out.append({"label": f"{r['name']} · {region}", "value": k,
                        "platforms": [r["platform"]]})
        else:
            for o in out:
                if o["value"] == k and r["platform"] not in o["platforms"]:
                    o["platforms"].append(r["platform"])
    return jsonify(out)

@app.route("/api/data")
def api_data():
    mkt_code = request.args.get("market")
    platform = request.args.get("platform", "instagram")

    mkts = db.table("markets").select("*").eq("country_code", mkt_code).eq("platform", platform).execute().data
    if not mkts:
        return jsonify({})
    market = mkts[0]; mid = market["id"]

    creators = db.table("creators").select("*").eq("market_id", mid).eq("platform", platform)\
                 .order("total_score", desc=True).limit(300).execute().data or []
    runs = db.table("runs").select("*").eq("market_id", mid).eq("platform", platform)\
              .order("started_at", desc=True).limit(20).execute().data or []

    cids = [c["id"] for c in creators]
    posts, media, analysis = [], [], []

    # Posts/media: sample for the Overview/Creators tabs (top creators is fine here —
    # accurate market totals are computed separately via count queries below).
    for i in range(0, min(len(cids), 150), 50):
        batch = cids[i:i+50]
        posts    += db.table("posts").select("id,creator_id,caption,likes,views,comments_count,media_url,thumbnail_url,media_type").in_("creator_id", batch).limit(3000).execute().data or []
        media    += db.table("media_assets").select("id,creator_id,original_url,minio_path,analysis_status,asset_type,post_id").in_("creator_id", batch).limit(3000).execute().data or []

    # Analysis: fetch ALL rows for EVERY creator, paginated. Supabase caps each
    # response at ~1000 rows, so a flat .limit() silently truncated the WOMEN /
    # SKIPPED counts — page through with .range() instead.
    for i in range(0, len(cids), 50):
        batch = cids[i:i+50]
        start = 0
        while True:
            page = db.table("analysis_results").select("*").in_("creator_id", batch)\
                     .order("id").range(start, start + 999).execute().data or []
            analysis += page
            if len(page) < 1000:
                break
            start += 1000

    media = [m for m in media if m.get("asset_type") in ("image", "thumbnail", "frame")]

    ar_map = {a["media_asset_id"]: a for a in analysis}
    # Build post_url map for TikTok embed
    all_post_ids = list({m["post_id"] for m in media if m.get("post_id")})
    post_url_map = {}
    for i in range(0, len(all_post_ids), 200):
        batch_posts = db.table("posts").select("id,post_url,platform").in_("id", all_post_ids[i:i+200]).execute().data or []
        for p in batch_posts:
            post_url_map[p["id"]] = p.get("post_url", "")

    for m in media:
        m["analysis"] = ar_map.get(m["id"])
        m["post_url"] = post_url_map.get(m.get("post_id"), "")

    # Categorise each analysis result by subject type (from raw_json)
    def subject_of(a):
        raw = a.get("raw_json")
        if isinstance(raw, dict):
            if raw.get("subject_type"):
                return raw["subject_type"]
            # derive for older rows without subject_type
            if not raw.get("person_visible"):
                return "none"
            if raw.get("is_ad_or_product") is True:
                return "ad"
            if raw.get("is_child") is True:
                return "child"
            if raw.get("person_is_female") is True:
                return "female"
            if raw.get("person_is_female") is False:
                return "male"
        return "unclear"

    for a in analysis:
        a["subject_type"] = subject_of(a)

    # Women = the real results for AI analysis; rest = skipped (still viewable)
    clean_analysis = [a for a in analysis if a["subject_type"] == "female"]
    skipped_analysis = [a for a in analysis if a["subject_type"] in ("male", "child", "ad")]

    # Fetch media assets directly by media_asset_id from analysis results
    # so gallery always has the right image regardless of creator batch limits
    analysis_asset_ids = [a["media_asset_id"] for a in (clean_analysis + skipped_analysis) if a.get("media_asset_id")]
    analysis_media = {}
    for i in range(0, len(analysis_asset_ids), 200):
        batch = analysis_asset_ids[i:i+200]
        rows = db.table("media_assets").select("id,original_url,minio_path,asset_type,creator_id").in_("id", batch).execute().data or []
        for r in rows:
            analysis_media[r["id"]] = r

    # Accurate per-market counts using creator_ids
    cids_all = [c["id"] for c in creators]

    total_posts = 0
    total_media = 0
    total_processed = 0
    for i in range(0, len(cids_all), 100):
        batch = cids_all[i:i+100]
        total_posts += (db.table("posts").select("id", count="exact")
                        .in_("creator_id", batch).execute().count or 0)
        total_media += (db.table("media_assets").select("id", count="exact")
                        .in_("creator_id", batch).execute().count or 0)
        # ANALYZED = truly sent through the vision model. 'skipped' now also covers
        # over-cap / early-stopped images that were never analyzed, so count 'done' only.
        total_processed += (db.table("media_assets").select("id", count="exact")
                            .eq("analysis_status", "done")
                            .in_("creator_id", batch).execute().count or 0)

    return jsonify({
        "market": market, "market_id": mid,
        "creators": creators, "runs": runs,
        "posts": posts, "media": media,
        "analysis": clean_analysis,
        "skipped_analysis": skipped_analysis,
        "analysis_media": analysis_media,
        "total_posts": total_posts,
        "total_media": total_media,
        "total_processed": total_processed,
    })

# ── trends API ───────────────────────────────────────────────────────────────
def _enrich_clips(clips):
    """Attach each clip's sound (name/id/page/audio/usage) for the dashboard cards."""
    sound_ids = list({c["sound_id"] for c in clips if c.get("sound_id")})
    smap = {}
    for i in range(0, len(sound_ids), 100):
        rows = db.table("sounds").select("*").in_("id", sound_ids[i:i+100]).execute().data or []
        for s in rows:
            smap[s["id"]] = s
    for c in clips:
        c["sound"] = smap.get(c.get("sound_id"))
    return clips


def _clip_format(c):
    dur = c.get("duration_sec") or 0
    if dur and dur <= 20:
        length = "short"
    elif dur and dur <= 60:
        length = "medium"
    elif dur:
        length = "long"
    else:
        length = "unknown"
    audio = "audio" if c.get("sound_id") else "no-audio"
    return f"{length}_{audio}"


def _avg(rows, key):
    vals = [r.get(key) or 0 for r in rows]
    return round(sum(vals) / len(vals)) if vals else 0


def _engagement(c):
    return (c.get("likes") or 0) + (c.get("comments") or 0) + (c.get("shares") or 0) + (c.get("saves") or 0)


def _summarize_group(rows):
    views = [r.get("views") or 0 for r in rows]
    fmts = {}
    platforms = {}
    for r in rows:
        fmts[r["format"]] = fmts.get(r["format"], 0) + 1
        platforms[r.get("platform") or "unknown"] = platforms.get(r.get("platform") or "unknown", 0) + 1
    top_format = max(fmts.items(), key=lambda x: x[1])[0] if fmts else None
    return {
        "clip_count": len(rows),
        "avg_views": round(sum(views) / len(views)) if views else 0,
        "total_views": sum(views),
        "avg_velocity": _avg(rows, "velocity"),
        "total_engagement": sum(_engagement(r) for r in rows),
        "top_format": top_format,
        "formats": fmts,
        "platforms": platforms,
    }


@app.route("/api/trends")
def api_trends():
    platform = request.args.get("platform")          # 'tiktok' | 'instagram' | None
    region = request.args.get("region")              # MENA | LATAM | INDOPAC | None
    min_views = int(request.args.get("min_views", 0))

    # Lane 1 — proven trends, velocity-ranked, women only (built from female clips)
    trends = db.table("trends").select("*").order("velocity", desc=True).limit(2000).execute().data or []
    ref_ids = [t["reference_clip_id"] for t in trends if t.get("reference_clip_id")]
    refs = {}
    for i in range(0, len(ref_ids), 100):
        for c in (db.table("clips").select("*").in_("id", ref_ids[i:i+100]).execute().data or []):
            refs[c["id"]] = c
    lane1 = []
    for t in trends:
        ref = refs.get(t.get("reference_clip_id"))
        if not ref:
            continue
        if platform and ref.get("platform") != platform:
            continue
        if region and ref.get("region") != region:
            continue
        if (ref.get("views") or 0) < min_views:
            continue
        t["clip"] = ref
        lane1.append(t)
    _enrich_clips([t["clip"] for t in lane1])

    # Lane 2 — watchlist signal (recent posts from reference accounts, women)
    wq = db.table("clips").select("*").eq("feed_source", "watchlist")\
        .eq("subject_type", "female").order("posted_at", desc=True).limit(120)
    if platform:
        wq = wq.eq("platform", platform)
    lane2 = wq.execute().data or []
    _enrich_clips(lane2)

    audios = db.table("sounds").select("*").order("clip_count", desc=True).limit(50).execute().data or []
    return jsonify({"lane1": lane1, "lane2": lane2, "audios": audios})


@app.route("/api/reference")
def api_reference():
    """Reference influencers grouped by topic x region, with per-account stats
    (traction, appearance) + top clips. Powers the niche-library page."""
    accts = db.table("reference_accounts").select("*").execute().data or []

    # all reference clips (topic-tagged), grouped by (platform, handle)
    clips, start = [], 0
    while True:
        page = db.table("clips").select("id,platform,creator_handle,views,likes,comments,shares,saves,"
                                        "velocity,duration_sec,video_url,video_minio_path,caption,"
                                        "sound_id,topic,region,posted_at")\
            .not_.is_("topic", "null").order("id").range(start, start + 999).execute().data or []
        clips += page
        if len(page) < 1000:
            break
        start += 1000

    by_handle = {}
    for c in clips:
        c["format"] = _clip_format(c)
        by_handle.setdefault((c["platform"], (c["creator_handle"] or "").lower()), []).append(c)
    _enrich_clips(clips)

    out = []
    for a in accts:
        cl = sorted(by_handle.get((a["platform"], a["handle"].lower()), []),
                    key=lambda c: c.get("velocity") or 0, reverse=True)
        views = [c.get("views") or 0 for c in cl]
        a["clip_count"] = len(cl)
        a["avg_views"] = round(sum(views) / len(views)) if views else 0
        a["avg_velocity"] = round(sum(c.get("velocity") or 0 for c in cl) / len(cl)) if cl else 0
        a["total_views"] = sum(views)
        a["total_engagement"] = sum(_engagement(c) for c in cl)
        a["top_format"] = _summarize_group(cl)["top_format"] if cl else None
        a["clips"] = cl[:6]
        out.append(a)

    by_topic, by_region, matrix, by_format = {}, {}, {}, {}
    for c in clips:
        topic = c.get("topic") or "unknown"
        region = c.get("region") or "unknown"
        by_topic.setdefault(topic, []).append(c)
        by_region.setdefault(region, []).append(c)
        matrix.setdefault(topic, {}).setdefault(region, []).append(c)
        by_format.setdefault(c["format"], []).append(c)

    topic_summary = [{"topic": k, **_summarize_group(v)} for k, v in by_topic.items()]
    region_summary = [{"region": k, **_summarize_group(v)} for k, v in by_region.items()]
    format_summary = [{"format": k, **_summarize_group(v)} for k, v in by_format.items()]
    matrix_summary = {
        topic: {region: _summarize_group(rows) for region, rows in regions.items()}
        for topic, regions in matrix.items()
    }

    return jsonify({
        "accounts": out,
        "topics": sorted(topic_summary, key=lambda x: x["avg_views"], reverse=True),
        "regions": sorted(region_summary, key=lambda x: x["avg_views"], reverse=True),
        "formats": sorted(format_summary, key=lambda x: x["clip_count"], reverse=True),
        "matrix": matrix_summary,
        "totals": {
            "accounts": len(out),
            "scraped_accounts": len([a for a in out if a.get("scraped_at")]),
            "clips": len(clips),
            "topics": len(by_topic),
            "regions": len(by_region),
        },
    })


@app.route("/api/trend-status", methods=["POST"])
def api_trend_status():
    body = request.get_json(force=True)
    tid, status = body.get("id"), body.get("status")
    if status not in ("new", "queued", "recreated", "skipped"):
        return jsonify({"error": "bad status"}), 400
    db.table("trends").update({"status": status}).eq("id", tid).execute()
    return jsonify({"ok": True})


# ── serve React app ──────────────────────────────────────────────────────────
@app.route("/")
def index():
    return send_file("static/index.html")

if __name__ == "__main__":
    print("Community Mapper → http://localhost:8050")
    app.run(port=8050, host="0.0.0.0", debug=False)
