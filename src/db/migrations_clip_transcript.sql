-- ============================================================================
-- Store TikTok's own subtitles as clip transcripts  (brief task D1, §3.3)
-- Run this in the Supabase SQL editor. Safe to re-run.
--
-- clockworks/tiktok-scraper exposes downloadSubtitlesOptions. We use
-- DOWNLOAD_SUBTITLES, which returns only the captions TikTok already generated
-- — free with the scrape. The other two enum values (…TRANSCRIBE_VIDEOS…,
-- TRANSCRIBE_ALL_VIDEOS) run speech-to-text and are "charged as an extra
-- event", which the brief explicitly does not ask for: WhisperX is the fallback.
--
-- Coverage is partial by nature — TikTok only generates captions for some
-- videos, so transcript stays NULL on the rest.
--
-- transcript_vtt keeps the original WebVTT with its cue timings, because task
-- D2 (hook = first ~3 seconds) needs timestamps and re-scraping to recover
-- them later would be the expensive mistake.
-- ============================================================================

ALTER TABLE clips
    ADD COLUMN IF NOT EXISTS transcript        TEXT,   -- plain text, cues joined
    ADD COLUMN IF NOT EXISTS transcript_vtt    TEXT,   -- original WebVTT + timings
    ADD COLUMN IF NOT EXISTS transcript_lang   TEXT,   -- e.g. 'eng-US'
    ADD COLUMN IF NOT EXISTS transcript_source TEXT;   -- 'ASR' | 'MT' as TikTok reports it

COMMENT ON COLUMN clips.transcript IS
    'Spoken words from TikTok''s own captions, cues joined and consecutive duplicates collapsed. NULL when TikTok generated none.';
COMMENT ON COLUMN clips.transcript_vtt IS
    'Original WebVTT including cue timings — required by task D2 for hook extraction.';
