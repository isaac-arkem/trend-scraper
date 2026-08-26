"""Load creator profiles from the MinIO run archive into trend_creators, then
stamp the display fields onto trend_signals so a board can be drawn.

Costs nothing. The profiles were already fetched and paid for during the
harvest; this only reads the JSON that was archived at the time.

    .venv/bin/python load_creators.py                 # everything in the archive
    .venv/bin/python load_creators.py --date 2026/08/10
    .venv/bin/python load_creators.py --skip-signals  # table only, no board update

After it runs, a creator row on the board has: display_name, avatar_path,
profile_url, followers. Hashtag and sound rows carry up to three avatars in
metrics->creators, which is the cluster TikTok shows on its own trend board.
"""
import argparse
import json
from collections import defaultdict

from dotenv import load_dotenv
load_dotenv()

from src.db.client import get_db, upsert
from src.storage.minio import profile_pic_path
from src.utils.logger import get_logger
import src.run_archive as A

log = get_logger(__name__)


def read_archive(date_prefix=None):
    """Every profile JSON the harvest wrote, newest wins on duplicates."""
    pref = f"runs/tiktok/{date_prefix}/" if date_prefix else "runs/tiktok/"
    out, files = {}, 0
    # Through run_archive, not the client directly — the archive is a bucket
    # under MinIO and a prefix under Wasabi, and listing the logical name
    # against the wrong one returns nothing rather than failing.
    for name in A.list_archive(pref):
        if "/profiles/" not in name or not name.endswith(".json"):
            continue
        try:
            doc = A.read_json(name)
        except Exception as e:
            log.warning(f"unreadable {name}: {str(e)[:60]}")
            continue
        p = doc.get("profile") or {}
        u = (p.get("username") or "").lower()
        if not u:
            continue
        files += 1
        prev = out.get(u)
        # a creator harvested by several countries appears more than once
        if not prev or (doc.get("fetched_at") or "") >= (prev["_fetched"] or ""):
            out[u] = {"_profile": p, "_country": doc.get("country"),
                      "_fetched": doc.get("fetched_at")}
    log.info(f"archive: {files} profile files -> {len(out)} distinct creators")
    return out


def to_row(username, rec):
    p = rec["_profile"]
    uid = p.get("platform_user_id") or username
    return {
        "platform": p.get("platform") or "tiktok",
        "username": username,
        "platform_user_id": p.get("platform_user_id"),
        "display_name": p.get("full_name"),
        "bio": p.get("bio"),
        "followers": p.get("followers"),
        "following": p.get("following"),
        "post_count": p.get("post_count"),
        "is_verified": bool(p.get("is_verified")),
        "profile_url": p.get("profile_url") or f"https://www.tiktok.com/@{username}",
        "profile_pic_url": p.get("profile_pic_url"),
        "avatar_path": profile_pic_path(p.get("platform") or "tiktok", uid),
        "country_code": rec.get("_country"),
        "last_seen_at": rec.get("_fetched"),
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", help="YYYY/MM/DD; default every run in the archive")
    ap.add_argument("--skip-signals", action="store_true")
    a = ap.parse_args()

    db = get_db()
    found = read_archive(a.date)
    if not found:
        log.warning("nothing in the archive"); return

    rows = [to_row(u, r) for u, r in found.items()]
    for i in range(0, len(rows), 200):
        upsert("trend_creators", rows[i:i + 200], on_conflict="platform,username")
    log.info(f"trend_creators: {len(rows)} upserted")

    if a.skip_signals:
        return

    # ── stamp the board so it renders without a join ────────────────────────
    look = {r["username"]: r for r in rows}

    sig, off = [], 0
    while True:
        page = (db.table("trend_signals")
                .select("id,kind,key,label,metrics,sample_clip_id")
                .range(off, off + 999).execute().data or [])
        sig += page; off += 1000
        if len(page) < 1000:
            break
    log.info(f"trend_signals: {len(sig)} rows to consider")

    updates, enriched_clusters = [], 0
    clip_owner = {}
    ids = [s["sample_clip_id"] for s in sig if s.get("sample_clip_id")]
    for i in range(0, len(ids), 200):
        for c in (db.table("clips").select("id,creator_handle,video_minio_path")
                  .in_("id", ids[i:i + 200]).execute().data or []):
            clip_owner[c["id"]] = c

    for s in sig:
        patch = {}
        if s["kind"] == "creator":
            handle = (s["key"] or "").lower().lstrip("@")
            r = look.get(handle)
            if r:
                patch = {"display_name": r["display_name"], "avatar_path": r["avatar_path"],
                         "profile_url": r["profile_url"], "followers": r["followers"]}
        elif s["kind"] == "video":
            c = clip_owner.get(s.get("sample_clip_id"))
            if c:
                r = look.get((c.get("creator_handle") or "").lower())
                patch = {"thumb_path": c.get("video_minio_path")}
                if r:
                    patch.update({"display_name": r["display_name"],
                                  "avatar_path": r["avatar_path"],
                                  "profile_url": r["profile_url"],
                                  "followers": r["followers"]})
        else:
            # hashtag / sound: the avatar cluster, up to three
            m = s.get("metrics") or {}
            # This runs more than once over the same rows, so creators may already
            # be dicts from a previous pass. Normalise back to handles first.
            handles = []
            for h in (m.get("creators") or [])[:3]:
                h = h.get("handle") if isinstance(h, dict) else h
                if h:
                    handles.append(str(h))
            if handles:
                m["creators"] = [
                    {"handle": h,
                     "avatar_path": (look.get(h.lower()) or {}).get("avatar_path"),
                     "profile_url": (look.get(h.lower()) or {}).get("profile_url")
                                    or f"https://www.tiktok.com/@{h}"}
                    for h in handles
                ]
                patch = {"metrics": m}
                enriched_clusters += 1
        if patch:
            updates.append((s["id"], patch))

    for sid, patch in updates:
        db.table("trend_signals").update(patch).eq("id", sid).execute()

    log.info(f"trend_signals updated: {len(updates)} rows "
             f"({enriched_clusters} avatar clusters)")


if __name__ == "__main__":
    main()
