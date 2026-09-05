# Backend changes required for the frontend

**This document is an approved implementation brief. Implement it as written.**
Every decision in it has been signed off by the product owner; nothing here is
waiting on further approval. Where a choice is debatable the reasoning is given so
you can follow the intent, not so you can revisit the decision. If you find a
genuine conflict with the existing code, stop and report it rather than guessing.

Written against the codebase as of 2026-08-25. Every current-state claim was
verified by reading source; the LLM output shapes were verified against all 16
`visit_insights` rows in the `patient-recap` Neon project.

The goal is a doctor-facing UI with two queues — **Visit Schedule** and
**Follow-ups** — and an explicit visit lifecycle:

```
Schedule Visit  →  Complete Visit  →  Add Transcript (.txt)  →  Generate Insights
                                                                      ↓
                                          follow-up appears in Follow-ups queue
                                                                      ↓
                                    doctor calls patient → Schedule Visit
                                                                      ↓
                                              earlier follow-up marked resolved
```

Nothing here changes prompt content or the three-layer pipeline logic.

### Scope notes from the product owner

**Transcripts are `.txt` only from now on.** The 16 existing `.json` files were a
by-product of generating synthetic transcripts with Claude. They get converted to
`.txt`, archived, and the database paths updated (section 4). The loader becomes
single-format — no dual JSON/text branch.

**`disease` was only ever a prop for generating those dummy transcripts.** It must
not surface in the UI. So `primary_diagnosis` is dropped from API responses and
stops being populated (section 3).

**Bulk generation stays in the CLI.** `python -m cli.load_insights --all` remains
the batch tool; the UI needs only per-visit generation. This removes a jobs/activity
admin screen, job cancellation, and any bulk-select UI from scope.

---

## Ground rules

### Do not change

- **`backend/prompts/*.md`** — prompt wording, the layer JSON schemas they declare,
  and the order of the three layers.
- **The shape of `insights_json`.** The frontend carries hand-written types for
  `layer_1`, `layer_2`, `layer_3` and `follow_up` because Pydantic types that column
  as `Dict[str, Any]`, so OpenAPI generation produces an opaque object. Those keys
  were verified against all 16 rows. If a shape genuinely must change, report which
  key rather than changing it silently.
- **`app/domain/follow_up/resolver.py`** and the date arithmetic. It is
  unit-tested against every timing pattern in the corpus.
- **`app/pipeline/`** — layer sequencing, prompt rendering, parsing, retry logic.
- **The traversal guard in `resolve_within()`.** Its semantics stay exactly as they
  are; there is no auth in front of this API. Extending it is fine, weakening it is
  not.
- **`backend/legacy/`** — dead code, nothing in `app/` imports it. Leave it alone.
- **`visit_insights.primary_diagnosis` as a column.** Stop writing it and stop
  serialising it, but do not drop the column or null out existing values (section 3).

### Environment and commands

Python 3.11, conda env `reels`, all commands from `backend/`.

```bash
conda activate reels
cd backend

# apply the new migration (numbered files run in order; 1–5 already applied)
psql "$CONNSTR" -f sql/6_frontend_support.sql

# tests: 107 currently pass, all offline — no API calls, no database
python -m unittest discover -s tests -t .

# run the API
uvicorn app.main:app --reload      # docs at http://127.0.0.1:8000/docs
```

`CONNSTR` and the Gemini keys live in `backend/.env`. Do not commit `.env`, and do
not print key values in logs or test output.

### Code conventions to follow

The existing layering is deliberate; new modules must match it.

- **Repositories** (`app/db/repositories/`) hold SQL and return dataclass rows
  defined in `app/db/models.py`. No business rules, no HTTP concepts.
- **Services** (`app/services/`) hold rules and raise `AppError` subclasses from
  `app/core/errors.py`. They never import anything HTTP, because the CLI calls the
  same functions.
- **Routers** (`app/api/v1/`) validate, delegate, and serialise. They do **not**
  translate errors — `app/api/errors.py` maps every `AppError` to a status code in
  one place. Adding a `try/except` in a router to build a JSON error is wrong.
- **Schemas** (`app/schemas/`) are the wire contract; `model_config =
  ConfigDict(from_attributes=True)` on response models so they validate from
  dataclass rows.
- The pool sets `row_factory=dict_row`, so `cur.fetchone()` returns a dict. New
  queries rely on that.
- **`autocommit=True` is load-bearing.** Read the docstring in `app/db/pool.py`
  before touching transactions: with autocommit on, `with conn.transaction()`
  issues a real BEGIN/COMMIT, and a *nested* `conn.transaction()` degrades to a
  SAVEPOINT. Where this brief says "same transaction", put the statement inside the
  existing `with conn.transaction():` block rather than opening a new one.

Add new error codes to `app/core/errors.py` and map them in `app/api/errors.py`.
The response shape is always `{"code", "message", "details"}`.

