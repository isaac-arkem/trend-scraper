import json
from datetime import datetime, timezone
from src.db.client import get_db, insert, update, select
from src.utils.logger import get_logger

log = get_logger(__name__)


def get_or_create_run(market_id: str, platform: str, config: dict, resume: bool = False) -> dict:
    """Get an existing resumable run or create a new one."""
    db = get_db()

    if resume:
        existing = (
            db.table("runs")
            .select("*")
            .eq("market_id", market_id)
            .eq("platform", platform)
            .eq("status", "running")
            .order("started_at", desc=True)
            .limit(1)
            .execute()
            .data
        )
        if existing:
            run = existing[0]
            log.info(f"Resuming run {run['id']} at stage {run['current_stage']}")
            return run

    row = {
        "market_id": market_id,
        "platform": platform,
        "current_stage": 1,
        "stage_statuses": {str(i): "pending" for i in range(1, 7)},
        "status": "running",
        "config": config,
    }
    result = insert("runs", row)
    run = result[0]
    log.info(f"Created new run {run['id']} for market {market_id} / {platform}")
    return run


def stage_done(run_id: str, stage: int) -> None:
    db = get_db()
    run = db.table("runs").select("stage_statuses").eq("id", run_id).execute().data[0]
    statuses = run["stage_statuses"]
    statuses[str(stage)] = "done"
    next_stage = min(stage + 1, 6)
    db.table("runs").update({
        "stage_statuses": statuses,
        "current_stage": next_stage,
    }).eq("id", run_id).execute()


def stage_failed(run_id: str, stage: int, error: str) -> None:
    db = get_db()
    run = db.table("runs").select("stage_statuses").eq("id", run_id).execute().data[0]
    statuses = run["stage_statuses"]
    statuses[str(stage)] = "failed"
    db.table("runs").update({
        "stage_statuses": statuses,
        "status": "failed",
        "error_message": error[:1000],
        "ended_at": datetime.now(timezone.utc).isoformat(),
    }).eq("id", run_id).execute()


def run_complete(run_id: str) -> None:
    db = get_db()
    db.table("runs").update({
        "status": "done",
        "ended_at": datetime.now(timezone.utc).isoformat(),
    }).eq("id", run_id).execute()


def is_stage_done(run: dict, stage: int) -> bool:
    return run.get("stage_statuses", {}).get(str(stage)) == "done"


def update_run_counts(run_id: str, creators: int = 0, posts: int = 0, media: int = 0) -> None:
    db = get_db()
    db.table("runs").update({
        "total_creators": creators,
        "total_posts": posts,
        "total_media": media,
    }).eq("id", run_id).execute()
