CREATE TABLE visits (
    visit_id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),

    patient_id          UUID NOT NULL
                        REFERENCES patients(patient_id),

    visit_number        INTEGER NOT NULL,

    visit_date          DATE NOT NULL,
    visit_time          TIME NOT NULL,

    visit_type          TEXT DEFAULT 'follow_up',
    -- initial / follow_up

    transcript_path     TEXT,

    transcript_provider TEXT,
    -- e.g. local, s3, gcs, azure_blob

    visit_status              TEXT NOT NULL DEFAULT 'completed',
    -- scheduled / in_progress / completed / cancelled

    -- TRUE only once all three pipeline layers have succeeded and the
    -- visit_insights row has been written, both in the same transaction.
    -- A failure at any layer leaves this FALSE, which is how a partially
    -- generated visit stays visible and gets picked up on the next run.
    insights_generated  BOOLEAN NOT NULL DEFAULT FALSE,

    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),

    UNIQUE(patient_id, visit_number)
);

CREATE INDEX idx_visits_patient
    ON visits(patient_id);

CREATE INDEX idx_visits_patient_date
    ON visits(patient_id, visit_date DESC);

-- Finding pending visits is the loader's hot path.
CREATE INDEX idx_visits_pending_insights
    ON visits(visit_id)
    WHERE NOT insights_generated;


-- ---------------------------------------------------------------------------
-- Seed data: one row per transcript in transcripts/.
--
-- visit_date is not arbitrary. Each transcript ends with the doctor stating a
-- follow-up interval ("come back for a review in three weeks"), so each visit
-- is dated by applying the previous visit's stated interval. Visit N+1 lands
-- exactly on the date visit N promised, which means the pipeline's resolved
-- follow_up_date for visit N should equal visit_date for visit N+1 — a free
-- end-to-end check on the extraction.
--
-- Intervals taken from the transcripts:
--   riya    3w, 3w, 4w, 4w, 6w   (visit 6 says "about three to four months")
--   kavita  2w, 3w               (visit 3 says "four weeks")
--   manish  2w, 3w               (visit 3 says "three weeks")
--   jayesh  2w                   (visit 2 says "three weeks")
--   vikas   2w                   (visit 2 says "three weeks")
--
-- visit_time is dummy clinic-hours data; the transcripts state no times.
-- Patients are matched on first_name, so this runs without hardcoded UUIDs.
-- ---------------------------------------------------------------------------

INSERT INTO visits (
    patient_id, visit_number, visit_date, visit_time,
    visit_type, transcript_path, transcript_provider, visit_status
)
SELECT
    p.patient_id, v.visit_number, v.visit_date, v.visit_time,
    v.visit_type, v.transcript_path, 'local', 'completed'
FROM (VALUES
    ('Jayesh', 1, DATE '2026-07-02', TIME '10:30:00', 'initial',   'transcripts/jayesh_1.json'),
    ('Jayesh', 2, DATE '2026-07-16', TIME '11:15:00', 'follow_up', 'transcripts/jayesh_2.json'),

    ('Kavita', 1, DATE '2026-05-19', TIME '10:30:00', 'initial',   'transcripts/kavita_1.json'),
    ('Kavita', 2, DATE '2026-06-02', TIME '11:15:00', 'follow_up', 'transcripts/kavita_2.json'),
    ('Kavita', 3, DATE '2026-06-23', TIME '09:45:00', 'follow_up', 'transcripts/kavita_3.json'),

    ('Manish', 1, DATE '2026-06-03', TIME '10:30:00', 'initial',   'transcripts/manish_1.json'),
    ('Manish', 2, DATE '2026-06-17', TIME '11:15:00', 'follow_up', 'transcripts/manish_2.json'),
    ('Manish', 3, DATE '2026-07-08', TIME '09:45:00', 'follow_up', 'transcripts/manish_3.json'),

    ('Riya',   1, DATE '2026-02-16', TIME '10:30:00', 'initial',   'transcripts/riya_1.json'),
    ('Riya',   2, DATE '2026-03-09', TIME '11:15:00', 'follow_up', 'transcripts/riya_2.json'),
    ('Riya',   3, DATE '2026-03-30', TIME '09:45:00', 'follow_up', 'transcripts/riya_3.json'),
    ('Riya',   4, DATE '2026-04-27', TIME '16:00:00', 'follow_up', 'transcripts/riya_4.json'),
    ('Riya',   5, DATE '2026-05-25', TIME '12:00:00', 'follow_up', 'transcripts/riya_5.json'),
    ('Riya',   6, DATE '2026-07-06', TIME '17:30:00', 'follow_up', 'transcripts/riya_6.json'),

    ('Vikas',  1, DATE '2026-07-24', TIME '10:30:00', 'initial',   'transcripts/vikas_1.json'),
    ('Vikas',  2, DATE '2026-08-07', TIME '11:15:00', 'follow_up', 'transcripts/vikas_2.json')
) AS v(first_name, visit_number, visit_date, visit_time, visit_type, transcript_path)
JOIN patients p ON p.first_name = v.first_name
ON CONFLICT (patient_id, visit_number) DO NOTHING;
