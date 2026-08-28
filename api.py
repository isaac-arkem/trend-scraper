"""HTTP front door for the creator intelligence pipeline.

Railway runs this; the platform calls it. The pipeline itself stays a plain CLI
script — this only starts one, tracks it, and reports on it, so anything that
works from a terminal works from the platform and vice versa.

    POST   /runs          start a run          (niche REQUIRED)
    GET    /runs          history — drives "Last run" and "Re-run"
    GET    /runs/{id}     status, counts, spend, live log tail
    GET    /niches        the niche list for the dropdown
    POST   /niches        create one
    GET    /health

Every endpoint except /health needs `Authorization: Bearer <API_TOKEN>`.

CONCURRENCY
-----------
Two runs at once, hard. Each run is an Apify-bound subprocess that can take an
hour, and Railway bills for what is running — an unbounded queue turns a busy
afternoon into a very large invoice. A third request is refused with 429 and
told what is already running, rather than being queued invisibly.
"""
import asyncio
import os
import re
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone
from typing import Optional

from dotenv import load_dotenv
from fastapi import Depends, FastAPI, Header, HTTPException
from pydantic import BaseModel, Field

load_dotenv()

from src.db.client import get_db          # noqa: E402
from src.utils.logger import get_logger   # noqa: E402

log = get_logger(__name__)

API_TOKEN = os.environ.get("API_TOKEN", "")
MAX_CONCURRENT = int(os.environ.get("MAX_CONCURRENT_RUNS", "2"))
LOG_DIR = os.environ.get("RUN_LOG_DIR", "/tmp/runs")
os.makedirs(LOG_DIR, exist_ok=True)

app = FastAPI(title="Creator Intelligence", version="1.0")

# In-process registry. Railway runs one instance of this service, so a dict is
# the honest scope — a second instance would need the cap in the database, and
# that is worth doing only when there is a second instance.
RUNS: dict = {}


def auth(authorization: str = Header(default="")) -> None:
    if not API_TOKEN:
        raise HTTPException(500, "API_TOKEN is not configured on the server")
    if authorization != f"Bearer {API_TOKEN}":
        raise HTTPException(401, "bad or missing bearer token")


# The three pipelines, and how many of each may run at once.
#
# One service rather than three: they live in the same repo, import the same
# src/, and share one set of credentials, so three Railway services would be
# three copies of the same image and three times the idle bill for no isolation
# anyone benefits from.
#
# The caps differ because the work differs. Creator Intelligence runs an Apify
# sweep per country and then an OpenAI pass, so two is already a lot of parallel
# spend. Reference Profiles is a short fetch against known handles. Trends is
# the heaviest — a full sweep has run for three and a half hours — so it gets
# one. GLOBAL_MAX is the real protection: Railway bills for what is running, and
# a month of overlapping runs took a personal project from 16 EUR to 80.
PIPELINES = {
    "creator_intelligence": {"script": "creator_intelligence.py", "max": 2},
    "reference_profiles":   {"script": "scrape_reference_accounts.py", "max": 3},
    "trends":               {"script": "scrape_weekly_trends.py", "max": 1},
}
GLOBAL_MAX = int(os.environ.get("MAX_CONCURRENT_RUNS", "3"))


