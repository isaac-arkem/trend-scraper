-- ============================================================================
-- Regional Overview — trend_signals
-- Apply in the Supabase Studio SQL editor. Safe to re-run.
--
-- One row = one thing on one country's board at one point in time.
--   "on 2026-08-09, in AE, #gymtok was rank 3 on the hashtag board"
--
-- Why a table and not a live query over clips:
--   1. The board must load instantly. Ranking 9k+ clips per request does not.
--   2. Week-over-week needs a frozen "last week". Recomputing from clips gives
--      you today's answer about last week, which is not the same thing — clips
--      keep arriving for dates already past.
--   3. rank_delta / is_new only mean something against a stored previous run.
--
-- Videos are NOT duplicated here. kind='video' rows point at clips.id via
-- sample_clip_id. clips stays the one home for content.
-- ============================================================================

CREATE TABLE IF NOT EXISTS trend_signals (
    id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),

    -- when + which run produced this row
    captured_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    run_key       TEXT NOT NULL,           -- 'YYYY-WW' or a request id; one board per run_key

    -- where
    country_code  TEXT,                    -- ISO-2, normalised ('AE' never 'UAE')
    region        TEXT,                    -- MENA / LATAM / INDOPAC / CIS

    -- what
    kind          TEXT NOT NULL CHECK (kind IN ('hashtag','sound','creator','video')),
    key           TEXT NOT NULL,           -- '#gymtok' | sound id | '@handle' | clip id
    label         TEXT,                    -- display name

    -- where it placed
    rank          INTEGER,
    rank_delta    INTEGER,                 -- vs the previous run_key; NULL when new
    is_new        BOOLEAN DEFAULT FALSE,

    -- how it did
    posts         INTEGER DEFAULT 0,
    views         BIGINT  DEFAULT 0,
    likes         BIGINT  DEFAULT 0,
    lift          NUMERIC,                 -- median (clip views / that account's median)
    best_lift     NUMERIC,
    breakout      NUMERIC,                 -- share of clips at >= 2x their own normal
    scored        INTEGER DEFAULT 0,       -- clips that had a usable baseline

    -- what it is about
    niche         TEXT,
    niche_source  TEXT CHECK (niche_source IN ('creator','hashtag','model','operator')),

    -- a clip to show
    sample_clip_id UUID REFERENCES clips(id) ON DELETE SET NULL,

    metrics       JSONB DEFAULT '{}',      -- raw numbers, room to grow

    UNIQUE (run_key, country_code, kind, key)
);

CREATE INDEX IF NOT EXISTS idx_ts_board   ON trend_signals(country_code, kind, run_key, rank);
CREATE INDEX IF NOT EXISTS idx_ts_recent  ON trend_signals(captured_at DESC);
CREATE INDEX IF NOT EXISTS idx_ts_niche   ON trend_signals(niche);

COMMENT ON TABLE trend_signals IS
    'Frozen per-country board per run. Rank/delta/is_new are only meaningful against a stored previous run_key.';


-- ── clips: two changes the harvest needs ────────────────────────────────────

-- 1. REQUIRED. feed_source is limited to ('dance','hook','watchlist'), so ANY
--    new feed inserts zero rows. This has already silently killed three feeds:
--    buchonalifestyle, buchonabeauty and narcoglammusic all have 0 clips.
ALTER TABLE clips DROP CONSTRAINT IF EXISTS clips_feed_source_check;

-- 2. Niche kept SEPARATE from topic. topic currently means "came from a curated
--    reference account" and is what Reference Profiles filters on — writing
--    guesses into it would fill that tab with trend noise, permanently, with no
--    way to tell curated from guessed afterwards.
ALTER TABLE clips
    ADD COLUMN IF NOT EXISTS niche        TEXT,
    ADD COLUMN IF NOT EXISTS niche_source TEXT;

CREATE INDEX IF NOT EXISTS idx_clips_niche  ON clips(niche);
CREATE INDEX IF NOT EXISTS idx_clips_posted ON clips(posted_at DESC);

COMMENT ON COLUMN clips.niche IS
    'Derived niche. NULL is allowed and expected — a forced guess pollutes every niche statistic downstream.';
COMMENT ON COLUMN clips.niche_source IS
    'How the niche was decided: creator (inherited from a curated account) | hashtag (from the seed feed) | model (LLM) | operator.';
