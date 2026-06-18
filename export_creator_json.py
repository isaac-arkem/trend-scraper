#!/usr/bin/env python3
"""
Write one consolidated JSON dossier per creator to MinIO, for the creation wizard.

Contains: profile fields, profile-pic MinIO path, an aggregated appearance summary
(dominant attributes + % female across analyzed images), and the per-image
attributes. Stored next to the profile pic at:
    social-intel : profiles/<platform>/<platform_user_id>.json

Run:  .venv/bin/python export_creator_json.py
"""
import io
import os
import json
import time
from collections import Counter, defaultdict

from src.db.client import get_db
from src.storage.minio import get_minio, profile_pic_path

BUCKET = os.environ.get("MINIO_BUCKET", "social-intel")

# attributes we summarise (single-value vs list-valued)
SINGLE = ["skin_tone", "hair_color", "hair_length", "hair_texture", "body_frame",
          "body_shape", "eye_color", "makeup_style", "image_quality"]
MULTI = ["fashion_style", "content_style"]


def log(m):
    print(f"[{time.strftime('%H:%M:%S')}] {m}", flush=True)


def page(table, select, **eq):
    out, start = [], 0
    while True:
        q = get_db().table(table).select(select)
        for k, v in eq.items():
            q = q.eq(k, v)
        rows = q.order("id").range(start, start + 999).execute().data or []
        out += rows
        if len(rows) < 1000:
            break
        start += 1000
    return out


def top(counter: Counter):
    return counter.most_common(1)[0][0] if counter else None


def summarise(results: list[dict]) -> dict:
    """Aggregate a creator's per-image analysis into a dominant-attribute summary."""
    singles = {f: Counter() for f in SINGLE}
    multis = {f: Counter() for f in MULTI}
    n_person = n_female = 0
    for r in results:
        if r.get("person_visible"):
            n_person += 1
        raw = r.get("raw_json") or {}
        if raw.get("person_is_female") is True or raw.get("subject_type") == "female":
            n_female += 1
        for f in SINGLE:
            v = r.get(f)
            if v and v != "unclear":
                singles[f][v] += 1
        for f in MULTI:
            for v in (r.get(f) or []):
                if v:
                    multis[f][v] += 1
    return {
        "images_analyzed": len(results),
        "images_with_person": n_person,
        "images_female": n_female,
        "dominant": {f: top(c) for f, c in singles.items()},
        "top_fashion_style": [k for k, _ in multis["fashion_style"].most_common(3)],
        "top_content_style": [k for k, _ in multis["content_style"].most_common(3)],
    }


def main():
    db, mc = get_db(), get_minio()
    log("loading creators…")
    creators = page("creators", "*")
    log(f"{len(creators)} creators")

    log("loading analysis_results…")
    analysis = page("analysis_results",
                    "creator_id,person_visible,skin_tone,hair_color,hair_length,hair_texture,"
                    "body_frame,body_shape,eye_color,makeup_style,image_quality,fashion_style,"
                    "content_style,confidence,notes,media_asset_id,raw_json")
    by_creator = defaultdict(list)
    for a in analysis:
        by_creator[a["creator_id"]].append(a)
    log(f"{len(analysis)} analysis rows across {len(by_creator)} creators")

    written = 0
    for i, c in enumerate(creators, 1):
        key = c.get("platform_user_id") or c["username"]
        results = by_creator.get(c["id"], [])
        dossier = {
            "creator": {k: c.get(k) for k in (
                "id", "platform", "platform_user_id", "username", "full_name", "bio",
                "followers", "following", "post_count", "is_verified", "profile_url",
                "profile_pic_url", "market_id", "tier", "source_type",
                "relevance_score", "engagement_score", "community_score", "total_score",
                "language_detected")},
            "profile_pic_minio": profile_pic_path(c["platform"], key),
            "appearance_summary": summarise(results),
            "images": [{
                "media_asset_id": r.get("media_asset_id"),
                "person_visible": r.get("person_visible"),
                "skin_tone": r.get("skin_tone"), "hair_color": r.get("hair_color"),
                "hair_length": r.get("hair_length"), "hair_texture": r.get("hair_texture"),
                "body_frame": r.get("body_frame"), "makeup_style": r.get("makeup_style"),
                "fashion_style": r.get("fashion_style"), "content_style": r.get("content_style"),
                "confidence": r.get("confidence"),
            } for r in results],
        }
        data = json.dumps(dossier, ensure_ascii=False, indent=2).encode("utf-8")
        path = f"profiles/{c['platform']}/{key}.json"
        mc.put_object(BUCKET, path, io.BytesIO(data), length=len(data), content_type="application/json")
        written += 1
        if i % 200 == 0:
            log(f"  {i}/{len(creators)} dossiers written")
    log(f"DONE — {written} creator JSON dossiers written to MinIO ({BUCKET}/profiles/<platform>/<id>.json)")


if __name__ == "__main__":
    main()
