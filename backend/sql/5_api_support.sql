-- Migration: support the HTTP API.
--
-- 1. external_id becomes the hard patient identity key, auto-generated
-- 2. an index to detect probable duplicate patients
-- 3. insight_jobs, so generate_insights can return 202 and be polled
--
-- Run once against an existing database.

-- ---------------------------------------------------------------------------
-- 1. Patient identity
--
-- external_id is the clinic's own patient number and the only hard uniqueness
-- rule. Phone is not a key because families share one mobile, and names are not
-- a key because spelling and transliteration vary; either would both over-block
-- and under-catch. Readable on purpose ('P-2026-00042'): front-desk staff need
-- something they can say aloud, which a UUID is not.
-- ---------------------------------------------------------------------------

CREATE SEQUENCE IF NOT EXISTS patient_external_id_seq;

CREATE OR REPLACE FUNCTION next_patient_external_id()
RETURNS TEXT
LANGUAGE sql
VOLATILE
AS $$
    SELECT 'P-' || to_char(now(), 'YYYY') || '-'
           || lpad(nextval('patient_external_id_seq')::text, 5, '0');
$$;

-- Backfill anyone registered before this column was in use.
UPDATE patients
SET external_id = next_patient_external_id()
WHERE external_id IS NULL;

ALTER TABLE patients
    ALTER COLUMN external_id SET DEFAULT next_patient_external_id();

ALTER TABLE patients
    ALTER COLUMN external_id SET NOT NULL;

-- ---------------------------------------------------------------------------
-- 2. Probable-duplicate detection
--
-- Advisory, not a constraint. add_patient looks here and returns 409 with the
-- existing record so a human can confirm, because a hard constraint on
-- demographics would eventually reject a legitimate patient (two siblings, one
-- shared phone number).
-- ---------------------------------------------------------------------------

CREATE INDEX IF NOT EXISTS idx_patients_identity
    ON patients (lower(btrim(first_name)), lower(btrim(coalesce(last_name, ''))),
                 date_of_birth);

CREATE INDEX IF NOT EXISTS idx_patients_phone
    ON patients (phone)
    WHERE phone IS NOT NULL;

-- ---------------------------------------------------------------------------
-- 3. Insight jobs
--
-- The chain is three sequential LLM calls, around a minute in total, which is
-- past common proxy timeouts. So generate_insights records a job, returns 202,
-- and the caller polls. Kept in Postgres rather than memory so a job survives a
-- restart and stays queryable.
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS insight_jobs (
    job_id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),

    visit_id            UUID NOT NULL
                        REFERENCES visits(visit_id)
                        ON DELETE CASCADE,

    status              TEXT NOT NULL DEFAULT 'queued'
                        CHECK (status IN ('queued', 'running', 'succeeded',
                                          'failed', 'failed_quota')),

    -- How far the chain got. Useful when a run dies midway.
    layers_completed    INTEGER NOT NULL DEFAULT 0,
    stop_after          INTEGER NOT NULL DEFAULT 3,

    regenerate          BOOLEAN NOT NULL DEFAULT FALSE,
    model_name          TEXT,

    insight_id          UUID
                        REFERENCES visit_insights(insight_id)
                        ON DELETE SET NULL,

    error_code          TEXT,
    error_message       TEXT,

    total_tokens        INTEGER,

    -- Identifies the process that claimed the job, so orphaned 'running' jobs
    -- can be failed on startup instead of hanging forever.
    worker_id           TEXT,

    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    started_at          TIMESTAMPTZ,
    finished_at         TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_insight_jobs_visit
    ON insight_jobs(visit_id, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_insight_jobs_active
    ON insight_jobs(status)
    WHERE status IN ('queued', 'running');

-- At most one live job per visit, so a double-submit cannot start the chain
-- twice and spend two sets of API calls on the same transcript.
CREATE UNIQUE INDEX IF NOT EXISTS idx_insight_jobs_one_active
    ON insight_jobs(visit_id)
    WHERE status IN ('queued', 'running');
