# Community Mapper

Internal intelligence pipeline for mapping creator communities on Instagram and TikTok by market. It scrapes, ranks, expands, and then uses AI vision to understand what types of creators and content perform in each region.

---

## What it does

You give it a country and a platform. It finds the top creators, expands outward through their social graph, downloads their content, and runs every image through GPT-4o Vision to extract appearance and style descriptors. The output lives in Supabase and is visible in the dashboard.

The full run goes through 6 stages:

**Stage 1 — Hashtag Sweep**
Hits Instagram or TikTok with localised hashtags for that market. Pulls posts, scores them by engagement, and extracts creator usernames ranked by relevance.

**Stage 2 — Profile Enrichment**
Scrapes full profile data for every discovered creator. Filters out anyone outside the 10k to 2M follower range, private accounts, and irrelevant languages. Scores each creator on engagement rate, follower weight, and language/hashtag match.

**Stage 3 — Community Expansion (Snowball)**
Takes the top creators and expands outward. On Instagram this uses the `relatedProfiles` field from the profile scraper plus mutual commenters. On TikTok it uses the following list. All new connections are saved as edges in the cluster graph.

**Stage 4 — Content Harvest**
Picks the top N creators by score and scrapes their most recent posts. Downloads the media to MinIO and saves post metadata (captions, hashtags, engagement counts) to the database.

**Stage 5 — AI Vision Analysis**
Downloads each image as bytes and sends it to GPT-4o Vision. Gets back structured JSON with skin tone, body frame, body shape, makeup style, hair color, hair length, hair texture, fashion style, and content style. Skips images where no person is visible. Saves everything to the analysis table.

**Stage 6 — Aggregation**
Rolls up the analysis results for that market and prints a report to the terminal showing distributions across all descriptors plus top creators and hashtags.

---

## Project structure

```
community-mapper/
  main.py                   the CLI you actually run
  dashboard.py              Flask server for the dashboard + image proxy + data API
  test_connections.py       quick check that Apify and MinIO are connected

  config/
    markets.yaml            market configs: hashtags, seed profiles, limits, region codes
    actors.yaml             Apify actor IDs and field mappings

  src/
    apify/
      client.py             Apify wrapper, handles actor runs and result fetching
      instagram.py          Instagram scraper functions (hashtags, profiles, posts, comments)
      tiktok.py             TikTok scraper functions (search, profiles, following lists)

    pipeline/
      runner.py             Stage checkpointing: tracks which stages are done per run
      stage1_discovery.py   Hashtag sweep and creator extraction
      stage2_enrichment.py  Profile scraping, filtering, scoring
      stage3_expand.py      Snowball expansion via related profiles and comments
      stage4_harvest.py     Post scraping and media download to MinIO
      stage5_analysis.py    GPT-4o Vision analysis of downloaded images
      stage6_aggregate.py   Result rollup and terminal report

    ai/
      vision.py             OpenAI Vision API calls (URL and base64 modes)
      frames.py             Video frame extraction using ffmpeg
      prompts.py            The system and user prompts sent to GPT-4o

    db/
      client.py             Supabase query helpers
      migrations.sql        Full schema (run this in Supabase SQL editor once)

    storage/
      minio.py              MinIO client, upload from URL, presigned URLs, path builder

    utils/
      scoring.py            Post score and creator score formulas
      dedupe.py             Deduplication helpers
      logger.py             Rich logging setup

  static/
    index.html              The React dashboard (single file, no build needed)
```

---

## Setup

Copy `.env.example` to `.env` and fill in your keys:

```
APIFY_TOKEN=
SUPABASE_URL=
SUPABASE_PUBLIC_KEY=
SUPABASE_SECRET_KEY=
OPENAI_API_KEY=
MINIO_ENDPOINT=
MINIO_ACCESS_KEY=
MINIO_SECRET_KEY=
MINIO_BUCKET=social-intel
```

Install dependencies (requires uv):

```
uv venv .venv
uv pip install -r requirements.txt
```

Run the Supabase migrations once. Go to your Supabase project, open the SQL Editor, paste the contents of `src/db/migrations.sql` and run it. This creates all 7 tables.

---

## Running the pipeline

Basic run for UAE on Instagram with default settings (20 creators, 5 posts each for testing):

```
uv run python main.py --market UAE --platform instagram --limit 20 --posts 5
```

Full production run with no creator cap:

```
uv run python main.py --market UAE --platform both
```

Resume a run that crashed partway through:

```
uv run python main.py --market UAE --platform instagram --resume
```

Start from a specific stage (useful when debugging or re-running just analysis):

```
uv run python main.py --market UAE --platform instagram --from-stage 5
```

Skip AI analysis and just scrape:

```
uv run python main.py --market UAE --platform instagram --skip-analysis
```

Available markets: `UAE`, `SA`, `TR`, `BR`, `ID`. Add more in `config/markets.yaml`.

---

## Dashboard

Start the dashboard server:

```
uv run python dashboard.py
```

Open `http://localhost:8050` in your browser.

Select a market and platform from the dropdowns. The dashboard has four tabs:

**Overview** shows the pipeline stage status (which stages completed, running, or failed), a ranked list of the top creators by score, and a feed of recent posts with thumbnails and engagement numbers.

**Creators and Images** shows a grid of creator cards. Each card has the creator's username, follower count, score, source type, engagement rate, and a grid of their scraped images. Where AI analysis has run, each image shows the extracted descriptors as coloured tags below it (skin tone, body frame, makeup style, hair color, fashion style, etc.).

**AI Analysis** shows distribution charts for every appearance descriptor across all analyzed images for that market. Skin tone, body frame, makeup style, hair color, hair length, hair texture, fashion style, and content style are all broken out. Below the charts is an image gallery of all analyzed photos with their descriptors and confidence score.

**Pipeline** shows the full run history. Each run card shows the overall status, start and end time, and the status of each of the 6 stages. If a run failed you can see the error message.

The dashboard auto-refreshes every 90 seconds. Images are proxied through the Flask server to avoid Instagram CDN authentication and CORS issues.

---

## How the scoring works

Post score is calculated as:
```
likes + (comments * 5) + (views * 0.01)
```

Creator score uses four components weighted as:
```
follower score    30%
engagement rate   35%
community connections (links to known cluster accounts)   25%
local relevance (language + hashtag match)   10%
```

Follower score is log-normalised between 10k and 2M so creators in the middle of the range score better than massive accounts with low engagement.

---

## Adding a new market

Open `config/markets.yaml` and add a block following the same pattern as UAE. You need a country code, region bucket (META, LATAM, or INDOPAC), TikTok region code, seed hashtags for Instagram and TikTok, seed profiles, and the languages to use for language detection filtering.

Then run:

```
uv run python main.py --market XX --platform instagram
```

where `XX` is your new country code.

---

## Apify actors used

Instagram: `apify/instagram-hashtag-scraper`, `apify/instagram-profile-scraper`, `apify/instagram-scraper`, `apify/instagram-comment-scraper`

TikTok: `clockworks/tiktok-scraper`, `clockworks/tiktok-profile-scraper`, `clockworks/tiktok-followers-scraper`
