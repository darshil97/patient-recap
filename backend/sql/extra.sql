-- CREATE TABLE patient_documents (
--     id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),

--     patient_id          UUID NOT NULL
--                         REFERENCES patients(patient_id)
--                         ON DELETE CASCADE,

--     visit_id            UUID
--                         REFERENCES visits(visit_id)
--                         ON DELETE SET NULL,

--     document_type       TEXT NOT NULL,
--     -- transcript
--     -- lab_report
--     -- prescription
--     -- imaging
--     -- referral
--     -- other

--     storage_path        TEXT NOT NULL,

--     file_name           TEXT,

--     mime_type            TEXT,

--     file_size_bytes     BIGINT,

--     checksum            TEXT,

--     created_at          TIMESTAMPTZ NOT NULL DEFAULT now()
-- );

-- CREATE INDEX idx_patient_documents_patient
--     ON patient_documents(patient_id);

-- CREATE INDEX idx_patient_documents_visit
--     ON patient_documents(visit_id);

-- CREATE TABLE patient_conditions (
--     id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),

--     patient_id          UUID NOT NULL
--                         REFERENCES patients(patient_id)
--                         ON DELETE CASCADE,

--     condition_name      TEXT NOT NULL,

--     status              TEXT,
--     -- active / resolved / suspected / historical

--     diagnosed_at        DATE,

--     first_seen_visit_id UUID
--                         REFERENCES visits(visit_id),

--     notes               TEXT,

--     created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),

--     updated_at          TIMESTAMPTZ NOT NULL DEFAULT now()
-- );

-- CREATE INDEX idx_patient_conditions_patient
--     ON patient_conditions(patient_id);

-- CREATE INDEX idx_patient_conditions_name
--     ON patient_conditions(condition_name);


-- -- Example
-- -- patient: Riya

-- -- PCOS
-- -- status: active
-- -- diagnosed_at: ...
-- -- first_seen_visit_id: ...