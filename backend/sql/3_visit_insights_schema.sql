-- One row per generated insight set for a visit.
-- Regenerating insights inserts a new row and flips the previous one's
-- is_current to FALSE, so history is preserved rather than overwritten.

CREATE TABLE visit_insights (
    insight_id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),

    visit_id            UUID NOT NULL
                        REFERENCES visits(visit_id)
                        ON DELETE CASCADE,

    -- Human-readable summary shown to doctor
    clinical_summary    TEXT,

    -- Key searchable fields
    chief_issue         TEXT,

    -- Sourced from the transcript's own `disease` field, not from the model.
    -- The prompts deliberately withhold the diagnosis so Layer 1 extracts only
    -- what was said on the call; this column carries the clinic's own label.
    primary_diagnosis   TEXT,

    -- Snapshot at the time of the visit. Age is denormalised on purpose:
    -- it is derived from date_of_birth as at visit_date, so it stays correct
    -- for historical visits instead of drifting as the patient gets older.
    patient_age         INTEGER,
    patient_gender      TEXT,

    -- Follow-up, extracted from the call by Layer 1 and resolved to calendar
    -- dates in Python. Split across columns because a single DATE cannot
    -- represent the three real cases: a fixed visit, a window ("three to four
    -- months"), and no scheduled visit at all ("come in if you feel unwell").
    follow_up_type      TEXT NOT NULL DEFAULT 'not_discussed'
                        CHECK (follow_up_type IN
                              ('scheduled', 'conditional_only', 'not_discussed')),

    -- Earliest actionable date. NULL when nothing was scheduled.
    follow_up_date      DATE,

    -- Equal to follow_up_date unless the doctor gave a range.
    follow_up_date_latest DATE,

    -- What the doctor actually said, kept for audit alongside the computed date.
    follow_up_stated    TEXT,

    -- TRUE when the timing was vague, hedged, or a range and a human should
    -- pick the actual appointment date.
    follow_up_needs_confirmation BOOLEAN NOT NULL DEFAULT FALSE,

    next_visit_focus    TEXT,

    -- Full pipeline output: {"layer_1": {...}, "layer_2": {...},
    --                        "layer_3": {...}, "follow_up": {...}}
    insights_json       JSONB NOT NULL,

    -- LLM metadata
    model_name          TEXT NOT NULL,
    model_version       TEXT,

    -- Short hash of the prompt templates used, so a row can be traced back to
    -- the exact prompts that produced it.
    prompt_version      TEXT,

    extraction_version  TEXT,

    -- Cost tracking
    input_tokens        INTEGER,
    output_tokens       INTEGER,
    total_tokens        INTEGER,

    -- created_at is when this visit's insights were first generated;
    -- generated_at is bumped to now() on every regeneration.
    generated_at        TIMESTAMPTZ NOT NULL DEFAULT now(),

    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),

    -- Vestigial. Regeneration updates the single row for a visit in place, so
    -- this can no longer be FALSE. Kept only so existing queries don't break.
    is_current          BOOLEAN NOT NULL DEFAULT TRUE,

    -- One insight row per visit. Also the conflict target for the loader's
    -- upsert: a second generation updates this row instead of adding another.
    CONSTRAINT visit_insights_visit_id_key UNIQUE (visit_id),

    CONSTRAINT follow_up_window_ordered
        CHECK (follow_up_date IS NULL
               OR follow_up_date_latest IS NULL
               OR follow_up_date_latest >= follow_up_date),

    -- A scheduled follow-up must carry a date; the other two types must not.
    CONSTRAINT follow_up_type_matches_date
        CHECK ((follow_up_type = 'scheduled' AND follow_up_date IS NOT NULL)
               OR (follow_up_type <> 'scheduled' AND follow_up_date IS NULL))
);

CREATE INDEX idx_visit_insights_visit
    ON visit_insights(visit_id);

CREATE INDEX idx_visit_insights_json
    ON visit_insights USING GIN(insights_json);

CREATE INDEX idx_visit_insights_diagnosis
    ON visit_insights(primary_diagnosis);

-- Partial: "who is due back, and when" only ever asks about live rows.
CREATE INDEX idx_visit_insights_followup
    ON visit_insights(follow_up_date)
    WHERE is_current AND follow_up_date IS NOT NULL;

-- Enforces the is_current flag: at most one live insight row per visit.
-- Without this, a failed regeneration can leave two rows both marked current.
CREATE UNIQUE INDEX idx_visit_insights_one_current
    ON visit_insights(visit_id)
    WHERE is_current;
