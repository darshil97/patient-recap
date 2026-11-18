-- Migration: turn a single-tenant database into a per-doctor one.
--
--   1. doctors: just enough to anchor tenancy. Neon Auth owns login.
--   2. the first doctor (you), created from your Neon Auth email
--   3. patients.doctor_id, backfilled to you, then made NOT NULL
--   4. the probable-duplicate index becomes doctor-scoped (fixes the §8.10 bug)
--
-- Run once against an existing database, AFTER 6_frontend_support.sql.
-- Idempotent: safe to re-run.
--
-- ===========================================================================
-- BEFORE YOU RUN THIS: set FIRST_DOCTOR_EMAIL in section 2 to the email address
-- you sign in to Neon Auth with. The script aborts if you leave the placeholder,
-- rather than inventing a system account or leaving doctor_id nullable "for
-- now" — a nullable tenancy column is a leak waiting to be reintroduced the
-- first time someone writes `WHERE doctor_id = %s OR doctor_id IS NULL`.
-- ===========================================================================
--
-- WHY NO doctor_id ON visits / visit_insights / follow_ups
--
-- All three already carry a foreign key that leads to patients, so tenancy is
-- derivable: visits.patient_id -> patients.doctor_id, visit_insights.visit_id ->
-- visits -> patients, follow_ups.patient_id -> patients. Adding the column
-- anyway would create four places that must agree about who owns a record, and
-- the failure mode of disagreement is silent: a stale follow_ups.doctor_id after
-- a patient is reassigned shows one doctor another doctor's patient.
--
-- follow_ups denormalises patient_id (§6) because the queue is the most-read
-- query in the application — but that reasoning does not carry over to
-- doctor_id. The queue's SELECT already joins patients unconditionally, for
-- first_name, last_name, phone and date_of_birth; the join it would supposedly
-- avoid is one it performs regardless. So a denormalised doctor_id here buys no
-- read that isn't already paid for, and costs an update anomaly. Left off on
-- purpose. Revisit only if a query appears that filters follow-ups by doctor
-- *without* needing any patient column.