def build_command(pipeline: str, p: dict) -> list:
    """Turn a request body into argv for that pipeline.

    A field left empty is omitted rather than passed as "", so the script's own
    default applies — passing a blank would override the default with nothing
    and silently change behaviour.
    """
    def arg(flag, key, required=False):
        v = p.get(key)
        if v is None or (isinstance(v, str) and not v.strip()):
            if required:
                raise HTTPException(400, f"{key} is required for {pipeline}")
            return []
        return [flag, str(v).strip()]

    def flag(name, key):
        return [name] if p.get(key) else []

    if pipeline == "creator_intelligence":
        return (arg("--niche", "niche", required=True)
                + arg("--countries", "countries", required=True)
                + arg("--hashtags", "hashtags") + arg("--platform", "platform")
                + arg("--posts-per-profile", "posts_per_profile")
                + arg("--max-profiles", "max_profiles") + arg("--per-tag", "per_tag")
                + arg("--recency-days", "recency_days") + arg("--min-views", "min_views")
                + arg("--max-followers", "max_followers")
                + arg("--vision-budget-eur", "vision_budget_eur")
                + arg("--max-images-per-creator", "max_images_per_creator")
                + arg("--title", "title")
                + flag("--skip-appearance", "skip_appearance")
                + flag("--discover-only", "discover_only")
                + flag("--no-bio-filter", "no_bio_filter"))

    if pipeline == "reference_profiles":
        # Either add new accounts or refresh existing ones — but one of them,
        # or the script selects every active account and runs a full sweep.
        if not (p.get("accounts") or p.get("handles")):
            raise HTTPException(400, "reference_profiles needs accounts or handles")
        return (arg("--add", "accounts") + arg("--handles", "handles")
                + arg("--region", "region") + arg("--platform", "platform")
                + arg("--posts-per-account", "posts_per_account")
                + arg("--recency-days", "recency_days") + arg("--title", "title"))

    if pipeline == "trends":
        return (arg("--markets", "markets", required=True)
                + arg("--tags", "hashtags") + arg("--posts-per-tag", "posts_per_tag")
                + arg("--tags-per-market", "tags_per_market")
                + arg("--recency-days", "recency_days") + arg("--min-views", "min_views")
                + arg("--board-days", "board_days") + arg("--title", "title")
                + flag("--no-profiles", "no_profiles") + flag("--dry-run", "dry_run"))

    raise HTTPException(400, f"unknown pipeline {pipeline}")


class RunRequest(BaseModel):
    """One body for all three pipelines. Only `pipeline` is always required;
    what else is required depends on it and is enforced in build_command, so the
    error names the missing field instead of failing deep inside a subprocess."""
    pipeline: str = Field("creator_intelligence",
                          pattern="^(creator_intelligence|reference_profiles|trends)$")

    # creator_intelligence. A niche is what the run IS; a hashtag is one of
    # several ways to search for it — separate fields, deliberately.
    niche: Optional[str] = Field(None, description="REQUIRED for creator_intelligence")
    countries: Optional[str] = Field(None, description="ISO codes: GB,US,CA")
    hashtags: Optional[str] = Field(None, description="Comma separated; also --tags for trends")
    posts_per_profile: Optional[int] = Field(None, ge=1, le=50)
    max_profiles: Optional[int] = Field(None, ge=1, le=200)
    per_tag: Optional[int] = Field(None, ge=5, le=100)
    max_followers: Optional[int] = Field(None, ge=1000)
    vision_budget_eur: Optional[float] = Field(None, ge=0, le=500,
        description="Hard cap for the WHOLE run, every country included")
    max_images_per_creator: Optional[int] = Field(None, ge=1, le=10)
    skip_appearance: bool = False
    discover_only: bool = False
    no_bio_filter: bool = False

    # reference_profiles
    accounts: Optional[str] = Field(None, description="account=niche pairs, comma separated")
    handles: Optional[str] = Field(None, description="existing handles to refresh")
    region: Optional[str] = None
    posts_per_account: Optional[int] = Field(None, ge=1, le=50)

    # trends
    markets: Optional[str] = None
    posts_per_tag: Optional[int] = Field(None, ge=1, le=200)
    tags_per_market: Optional[int] = Field(None, ge=1, le=50)
    board_days: Optional[int] = Field(None, ge=1, le=90)
    no_profiles: bool = False
    dry_run: bool = False

    # shared
    platform: Optional[str] = Field(None, pattern="^(tiktok|instagram|both)$")
    recency_days: Optional[int] = Field(None, ge=1, le=365)
    min_views: Optional[int] = Field(None, ge=0)
    title: str = ""
    triggered_by: str = ""


_SLUG = re.compile(r"^[a-z0-9_]+$")


@app.get("/health")
def health():
    running = [r for r in RUNS.values() if _refresh(r)["status"] == "running"]
    by = {k: sum(1 for r in running if r["pipeline"] == k) for k in PIPELINES}
    return {"ok": True, "running": len(running), "global_capacity": GLOBAL_MAX,
            "by_pipeline": {k: {"running": by[k], "max": v["max"]}
                            for k, v in PIPELINES.items()}}


