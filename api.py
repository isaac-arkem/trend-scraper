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


class RunRequest(BaseModel):
    # Required. A niche is what the run IS. A hashtag is one of several ways to
    # search for it — they are separate fields on purpose, because conflating
    # them is what made the earlier runs hard to interpret.
    niche: str = Field(..., min_length=1,
                       description="REQUIRED. Niche slug, e.g. chinese_student. Created if new.")
    countries: str = Field(..., min_length=2, description="ISO codes, comma separated: GB,US,CA")
    hashtags: str = Field("", description="Comma separated. Blank uses the niche's saved tags.")
    platform: str = Field("both", pattern="^(tiktok|instagram|both)$")
    posts_per_profile: int = Field(20, ge=1, le=50)
    max_profiles: int = Field(25, ge=1, le=200)
    per_tag: int = Field(30, ge=5, le=100)
    recency_days: int = Field(90, ge=1, le=365)
    min_views: int = Field(300, ge=0)
    max_followers: int = Field(300_000, ge=1000)
    vision_budget_eur: float = Field(44.0, ge=0, le=500,
                                     description="Hard cap for the WHOLE run, all countries")
    max_images_per_creator: int = Field(3, ge=1, le=10)
    skip_appearance: bool = False
    discover_only: bool = False
    title: str = ""
    triggered_by: str = ""


_SLUG = re.compile(r"^[a-z0-9_]+$")


@app.get("/health")
def health():
    running = [r for r in RUNS.values() if r["status"] == "running"]
    return {"ok": True, "running": len(running), "capacity": MAX_CONCURRENT}


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
    running = [r for r in RUNS.values() if r["status"] == "running"]
    if len(running) >= MAX_CONCURRENT:
        # Refused, not queued. A caller that is told "no, these two are running"
        # can decide what to do; a silent queue just hides the wait and keeps
        # billing.
        raise HTTPException(429, {
            "error": f"{MAX_CONCURRENT} runs already in progress",
            "running": [{"id": r["id"], "niche": r["niche"],
                         "started_at": r["started_at"]} for r in running],
        })

    slug = req.niche.strip().lower().replace(" ", "_")
    if not _SLUG.match(slug):
        raise HTTPException(400, "niche must be lowercase letters, numbers and underscores")

    run_id = uuid.uuid4().hex[:12]
    log_path = os.path.join(LOG_DIR, f"{run_id}.log")

    # sys.executable is right on Railway, where the service and the script share
    # one interpreter. Locally they can differ (a venv whose python resolves
    # elsewhere), and the failure is a confusing ModuleNotFoundError from inside
    # the subprocess — so allow it to be named explicitly.
    python_bin = os.environ.get("PYTHON_BIN") or sys.executable
    cmd = [python_bin, "creator_intelligence.py",
           "--niche", slug,
           "--countries", req.countries,
           "--platform", req.platform,
           "--posts-per-profile", str(req.posts_per_profile),
           "--max-profiles", str(req.max_profiles),
           "--per-tag", str(req.per_tag),
           "--recency-days", str(req.recency_days),
           "--min-views", str(req.min_views),
           "--max-followers", str(req.max_followers),
           "--vision-budget-eur", str(req.vision_budget_eur),
           "--max-images-per-creator", str(req.max_images_per_creator)]
    if req.hashtags.strip():
        cmd += ["--hashtags", req.hashtags.strip()]
    if req.title.strip():
        cmd += ["--title", req.title.strip()]
    if req.skip_appearance:
        cmd += ["--skip-appearance"]
    if req.discover_only:
        cmd += ["--discover-only"]

    fh = open(log_path, "w")
    proc = subprocess.Popen(cmd, stdout=fh, stderr=subprocess.STDOUT,
                            cwd=os.path.dirname(os.path.abspath(__file__)))
    RUNS[run_id] = {
        "id": run_id, "pid": proc.pid, "proc": proc, "log": log_path,
        "status": "running", "niche": slug, "countries": req.countries,
        "hashtags": req.hashtags, "platform": req.platform,
        "title": req.title, "triggered_by": req.triggered_by,
        "started_at": datetime.now(timezone.utc).isoformat(),
        "finished_at": None, "exit_code": None,
    }
    log.info(f"[api] run {run_id} started: {slug} / {req.countries}")
    return {"run_id": run_id, "status": "running",
            "poll": f"/runs/{run_id}", "command": " ".join(cmd[1:])}


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
