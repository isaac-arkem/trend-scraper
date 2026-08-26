# Weekly trend harvest — plan

**Date:** 2026-08-07
**Goal:** a living, weekly-refreshed picture of what is working per country, so aliases can copy it.

---

## 1. Verified this morning, against the live Apify account

Account `cinnamon_appendix`, plan SCALE.

| Actor | State |
|---|---|
| `clockworks/tiktok-scraper` | **WORKS.** Returns posts by hashtag + region, with handle, views, posted_at, hashtags, sound, subtitles. |
| `clockworks/tiktok-trends-scraper` | **BROKEN.** Runs succeed and return **0 items** — US and AE, hashtags and sounds. AE logged `No data received` and failed after 5 retries against `ads.tiktok.com/business/creativecenter`. |

**This is the blocker Isaac kept hitting.** It is not an actor-choice problem and not his fault — the Creative Center route is currently dead. Swapping actors will not fix it.

The trends actor also could not have covered us anyway. Its own country lists, read from the live input schema against our 19 markets:

| Feed | Covered | Missing |
|---|---|---|
| Hashtags | 18/19 | IN |
| Sounds | 18/19 | IN |
| Creators | 11/19 | AR, CO, IN, KW, MA, MX, NG, ZA |
| Videos | 12/19 | AR, CO, IN, KW, MA, NG, ZA |

**India is unsupported on every feed** (TikTok is banned there). **Armenia is unsupported too** — and Armenia is our single biggest country (1,285 clips, 79 curated accounts) and is not in `markets.yaml` at all. Armenia can only be served by hashtag search, never by presets.

**Conclusion:** the plan below uses only the actor that works.

---

## 2. The loop

```
1. SEED      30–50 hashtags per country
2. HARVEST   top 20 posts per hashtag, geo-targeted   → clockworks/tiktok-scraper
3. STORE     posts land in clips
4. RESOLVE   assign a niche, honestly labelled
5. RANK      compute the board from clips by date window
```

Weekly for priority markets, monthly for the rest.

### Where seed hashtags come from, now that Creative Center is dead

Three sources, unioned:

1. **Operator list** — what the team knows matters per niche. Already supported: `dance_feeds` holds `name`, `slug`, `tags[]` and has a UI to add feeds.
2. **Co-occurrence expansion** — every harvested post carries its other hashtags. We already observe **6,292 distinct hashtags** across our clips. Rank them by frequency × recency × lift, and the top ones become next week's seeds. This compounds each run.
3. **Manual injection** — anything Dave wants tracked.

Honest limit: this only finds hashtags adjacent to what we already scrape. It cannot discover a trend outside our bubble the way Creative Center could. If that route comes back, it plugs into the same seed slot — so keep the seed source swappable.

---

## 3. Tables — no new ones needed

Everything already exists.

| Need | Table | Change |
|---|---|---|
| Seed hashtag lists per feed | `dance_feeds` | none — `tags[]` already |
| Harvested posts | `clips` | 2 columns + 1 constraint |
| Audio | `sounds` | none |
| The 19 countries | `markets` | none |
| Run log | `dance_scrape_requests` | none |

**Why not a new `tiktok_trends` table.** The posts are the same shape as clips — same fields, same downstream code. The lift scoring, sound clustering, label normalisation, and the What's Working board all read `clips`. A second table means writing all of that twice and keeping two truths in sync. That is exactly the problem the three account tables already caused.

**Why we do not need a rank-snapshot table either.** The reason to store TikTok's own weekly rank was to compute rank-delta. Creative Center is dead, so there is no authoritative rank to store. We compute rank ourselves from `clips` — filter by `posted_at` for any window, group by hashtag. Week-over-week comes free because the history is in the clips. Nothing to backfill, nothing to schedule.

**Volume is self-limiting.** 19 countries × 50 hashtags × 20 posts = 19,000 rows/week on paper, but `clips` has `UNIQUE(platform, platform_post_id)` and the same video appears under many hashtags, so duplicates collapse on insert. Do **not** download video/audio for everything — that is what put 44 GB in MinIO. Download only for clips that clear the lift bar.

### Schema changes — the whole list

