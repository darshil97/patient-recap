-- Migration: support the doctor-facing frontend.
--
--   1. explicit visit lifecycle timestamps
--   2. follow_ups: a tracked, resolvable follow-up per visit
--   3. backfill for the existing 16 visits
--
-- Run once against an existing database. Idempotent.
--
-- ORDERING NOTE: run this BEFORE cli.convert_transcripts. The brief's build
-- order lists conversion first, but the conversion script writes
-- transcript_filename and transcript_uploaded_at, which are created here. The
-- backfill at the bottom touches only visit_insights and visits.completed_at,
-- so it is unaffected by transcript format and is safe to run first.

-- ---------------------------------------------------------------------------
-- 1. Visits
--
-- Visits are now created by a human clicking "Schedule Visit", so 'scheduled'
-- becomes the default rather than 'completed' (which suited bulk-loading
-- historical transcripts).
-- ---------------------------------------------------------------------------

ALTER TABLE visits
    ALTER COLUMN visit_status SET DEFAULT 'scheduled';

-- ADD CONSTRAINT has no IF NOT EXISTS, so guard it to keep this file re-runnable.
-- Every existing row is 'completed', so the check validates cleanly.
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint
                   WHERE conname = 'visits_status_allowed') THEN
        ALTER TABLE visits
            ADD CONSTRAINT visits_status_allowed
            CHECK (visit_status IN ('scheduled', 'in_progress',
                                    'completed', 'cancelled'));
    END IF;
END $$;

-- When the consultation was marked done. Distinct from visit_date: the date is
-- clinical, these are audit.
ALTER TABLE visits ADD COLUMN IF NOT EXISTS completed_at TIMESTAMPTZ;
ALTER TABLE visits ADD COLUMN IF NOT EXISTS cancelled_at TIMESTAMPTZ;
ALTER TABLE visits ADD COLUMN IF NOT EXISTS updated_at   TIMESTAMPTZ;

-- The doctor's own filename, kept for display only. The stored path is always
-- server-generated from visit_id.
ALTER TABLE visits ADD COLUMN IF NOT EXISTS transcript_filename    TEXT;
ALTER TABLE visits ADD COLUMN IF NOT EXISTS transcript_uploaded_at TIMESTAMPTZ;

-- The scheduled queue is a date-ordered read over one status.
CREATE INDEX IF NOT EXISTS idx_visits_status_date ON visits(visit_status, visit_date);
CREATE INDEX IF NOT EXISTS idx_visits_date        ON visits(visit_date DESC);

