-- Niche as a first-class thing.
--
-- Niche has been a free-text string in two different columns with two different
-- meanings: reference_accounts.topic (a curator's choice) and clips.topic (copied
-- from whichever account the clip came from). Creator Intelligence never had one
-- at all — 2,885 creators are addressable only by market. So "show me every
-- creator in this niche" could not be answered, and the same niche was spelled
-- three ways ("MAGA"/"maga", "cooking"/"cooking_mum").
--
-- A hashtag is NOT a niche. #chinesestudentsuk is one of many ways to search for
-- the chinese_student niche, which is why hashtags hang off the niche rather than
-- standing in for it.
--
-- Additive only. `topic` keeps working and keeps its meaning; Reference Profiles
-- filters on it and must not change behaviour mid-demo. niche_id sits alongside.
BEGIN;

CREATE TABLE IF NOT EXISTS niches (
  id          bigserial PRIMARY KEY,
  slug        text NOT NULL UNIQUE,          -- chinese_student
  label       text NOT NULL,                 -- "Chinese students"
  hashtags    text[] NOT NULL DEFAULT '{}',  -- default search terms, overridable per run
  countries   text[] NOT NULL DEFAULT '{}',  -- ISO codes this niche is usually run for
  description text,
  active      boolean NOT NULL DEFAULT true,
  created_by  text,
  created_at  timestamptz NOT NULL DEFAULT now()
);

-- Every table that holds something belonging to a niche.
ALTER TABLE clips              ADD COLUMN IF NOT EXISTS niche_id bigint REFERENCES niches(id);
ALTER TABLE reference_accounts ADD COLUMN IF NOT EXISTS niche_id bigint REFERENCES niches(id);
ALTER TABLE creators           ADD COLUMN IF NOT EXISTS niche_id bigint REFERENCES niches(id);
ALTER TABLE runs               ADD COLUMN IF NOT EXISTS niche_id bigint REFERENCES niches(id);

CREATE INDEX IF NOT EXISTS idx_clips_niche      ON clips (niche_id);
CREATE INDEX IF NOT EXISTS idx_ref_acc_niche    ON reference_accounts (niche_id);
CREATE INDEX IF NOT EXISTS idx_creators_niche   ON creators (niche_id);

-- Cover image alongside the video. The pipeline has always downloaded the mp4
-- and never the cover frame, so the console had nothing to show for an image
-- post and nothing to run vision against without re-fetching from the CDN —
-- where the URLs expire. Same bucket and shape as video_minio_path.
ALTER TABLE clips ADD COLUMN IF NOT EXISTS image_minio_path text;

-- Vision bookkeeping. `vision_checked` already existed but was being set true on
-- clips nothing had looked at, which is how 504 clips ended up flagged as
-- analysed with a NULL appearance. These record what actually happened.
ALTER TABLE clips ADD COLUMN IF NOT EXISTS vision_stage    text;   -- gated | analysed | skipped_child | skipped_ad | skipped_no_person | budget_exhausted
ALTER TABLE clips ADD COLUMN IF NOT EXISTS vision_cost_usd numeric(10,6);

COMMIT;
