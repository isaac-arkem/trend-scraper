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

# ── data API ─────────────────────────────────────────────────────────────────
@app.route("/api/markets")
def api_markets():
    rows = db.table("markets").select("*").execute().data or []
    seen, out = set(), []
    for r in rows:
        k = r["country_code"]
        if k not in seen:
            seen.add(k)
            out.append({"label": f"{r['name']} · {r['region']}", "value": k,
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

    for i in range(0, min(len(cids), 150), 50):
        batch = cids[i:i+50]
        posts    += db.table("posts").select("id,creator_id,caption,likes,views,comments_count,media_url,thumbnail_url,media_type").in_("creator_id", batch).limit(3000).execute().data or []
        media    += db.table("media_assets").select("id,creator_id,original_url,minio_path,analysis_status,asset_type,post_id").in_("creator_id", batch).limit(3000).execute().data or []
        analysis += db.table("analysis_results").select("*").in_("creator_id", batch).limit(3000).execute().data or []

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

    # AI Analysis: female only — confirmed True in raw_json
    def confirmed_female(a):
        if not a.get("person_visible"):
            return False
        raw = a.get("raw_json")
        if isinstance(raw, dict):
            return raw.get("person_is_female") is True
        return False  # no raw_json = can't confirm = exclude

    clean_analysis = [a for a in analysis if confirmed_female(a)]

    # Fetch media assets directly by media_asset_id from analysis results
    # so gallery always has the right image regardless of creator batch limits
    analysis_asset_ids = [a["media_asset_id"] for a in clean_analysis if a.get("media_asset_id")]
    analysis_media = {}
    for i in range(0, len(analysis_asset_ids), 200):
        batch = analysis_asset_ids[i:i+200]
        rows = db.table("media_assets").select("id,original_url,minio_path,asset_type,creator_id").in_("id", batch).execute().data or []
        for r in rows:
            analysis_media[r["id"]] = r

    return jsonify({
        "market": market, "market_id": mid,
        "creators": creators, "runs": runs,
        "posts": posts, "media": media,
        "analysis": clean_analysis,
        "analysis_media": analysis_media,
    })

# ── serve React app ──────────────────────────────────────────────────────────
@app.route("/")
def index():
    return send_file("static/index.html")

if __name__ == "__main__":
    print("Community Mapper → http://localhost:8050")
    app.run(port=8050, host="0.0.0.0", debug=False)
