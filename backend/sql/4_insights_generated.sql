-- Migration: track insight generation on the visit, and make visit_insights
-- one row per visit so regeneration updates in place.
--
-- Run once against an existing database. Files 2 and 3 have been updated to
-- match, so a fresh setup produces this shape without needing this migration.

-- ---------------------------------------------------------------------------
-- 1. The flag lives on visits, so "what still needs processing" is answerable
--    without touching visit_insights at all.
--
--    It is only set TRUE after all three layers succeed AND the insight row is
--    written, both inside one transaction. A failure at any layer therefore
--    leaves it FALSE, which is what makes a partially-generated visit visible.
-- ---------------------------------------------------------------------------

ALTER TABLE visits
    ADD COLUMN IF NOT EXISTS insights_generated BOOLEAN NOT NULL DEFAULT FALSE;

-- Backfill for visits processed before this column existed.
UPDATE visits v
SET insights_generated = TRUE
WHERE EXISTS (
    SELECT 1 FROM visit_insights i WHERE i.visit_id = v.visit_id
);

-- Finding pending visits is the loader's hot path.
CREATE INDEX IF NOT EXISTS idx_visits_pending_insights
    ON visits(visit_id)
    WHERE NOT insights_generated;

-- ---------------------------------------------------------------------------
-- 2. One insight row per visit. Regenerating updates the existing row rather
--    than inserting a second one, so this is now a hard constraint and also
--    the conflict target for the loader's upsert.
-- ---------------------------------------------------------------------------

ALTER TABLE visit_insights
    ADD CONSTRAINT visit_insights_visit_id_key UNIQUE (visit_id);

-- Fully subsumed by the UNIQUE constraint above.
DROP INDEX IF EXISTS idx_visit_insights_one_current;
