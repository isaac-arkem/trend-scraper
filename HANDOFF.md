# Community Mapper — Session Handoff (as of 2026-07-21)

## What this project does

Two related pipelines, both feeding the same dashboard (`dashboard.py`):

1. **Trend sweep** (`scrape_trends.py`) — scrapes TikTok/IG dance + hook hashtag
   feeds across 19 markets (MENA: UAE, SA, KW, EG, TR, NG, ZA, MA / LATAM: BR,
   MX, CO, AR / INDOPAC: IN, ID, PH, TH, MY, JP, KR), filters to women-only via
   gpt-4o vision, clusters by sound, ranks by velocity. Saves each clip with
   `market_code` (country) + `region` (MENA/LATAM/INDOPAC) — this is "where the
   scrape found it trending," not necessarily the creator's home country.

2. **Creator pipeline** (`main.py`, `src/pipeline/stage1..6`) — discovers
   creators per market via hashtags + seed profiles, enriches, expands,
   harvests ~20 posts/creator, runs gpt-4o vision appearance analysis (capped:
   max 20 images/creator, stops early after 2 "good reads" — see
   `src/ai/vision.py` and `src/pipeline/stage5_analysis.py` for the cost
   controls), aggregates into the `creators` table.

Current volumes (as of this session): 2,650 creators, 51,363 posts, 138,787
media assets, 51,196 images actually sent to gpt-4o (rest skipped by cost
caps). Trend sweeps run ~3.5–4.5h and land 3,400–3,800 clips per run.

## Running the trend sweep manually (local)

```bash
caffeinate -ims .venv/bin/python -u scrape_trends.py >> logs/trends_cron.log 2>&1 &
```

`caffeinate` is required — a laptop sleep mid-run previously killed a sweep
(that's the July 2 gap in `logs/trends_cron.log`). Watch progress with
`tail -f logs/trends_cron.log`; look for `✓ <region>/<market> <feed>: +N new`
lines and a final `━━ ALL DONE — N clips saved in Xm ━━`.

## Jenkins setup (now working, mostly)

Two separate Jenkins jobs, on two different nodes, at `streetsmashburgers.com`:

### 1. `community-mapper-pipeline` (node: raspberry-pi) — ✅ working
- Source: `Arkem-LLC/community-mapper-pipeline` (GitHub)
- Parameterized: `SCRAPER_TYPE` (`dance_trends` | `reference_profiles`),
  `REQUEST_ID`, `MARKETS`, `FEEDS`, `TAGS`, `MIN_VIEWS`, `RECENCY_DAYS`,
  `HANDLES` — leave all blank except SCRAPER_TYPE for a full local-equivalent
  19-market run.
- Needs these Jenkins credentials, **Kind: Secret text, Scope: Global**, under
  **Manage Jenkins → Credentials → System → Global** (not a personal user
  store — see gotcha below): `apify-token`, `supabase-url`,
  `supabase-secret-key`, `openai-api-key`, `minio-endpoint`,
  `minio-access-key`, `minio-secret-key`. Values live in the project's
  `.env` file (not repeated here — don't paste secrets into shared docs).

### 2. `trend-scheduler` (node: Super-pc-ubuntu) — ⚠️ partially working
- Source: `Arkem-LLC/trend-scheduler` (GitHub); runs on a 5-min cron.
- Polls Supabase `dance_scrape_schedules` / `reference_scrape_schedules` for
  due rows, then calls the Jenkins REST API
  (`{JENKINS_URL}/job/{JENKINS_JOB_NAME}/buildWithParameters`) to trigger
  `community-mapper-pipeline` with the right params. This is what makes
  dashboard-created schedules (e.g. "scrape @handle weekly") actually fire —
  `community-mapper-pipeline` itself has no cron trigger of its own.
- Needs credentials: `supabase-url`, `supabase-secret-key`, `jenkins_url`,
  `jenkins_user`, `jenkins_api_token` — **must be in System → Global**, not a
  personal user's credential store (jobs can't see personal-store
  credentials even though they're visible in some list views — this was the
  first bug found).
- `JENKINS_JOB_NAME` is a plain (non-secret) env var baked into the
  Jenkinsfile itself — was wrongly `scraper/job/trend-scraper` (stale),
  fixed to `community-mapper-pipeline`. ✅ done.
- Fixed a second bug: `scheduler.py`'s `trigger_jenkins()` sent only Basic
  auth on the build POST with no CSRF crumb — Jenkins requires a crumb on
  POSTs by default, causing 403 even with correct job name. Added
  `_fetch_crumb()` (GETs `/crumbIssuer/api/json`, attaches the crumb header).
  Pushed to `Arkem-LLC/trend-scheduler` main. ✅ done.

### 🔴 Still broken / open issue
Even after both fixes, dashboard shows: schedules **"Triggered by Isaac
Kusi"** (his personal login) → success, but **"Triggered by Scheduler"**
(the `jenkins_user`/`jenkins_api_token` service account) → still **403
Forbidden**. This points to a **permissions** problem, not crumb/job-name:
- Check **Manage Jenkins → Security → Authorization** (Matrix or Role-based
  strategy) — confirm the account behind `jenkins_user`/`jenkins_api_token`
  has **Job → Build** (+ Job → Read) on `community-mapper-pipeline`.
- Check the API token itself hasn't been revoked/regenerated since it was
  saved as a credential (a dead token can silently fall back to anonymous,
  which then also 403s if anonymous lacks build rights).
- Was in the middle of requesting the **full Jenkins console log** of a
  failed scheduler-triggered run (not just the dashboard summary card) to
  confirm whether the crumb fetch itself succeeded before the 403 on the
  actual build call — that would definitively separate "crumb still broken"
  from "permissions problem." **Do this next.**

## Credential-setup gotchas (bit us twice)

1. **Leading/trailing space in the credential ID** — pasted IDs can carry an
   invisible space (e.g. `" apify-token"`), which shows fine in the
   credentials list but fails lookup from a job with "Could not find
   credentials entry with ID 'apify-token'". Symptom check: open the
   credential's Update page and look at the URL — a `%20` in it means there's
   a hidden space. Fix: delete and recreate, **typing** the ID by hand
   instead of pasting.
2. **Personal user credential store vs System → Global store** — credentials
   created under "User: <name> — Global" are invisible to pipeline jobs
   entirely, regardless of the Scope field. They must live under
   **Manage Jenkins → Credentials → System → Global credentials**.
3. **Wrong Supabase key** — pasting `sb_publishable_...` instead of
   `sb_secret_...` into `supabase-secret-key` gives "Invalid API key... might
   be owned by another Supabase project" at runtime, not at save time.

## Cost notes (from a prior session's estimate)

- One full 19-market trend sweep: ~$23–35 in Apify (scrape + video download)
  + ~$15–20 in gpt-4o vision filtering ≈ **~$40–55/run**.
- Creator pipeline to date (2,650 creators, 51k images analyzed): **~$550–680**
  total, ~$0.20–0.25/creator, thanks to the per-creator analysis caps.
- The Apify account is **shared** with unrelated heavy workloads (a
  search-scraper + mass video-downloader project pushing $1,000–4,000+/month
  on its own) — don't mistake the full Apify invoice for community-mapper's
  cost; use the per-run figures above instead.
