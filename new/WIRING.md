# Wiring the console to the scripts

For whoever connects the UI to the backend. Each pipeline is one command plus one
table to poll. Nothing else.

---

## The contract

Every screen in `console.html` shows the exact command it would run. Take that
string, execute it with `REQUEST_ID` set, then poll the matching table for status.

```
console  ──emits──▶  command + REQUEST_ID  ──▶  script  ──writes──▶  status table
                                                             │
                                                             └──▶  data tables
```

---

## 1 · Track accounts you know

```bash
REQUEST_ID=<uuid> python scrape_reference_accounts.py \
  --title "Gulf beauty — August" \
  --add "https://www.tiktok.com/@shams.hd=gulf_beauty,https://www.instagram.com/layan.noor/=gulf_beauty" \
  --region AE \
  --platform tiktok \
  --posts-per-account 25 \
  --recency-days 14
```

| flag | from | notes |
|---|---|---|
| `--title` | Run title | appears in the log |
| `--add` | Accounts + niche | `account=niche` pairs. Platform is read off each URL, so mixed TikTok/Instagram works. A bare `@handle` defaults to TikTok. |
| `--region` | first country | optional |
| `--platform` | Platform toggle | only when one is switched off |
| `--posts-per-account` | Depth | 10 / 25 / 50 / 100 |
| `--recency-days` | Recency | omit for no limit |

**Status:** create a `reference_scrape_requests` row first, pass its id as
`REQUEST_ID`. The script sets `status` to `running`, then `success` or `failed`,
and fills `completed_at` and `error_message`.

**Existing accounts are never modified.** If a pasted handle is already tracked,
its niche and region are left alone and it is simply refreshed. The log says so.

**Writes to:** `reference_accounts` (new accounts only) → `clips`
(`topic` = niche, `feed_source` = NULL, `subject_type` = `ref`).

---

## 2 · Find new creators

```bash
python main.py \
  --title "Dubai discovery" \
  --market AE \
  --platform both \
  --limit 50 \
  --posts 25 \
  --hashtags "catgirls,dubaibeauty"
```

| flag | from | notes |
|---|---|---|
| `--market` | Country | **must already exist in `markets`** — see limitations |
| `--platform` | Platform toggle | `tiktok` · `instagram` · `both` |
| `--limit` | Max creators | caps discovery, profile enrichment, expansion, and harvest |
| `--posts` | Depth | posts per creator at harvest |
| `--hashtags` | Hashtags | optional; overrides the market's stored list |

**Status:** poll the `runs` table. `current_stage` (1–6), `stage_statuses` (JSON
per stage), `status` = `running` / `done` / `failed`, plus `total_creators`,
`total_posts`, `total_media`. This is the only pipeline with per-stage progress,
which maps directly onto the console's stage list.

**Writes to:** `markets` → `creators` → `posts` → `media_assets` →
`analysis_results`.

**This is the only pipeline that uses AI**, and it needs a valid
`OPENAI_API_KEY`. Without one, stage 5 fails.

---

## 2b · Find new creators — hashtag discovery (AI off)

When the console **AI analysis** switch is off under *Find new creators*, Jenkins
runs `scrape_hashtag_profiles.py` instead of `main.py`. Same UI entry point;
different script and request table.

```bash
REQUEST_ID=<uuid> python scrape_hashtag_profiles.py \
  --title "Gulf beauty — western sweep" \
  --countries "GB,US,AE" \
  --niche gulf_beauty \
  --hashtags "grwm,softglam,dubaibeauty" \
  --posts-per-profile 10 \
  --max-profiles 25 \
  --platform both \
  --skip-appearance
```

| flag | from | notes |
|---|---|---|
| `--countries` | Countries | **required** — one or many ISO codes |
| `--niche` | Niche | **required** — one niche applied to every profile found |
| `--hashtags` | Hashtags | **required** — no built-in defaults |
| `--posts-per-profile` | Depth (`POSTS_PER_CREATOR`) | posts pulled per discovered profile |
| `--max-profiles` | Max creators (`MAX_CREATORS`) | cap per country |
| `--platform` | Platform toggle | |
| `--skip-appearance` | always on from Jenkins for this path | no OpenAI |

A light bio filter still drops private accounts, accounts over `--max-followers`,
and obvious brand/agency bios. It does **not** require Chinese-student wording.
Pass `--no-bio-filter` to keep everyone found under the hashtags.

**Status:** create a `reference_scrape_requests` row, pass its id as `REQUEST_ID`
(same table as *Track accounts*). The script sets `running`, then `success` /
`failed`.

**Writes to:** `reference_accounts` → `clips` (`subject_type` = `ref`,
`topic` = niche).

---

## 3 · What's trending (dev only)

```bash
REQUEST_ID=<uuid> python scrape_weekly_trends.py \
  --title "AE trends — W34" \
  --markets AE \
  --tags "catgirls,dubaibeauty" \
  --posts-per-tag 15 \
  --recency-days 7
```

| flag | from | notes |
|---|---|---|
| `--markets` | Countries | comma list |
| `--tags` | Hashtags | optional; **omit and it uses TikTok's live trending board** |
| `--posts-per-tag` | Depth | |
| `--recency-days` | Recency | |
| `--board-days` | — | 7 weekly, 30 for a month-end board |

**Status:** create a `dance_scrape_requests` row, pass its id as `REQUEST_ID`.
The script sets `running`, then `success` / `failed`, and writes a
`final_progress` JSON with `fetched`, `saved`, `profile_pics`, `board_rows`,
`run_key` and `minutes`.

**Writes to:** `clips` · `sounds` · `trend_signals` · `trend_creators`.

Boards are written **per country as it goes**, not at the end, so a run that dies
half way still leaves everything it completed.

---

## Status tables at a glance

| Pipeline | Table | Key fields |
|---|---|---|
| Track accounts | `reference_scrape_requests` | `status` · `error_message` · `completed_at` · `final_progress` |
| Find creators (AI on) | `runs` | `current_stage` · `stage_statuses` · `status` · `total_*` |
| Find creators (AI off / hashtag) | `reference_scrape_requests` | `status` · `error_message` · `completed_at` |
| What's trending | `dance_scrape_requests` | `status` · `final_progress` · `completed_at` |

---

## Cost ceiling

Only *Find new creators* spends on AI. Three rules in
`src/pipeline/stage5_analysis.py` cap it:

```
ABANDON_AFTER      = 3    give up on a creator if the first 3 images show nobody
GOOD_READS_TO_STOP = 2    stop once we can tell what they look like
CAP_PER_CREATOR    = 20   hard ceiling per creator
```

Worst case per creator is **$0.083**; typical is **$0.012**. It cannot run away.

---

## Known limitations

- **`--market` must match a row in `markets`.** The console's country list is
  broader than the configured markets, so picking a country we have never set up
  fails with "Market not found". Either restrict that dropdown to configured
  markets, or create the market row on demand.
- **No runs quota.** The mockup shows "1 of your 5 left"; nothing enforces it.
  Needs a policy before it means anything.
- **Trends has no per-stage progress**, only start and finish. Per-country
  progress is inferable from `trend_signals` rows appearing as it goes.

---

## Hosting

The scripts are plain Python with no server. To host:

1. `pip install -r requirements.txt`
2. Provide `.env` — see `.env.example` for the variable names
3. Run the command as a subprocess with `REQUEST_ID` in the environment
4. Poll the status table

Runs are long — 2 minutes to several hours — so they must be launched detached
and polled, never awaited inside a request.