@app.get("/pipelines", dependencies=[Depends(auth)])
def list_pipelines():
    """What can be run, and what each needs — so the console can build its form
    from the server rather than hardcoding fields that later drift."""
    return {"pipelines": {
        "creator_intelligence": {"requires": ["niche", "countries"], "max_concurrent": 2,
                                 "note": "niche is REQUIRED; hashtags are optional and separate"},
        "reference_profiles":   {"requires": ["accounts or handles"], "max_concurrent": 3},
        "trends":               {"requires": ["markets"], "max_concurrent": 1},
    }, "global_max": GLOBAL_MAX}


@app.get("/niches", dependencies=[Depends(auth)])
def list_niches():
    try:
        rows = get_db().table("niches").select("*").eq("active", True)\
                 .order("slug").execute().data or []
        return {"niches": rows}
    except Exception as e:
        raise HTTPException(500, f"niches unavailable: {str(e)[:120]}")


class NicheRequest(BaseModel):
    slug: str
    label: str = ""
    hashtags: list = []
    countries: list = []
    description: str = ""


@app.post("/niches", dependencies=[Depends(auth)])
def create_niche(n: NicheRequest):
    slug = n.slug.strip().lower().replace(" ", "_")
    if not _SLUG.match(slug):
        raise HTTPException(400, "slug must be lowercase letters, numbers and underscores")
    db = get_db()
    existing = db.table("niches").select("*").eq("slug", slug).limit(1).execute().data
    if existing:
        return {"niche": existing[0], "created": False}
    row = db.table("niches").insert({
        "slug": slug,
        "label": n.label or slug.replace("_", " ").title(),
        "hashtags": n.hashtags, "countries": n.countries,
        "description": n.description or None, "created_by": "api",
    }).execute().data
    return {"niche": row[0] if row else None, "created": True}


@app.post("/runs", dependencies=[Depends(auth)])
def start_run(req: RunRequest):
    spec = PIPELINES[req.pipeline]
    running = [r for r in RUNS.values() if _refresh(r)["status"] == "running"]
    same = [r for r in running if r["pipeline"] == req.pipeline]

    # Refused, never queued. A caller told "these are running" can decide what to
    # do; a silent queue hides the wait and keeps billing either way.
    if len(same) >= spec["max"]:
        raise HTTPException(429, {
            "error": f"{spec['max']} {req.pipeline} run(s) already in progress",
            "limit": spec["max"], "pipeline": req.pipeline,
            "running": [{"id": r["id"], "title": r["title"],
                         "started_at": r["started_at"]} for r in same]})
    if len(running) >= GLOBAL_MAX:
        raise HTTPException(429, {
            "error": f"{GLOBAL_MAX} runs already in progress across all pipelines",
            "limit": GLOBAL_MAX,
            "running": [{"id": r["id"], "pipeline": r["pipeline"],
                         "started_at": r["started_at"]} for r in running]})

    body = req.model_dump()
    if req.pipeline == "creator_intelligence":
        slug = (req.niche or "").strip().lower().replace(" ", "_")
        if not _SLUG.match(slug):
            raise HTTPException(400, "niche must be lowercase letters, numbers and underscores")
        body["niche"] = slug

    args = build_command(req.pipeline, body)
    run_id = uuid.uuid4().hex[:12]
    log_path = os.path.join(LOG_DIR, f"{run_id}.log")

    # sys.executable is right on Railway, where the service and the scripts share
    # one interpreter. Locally they can differ (a venv whose python resolves
    # elsewhere) and the failure is a confusing ModuleNotFoundError from inside
    # the subprocess, so allow it to be named explicitly.
    python_bin = os.environ.get("PYTHON_BIN") or sys.executable
    cmd = [python_bin, spec["script"]] + args

    fh = open(log_path, "w")
    proc = subprocess.Popen(cmd, stdout=fh, stderr=subprocess.STDOUT,
                            cwd=os.path.dirname(os.path.abspath(__file__)))
    RUNS[run_id] = {
        "id": run_id, "pipeline": req.pipeline, "pid": proc.pid, "proc": proc,
        "log": log_path, "status": "running",
        "niche": body.get("niche"), "countries": req.countries or req.markets,
        "hashtags": req.hashtags, "platform": req.platform,
        "title": req.title, "triggered_by": req.triggered_by,
        "started_at": datetime.now(timezone.utc).isoformat(),
        "finished_at": None, "exit_code": None,
    }
    log.info(f"[api] {req.pipeline} run {run_id}: {' '.join(args)}")
    return {"run_id": run_id, "pipeline": req.pipeline, "status": "running",
            "poll": f"/runs/{run_id}", "command": " ".join(cmd[1:])}


