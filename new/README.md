# SSE Social Listening — demo and dev

Two builds of the same console. Same design, same step machine, same backend
scripts. They differ in what is offered and what is shown.

|              | Pipelines                                  | Prices shown |
|--------------|--------------------------------------------|--------------|
| **demo/**    | Track accounts you know · Find new creators | no           |
| **dev/**     | all three, including What's trending        | yes          |

Each folder holds the console HTML and the scripts it calls, so either can be
lifted out on its own.

```
demo/
  console.html            the wizard
  scripts/
    scrape_reference_accounts.py    Track accounts you know
    main.py                         Find new creators
    src/  config/  requirements.txt
dev/
  console.html
  scripts/
    scrape_reference_accounts.py    Track accounts you know
    main.py                         Find new creators
    scrape_weekly_trends.py         What's trending
    rebuild_boards.py               rebuild boards, no scraping cost
    load_creators.py                backfill creator profiles from the archive
    src/  config/  requirements.txt
```

---

## Each pipeline asks for different things

A field a pipeline cannot use is not shown at all. An irrelevant field still
reads as a question you are expected to answer.

|                        | Country | Accounts | Hashtags | Niche | Platform |
|------------------------|:-------:|:--------:|:--------:|:-----:|:--------:|
| Track accounts you know |    –    |    ✓     |    –     |   ✓   |    ✓     |
| Find new creators       |    ✓    |    –     |    ✓     |   –   |    ✓     |
| What's trending (dev)   |    ✓    |    –     |    ✓     |   –   |    –     |

---

## What the console runs

**Track accounts you know**

```bash
python scrape_reference_accounts.py \
  --add "https://www.tiktok.com/@shams.hd=gulf_beauty,https://www.instagram.com/layan.noor/=gulf_beauty" \
  --region AE
```

`--add` takes `account=niche` pairs, comma or newline separated. Platform is read
off each URL, so a mixed list of TikTok and Instagram links works in one go — a
bare `@handle` with no URL defaults to TikTok. Accounts that do not exist yet are
created in `reference_accounts` with their niche before scraping starts; without
that step the script silently did nothing for anything newly typed in.

A niche that has never been used before is created on the spot and is selectable
from then on.

**Find new creators**

```bash
python main.py --market AE --platform both --posts 25
```

**What's trending** (dev only)

```bash
python scrape_weekly_trends.py --markets AE --posts-per-tag 15 --recency-days 7
```

---

## Where the data lands

| Pipeline                | Tables |
|-------------------------|--------|
| Track accounts you know | `reference_accounts` → `clips` (tagged with `topic` = the niche) |
| Find new creators       | `markets` → `creators` → `posts` → `media_assets` → `analysis_results` |
| What's trending         | `clips` · `sounds` · `trend_signals` · `trend_creators` |

---

## Cost control on the AI

Only *Find new creators* uses AI. Three rules cap it, in `src/pipeline/stage5_analysis.py`:

```
ABANDON_AFTER      = 3     stop on a creator if the first 3 images show nobody
GOOD_READS_TO_STOP = 2     stop once we can tell what they look like
CAP_PER_CREATOR    = 20    never analyse more than this for one creator
```

What that costs per creator:

```
nobody in the images   3 images    $0.012   abandoned
person found           2-4 images  $0.012   typical
worst case             20 images   $0.083   hard ceiling
```

The abandon rule is the important one. Without it a creator posting products,
scenery or text burned all 20 calls before stopping, and those are common in
hashtag discovery.

---

## No new tables

Everything writes to tables that already exist. Nothing here needs a migration.

---

## Setup

```bash
cd scripts
pip install -r requirements.txt
cp .env.example .env      # fill in Supabase, Apify, OpenAI, MinIO
```

The console is a static page — open `console.html` directly. Every screen shows
the exact command it would run, so wiring it to a backend is a matter of taking
that string and executing it.
