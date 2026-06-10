#!/usr/bin/env python3
"""
Community Intelligence Pipeline
Usage: python main.py --market UAE --platform instagram --limit 20
"""
import sys
import yaml
import click
from dotenv import load_dotenv
from rich.console import Console

load_dotenv()
console = Console()

from src.utils.logger import get_logger
from src.db.client import get_db, upsert, select
from src.pipeline import runner
from src.pipeline import (
    stage1_discovery,
    stage2_enrichment,
    stage3_expand,
    stage4_harvest,
    stage5_analysis,
    stage6_aggregate,
)

log = get_logger(__name__)


def load_markets() -> dict:
    with open("config/markets.yaml") as f:
        return yaml.safe_load(f)["markets"]


def get_or_create_market(market_code: str, platform: str, market_cfg: dict) -> dict:
    db = get_db()
    existing = (
        db.table("markets")
        .select("*")
        .eq("country_code", market_code)
        .eq("platform", platform)
        .execute()
        .data
    )
    if existing:
        return existing[0]

    row = {
        "name": market_cfg["name"],
        "region": market_cfg["region"],
        "country_code": market_code,
        "platform": platform,
        "apify_region_code": market_cfg.get("apify_region_code"),
        "seed_hashtags": market_cfg["hashtags"].get(platform, []),
        "seed_profiles": market_cfg["seed_profiles"].get(platform, []),
        "languages": market_cfg.get("languages", []),
    }
    result = upsert("markets", row, on_conflict="country_code,platform")
    return result[0]


@click.command()
@click.option("--market", required=True, help="Market code e.g. UAE, SA, BR")
@click.option("--platform", default="both", type=click.Choice(["instagram", "tiktok", "both"]), show_default=True)
@click.option("--limit", default=10000, type=int, show_default=True, help="Max creators to target (no cap by default)")
@click.option("--posts", default=20, type=int, show_default=True, help="Posts per creator")
@click.option("--resume", is_flag=True, help="Resume from last checkpoint")
@click.option("--from-stage", default=1, type=int, show_default=True, help="Start from specific stage (1-6)")
@click.option("--skip-analysis", is_flag=True, help="Skip AI analysis (stage 5)")
def main(market, platform, limit, posts, resume, from_stage, skip_analysis):
    markets_cfg = load_markets()

    if market not in markets_cfg:
        console.print(f"[red]Market '{market}' not found. Available: {', '.join(markets_cfg.keys())}")
        sys.exit(1)

    market_cfg = markets_cfg[market]
    platforms = ["instagram", "tiktok"] if platform == "both" else [platform]

    console.rule(f"[bold green] Community Mapper — {market} ({', '.join(p.upper() for p in platforms)})")
    console.print(f"Target: {limit} creators | {posts} posts each | Resume: {resume}")

    for plat in platforms:
        console.rule(f"[bold] {plat.upper()}")
        _run_platform(market, plat, market_cfg, limit, posts, resume, from_stage, skip_analysis)


