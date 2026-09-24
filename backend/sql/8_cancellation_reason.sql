-- Migration: record *why* a visit was cancelled.
--
-- Run once against an existing database, AFTER 7_multi_tenancy.sql. Idempotent.
--
-- POST /visits/{id}/cancel has accepted a `reason` since the endpoint existed,
-- and threw it away: it reached the service, went into a log line, and stopped
-- there. The API advertised a field it did not store, so the answer to "why was
-- this consultation cancelled" lived only in whichever log file happened to
-- still exist.
--
-- Nullable with no default and no backfill, deliberately. Every visit cancelled
-- before this migration genuinely has no recorded reason, and inventing one --
-- even an empty string -- would make "not recorded" indistinguishable from
-- "cancelled without giving a reason". NULL says the first; '' would claim the
-- second.

ALTER TABLE visits ADD COLUMN IF NOT EXISTS cancelled_reason TEXT;

-- Verify
DO $verify$
DECLARE
    has_column BOOLEAN;
BEGIN
    SELECT EXISTS (SELECT 1 FROM information_schema.columns
                   WHERE table_name = 'visits'
                     AND column_name = 'cancelled_reason')
      INTO has_column;
    IF NOT has_column THEN
        RAISE EXCEPTION 'visits.cancelled_reason was not created.';
    END IF;
    RAISE NOTICE 'visits.cancelled_reason is present.';
END
$verify$;
