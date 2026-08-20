-- ============================================================================
-- Persist the clip appearance read  (brief task A5, §3.6)
-- Run this in the Supabase SQL editor. Safe to re-run.
--
-- classify_subject() already runs the full 15-field VISION_USER prompt on every
-- clip cover, then keeps only subject_type. The call is already paid for, so
-- storing the rest adds no API spend.
--
-- Not already stored elsewhere: analysis_results is keyed by media_asset_id +
-- creator_id (the Stage 5 creator pipeline) and has no clip_id, and trends.py
-- only ever reads that table. Clip reads had no home at all.
--
-- JSON only, unlike analysis_results which has typed columns AND raw_json.
-- One column beats twelve plus CHECK constraints, and appearance->>'skin_tone'
-- filters fine against the GIN index. Promote fields to columns later if a
-- filter proves slow.
--
-- Forward-only: existing clips can only be filled by paying for the calls again.
-- ============================================================================

ALTER TABLE clips
    ADD COLUMN IF NOT EXISTS appearance JSONB;

CREATE INDEX IF NOT EXISTS idx_clips_appearance
    ON clips USING GIN (appearance);

COMMENT ON COLUMN clips.appearance IS
    'Full gpt-4o vision read of the clip cover — src/ai/prompts.py VISION_USER. Includes is_child and is_ad_or_product for safety filtering.';
