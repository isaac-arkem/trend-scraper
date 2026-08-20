-- Community Intelligence Pipeline — Supabase Schema

CREATE EXTENSION IF NOT EXISTS "pgcrypto";

-- Markets: one row per country+platform combo
CREATE TABLE IF NOT EXISTS markets (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name TEXT NOT NULL,
    region TEXT NOT NULL CHECK (region IN ('META', 'LATAM', 'INDOPAC')),
    country_code TEXT NOT NULL,
    platform TEXT NOT NULL CHECK (platform IN ('instagram', 'tiktok')),
    apify_region_code TEXT,
    seed_hashtags JSONB DEFAULT '[]',
    seed_profiles JSONB DEFAULT '[]',
    languages JSONB DEFAULT '[]',
    created_at TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE(country_code, platform)
);

-- Runs: one row per pipeline execution (market + platform + timestamp)
CREATE TABLE IF NOT EXISTS runs (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    market_id UUID REFERENCES markets(id) ON DELETE CASCADE,
    platform TEXT NOT NULL CHECK (platform IN ('instagram', 'tiktok')),
    current_stage INTEGER DEFAULT 1 CHECK (current_stage BETWEEN 1 AND 6),
    stage_statuses JSONB DEFAULT '{"1":"pending","2":"pending","3":"pending","4":"pending","5":"pending","6":"pending"}',
    status TEXT NOT NULL DEFAULT 'running' CHECK (status IN ('running', 'done', 'failed', 'paused')),
    total_creators INTEGER DEFAULT 0,
    total_posts INTEGER DEFAULT 0,
    total_media INTEGER DEFAULT 0,
    compute_units_used FLOAT DEFAULT 0,
    error_message TEXT,
    config JSONB DEFAULT '{}',
    started_at TIMESTAMPTZ DEFAULT NOW(),
    ended_at TIMESTAMPTZ
);

-- Creators: deduplicated by platform + username
CREATE TABLE IF NOT EXISTS creators (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    platform TEXT NOT NULL CHECK (platform IN ('instagram', 'tiktok')),
    platform_user_id TEXT,
    username TEXT NOT NULL,
    full_name TEXT,
    bio TEXT,
    profile_url TEXT,
    profile_pic_url TEXT,
    followers INTEGER,
    following INTEGER,
    post_count INTEGER,
    is_verified BOOLEAN DEFAULT FALSE,
    market_id UUID REFERENCES markets(id),
    run_id UUID REFERENCES runs(id),
    source_type TEXT CHECK (source_type IN ('seed', 'hashtag_discovery', 'related_profile', 'following', 'mutual_comment', 'shared_hashtag')),
    tier INTEGER DEFAULT 1,
    relevance_score FLOAT DEFAULT 0,
    engagement_score FLOAT DEFAULT 0,
    community_score FLOAT DEFAULT 0,
    total_score FLOAT DEFAULT 0,
    language_detected TEXT,
    scraped_at TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE(platform, username)
);

-- Posts: scraped content per creator
CREATE TABLE IF NOT EXISTS posts (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    creator_id UUID REFERENCES creators(id) ON DELETE CASCADE,
    platform TEXT NOT NULL CHECK (platform IN ('instagram', 'tiktok')),
    platform_post_id TEXT,
    post_url TEXT,
    caption TEXT,
    hashtags JSONB DEFAULT '[]',
    likes INTEGER DEFAULT 0,
    comments_count INTEGER DEFAULT 0,
    views INTEGER DEFAULT 0,
    shares INTEGER DEFAULT 0,
    media_type TEXT CHECK (media_type IN ('image', 'video', 'carousel', 'reel')),
    media_url TEXT,
    thumbnail_url TEXT,
    posted_at TIMESTAMPTZ,
    scraped_at TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE(platform, platform_post_id)
);