def _run_platform(market_code, platform, market_cfg, max_creators, posts_per_creator, resume, from_stage, skip_analysis):
    market_row = get_or_create_market(market_code, platform, market_cfg)
    market_id = market_row["id"]
    market_cfg["id"] = market_id

    run_config = {
        "max_creators": max_creators,
        "posts_per_creator": posts_per_creator,
        "from_stage": from_stage,
    }
    run = runner.get_or_create_run(market_id, platform, run_config, resume=resume)
    run_id = run["id"]

    seed_profiles = market_cfg["seed_profiles"].get(platform, [])
    limits = market_cfg.get("limits", {})
    posts_per_hashtag = limits.get("posts_per_hashtag", 100)
    max_expansion = limits.get("max_expansion_profiles", 200)

    enriched_creators = []

    # Stage 1 — Discovery
    if from_stage <= 1 and not runner.is_stage_done(run, 1):
        try:
            discovered, raw_posts = stage1_discovery.run(
                market_cfg, platform, limit_per_hashtag=posts_per_hashtag
            )
            runner.stage_done(run_id, 1)
            run = _reload_run(run_id)
        except Exception as e:
            log.error(f"Stage 1 failed: {e}")
            runner.stage_failed(run_id, 1, str(e))
            return
    else:
        discovered = []
        log.info("Stage 1 already done, skipping")

    # Stage 2 — Enrichment
    if from_stage <= 2 and not runner.is_stage_done(run, 2):
        try:
            enriched_creators = stage2_enrichment.run(
                candidates=discovered,
                market=market_cfg,
                market_id=market_id,
                run_id=run_id,
                platform=platform,
                seed_profiles=seed_profiles,
            )
            runner.stage_done(run_id, 2)
            run = _reload_run(run_id)
        except Exception as e:
            log.error(f"Stage 2 failed: {e}")
            runner.stage_failed(run_id, 2, str(e))
            return
    else:
        log.info("Stage 2 already done, skipping")
        enriched_creators = _load_creators_from_db(market_id, platform)

    # Stage 3 — Expand
    if from_stage <= 3 and not runner.is_stage_done(run, 3):
        try:
            all_creators = stage3_expand.run(
                enriched_creators=enriched_creators,
                market=market_cfg,
                market_id=market_id,
                run_id=run_id,
                platform=platform,
                max_candidates=max_expansion,
            )
            runner.stage_done(run_id, 3)
            run = _reload_run(run_id)
        except Exception as e:
            log.error(f"Stage 3 failed: {e}")
            runner.stage_failed(run_id, 3, str(e))
            return
    else:
        log.info("Stage 3 already done, skipping")

    # Stage 4 — Harvest
    if from_stage <= 4 and not runner.is_stage_done(run, 4):
        try:
            saved_posts, saved_assets = stage4_harvest.run(
                all_creators=enriched_creators,
                market=market_cfg,
                platform=platform,
                run_id=run_id,
                max_creators=max_creators,
                posts_per_creator=posts_per_creator,
            )
            runner.update_run_counts(run_id, media=len(saved_assets), posts=len(saved_posts))
            runner.stage_done(run_id, 4)
            run = _reload_run(run_id)
        except Exception as e:
            log.error(f"Stage 4 failed: {e}")
            runner.stage_failed(run_id, 4, str(e))
            return
    else:
        log.info("Stage 4 already done, skipping")

    # Stage 5 — AI Analysis
    if not skip_analysis and from_stage <= 5 and not runner.is_stage_done(run, 5):
        try:
            stage5_analysis.run(market_id=market_id, platform=platform)
            runner.stage_done(run_id, 5)
            run = _reload_run(run_id)
        except Exception as e:
            log.error(f"Stage 5 failed: {e}")
            runner.stage_failed(run_id, 5, str(e))
            return
    else:
        if skip_analysis:
            log.info("Stage 5 skipped (--skip-analysis)")
        else:
            log.info("Stage 5 already done, skipping")

    # Stage 6 — Aggregate
    if from_stage <= 6 and not runner.is_stage_done(run, 6):
        try:
            stage6_aggregate.run(market=market_cfg, platform=platform)
            runner.stage_done(run_id, 6)
        except Exception as e:
            log.error(f"Stage 6 failed: {e}")
            runner.stage_failed(run_id, 6, str(e))
            return

    runner.run_complete(run_id)
    console.print(f"\n[bold green]Pipeline complete for {market_cfg['name']} / {platform.upper()}")


def _reload_run(run_id: str) -> dict:
    from src.db.client import get_db
    return get_db().table("runs").select("*").eq("id", run_id).execute().data[0]


def _load_creators_from_db(market_id: str, platform: str) -> list[dict]:
    from src.db.client import get_db
    return get_db().table("creators").select("*").eq("market_id", market_id).eq("platform", platform).execute().data


if __name__ == "__main__":
    main()
