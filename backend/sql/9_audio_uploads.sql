-- Migration: chunked audio uploads, and the recording each one leaves behind.
--
-- Run once against an existing database, AFTER 8_cancellation_reason.sql.
-- Idempotent. Additive only: two new tables, nothing existing is altered.
--
-- Why chunks: every browser request passes through the Next.js BFF, and on Vercel
-- a function's request body is capped at 4.5 MB. A consultation recording is
-- routinely larger, so the browser slices the file and sends it in pieces under
-- that cap. The backend stores the pieces, stitches them into one recording once
-- all have arrived, deletes the pieces, and transcribes the recording on a
-- background task (the same in-process pattern as insight_jobs).
--
-- Tenancy is derived, like insight_jobs: audio_uploads.visit_id -> visits ->
-- patients.doctor_id. No doctor_id column (§6).

CREATE TABLE IF NOT EXISTS audio_uploads (
    upload_id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),

    visit_id            UUID NOT NULL
                        REFERENCES visits(visit_id)
                        ON DELETE CASCADE,

    -- receiving   chunks are arriving
    -- processing  stitching and/or transcribing, on a background task
    -- completed   the transcript is attached; audio_path is THE visit recording
    -- failed      worth retrying: chunks or the stitched recording are kept
    -- abandoned   replaced by a newer upload before it finished; files removed
    -- superseded  was completed, then a newer recording completed; file removed
    status              TEXT NOT NULL DEFAULT 'receiving'
                        CHECK (status IN ('receiving', 'processing', 'completed',
                                          'failed', 'abandoned', 'superseded')),

    -- The doctor's own filename, for display only. Never part of a storage key.
    filename            TEXT,
    -- From the filename, checked against the server's allowlist. Decides the
    -- recording's key suffix and the Content-Type sent to speech-to-text.
    extension           TEXT NOT NULL CHECK (extension ~ '^\.[a-z0-9]{2,5}$'),

    total_bytes         BIGINT NOT NULL CHECK (total_bytes > 0),
    chunk_bytes         INTEGER NOT NULL CHECK (chunk_bytes > 0),
    chunk_count         INTEGER NOT NULL CHECK (chunk_count > 0),

    -- The upload's own `replace`, applied when the transcript is written.
    replace_transcript  BOOLEAN NOT NULL DEFAULT FALSE,

    -- Where the chunks and the recording live: local | neon_object_storage.
    -- Recorded per row for the same reason as visits.transcript_provider (§8.22).
    storage_provider    TEXT NOT NULL,

    -- recordings/<visit_id>/<upload_id><extension> once stitched. Kept on a
    -- failed row so a retry transcribes it again without a re-upload.
    audio_path          TEXT,

    duration_seconds    DOUBLE PRECISION,
    -- The speaker labelling from the last successful run, for the UI. The
    -- labels themselves are in the stored transcript.
    speakers            JSONB,
    warnings            JSONB,

    error_code          TEXT,
    error_message       TEXT,

    -- The process that claimed it, so an interrupted run is detectable at startup.
    worker_id           TEXT,

    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    completed_at        TIMESTAMPTZ,

    -- The chunk plan must cover the file exactly, so "every chunk arrived" and
    -- "the stitched file is the whole recording" are the same statement.
    CONSTRAINT audio_uploads_chunks_cover_file
        CHECK (chunk_count = ceil(total_bytes::numeric / chunk_bytes))
);

-- One upload in flight per visit, so two tabs cannot stitch and transcribe the
-- same visit at once and race to write its transcript.
CREATE UNIQUE INDEX IF NOT EXISTS uq_audio_uploads_one_active
    ON audio_uploads(visit_id)
    WHERE status IN ('receiving', 'processing');

-- At most one recording per visit. A newer completed upload supersedes the old
-- one in the same transaction, older row first.
CREATE UNIQUE INDEX IF NOT EXISTS uq_audio_uploads_one_recording
    ON audio_uploads(visit_id)
    WHERE status = 'completed' AND audio_path IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_audio_uploads_visit
    ON audio_uploads(visit_id, created_at DESC);

-- Which pieces have arrived. A row is written only after its bytes are stored,
-- and re-sending a piece replaces it, so a retried request is harmless. Rows are
-- removed with their files once the recording is stitched.
CREATE TABLE IF NOT EXISTS audio_upload_chunks (
    upload_id           UUID NOT NULL
                        REFERENCES audio_uploads(upload_id)
                        ON DELETE CASCADE,
    chunk_index         INTEGER NOT NULL CHECK (chunk_index >= 0),
    size_bytes          INTEGER NOT NULL CHECK (size_bytes > 0),
    received_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (upload_id, chunk_index)
);

-- Verify
DO $verify$
DECLARE
    missing TEXT[];
BEGIN
    SELECT array_agg(t) INTO missing
    FROM unnest(ARRAY['audio_uploads', 'audio_upload_chunks']) AS t
    WHERE NOT EXISTS (SELECT 1 FROM information_schema.tables
                      WHERE table_name = t);
    IF missing IS NOT NULL THEN
        RAISE EXCEPTION 'Missing tables: %', missing;
    END IF;
    RAISE NOTICE 'audio_uploads and audio_upload_chunks are present.';
END
$verify$;