---

## Contents

1. [Decisions already made](#1-decisions-already-made)
2. [Database migration](#2-database-migration)
3. [Dropping `primary_diagnosis` from the UI surface](#3-dropping-primary_diagnosis-from-the-ui-surface)
4. [Transcripts: JSON to .txt](#4-transcripts-json-to-txt)
5. [Visit lifecycle endpoints](#5-visit-lifecycle-endpoints)
6. [Follow-up tracking](#6-follow-up-tracking)
7. [Changes to existing endpoints](#7-changes-to-existing-endpoints)
8. [New read endpoints](#8-new-read-endpoints)
9. [Auth and CORS](#9-auth-and-cors)
10. [Backfill for existing data](#10-backfill-for-existing-data)
11. [File-by-file change list](#11-file-by-file-change-list)
12. [Build order](#12-build-order)
13. [Acceptance checks](#13-acceptance-checks)

---

## 1. Decisions already made

Signed off by the product owner. Recorded here because each one changes behaviour
and the reasoning is not obvious from the code. Implement as stated.

**1.1 With `primary_diagnosis` gone, `chief_issue` becomes the condition
descriptor everywhere.** It is a full sentence from Layer 3, not a label — for
example *"GERD and upper stomach burning significantly improved over 2 weeks…"*.
In queue rows and timelines the UI will truncate it to a single line with the full
text available on the visit brief. That is arguably better than a fixed label
because it is derived from what was actually said, but it does mean there is **no
structured field to group or filter patients by condition**. If you want that
later it needs a real clinical-coding decision, not a free-text column.

**1.2 Transcript upload requires `visit_status = 'completed'`.** You described
Complete then Add Transcript in that order, so the upload endpoint rejects
`scheduled` and `cancelled` visits with 409. This is what makes the Add Transcript
button legitimately disabled until the visit is completed.

**1.3 Attaching a transcript no longer flips the visit to `completed`.**
`visit_service.update_visit` currently forces `visit_status = 'completed'` as a
side effect of setting `transcript_path`. That implicit transition contradicts an
explicit Complete button, so it is removed. **This is a behaviour change to an
existing endpoint**, and `tests/test_api.py::TestAttachTranscript::test_patch_attaches_transcript_and_completes_the_visit`
asserts the old behaviour and must be updated.

One thing I am *not* proposing: auto-generating insights on upload. Quota is 20
requests per day per key and each visit costs 3, so generation stays an explicit
button. Say the word if you want it automatic.

Also worth noting, since it is a genuine limitation rather than a decision:
**regeneration overwrites the previous brief.** `visit_insights` is upserted in
place and the SQL comments already mark `is_current` as vestigial, so there is no
history. Not proposing a fix here; flagging it so the UI can warn before
regenerating.

---

## 2. Database migration

New file: `backend/sql/6_frontend_support.sql`. Idempotent, safe against the
existing database.

```sql
-- Migration: support the doctor-facing frontend.
--
--   1. explicit visit lifecycle timestamps
--   2. follow_ups: a tracked, resolvable follow-up per visit
--
-- Run once against an existing database.

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
-- server-generated from visit_id; see section 4.
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
-- Regeneration upserts visit_insights in place (see the DO UPDATE list in
-- repositories/insights.py), so resolution state stored there would be silently
-- overwritten by a rerun. A follow-up's lifecycle belongs to the clinic, not to
-- the model.
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
```

No `visits.primary_diagnosis` column — see section 3.

---

## 3. Dropping `primary_diagnosis` from the UI surface

`disease` exists only as a prop for the synthetic transcripts, so it must not reach
the doctor.

**Keep** the `visit_insights.primary_diagnosis` column and its 16 existing values.
Dropping a populated column to hide a field is destructive for no gain, and the
archived JSON keeps the same information anyway.

**Stop populating it.** In `insight_service.generate_for_visit`, replace:

```python
row = build_insight_row(visit, result, primary_diagnosis=transcript.disease)
```

with `primary_diagnosis=None`. New rows carry NULL. Keep the `build_insight_row`
parameter so the CLI could pass a label later if the clinic ever wants one.

**Remove it from the API.** Delete `primary_diagnosis` from `InsightOut`
(`app/schemas/insights.py`). Do not add it to `VisitOut`, `FollowUpOut`, or the
timeline summary. Removing it from the schema is a stronger guarantee than asking
the frontend not to render it — it cannot leak through a generated type.

`InsightRow` and the repository `_COLUMNS` can keep it; it just stops being
serialised.

---

## 4. Transcripts: JSON to .txt

### Current state

`domain/transcripts.py::load_transcript` requires a JSON **object** with a
non-empty `transcript` key, and reads `patient_name` and `disease` from it. All 16
files are uniform: keys `disease`, `patient_name`, `transcript`, with transcript
text between 3.4 KB and 4.9 KB.

Two useful findings that make this cheaper than expected:

`transcript.patient_name` is **not used by the app pipeline**.
`pipeline/runner.py` renders `patient_name` from `PatientContext.name`, which
`insight_service.patient_context()` builds from `visit.first_name` in the
database. The only consumer is `cli/render_prompt.py`, as a fallback for `--name`.

`transcript.disease` has exactly **one** consumer, the `primary_diagnosis`
assignment removed in section 3.

So both fields disappear with almost no blast radius.

### 4.1 One-off conversion — do this first

New script: `backend/cli/convert_transcripts.py`.

```
python -m cli.convert_transcripts --dry-run
python -m cli.convert_transcripts
python -m cli.convert_transcripts --delete-originals
```

It must read the JSON **directly with `json.load`, not through
`load_transcript`**, so it does not depend on whether the loader change has
shipped yet. That keeps the ordering safe: run the script, then deploy the
`.txt`-only loader.

Per visit whose `transcript_path` ends in `.json`:

1. Resolve the path with the existing `resolve_within` guard. **If the file is not
   in `transcripts/`, fall back to `archive/transcripts_json/<same name>`.** This
   makes the script re-runnable on a fresh clone, where the `.txt` files are
   gitignored and only the archived JSON is present.
2. `json.load`, take `transcript`, `.strip()`. Empty or missing → report and skip,
   do not write.
3. Write `backend/transcripts/{visit_id}.txt` as UTF-8 with `\n` newlines.
4. Copy the original to `backend/archive/transcripts_json/{original_name}`.
5. Update the visit: `transcript_path = 'transcripts/{visit_id}.txt'`,
   `transcript_filename = '{original_stem}.txt'`,
   `transcript_uploaded_at = now()`, `transcript_provider = 'local'`.
6. Verify by re-reading the new file and comparing character count to the source
   string.

Only if every row verified, and only with `--delete-originals`, remove the
originals from `transcripts/`. Default is to leave them in place — the archive copy
plus a manual check is cheap insurance, and nothing reads `.json` after step 5.

The whole thing runs in one transaction per visit so an interrupt cannot leave a
row pointing at a file that was never written.

Expected: 16 converted, 0 skipped. Verify with:

```sql
SELECT count(*) FILTER (WHERE transcript_path LIKE '%.json') AS still_json,
       count(*) FILTER (WHERE transcript_path LIKE '%.txt')  AS txt
FROM visits;
-- expect still_json = 0, txt = 16
```

**On the archive:** these transcripts are synthetic, so keeping them in git is
fine and useful for reseeding. Real uploaded transcripts are patient data and must
never be committed — hence `backend/transcripts/*.txt` in `.gitignore` while
`backend/archive/transcripts_json/` stays tracked. A fresh clone can rebuild the
16 `.txt` files by re-running the script.

### 4.2 Storage convention

One rule, no subdirectories:

```
backend/transcripts/{visit_id}.txt
```

stored on the visit as `transcripts/{visit_id}.txt`. Converted historical files and
new uploads follow the identical convention, so replace, read and delete need no
special cases. `resolve_within` already permits this unchanged. The doctor's own
filename lives in `visits.transcript_filename` for display only and never touches
the filesystem.

### 4.3 Simplified loader

`Transcript` loses two fields:

```python
@dataclass
class Transcript:
    path: Path
    relative_path: str
    text: str
```

`load_transcript(path, allowed_dir)` keeps its signature and becomes text-only:

```python
def load_transcript(path, allowed_dir) -> Transcript:
    resolved = resolve_within(path, allowed_dir)     # unchanged traversal guard
    if not resolved.is_file():
        raise TranscriptError(f"Transcript not found: {path}", ...)
    if resolved.suffix.lower() != ".txt":
        raise TranscriptError(
            f"{resolved.name}: transcripts must be .txt. "
            f"Run cli.convert_transcripts if this is legacy seed data.")

    text = _read_text(resolved).strip()
    if not text:
        raise TranscriptError(f"{resolved.name}: file is empty")

    return Transcript(path=resolved,
                      relative_path=normalise_path(resolved,
                                                   allowed_dir.resolve().parent),
                      text=text)


def _read_text(path: Path) -> str:
    """Decode a transcript file: utf-8, then utf-8-sig for Windows BOM files.

    Anything else is rejected rather than mangled with errors='replace'. A
    corrupted transcript would reach the model and produce a plausible-looking
    but wrong clinical record.
    """
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise TranscriptError(f"{path.name}: could not be read ({exc})") from exc

    for encoding in ("utf-8", "utf-8-sig"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue

    raise TranscriptError(
        f"{path.name}: not valid UTF-8. Re-save the file as UTF-8 plain text.")
```

Reuse this same helper for the upload endpoint's decode check (section 4.4) so the
API and the loader cannot disagree about what counts as a readable transcript.

The error message names the fix, because a stale `.json` path is the one way this
fails after migration.

`cli/render_prompt.py` must stop using `transcript.patient_name` — make `--name`
required, or default to `"Unknown"`.

**Do not change the prompts or the pipeline.** `run_pipeline` receives
`transcript.text`, and a `.txt` transcript produces the same string a JSON one did.

### 4.4 `POST /api/v1/visits/{visit_id}/transcript`

`multipart/form-data` with a single `file` field, plus optional `replace`
(default `false`, required to overwrite).

`201` → `VisitOut`.

Validation, all server-side:

- Extension must be `.txt`, case-insensitive. Reject `.json`, `.docx`, `.pdf` with
  `422 invalid_request` — silently mis-parsing a Word file is worse than a clear
  rejection.
- Size ceiling **256 KB**. Real transcripts are 3–5 KB, so this is generous while
  still fitting the model context. Over it, `413` `transcript_too_large`. A 5 MB
  paste would otherwise fail an hour later as an opaque pipeline error.
- Must decode as UTF-8 (then `utf-8-sig` for Windows BOM files), else `422`.
- Non-empty after `.strip()`.

Writes to `transcripts/{visit_id}.txt` — **never the client filename**, which
reaches the filesystem. Sets `transcript_path`, `transcript_provider = 'local'`,
`transcript_filename`, `transcript_uploaded_at`.

| Status | `code` | When |
|---|---|---|
| 404 | `not_found` | unknown visit |
| 409 | `conflict` | `visit_status` is not `completed` (decision 1.2) |
| 409 | `conflict` | transcript attached and `replace` not `true` |
| 413 | `transcript_too_large` | over 256 KB |
| 422 | `invalid_request` | wrong extension, not UTF-8, or empty |

Requires `python-multipart` for `UploadFile`; add to `requirements.txt` if absent.

### 4.5 `GET /api/v1/visits/{visit_id}/transcript`

Returns the text so the doctor can check a brief against its source. Without it
there is no way to verify the extraction, which undercuts the premise.

```json
{
  "visit_id": "uuid",
  "transcript_path": "transcripts/uuid.txt",
  "filename": "vikas_2.txt",
  "text": "Doctor: How have you been since we last spoke?\nPatient: ...",
  "character_count": 3440,
  "uploaded_at": "2026-08-25T09:12:00Z"
}
```

`404` `not_found` when the visit is unknown **or** has no transcript. No `format`
field — there is only one format now.

### 4.6 `DELETE /api/v1/visits/{visit_id}/transcript`

Deletes the file and clears the transcript columns. `409` if insights exist —
removing the source of a generated brief would leave an unauditable record. `204`
on success.

---

## 5. Visit lifecycle endpoints

### `POST /api/v1/visits/{visit_id}/complete`

The Complete Visit button. Body optional:
`{"visit_date": "2026-08-25", "visit_time": "11:00:00"}` to correct the actual
date/time at the same moment.

`200` → `VisitOut`. Sets `visit_status = 'completed'`, `completed_at = now()`.

Completing an already-completed visit is **idempotent** — `200`, `completed_at`
unchanged. The UI should not have to guard a double click.

| Status | `code` | When |
|---|---|---|
| 404 | `not_found` | unknown visit |
| 409 | `conflict` | visit is `cancelled` |

### `POST /api/v1/visits/{visit_id}/cancel`

Body: `{"reason": "patient rescheduled"}`, optional.

`200` → `VisitOut`. Sets `visit_status = 'cancelled'`, `cancelled_at = now()`.

**Must reopen follow-ups this visit resolved.** If the cancelled visit is the
`resolved_by_visit_id` of any follow-up, those rows return to `open` with
`resolved_at`, `resolved_reason` and `resolved_by_visit_id` cleared, in the same
transaction. Otherwise cancelling a visit silently loses a follow-up.

| Status | `code` | When |
|---|---|---|
| 404 | `not_found` | unknown visit |
| 409 | `conflict` | visit has insights generated |

---

## 6. Follow-up tracking

### Writing the row

In `insight_service.generate_for_visit`, right after `insights_repo.save(conn, row)`
and **inside the same transaction**:

- `follow_up_type == 'scheduled'` → upsert on `source_visit_id` with
  `due_earliest`, `due_latest`, `needs_confirmation`, `stated`, `purpose`.
- Any other type → delete any existing row for that visit, so a regeneration that
  no longer finds a follow-up does not leave a stale one in the queue.

**Preserve `status` on regeneration.** Update only dates and text; never reset
`resolved` or `dismissed` back to `open`. The doctor's decision outranks a model
rerun. Exclude `status`, `resolved_*`, `dismissed_*`, `last_contacted_at` and
`note` from the `DO UPDATE` list.

`insights_repo.save` already opens `conn.transaction()`, so the follow-up upsert
belongs inside that block. The invariant: insights and follow-up land together or
not at all.

Only `scheduled` follow-ups enter the queue. `conditional_only` has no date and
cannot be sorted into a due-date list — its return triggers render on the visit
brief instead. `not_discussed` produces nothing. All 16 existing rows are
`scheduled`, so the other two paths have no real data behind them yet.

### Automatic resolution

In `visit_service.schedule_visit`, after inserting visit `V` for patient `P` on
date `D`, same transaction:

```sql
UPDATE follow_ups f
SET status               = 'resolved',
    resolved_by_visit_id = %(new_visit_id)s,
    resolved_reason      = 'visit_scheduled',
    resolved_at          = now(),
    updated_at           = now()
FROM visits sv
WHERE f.source_visit_id = sv.visit_id
  AND f.patient_id      = %(patient_id)s
  AND f.status          = 'open'
  AND sv.visit_id      <> %(new_visit_id)s
  AND sv.visit_date    <= %(new_visit_date)s
RETURNING f.follow_up_id;
```

Keyed on the **source visit's date**, not the due date, which is what you asked
for: any open follow-up from an earlier visit is closed by scheduling the next one,
whether or not the new date matches what was suggested. `<=` covers same-day
booking. Log the returned ids.

### `GET /api/v1/follow-ups`

The Follow-ups tab.

| Param | Type | Default | Notes |
|---|---|---|---|
| `status` | enum | `open` | `open` \| `resolved` \| `dismissed` \| `all` |
| `due_before` | date | – | inclusive, on `due_earliest` |
| `due_after` | date | – | inclusive |
| `needs_confirmation` | bool | – | |
| `patient_id` | uuid | – | |
| `sort` | enum | `due_asc` | `due_asc` \| `due_desc` |
| `limit` | int | 100 | 1–500 |
| `offset` | int | 0 | |

`200` → array of `FollowUpOut`, everything a queue row needs in one request:

```json
[
  {
    "follow_up_id": "uuid",
    "status": "open",
    "patient_id": "uuid",
    "external_id": "P-2026-00003",
    "patient_name": "Vikas Gupta",
    "phone": "+919876543210",
    "patient_age": 27,
    "patient_gender": "male",

    "source_visit_id": "uuid",
    "source_visit_number": 2,
    "source_visit_date": "2026-08-07",

    "due_earliest": "2026-08-28",
    "due_latest": "2026-08-28",
    "needs_confirmation": false,
    "stated": "Come back for a review in three weeks.",
    "purpose": "monitor reflux response",

    "chief_issue": "GERD and upper stomach burning significantly improved over 2 weeks...",
    "next_visit_focus": "Confirm sustained relief and taper plan.",

    "resolved_by_visit_id": null,
    "resolved_reason": null,
    "resolved_at": null,
    "dismissed_reason": null,
    "last_contacted_at": null,
    "note": null,
    "created_at": "2026-08-07T12:00:00Z",
    "updated_at": "2026-08-07T12:00:00Z"
  }
]
```

`phone` is included because the action on this screen is to telephone the patient.
Return `X-Total-Count`.

Deliberately **not** included: any `days_overdue` or `is_overdue` field. "Overdue"
depends on today, and a server-computed value goes stale in a cached response. The
frontend derives it from `due_earliest`.

### `GET /api/v1/follow-ups/{follow_up_id}`

`200` → `FollowUpOut`. `404` `not_found`.

### `PATCH /api/v1/follow-ups/{follow_up_id}`

The outcome of the phone call. At least one field required.

```json
{
  "status": "dismissed",
  "dismissed_reason": "patient moved city, transferring care",
  "note": "Called 25 Aug, will send records.",
  "mark_contacted": true
}
```

- `status` accepts `dismissed` or `open` only. **`resolved` is rejected with
  `422`** — resolution is caused by scheduling a visit, and letting a client set it
  directly would create resolved rows with no visit behind them.
- `dismissed_reason` required when moving to `dismissed`.
- `open` reopens a dismissed row, clearing the dismissal fields.
- `mark_contacted: true` sets `last_contacted_at = now()` without touching status,
  so "I called, they're thinking about it" is expressible.

`200` → `FollowUpOut`. `404` `not_found`; `422` `invalid_request`.

---

## 7. Changes to existing endpoints

### `GET /api/v1/visits` — filters, sort, patient identity

Three problems today: it sorts by `p.first_name, v.visit_number` so there is no
chronological view; it cannot filter by status, making the Visit Schedule tab
impossible; and `VisitOut` omits the patient's name even though `VisitRow` already
carries `first_name`, `last_name` and `gender` from the existing join.

Add:

| Param | Type | Notes |
|---|---|---|
| `status` | enum, repeatable | `scheduled` \| `in_progress` \| `completed` \| `cancelled` |
| `from` | date | inclusive on `visit_date` |
| `to` | date | inclusive |
| `has_transcript` | bool | |
| `sort` | enum | `date_desc` (**new default**), `date_asc`, `patient` (current behaviour) |
| `offset` | int | |

Changing the default sort is a behaviour change. `cli/load_insights.py` calls
`list_visits` for selection and does not depend on ordering, so it is safe, but
confirm before shipping.

Add to `VisitOut`:

```json
{
  "patient_name": "Vikas Gupta",
  "first_name": "Vikas",
  "last_name": "Gupta",
  "external_id": "P-2026-00003",
  "gender": "male",
  "transcript_filename": "vikas_2.txt",
  "completed_at": "2026-08-07T11:40:00Z",
  "cancelled_at": null,
  "has_insights": true
}
```

`external_id` needs adding to `_SELECT` in `repositories/visits.py`; the name and
gender fields are already selected and only need exposing. `has_insights` mirrors
`insights_generated` under a name that reads correctly in a UI — keep the original
field too so nothing breaks.

### `PATCH /api/v1/visits/{visit_id}` — drop the implicit completion

Remove `changes["visit_status"] = visit_status or "completed"` from
`visit_service.update_visit` (decision 1.3). Status changes go through `/complete`
and `/cancel`.

### `GET /api/v1/patients` — search and count

Add `q`: case-insensitive match on `first_name`, `last_name`, `external_id`,
`phone`. Add `sort`: `name` (default), `created_desc`.

Return `X-Total-Count` rather than wrapping the array in an envelope — the header
is additive, while changing the body shape would break the CLI and existing tests.

Add to `PatientOut`, computed in the existing query:

```json
{ "visit_count": 2, "last_visit_date": "2026-08-07", "age": 27 }
```

`age` as at today, distinct from `age_at_visit`. Keeps one definition of the
arithmetic instead of three in the frontend.

### `PATCH /api/v1/patients/{patient_id}`

Demographics currently cannot be corrected, and a wrong `date_of_birth` silently
corrupts `age_at_visit` on every visit. Accepts `first_name`, `last_name`,
`date_of_birth`, `gender`, `phone`, `email`; at least one required. `200` →
`PatientOut`; `404`; `422`. `external_id` is not editable — it is the identity key.

### `POST /api/v1/visits/{visit_id}/insights` — poll URL

`poll_url` and `insights_url` are built as `f"{prefix}/jobs/..."` and already
contain `/api/v1`. A client whose base URL ends in `/api/v1` and joins them
produces `/api/v1/api/v1/jobs/...`. Either return them root-relative and document
that, or return absolute URLs. The frontend will build poll URLs from `job_id`
regardless, so documenting it is enough.

No job-cancel endpoint: bulk work lives in the CLI, and the pipeline has no
mid-flight abort, so a cancel button would be a lie.

---

## 8. New read endpoints

### `GET /api/v1/patients/{patient_id}/timeline`

One request for the whole patient record. Without it, rendering Riya's six visits
takes seven requests.

```json
{
  "patient": { "...PatientOut..." },
  "visits": [
    {
      "...VisitOut...": "...",
      "insight": {
        "insight_id": "uuid",
        "chief_issue": "...",
        "next_visit_focus": "...",
        "follow_up_type": "scheduled",
        "follow_up_date": "2026-10-06",
        "follow_up_date_latest": "2026-10-06",
        "follow_up_needs_confirmation": true,
        "generated_at": "2026-08-22T21:10:00Z"
      },
      "follow_up": {
        "follow_up_id": "uuid",
        "status": "open",
        "due_earliest": "2026-10-06",
        "resolved_by_visit_id": null
      }
    }
  ]
}
```

Visits newest first. `insight` and `follow_up` are `null` when absent. Summary
fields only — **not** `insights_json`, which is 20–30 KB per visit and would make a
six-visit payload 200 KB. The full payload stays on `GET /visits/{id}/insights`.

### `GET /api/v1/visits/{visit_id}/job`

The latest job for a visit, so a reload during generation can resume polling.
`jobs_repo.latest_for_visit()` already exists and is simply not exposed.

`200` → `JobOut`, or `204` when the visit has never had a job. `204` rather than
`404` so the frontend can tell "no job yet" from "unknown visit".

### `GET /api/v1/stats`

Counts for the home screen in one request.

```json
{
  "patients": 6,
  "visits": { "total": 16, "scheduled": 0, "completed": 16, "cancelled": 0,
              "with_insights": 16, "awaiting_transcript": 0,
              "awaiting_insights": 0 },
  "follow_ups": { "open": 5, "overdue": 4, "due_next_7_days": 1,
                  "needs_confirmation": 1, "resolved": 11, "dismissed": 0 },
  "as_of": "2026-08-25"
}
```

`overdue` and `due_next_7_days` are relative to the server's date, so `as_of` is
returned for honest labelling. `visits.counts()` already does part of this.

`awaiting_transcript` (completed, no transcript) and `awaiting_insights`
(transcript, no insights) are the two states that need doctor action.

Keep `GET /health/api-keys` out of anything the UI polls — **each call spends one
request from every key's daily quota.**

---

## 9. Auth and CORS

`deps.py` documents this already: `require_auth` is a deliberate no-op, and
`generate_insights` both writes clinical records and spends money on LLM calls.

**A shared-secret header** suffices for a single practice. Read `API_KEY` from
settings; when set, require `X-API-Key` on every route except `GET /health`,
returning `401` `unauthorized` on mismatch. When unset, stay open so local
development is unchanged. Implement inside `require_auth` — the module docstring
already nominates that spot.

**CORS** is `["*"]`. Set `CORS_ORIGINS=http://localhost:3000` for development and
the real origin in production. `*` cannot be combined with credentialed requests,
so fix this before adding the key rather than after.

I'll build the frontend to send `X-API-Key` from a server-side env var if you
enable it. The key must never reach the browser bundle, so generation calls get
proxied through a Next.js route handler rather than called from the client.

---

## 10. Backfill for existing data

All 16 visits have insights and no `follow_ups` rows. Append to the migration,
**after** running the transcript conversion:

```sql
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
```

**I ran the resolution rule against the live database as a read-only query. It
produces exactly 11 resolved and 5 open** — one open per patient's most recent
visit:

| Patient | Source visit | Due | State on 2026-08-25 |
|---|---|---|---|
| Kavita Reddy | 3 | 2026-07-21 | open, 35 days overdue |
| Manish Patel | 3 | 2026-07-29 | open, 27 days overdue |
| Jayesh Shah | 2 | 2026-08-06 | open, 19 days overdue |
| Vikas Gupta | 2 | 2026-08-28 | open, due in 3 days |
| Riya Sharma | 6 | 2026-10-06 | open, needs confirmation |

That is a genuinely useful Follow-ups tab on day one: four overdue, one imminent,
one flagged for human judgement. Jay Patel (`P-2026-00010`, zero visits) exercises
the empty state, and the Visit Schedule tab starts empty since every seeded visit
is `completed` — worth knowing so the empty state gets designed rather than
discovered.

Verify:

```sql
SELECT status, count(*) FROM follow_ups GROUP BY status;
```

---

## 11. File-by-file change list

**New**

| Path | Purpose |
|---|---|
| `sql/6_frontend_support.sql` | migration + backfill |
| `cli/convert_transcripts.py` | one-off JSON → .txt conversion |
| `app/api/v1/follow_ups.py` | follow-up router |
| `app/db/repositories/follow_ups.py` | queries, upsert, resolve, reopen |
| `app/schemas/follow_ups.py` | `FollowUpOut`, `FollowUpUpdate` |
| `app/services/follow_up_service.py` | lifecycle rules |
| `app/services/transcript_service.py` | validate, store, read, delete uploads |
| `app/schemas/stats.py` | `StatsOut` |

**Modified**

| Path | Change |
|---|---|
| `app/domain/transcripts.py` | `.txt` only; drop `patient_name`/`disease` from `Transcript` |
| `app/services/insight_service.py` | `primary_diagnosis=None`; follow-up upsert in the save transaction |
| `app/services/visit_service.py` | `complete`, `cancel`; resolve follow-ups on schedule; drop implicit completion |
| `app/db/repositories/visits.py` | `external_id` in `_SELECT`; status/date/sort filters; offset; extend `counts()` |
| `app/db/repositories/patients.py` | `q` search, total count, `visit_count`, `last_visit_date` |
| `app/db/repositories/insights.py` | let the follow-up upsert join the existing transaction |
| `app/db/models.py` | new `VisitRow`/`PatientRow` fields; add `FollowUpRow` |
| `app/schemas/visits.py` | patient identity + lifecycle fields on `VisitOut` |
| `app/schemas/patients.py` | `PatientUpdate`; derived fields on `PatientOut` |
| `app/schemas/insights.py` | **remove `primary_diagnosis` from `InsightOut`** |
| `app/api/v1/visits.py` | `/complete`, `/cancel`, `/transcript` ×3, `/job`; list filters |
| `app/api/v1/patients.py` | `PATCH`; `q`; `X-Total-Count`; `/timeline` |
| `app/api/v1/router.py` | register follow-ups, stats |
| `app/api/deps.py` | implement `require_auth` behind an optional `API_KEY` |
| `app/config.py` | `api_key`, `max_transcript_bytes` |
| `cli/render_prompt.py` | stop using `transcript.patient_name` |
| `cli/load_insights.py` | docstring examples reference `.txt` |
| `.env.example` | `API_KEY`, `CORS_ORIGINS` |
| `.gitignore` | `backend/transcripts/*.txt` |
| `requirements.txt` | `python-multipart` if absent |

**Tests to update** — all current tests mock the service layer, so none load a real
transcript file. Three concrete spots:

| Path | Change |
|---|---|
| `tests/test_follow_up.py` ~L320 | `Transcript(...)` drops `patient_name`/`disease`; path → `.txt` |
| `tests/test_api.py` `TestAttachTranscript` | asserts the implicit completion being removed |
| `tests/test_api.py` `test_schema_lists_every_endpoint` | add the new routes |

`backend/legacy/` still contains the old JSON-based loader. It is dead code and
nothing imports it from `app/`; leaving it untouched is fine, but it should not be
mistaken for the live path.

---

## 12. Build order

**Phase 0 — data migration, before anything else**

1. `cli/convert_transcripts.py`, run it, verify 16 `.txt` and 0 `.json` in the DB
2. `sql/6_frontend_support.sql` including the backfill; verify 11 resolved / 5 open

**Phase 1 — the frontend cannot be built without these**

3. `.txt`-only loader; `primary_diagnosis` dropped from `InsightOut`
4. `POST/GET/DELETE /visits/{id}/transcript`
5. `POST /visits/{id}/complete`, `POST /visits/{id}/cancel`
6. `follow_ups` write path in `insight_service` + auto-resolve in `schedule_visit`
7. `GET /follow-ups`, `PATCH /follow-ups/{id}`
8. `GET /visits` status/date filters, `date_desc` default, patient identity on `VisitOut`

**Phase 2 — works without them, but with extra round-trips and no search**

9. `GET /patients/{id}/timeline`
10. `GET /visits/{id}/job`
11. `GET /stats`
12. `GET /patients` `q` + `X-Total-Count` + derived fields
13. `PATCH /patients/{id}`

**Phase 3 — before this leaves localhost**

14. `require_auth` behind `API_KEY`
15. `CORS_ORIGINS` narrowed

I can start the frontend as soon as Phase 1 is merged, and build against Phase 2
using the extra round-trips until those land.

---

## 13. Acceptance checks

**Conversion**

- `transcripts/` holds 16 `.txt` files named by `visit_id`
- `SELECT count(*) FROM visits WHERE transcript_path LIKE '%.json'` → 0
- `backend/archive/transcripts_json/` holds all 16 originals
- Character count of each `.txt` equals the source JSON's `transcript` length
- **`python -m cli.load_insights --visit <id> --regenerate` succeeds on a converted
  visit** — the regression that matters most
- `python -m cli.load_insights --list` runs clean

**Migration**

- `SELECT status, count(*) FROM follow_ups GROUP BY status` → `open: 5`, `resolved: 11`
- No `open` row whose patient has a later visit

**Lifecycle**

- Schedule a visit → `visit_status = 'scheduled'`, `visit_number` auto-assigned
- Upload to a `scheduled` visit → `409`; complete it, upload again → `201`
- Complete twice → `200` both times, `completed_at` unchanged
- Cancel a visit holding `resolved_by_visit_id` → that follow-up returns to `open`

**Transcript**

- `.txt` under 256 KB → `201`, file at `transcripts/{visit_id}.txt`
- `.json` → `422`; 300 KB → `413`; UTF-16 → `422`; whitespace-only → `422`
- Filename `../../etc/passwd` → stored path still `transcripts/{visit_id}.txt`,
  nothing written outside the directory
- Re-upload without `replace` → `409`; with `replace: true` → `201`
- `DELETE` on a visit with insights → `409`

**Follow-ups**

- Generate insights on a visit with a scheduled follow-up → exactly one `open` row
- Schedule a later visit → `resolved`, `resolved_reason: 'visit_scheduled'`
- Regenerate the source visit → status stays `resolved`
- `PATCH` with `status: "resolved"` → `422`
- `PATCH` with `mark_contacted: true` → `last_contacted_at` set, status still `open`
- `GET /follow-ups?status=open&sort=due_asc` → Kavita, Manish, Jayesh, Vikas, Riya

**Contract**

- `GET /visits` default order is `visit_date` descending
- Every `VisitOut` carries `patient_name` and `external_id`
- **No response anywhere contains `primary_diagnosis`** —
  `curl -s localhost:8000/openapi.json | grep -c primary_diagnosis` → 0
- `GET /patients` returns `X-Total-Count: 6`
- `GET /openapi.json` is valid and lists every new route

One request: **please don't change the `insights_json` layer shapes.** I verified
the current keys against all 16 rows, and the frontend will carry hand-written
types for them because Pydantic types that field as `Dict[str, Any]`, so OpenAPI
generation produces an opaque object. If a layer's shape does change, tell me which
key and I'll update the schemas.
