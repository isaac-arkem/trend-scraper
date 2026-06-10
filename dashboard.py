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
        media    += db.table("media_assets").select("id,creator_id,original_url,analysis_status,asset_type").in_("creator_id", batch).limit(3000).execute().data or []
        analysis += db.table("analysis_results").select("*").in_("creator_id", batch).limit(3000).execute().data or []

    media = [m for m in media if m.get("asset_type") in ("image", "thumbnail", "frame")]

    ar_map = {a["media_asset_id"]: a for a in analysis}
    for m in media:
        m["analysis"] = ar_map.get(m["id"])

    return jsonify({
        "market": market, "market_id": mid,
        "creators": creators, "runs": runs,
        "posts": posts, "media": media,
        "analysis": [a for a in analysis if a.get("person_visible")],
    })

# ── serve React app ──────────────────────────────────────────────────────────
@app.route("/")
def index():
    return send_file("static/index.html")

if __name__ == "__main__":
    print("Community Mapper → http://localhost:8050")
    app.run(port=8050, host="0.0.0.0", debug=False)
