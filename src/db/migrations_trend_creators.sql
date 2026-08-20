-- ============================================================================
-- Creator identity for the boards
-- Apply in the Supabase Studio SQL editor. Safe to re-run.
--
-- trend_signals could rank a creator but carried nothing to draw: no name, no
-- avatar, no link. The profile data was fetched and archived to MinIO but never
-- reached a table, so the platform had a leaderboard of bare handles.
--
-- Two parts:
--   trend_creators  — one row per creator, their CURRENT profile
--   trend_signals   — denormalised display fields so a board renders from one
--                     query, no join, which is what a leaderboard needs
-- ============================================================================

CREATE TABLE IF NOT EXISTS trend_creators (
    id                UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    platform          TEXT NOT NULL CHECK (platform IN ('tiktok','instagram')),
    username          TEXT NOT NULL,
    platform_user_id  TEXT,
    display_name      TEXT,
    bio               TEXT,
    followers         BIGINT,
    following         BIGINT,
    post_count        BIGINT,
    is_verified       BOOLEAN DEFAULT FALSE,
    profile_url       TEXT,             -- link out to the real profile
    profile_pic_url   TEXT,             -- original CDN url; expires, hence the mirror
    avatar_path       TEXT,             -- MinIO: profiles/<platform>/<user_id>.jpg
    country_code      TEXT,             -- where we first saw them trending
    niche             TEXT,
    first_seen_at     TIMESTAMPTZ DEFAULT now(),
    last_seen_at      TIMESTAMPTZ DEFAULT now(),
    UNIQUE (platform, username)
);

CREATE INDEX IF NOT EXISTS idx_tc_followers ON trend_creators(followers DESC);
CREATE INDEX IF NOT EXISTS idx_tc_country   ON trend_creators(country_code);
CREATE INDEX IF NOT EXISTS idx_tc_niche     ON trend_creators(niche);

COMMENT ON COLUMN trend_creators.avatar_path IS
    'MinIO path in the social-intel bucket. Serve via /img-minio?p=<path> — the same convention the creators dashboard already uses, so images render with no new plumbing.';


-- ── display fields on the board itself ──────────────────────────────────────
-- Denormalised on purpose. A board is read far more often than it is written,
-- and it is always read whole; making the frontend join four tables to draw one
-- row is how leaderboards end up slow.
ALTER TABLE trend_signals
    ADD COLUMN IF NOT EXISTS display_name TEXT,
    ADD COLUMN IF NOT EXISTS avatar_path  TEXT,
    ADD COLUMN IF NOT EXISTS profile_url  TEXT,
    ADD COLUMN IF NOT EXISTS followers    BIGINT,
    ADD COLUMN IF NOT EXISTS thumb_path   TEXT;   -- video rows: the clip's own frame

COMMENT ON COLUMN trend_signals.avatar_path IS
    'For kind=creator and kind=video: the poster''s avatar. Hashtag and sound rows carry up to three in metrics->creators instead, matching the avatar cluster on TikTok''s own trend board.';