```sql
-- 1. REQUIRED. Any feed not named dance/hook/watchlist inserts ZERO rows today.
--    This has already silently killed the buchonalifestyle, buchonabeauty and
--    narcoglammusic feeds — all three have produced 0 clips.
ALTER TABLE clips DROP CONSTRAINT IF EXISTS clips_feed_source_check;

-- 2. Niche, kept separate from topic. See §4.
ALTER TABLE clips
  ADD COLUMN IF NOT EXISTS niche        TEXT,
  ADD COLUMN IF NOT EXISTS niche_source TEXT;   -- creator | hashtag | model | operator

CREATE INDEX IF NOT EXISTS idx_clips_niche       ON clips(niche);
CREATE INDEX IF NOT EXISTS idx_clips_posted_tags ON clips(posted_at DESC);
```

---

## 4. The niche question

This is the part that decides whether the data is usable or junk.

**Do not write guesses into `clips.topic`.** Today `topic` means *"this clip came from a curated reference account"* — it is what the Reference Profiles tab filters on. Writing guessed niches into the same column silently fills that tab with trend noise, and nobody can tell curated from guessed afterwards. That is unrecoverable.

Use a separate `niche` column with a `niche_source` beside it. Resolution order, most trustworthy first:

| Order | Rule | `niche_source` | Confidence |
|---|---|---|---|
| 1 | `creator_handle` matches a `reference_accounts.handle` → inherit its `topic` | `creator` | high — a human curated that account |
| 2 | The seed hashtag belongs to a feed with a known niche (`dance_feeds.slug`) | `hashtag` | good — we chose that tag deliberately |
| 3 | LLM over caption + transcript + hashtags, constrained to the existing niche list | `model` | medium |
| 4 | Nothing matches | `NULL` | leave it empty |

**Rule 4 matters most. Allow NULL.** A trend clip with no niche is still useful — it still ranks on the hashtag board. Forcing a guess pollutes every niche statistic downstream, and once polluted you cannot separate them again.

Run rule 3 **only on clips that clear the lift bar.** Classifying 19,000 clips a week with an LLM is the expensive mistake; classifying the few hundred that actually broke out is cheap and is the only set anyone will look at.

Because `niche_source` is stored, the dashboard can offer "confirmed niches only" (sources 1–2) versus "including guesses". Dave can decide what he trusts without anyone re-running anything.

---

## 5. Known risk: recency

Hashtag search returns *popular* posts, not *recent* ones. A live probe on `#dubailife` returned a top result from **February 2024**.

Existing code already filters on `createTimeISO` (`DEFAULT_RECENCY_DAYS = 14`), and that filter is what produced our 8,726 clips — so it works. But the tighter the window, the more you must over-fetch to fill it. For a 7-day board, pull well beyond 20 per tag and expect most to be discarded.

Worth measuring on the first run: of 20 posts per hashtag, how many land inside 7 days? That number sets the real per-tag fetch size and therefore the real cost.

---

## 6. Cost

The trends actor is `PAY_PER_EVENT`; `tiktok-scraper` bills per result. The per-unit rate is on the actor page, not exposed through the API — get it before switching on 19 countries.

The controllable knobs: number of countries, hashtags per country, posts per hashtag, and cadence. Recommend running **one country end-to-end first**, measuring actual spend and the recency hit rate, then extrapolating. Do not switch on 19 markets before that number exists.

---

## 7. Order of work

1. Drop the `feed_source` constraint. One line. Nothing works until this lands.
2. Add `niche` + `niche_source`.
3. Seed picker: union of `dance_feeds.tags` and co-occurrence ranking over `clips`.
4. Harvest job: per country, per hashtag, 20 posts, geo-targeted, recency-filtered — reuses `scrape_tiktok_feed()`.
5. Niche resolver, rules 1–2 only at first. Ship without the LLM.
6. Point the What's Working board at the fresh data.
7. Measure one country. Get the cost number. Then scale.

Steps 1–2 unblock everything and are migrations, not code.

---

## 8. What to tell Dave

- The Creative Center actor is dead. Verified today — zero items for US and AE. Not a choice-of-actor problem.
- We can still deliver the weekly refresh, from hashtag search, with the seed list built from our own data plus an operator list.
- We cannot deliver a true TikTok-wide trending board while that route is down. What we can deliver is *what is working in the hashtags and niches we track* — which is the thing aliases actually copy.
- India is impossible. Armenia needs its own approach and is not in the market config.
- Give us one country's worth of spend before 19.