-- ---------------------------------------------------------------------------
-- 1. Doctors
--
-- NOT a user-management system. No password, no session, no roles: Neon Auth
-- owns all of that. This table exists so our own tables have something to
-- foreign-key against, and so a patient's owner survives an email change.
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS doctors (
    doctor_id       UUID PRIMARY KEY DEFAULT gen_random_uuid(),

    -- From Neon Auth. Unique, but see auth_user_id below: this is the *label*,
    -- not the identity.
    email           TEXT NOT NULL,

    -- Neon Auth's own subject id (Better Auth `user.id`), stamped on first
    -- sign-in. TEXT rather than UUID because it is an opaque provider-issued
    -- string and we must not assume its format.
    --
    -- This is the resolution key the API prefers, because email is mutable:
    -- Better Auth ships a change-email flow, and a doctor who used it would
    -- otherwise be resolved as a brand-new doctor with an empty patient list
    -- while their real records became unreachable. Nullable because this
    -- migration creates the first row before that doctor has ever signed in;
    -- the API claims the row by email once and fills this in.
    auth_user_id    TEXT,

    name            TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Case-insensitive, because 'Doctor@Example.com' and 'doctor@example.com' are
-- the same mailbox and a plain UNIQUE would let both exist as separate tenants.
CREATE UNIQUE INDEX IF NOT EXISTS ux_doctors_email_lower
    ON doctors (lower(btrim(email)));

CREATE UNIQUE INDEX IF NOT EXISTS ux_doctors_auth_user_id
    ON doctors (auth_user_id)
    WHERE auth_user_id IS NOT NULL;


-- ---------------------------------------------------------------------------
-- 2. The first doctor, and the patients.doctor_id column
--
-- Done in one DO block so the new doctor's id is in scope for the backfill
-- without hardcoding a UUID.
-- ---------------------------------------------------------------------------

DO $migration$
DECLARE
    -- =======================================================================
    -- The email you sign in to Neon Auth with (Google OAuth). This is the
    -- account that inherits every existing patient: 7 patients and their 18
    -- visits, insights and follow-ups as of this migration.
    --
    -- (The project brief says "16" in places. That is the count of seeded
    -- *transcripts*, i.e. visits, from sql/2_visit_schema.sql — not patients.
    -- Two more visits and two more patients have been added through the API
    -- since, so the live numbers are 7 and 18. The backfill is unconditional,
    -- so it does not depend on either figure being right.)
    --
    -- It MUST match the address Neon Auth reports for your session. If it does
    -- not, your first signed-in request creates a second, empty doctor and every
    -- screen looks blank while these patients stay attached to this row. That is
    -- recoverable with a one-line UPDATE, but it is alarming, so it is worth
    -- being sure before running this.
    -- =======================================================================
    FIRST_DOCTOR_EMAIL TEXT := 'REPLACE_WITH_YOUR_NEON_AUTH_EMAIL';
    FIRST_DOCTOR_NAME  TEXT := 'Darshil';
    -- =======================================================================

    first_doctor_id  UUID;
    orphan_count     INTEGER;
    backfilled       INTEGER;
BEGIN
    -- Guard against the placeholder being left in on a future re-run or a copy
    -- of this file used to seed another environment.
    IF FIRST_DOCTOR_EMAIL = 'REPLACE_WITH_YOUR_NEON_AUTH_EMAIL'
       OR position('@' in FIRST_DOCTOR_EMAIL) = 0 THEN
        RAISE EXCEPTION
            'Set FIRST_DOCTOR_EMAIL near the top of section 2 to your Neon Auth '
            'sign-in email before running this migration. Nothing has changed.';
    END IF;

    -- Create or find the doctor. ON CONFLICT cannot target a functional unique
    -- index by column name, so look first and insert only if absent.
    SELECT doctor_id INTO first_doctor_id
    FROM doctors
    WHERE lower(btrim(email)) = lower(btrim(FIRST_DOCTOR_EMAIL));

    IF first_doctor_id IS NULL THEN
        INSERT INTO doctors (email, name)
        VALUES (lower(btrim(FIRST_DOCTOR_EMAIL)), FIRST_DOCTOR_NAME)
        RETURNING doctor_id INTO first_doctor_id;
        RAISE NOTICE 'Created first doctor % (%)', FIRST_DOCTOR_EMAIL, first_doctor_id;
    ELSE
        RAISE NOTICE 'Doctor % already exists (%)', FIRST_DOCTOR_EMAIL, first_doctor_id;
    END IF;

    -- Nullable at first so the backfill has somewhere to write. Tightened to
    -- NOT NULL at the end of this same block, so the column is never left
    -- nullable once the migration has run.
    ALTER TABLE patients
        ADD COLUMN IF NOT EXISTS doctor_id UUID;

    -- ON DELETE RESTRICT: deleting a doctor who still owns patients must fail
    -- loudly. Cascading would delete a clinical record because an account was
    -- removed, and SET NULL would silently produce untenanted patients.
    IF NOT EXISTS (SELECT 1 FROM pg_constraint
                   WHERE conname = 'patients_doctor_id_fkey') THEN
        ALTER TABLE patients
            ADD CONSTRAINT patients_doctor_id_fkey
            FOREIGN KEY (doctor_id) REFERENCES doctors(doctor_id)
            ON DELETE RESTRICT;
    END IF;

    -- Every patient that predates tenancy — the seeded ones and anything
    -- registered through the API since — belongs to the first doctor. No WHERE
    -- beyond `IS NULL`, so the count does not have to be known in advance.
    UPDATE patients SET doctor_id = first_doctor_id WHERE doctor_id IS NULL;
    GET DIAGNOSTICS backfilled = ROW_COUNT;
    RAISE NOTICE 'Assigned % existing patient(s) to %',
                 backfilled, FIRST_DOCTOR_EMAIL;

    SELECT count(*) INTO orphan_count FROM patients WHERE doctor_id IS NULL;
    IF orphan_count > 0 THEN
        RAISE EXCEPTION 'Refusing to continue: % patient(s) still have no '
                        'doctor_id.', orphan_count;
    END IF;

    ALTER TABLE patients
        ALTER COLUMN doctor_id SET NOT NULL;
END
$migration$;


-- ---------------------------------------------------------------------------
-- 3. Indexes
-- ---------------------------------------------------------------------------

-- Every patient list, search and count is now filtered by doctor first.
CREATE INDEX IF NOT EXISTS idx_patients_doctor
    ON patients (doctor_id);

-- Sorted listing: 'name' is the default sort on the patients screen.
CREATE INDEX IF NOT EXISTS idx_patients_doctor_name
    ON patients (doctor_id, first_name, last_name);

-- ---------------------------------------------------------------------------
-- The §8.10 fix.
--
-- idx_patients_identity (from 5_api_support.sql) indexes normalised name plus
-- date of birth with no doctor column, so the probable-duplicate lookup behind
-- it matched across every doctor in the database. Two doctors with a patient of
-- the same name and DOB — which is common, not hypothetical — made the second
-- registration a spurious 409, and the 409 body carried the *other* doctor's
-- patient_id and external_id. That was both a false conflict and a cross-tenant
-- disclosure of patient existence.
--
-- Replaced with a doctor-scoped index, so a soft conflict can only be raised
-- against the same doctor's own patients. Still advisory, still not a
-- constraint: demographics remain a bad key even within one practice (§8.10).
-- ---------------------------------------------------------------------------

DROP INDEX IF EXISTS idx_patients_identity;

CREATE INDEX IF NOT EXISTS idx_patients_doctor_identity
    ON patients (doctor_id,
                 lower(btrim(first_name)),
                 lower(btrim(coalesce(last_name, ''))),
                 date_of_birth);

-- Phone search is doctor-scoped too.
DROP INDEX IF EXISTS idx_patients_phone;

CREATE INDEX IF NOT EXISTS idx_patients_doctor_phone
    ON patients (doctor_id, phone)
    WHERE phone IS NOT NULL;

-- ---------------------------------------------------------------------------
-- external_id stays GLOBALLY unique across all doctors.
--
-- It is system-generated from one shared sequence, so collisions cannot happen
-- in practice and a global constraint costs nothing. Keeping it global also
-- means a clinic number is never ambiguous if the data is ever consolidated.
--
-- The disclosure risk is not in the constraint, it is in what the API says when
-- the constraint is hit: add_patient() used to return the offending patient's
-- id in the 409. Now scoped — see app/services/patient_service.py.
-- ---------------------------------------------------------------------------

-- Tenancy for visits/insights/follow-ups is derived through patients, so these
-- support the join rather than a doctor_id column.
CREATE INDEX IF NOT EXISTS idx_visits_patient_date
    ON visits (patient_id, visit_date DESC);


-- ---------------------------------------------------------------------------
-- 4. Verify
-- ---------------------------------------------------------------------------

DO $verify$
DECLARE
    doctors_count  INTEGER;
    patients_count INTEGER;
    untenanted     INTEGER;
BEGIN
    SELECT count(*) INTO doctors_count  FROM doctors;
    SELECT count(*) INTO patients_count FROM patients;
    SELECT count(*) INTO untenanted     FROM patients WHERE doctor_id IS NULL;

    RAISE NOTICE 'doctors=% patients=% untenanted=%',
                 doctors_count, patients_count, untenanted;

    IF untenanted > 0 THEN
        RAISE EXCEPTION 'Migration incomplete: % untenanted patient(s).', untenanted;
    END IF;
END
$verify$;