# ── one endpoint per pipeline ───────────────────────────────────────────────
# The generic POST /runs stays for anything scripted, but the platform gets a
# named endpoint each. A form that posts to /creator-intelligence cannot
# accidentally start a trends sweep because a `pipeline` field was mistyped or
# defaulted, and each endpoint documents its own body in the OpenAPI schema —
# which is what Isaac reads to build the form.


@app.post("/creator-intelligence", dependencies=[Depends(auth)])
def run_creator_intelligence(req: RunRequest):
    """Hashtags + countries -> creators -> their posts -> media -> appearance.

    niche is REQUIRED and is not a hashtag. Both countries and hashtags accept
    a comma-separated list of any length.

    vision_budget_eur caps the WHOLE run — ten countries and a hundred hashtags
    still share one ceiling. The scrape finishes before any analysis starts, so
    the creator count is exact and the per-creator image allowance is divided
    from it rather than guessed.
    """
    req.pipeline = "creator_intelligence"
    return start_run(req)


@app.post("/reference-profiles", dependencies=[Depends(auth)])
def run_reference_profiles(req: RunRequest):
    """Scrape accounts somebody already chose.

    `accounts` adds new ones as account=niche pairs; `handles` refreshes ones
    already stored. Both take comma-separated lists, so several profiles across
    several niches go in one call. One of the two must be present — without
    either, the underlying script selects every active account and runs a full
    sweep, which is never what a form submission means.
    """
    req.pipeline = "reference_profiles"
    return start_run(req)


@app.post("/trends", dependencies=[Depends(auth)])
def run_trends(req: RunRequest):
    """TikTok's own trending board per market, plus any hashtags you name.

    `markets` is comma separated. Leaving `hashtags` blank uses the live
    trending board for each market, which is the point of this pipeline.
    """
    req.pipeline = "trends"
    return start_run(req)


def _refresh(r: dict) -> dict:
    """Reap the subprocess so status reflects reality, not what we last saw."""
    proc = r.get("proc")
    if proc is not None and r["status"] == "running":
        code = proc.poll()
        if code is not None:
            r["status"] = "finished" if code == 0 else "failed"
            r["exit_code"] = code
            r["finished_at"] = datetime.now(timezone.utc).isoformat()
    return r


def _public(r: dict, tail_lines: int = 0) -> dict:
    out = {k: v for k, v in r.items() if k not in ("proc", "log")}
    if tail_lines:
        try:
            with open(r["log"]) as fh:
                lines = [l for l in fh.read().splitlines() if "HTTP Request" not in l]
            out["log_tail"] = lines[-tail_lines:]
        except Exception:
            out["log_tail"] = []
    return out


@app.get("/runs", dependencies=[Depends(auth)])
def list_runs(niche: Optional[str] = None, limit: int = 50):
    rows = [_public(_refresh(r)) for r in RUNS.values()]
    if niche:
        rows = [r for r in rows if r["niche"] == niche]
    rows.sort(key=lambda r: r["started_at"], reverse=True)
    return {"runs": rows[:limit],
            "running": sum(1 for r in rows if r["status"] == "running"),
            "capacity": MAX_CONCURRENT}


@app.get("/runs/{run_id}", dependencies=[Depends(auth)])
def get_run(run_id: str, tail: int = 40):
    r = RUNS.get(run_id)
    if not r:
        raise HTTPException(404, "no such run")
    return _public(_refresh(r), tail_lines=tail)


@app.delete("/runs/{run_id}", dependencies=[Depends(auth)])
def stop_run(run_id: str):
    r = RUNS.get(run_id)
    if not r:
        raise HTTPException(404, "no such run")
    if r["status"] != "running":
        return _public(r)
    r["proc"].terminate()
    r["status"] = "cancelled"
    r["finished_at"] = datetime.now(timezone.utc).isoformat()
    return _public(r)
