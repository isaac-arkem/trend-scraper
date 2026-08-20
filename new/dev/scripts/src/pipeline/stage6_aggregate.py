"""Stage 6: Aggregate analysis results into a market insight report."""
from collections import Counter
from src.db.client import get_db
from src.utils.logger import get_logger
from rich.console import Console
from rich.table import Table
import json

log = get_logger(__name__)
console = Console()


def run(market: dict, platform: str) -> dict:
    db = get_db()
    market_id = market["id"]
    market_name = market["name"]
    region = market["region"]

    log.info(f"[Stage 6] Aggregating results for {market_name} / {platform.upper()}")

    # Fetch all analysis results for this market
    rows = (
        db.table("analysis_results")
        .select("*, creators!inner(market_id, platform, username, followers, engagement_score)")
        .eq("creators.market_id", market_id)
        .eq("creators.platform", platform)
        .eq("person_visible", True)
        .execute()
        .data
    )

    if not rows:
        log.warning("[Stage 6] No analysis results found")
        return {}

    # Fetch top creators
    top_creators = (
        db.table("creators")
        .select("username, followers, total_score, engagement_score")
        .eq("market_id", market_id)
        .eq("platform", platform)
        .order("total_score", desc=True)
        .limit(20)
        .execute()
        .data
    )

    # Fetch caption/hashtag data for communication style
    posts = (
        db.table("posts")
        .select("caption, hashtags, likes, views, comments_count, creators!inner(market_id, platform)")
        .eq("creators.market_id", market_id)
        .eq("creators.platform", platform)
        .execute()
        .data
    )

    report = {
        "market": market_name,
        "region": region,
        "platform": platform,
        "total_creators_analyzed": len({r["creator_id"] for r in rows}),
        "total_media_analyzed": len(rows),
        "appearance": _aggregate_field(rows, [
            "body_frame", "body_shape", "skin_tone",
            "hair_color", "hair_length", "hair_texture",
            "eye_color", "makeup_style",
        ]),
        "fashion_style": _aggregate_array_field(rows, "fashion_style"),
        "content_style": _aggregate_array_field(rows, "content_style"),
        "top_creators": [
            {"username": c["username"], "followers": c["followers"], "score": c["total_score"]}
            for c in top_creators
        ],
        "top_hashtags": _top_hashtags(posts),
        "avg_engagement": _avg_engagement(rows),
    }

    _print_report(report)
    return report


def _aggregate_field(rows: list[dict], fields: list[str]) -> dict:
    result = {}
    for field in fields:
        values = [r.get(field) for r in rows if r.get(field) and r.get(field) != "unclear"]
        if values:
            counts = Counter(values)
            total = len(values)
            result[field] = [
                {"value": k, "count": v, "pct": round(v / total * 100, 1)}
                for k, v in counts.most_common(5)
            ]
    return result


def _aggregate_array_field(rows: list[dict], field: str) -> list[dict]:
    all_values = []
    for r in rows:
        vals = r.get(field) or []
        if isinstance(vals, list):
            all_values.extend(vals)
    if not all_values:
        return []
    counts = Counter(all_values)
    total = len(rows)
    return [
        {"value": k, "count": v, "pct": round(v / total * 100, 1)}
        for k, v in counts.most_common(8)
    ]


def _top_hashtags(posts: list[dict]) -> list[dict]:
    all_tags = []
    for p in posts:
        tags = p.get("hashtags") or []
        if isinstance(tags, list):
            all_tags.extend([t.lower().strip("#") for t in tags if t])
    counts = Counter(all_tags)
    return [{"hashtag": k, "count": v} for k, v in counts.most_common(15)]


def _avg_engagement(rows: list[dict]) -> float:
    scores = [r.get("creators", {}).get("engagement_score", 0) for r in rows if r.get("creators")]
    return round(sum(scores) / len(scores), 4) if scores else 0.0


def _print_report(report: dict) -> None:
    console.rule(f"[bold] {report['market']} — {report['platform'].upper()} Insights")
    console.print(f"Region: {report['region']} | Creators analyzed: {report['total_creators_analyzed']} | Media: {report['total_media_analyzed']}")

    for field, values in report["appearance"].items():
        t = Table(title=field.replace("_", " ").title(), show_header=True)
        t.add_column("Value"); t.add_column("Count"); t.add_column("%")
        for v in values:
            t.add_row(v["value"], str(v["count"]), f"{v['pct']}%")
        console.print(t)

    if report["fashion_style"]:
        t = Table(title="Fashion Style", show_header=True)
        t.add_column("Style"); t.add_column("Count"); t.add_column("%")
        for v in report["fashion_style"]:
            t.add_row(v["value"], str(v["count"]), f"{v['pct']}%")
        console.print(t)

    if report["top_creators"]:
        t = Table(title="Top Creators", show_header=True)
        t.add_column("Username"); t.add_column("Followers"); t.add_column("Score")
        for c in report["top_creators"][:10]:
            t.add_row(f"@{c['username']}", f"{c['followers']:,}" if c['followers'] else "?", str(c['score']))
        console.print(t)
