-- Migration: live recordings, sent to the backend while they are being recorded.
--
-- Run once against an existing database, AFTER 9_audio_uploads.sql. Idempotent.
--
-- A recording made in the browser goes up in small pieces *during* the
-- consultation, so a closed tab or a crashed laptop loses at most the last piece
-- instead of the whole consultation (§8.24). Such an upload cannot know its size
-- when it starts, which the file-upload plan in 9_audio_uploads.sql requires.
--
-- So a live upload starts with no plan. total_bytes and chunk_count stay NULL
-- while it is receiving, and are written -- from the chunks that actually
-- arrived -- in the same UPDATE that hands it to processing. From then on it is
-- an ordinary upload: the cover constraint, stitching, retry and the reaper all
-- see a known plan. The pieces are still exact-size (every one but the last is
-- chunk_bytes), so "every chunk arrived" still means "the stitched file is the
-- whole recording".
--
-- Nothing existing changes: every current row becomes mode 'file' with the plan
-- it already has.

ALTER TABLE audio_uploads
    ADD COLUMN IF NOT EXISTS mode TEXT NOT NULL DEFAULT 'file';

ALTER TABLE audio_uploads ALTER COLUMN total_bytes DROP NOT NULL;
ALTER TABLE audio_uploads ALTER COLUMN chunk_count DROP NOT NULL;

DO $constraints$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint
                   WHERE conrelid = 'audio_uploads'::regclass
                     AND conname = 'audio_uploads_mode_allowed') THEN
        ALTER TABLE audio_uploads
            ADD CONSTRAINT audio_uploads_mode_allowed
            CHECK (mode IN ('file', 'live'));
    END IF;

    -- A missing plan is allowed only where it is meaningful: a live recording
    -- still being recorded, or one discarded before it was ever finished. A file
    -- upload always declares its size, and nothing reaches processing, failed or
    -- completed without a plan, because stitching and Deepgram both need it.
    IF NOT EXISTS (SELECT 1 FROM pg_constraint
                   WHERE conrelid = 'audio_uploads'::regclass
                     AND conname = 'audio_uploads_plan_known') THEN
        ALTER TABLE audio_uploads
            ADD CONSTRAINT audio_uploads_plan_known
            CHECK ((total_bytes IS NOT NULL AND chunk_count IS NOT NULL)
                   OR (mode = 'live'
                       AND total_bytes IS NULL AND chunk_count IS NULL
                       AND status IN ('receiving', 'abandoned')));
    END IF;
END
$constraints$;

-- Verify
DO $verify$
DECLARE
    has_mode BOOLEAN;
    nullable INT;
    constraints INT;
BEGIN
    SELECT EXISTS (SELECT 1 FROM information_schema.columns
                   WHERE table_name = 'audio_uploads' AND column_name = 'mode')
      INTO has_mode;
    SELECT count(*) INTO nullable
    FROM information_schema.columns
    WHERE table_name = 'audio_uploads'
      AND column_name IN ('total_bytes', 'chunk_count')
      AND is_nullable = 'YES';
    SELECT count(*) INTO constraints
    FROM pg_constraint
    WHERE conrelid = 'audio_uploads'::regclass
      AND conname IN ('audio_uploads_mode_allowed', 'audio_uploads_plan_known',
                      'audio_uploads_chunks_cover_file');
    IF NOT has_mode OR nullable <> 2 OR constraints <> 3 THEN
        RAISE EXCEPTION 'Live recording migration incomplete (mode: %, nullable plan '
                        'columns: %, constraints: %).', has_mode, nullable, constraints;
    END IF;
    RAISE NOTICE 'audio_uploads supports live recordings.';
END
$verify$;