-- ---------------------------------------------------------------------------
-- 2. Follow-ups
--
-- A separate table rather than columns on visit_insights, for two reasons.
--
-- Regeneration upserts visit_insights in place, so resolution state stored
-- there would be silently overwritten by a rerun. A follow-up's lifecycle
-- belongs to the clinic, not to the model.
--
-- And resolution is a human act with its own audit trail: when it closed, and
-- because of which visit. That does not belong in a row describing model output.
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS follow_ups (
    follow_up_id        UUID PRIMARY KEY DEFAULT gen_random_uuid(),

    -- The visit whose consultation produced this follow-up.
    source_visit_id     UUID NOT NULL
                        REFERENCES visits(visit_id) ON DELETE CASCADE,

    -- Denormalised so the queue reads without joining through visits. The queue
    -- is the application's most-read query.
    patient_id          UUID NOT NULL
                        REFERENCES patients(patient_id) ON DELETE CASCADE,

    -- Mirrored from visit_insights at generation time, so resolution state and
    -- the date it was judged against stay together.
    due_earliest        DATE NOT NULL,
    due_latest          DATE,
    needs_confirmation  BOOLEAN NOT NULL DEFAULT FALSE,
    stated              TEXT,
    purpose             TEXT,

    status              TEXT NOT NULL DEFAULT 'open'
                        CHECK (status IN ('open', 'resolved', 'dismissed')),

    -- Set when a later visit is scheduled. The only automatic transition, and it
    -- is written at scheduling time, never inferred on read.
    resolved_by_visit_id UUID
                        REFERENCES visits(visit_id) ON DELETE SET NULL,
    resolved_reason     TEXT
                        CHECK (resolved_reason IS NULL OR
                               resolved_reason IN ('visit_scheduled', 'manual')),
    resolved_at         TIMESTAMPTZ,

    -- Set when the doctor calls and the patient is not returning.
    dismissed_reason    TEXT,
    dismissed_at        TIMESTAMPTZ,

    -- "I phoned them." Independent of status: a contacted follow-up whose patient
    -- is still deciding remains open.
    last_contacted_at   TIMESTAMPTZ,
    note                TEXT,

    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now(),

    -- One follow-up per source visit. Also the upsert conflict target, so
    -- regenerating a visit's insights updates this row instead of adding one.
    CONSTRAINT follow_ups_source_visit_key UNIQUE (source_visit_id),

    CONSTRAINT follow_ups_window_ordered
        CHECK (due_latest IS NULL OR due_latest >= due_earliest),

    -- A resolved row must say what closed it; a non-resolved row must not claim
    -- to be closed. Prevents half-written transitions.
    CONSTRAINT follow_ups_resolution_consistent
        CHECK ((status = 'resolved' AND resolved_at IS NOT NULL
                                    AND resolved_reason IS NOT NULL)
               OR (status <> 'resolved' AND resolved_at IS NULL
                                        AND resolved_by_visit_id IS NULL)),

    CONSTRAINT follow_ups_dismissal_consistent
        CHECK ((status = 'dismissed' AND dismissed_at IS NOT NULL)
               OR (status <> 'dismissed' AND dismissed_at IS NULL))
);

-- The Follow-ups tab: open rows in due-date order.
CREATE INDEX IF NOT EXISTS idx_follow_ups_open_due
    ON follow_ups(due_earliest) WHERE status = 'open';

CREATE INDEX IF NOT EXISTS idx_follow_ups_patient
    ON follow_ups(patient_id, due_earliest DESC);

-- ---------------------------------------------------------------------------
-- 3. Backfill
--
-- All existing visits have insights and no follow_ups rows.
-- ---------------------------------------------------------------------------

-- Create a follow-up per existing scheduled insight.
INSERT INTO follow_ups (source_visit_id, patient_id, due_earliest, due_latest,
                        needs_confirmation, stated, purpose)
SELECT i.visit_id, v.patient_id, i.follow_up_date, i.follow_up_date_latest,
       i.follow_up_needs_confirmation, i.follow_up_stated,
       i.insights_json -> 'follow_up' ->> 'purpose'
FROM visit_insights i
JOIN visits v USING (visit_id)
WHERE i.follow_up_type = 'scheduled'
  AND i.follow_up_date IS NOT NULL
ON CONFLICT (source_visit_id) DO NOTHING;

-- Close the ones a later visit already answered, applying the same rule the
-- application uses at scheduling time.
UPDATE follow_ups f
SET status               = 'resolved',
    resolved_by_visit_id = nv.visit_id,
    resolved_reason      = 'visit_scheduled',
    resolved_at          = now(),
    updated_at           = now()
FROM visits sv
JOIN visits nv ON nv.patient_id = sv.patient_id AND nv.visit_date > sv.visit_date
WHERE f.source_visit_id = sv.visit_id
  AND f.status = 'open'
  AND nv.visit_id = (SELECT n2.visit_id FROM visits n2
                     WHERE n2.patient_id = sv.patient_id
                       AND n2.visit_date > sv.visit_date
                     ORDER BY n2.visit_date LIMIT 1);

-- Existing rows are all historical completed consultations.
UPDATE visits SET completed_at = created_at
WHERE visit_status = 'completed' AND completed_at IS NULL;
