CREATE TABLE patients (
    patient_id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    external_id         TEXT UNIQUE,
    first_name          TEXT NOT NULL,
    last_name           TEXT,
    date_of_birth       DATE,
    gender              TEXT,
    phone               TEXT,
    email               TEXT,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);