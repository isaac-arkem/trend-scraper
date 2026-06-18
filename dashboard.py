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
    path = request.args.get("p", "")
    if not path:
        return _placeholder()
    try:
        from src.storage.minio import get_minio
        mc = get_minio()
        bucket = os.environ.get("TRENDS_BUCKET", "trends")
        obj = mc.get_object(bucket, path)
        data = obj.read()
        ct = obj.headers.get("content-type", "application/octet-stream")
        return Response(data, content_type=ct,
                        headers={"Cache-Control": "public,max-age=86400", "Accept-Ranges": "bytes"})
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


@app.route("/api/trends")
def api_trends():
    platform = request.args.get("platform")          # 'tiktok' | 'instagram' | None
    region = request.args.get("region")              # MENA | LATAM | INDOPAC | None
    min_views = int(request.args.get("min_views", 0))

    # Lane 1 — proven trends, velocity-ranked, women only (built from female clips)
    trends = db.table("trends").select("*").order("velocity", desc=True).limit(300).execute().data or []
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