-- Media Assets: files stored in MinIO, linked to posts
CREATE TABLE IF NOT EXISTS media_assets (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    post_id UUID REFERENCES posts(id) ON DELETE CASCADE,
    creator_id UUID REFERENCES creators(id),
    asset_type TEXT CHECK (asset_type IN ('image', 'video', 'frame', 'thumbnail')),
    minio_path TEXT NOT NULL,
    original_url TEXT,
    frame_index INTEGER,
    analysis_status TEXT DEFAULT 'pending' CHECK (analysis_status IN ('pending', 'analyzing', 'done', 'failed', 'skipped')),
    created_at TIMESTAMPTZ DEFAULT NOW()
);

-- Analysis Results: AI vision output per media asset
CREATE TABLE IF NOT EXISTS analysis_results (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    media_asset_id UUID REFERENCES media_assets(id) ON DELETE CASCADE,
    creator_id UUID REFERENCES creators(id),
    person_visible BOOLEAN,
    body_frame TEXT CHECK (body_frame IN ('petite','slim','average','curvy','athletic','plus','unclear')),
    body_shape TEXT CHECK (body_shape IN ('pear','balanced','apple','unclear')),
    skin_tone TEXT CHECK (skin_tone IN ('porcelain','fair','light','medium','olive','golden-tan','tan','caramel','deep','dark','unclear')),
    eye_color TEXT CHECK (eye_color IN ('brown','black','blue','green','hazel','unclear')),
    hair_color TEXT CHECK (hair_color IN ('black','brown','blonde','red','dyed','mixed','covered','unclear')),
    hair_length TEXT CHECK (hair_length IN ('short','medium','long','covered','unclear')),
    hair_texture TEXT CHECK (hair_texture IN ('straight','wavy','curly','coily','covered','unclear')),
    makeup_style TEXT CHECK (makeup_style IN ('natural','soft_glam','full_glam','bold','none_visible','unclear')),
    fashion_style JSONB DEFAULT '[]',
    content_style JSONB DEFAULT '[]',
    image_quality TEXT CHECK (image_quality IN ('good','medium','poor')),
    confidence FLOAT,
    notes TEXT,
    raw_json JSONB,
    analyzed_at TIMESTAMPTZ DEFAULT NOW()
);

-- Cluster Edges: graph of creator relationships
CREATE TABLE IF NOT EXISTS cluster_edges (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    source_creator_id UUID REFERENCES creators(id) ON DELETE CASCADE,
    target_creator_id UUID REFERENCES creators(id) ON DELETE CASCADE,
    edge_type TEXT CHECK (edge_type IN ('related_profile','following','mutual_comment','shared_hashtag','same_audio','same_location')),
    platform TEXT CHECK (platform IN ('instagram', 'tiktok')),
    weight FLOAT DEFAULT 1.0,
    run_id UUID REFERENCES runs(id),
    created_at TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE(source_creator_id, target_creator_id, edge_type)
);

-- Comments: scraped comments for mutual-comment expansion
CREATE TABLE IF NOT EXISTS comments (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    post_id UUID REFERENCES posts(id) ON DELETE CASCADE,
    commenter_username TEXT,
    commenter_platform_id TEXT,
    text TEXT,
    likes INTEGER DEFAULT 0,
    scraped_at TIMESTAMPTZ DEFAULT NOW()
);

-- Indexes for common queries
CREATE INDEX IF NOT EXISTS idx_creators_market ON creators(market_id);
CREATE INDEX IF NOT EXISTS idx_creators_platform ON creators(platform);
CREATE INDEX IF NOT EXISTS idx_creators_score ON creators(total_score DESC);
CREATE INDEX IF NOT EXISTS idx_posts_creator ON posts(creator_id);
CREATE INDEX IF NOT EXISTS idx_media_status ON media_assets(analysis_status);
CREATE INDEX IF NOT EXISTS idx_media_creator ON media_assets(creator_id);
CREATE INDEX IF NOT EXISTS idx_analysis_creator ON analysis_results(creator_id);
CREATE INDEX IF NOT EXISTS idx_edges_source ON cluster_edges(source_creator_id);
CREATE INDEX IF NOT EXISTS idx_runs_market ON runs(market_id);
