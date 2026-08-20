-- ============================================================================
-- Trend Intelligence schema  (TikTok + Instagram Reels)
-- Run this in the Supabase SQL editor. Safe to re-run (IF NOT EXISTS guards).
-- Powers: trending dances, hooks & audio, reference-account watchlist,
--         sound clustering, velocity ranking, and the dashboard Trends lanes.
-- ============================================================================

-- ── Sounds / audio (one row per platform sound id) ──────────────────────────
CREATE TABLE IF NOT EXISTS sounds (
    id                 UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    platform           TEXT NOT NULL CHECK (platform IN ('tiktok','instagram')),
    platform_sound_id  TEXT NOT NULL,              -- TikTok music id / IG audio id
    name               TEXT,                       -- sound / track name
    author             TEXT,                       -- original creator / artist
    sound_page_url     TEXT,                       -- TikTok sound page / IG audio page
    audio_minio_path   TEXT,                       -- downloaded audio file in MinIO
    duration_sec       NUMERIC,
    clip_count         INTEGER DEFAULT 0,          -- latest # of videos using this sound
    canonical_key      TEXT,                       -- normalized name+author for cross-platform grouping
    first_seen_at      TIMESTAMPTZ DEFAULT now(),
    last_seen_at       TIMESTAMPTZ DEFAULT now(),
    UNIQUE (platform, platform_sound_id)
);
CREATE INDEX IF NOT EXISTS idx_sounds_canonical ON sounds(canonical_key);
CREATE INDEX IF NOT EXISTS idx_sounds_clipcount ON sounds(clip_count DESC);

-- ── Sound usage snapshots (time-series → "how fast the count is rising") ─────
-- Each daily run inserts a row; velocity of usage = Δclip_count / Δhours.
CREATE TABLE IF NOT EXISTS sound_snapshots (
    id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    sound_id     UUID NOT NULL REFERENCES sounds(id) ON DELETE CASCADE,
    captured_at  TIMESTAMPTZ DEFAULT now(),
    clip_count   INTEGER
);
CREATE INDEX IF NOT EXISTS idx_snap_sound ON sound_snapshots(sound_id, captured_at DESC);

-- ── Clips (every scraped TikTok / Reel that crossed the threshold) ──────────
CREATE TABLE IF NOT EXISTS clips (
    id               UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    platform         TEXT NOT NULL CHECK (platform IN ('tiktok','instagram')),
    platform_post_id TEXT NOT NULL,
    video_url        TEXT,
    creator_handle   TEXT,
    caption          TEXT,
    hashtags         TEXT[],
    views            BIGINT DEFAULT 0,      -- views (TikTok) / plays (Reels)
    likes            BIGINT DEFAULT 0,
    comments         BIGINT DEFAULT 0,
    shares           BIGINT DEFAULT 0,
    saves            BIGINT DEFAULT 0,
    duration_sec     NUMERIC,
    posted_at        TIMESTAMPTZ,
    sound_id         UUID REFERENCES sounds(id) ON DELETE SET NULL,
    video_minio_path TEXT,                  -- downloaded video file in MinIO
    velocity         NUMERIC DEFAULT 0,     -- views / hours since posted
    suitability_flag BOOLEAN DEFAULT FALSE, -- single subject / clean silhouette = priority
    feed_source      TEXT CHECK (feed_source IN ('dance','hook','watchlist')),
    seed_term        TEXT,                  -- hashtag / keyword / account that surfaced it
    scraped_at       TIMESTAMPTZ DEFAULT now(),
    UNIQUE (platform, platform_post_id)
);
CREATE INDEX IF NOT EXISTS idx_clips_velocity ON clips(velocity DESC);
CREATE INDEX IF NOT EXISTS idx_clips_sound    ON clips(sound_id);
CREATE INDEX IF NOT EXISTS idx_clips_feed      ON clips(feed_source, posted_at DESC);

-- ── Trends (a sound clustered across platforms = one replication target) ────
CREATE TABLE IF NOT EXISTS trends (
    id                UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    canonical_key     TEXT UNIQUE,         -- shared key across the sounds that make up this trend
    title             TEXT,
    reference_clip_id UUID REFERENCES clips(id) ON DELETE SET NULL,  -- fastest-growing example
    velocity          NUMERIC DEFAULT 0,
    total_views       BIGINT DEFAULT 0,
    total_clips       INTEGER DEFAULT 0,
    status            TEXT DEFAULT 'new' CHECK (status IN ('new','queued','recreated','skipped')),
    first_seen_at     TIMESTAMPTZ DEFAULT now(),
    last_seen_at      TIMESTAMPTZ DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_trends_velocity ON trends(velocity DESC);
CREATE INDEX IF NOT EXISTS idx_trends_status   ON trends(status);

-- ── Reference-account watchlist (early-warning signal) ──────────────────────
CREATE TABLE IF NOT EXISTS watchlist_accounts (
    id             UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    platform       TEXT NOT NULL CHECK (platform IN ('tiktok','instagram')),
    handle         TEXT NOT NULL,
    account_group  TEXT,                    -- e.g. 'ai_reference' / 'human_dance'
    active         BOOLEAN DEFAULT TRUE,
    added_at       TIMESTAMPTZ DEFAULT now(),
    UNIQUE (platform, handle)
);

-- ── Seed the starter watchlist from the brief ──────────────────────────────
INSERT INTO watchlist_accounts (platform, handle, account_group) VALUES
    ('instagram','fit_aitana','ai_reference'),
    ('tiktok','fitaitana','ai_reference'),
    ('instagram','ninoir.xo','ai_reference'),
    ('tiktok','clararivzz','ai_reference'),
    ('instagram','onlyrives','ai_reference'),
    ('tiktok','luna.rayne','ai_reference'),
    ('tiktok','kenna_milian','human_dance'),
    ('tiktok','joriojess','human_dance'),
    ('tiktok','dancatrend','human_dance'),
    ('tiktok','aishahsofey','human_dance'),
    ('tiktok','bophouse','human_dance'),
    ('instagram','natalieyospina','human_dance'),
    ('tiktok','natalieyospina','human_dance'),
    ('tiktok','youstilldontknowme82','human_dance'),
    ('tiktok','chyburdxo','human_dance')
ON CONFLICT (platform, handle) DO NOTHING;
