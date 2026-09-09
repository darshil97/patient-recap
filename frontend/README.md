# Frontend

Not built yet. This file is the contract to build against, so a session working
here needs no knowledge of the Python backend beyond what is below.

## Suggested layout

Framework is an open choice. Whatever it is, keep this shape so the boundary
stays obvious:

```
frontend/
├── src/
│   ├── api/            generated client + thin wrappers (see below)
│   ├── components/     presentational
│   ├── features/       patients/, visits/, insights/ — one folder per workflow
│   ├── hooks/          e.g. useInsightJob (polling lives here, once)
│   ├── pages/ (routes/)
│   └── types/          generated from the OpenAPI schema
├── public/
├── .env.local          VITE_API_BASE_URL=http://127.0.0.1:8000/api/v1
├── package.json
└── README.md
```

## Generate the client, don't hand-write it

The backend publishes an OpenAPI schema at `/openapi.json`, and every request and
response model is fully typed. Generating from it means the types cannot drift
from the API:

```bash
# with the backend running
npx openapi-typescript http://127.0.0.1:8000/openapi.json -o src/types/api.d.ts
```

Browse the same schema interactively at http://127.0.0.1:8000/docs.

## The workflow

Base URL: `http://127.0.0.1:8000/api/v1`

```
GET    /stats                          home-screen counters
GET    /health                         service + db + key count

POST   /patients                       register              -> 201
GET    /patients?q=                    list / search         -> X-Total-Count
GET    /patients/{id}                  single
PATCH  /patients/{id}                  correct demographics
GET    /patients/{id}/timeline         whole record, one request

POST   /visits                         schedule              -> 201
GET    /visits?status=&from=&to=       list, newest first    -> X-Total-Count
GET    /visits/{id}                    single
PATCH  /visits/{id}                    correct details
POST   /visits/{id}/complete           mark done             -> 200
POST   /visits/{id}/cancel             cancel                -> 200

POST   /visits/{id}/transcript         upload .txt (multipart) -> 201
GET    /visits/{id}/transcript         read it back
DELETE /visits/{id}/transcript         remove it             -> 204

POST   /visits/{id}/insights           start generation      -> 202 + job_id
GET    /jobs/{job_id}                  poll
GET    /visits/{id}/job                latest job (204 if none)
GET    /visits/{id}/insights           read result

GET    /follow-ups?status=open         the queue             -> X-Total-Count
GET    /follow-ups/{id}                single
PATCH  /follow-ups/{id}                outcome of a call
```

### The lifecycle is explicit

```
Schedule -> Complete -> Add Transcript -> Generate Insights
```

A visit is created **before** the consultation, so it starts
`visit_status: "scheduled"` with no transcript. Each step is its own action:

- `PATCH /visits/{id}` does **not** complete a visit. Only `/complete` does.
- **Upload requires the visit to be `completed`** — a 409 otherwise. That is what
  makes an Add Transcript button legitimately disabled until then.
- `/complete` is idempotent, so no need to guard a double click.
- `/cancel` reopens any follow-up that visit had resolved, and is refused (409)
  once insights exist.

### Transcripts are `.txt` only

`multipart/form-data` with a `file` field and optional `replace` (default false,
required to overwrite). Server-side validation: extension must be `.txt`, max
256 KB, must decode as UTF-8, non-empty after trimming.

| Status | When |
| --- | --- |
| 409 | visit not `completed`, or transcript exists without `replace: true` |
| 413 | over 256 KB (`transcript_too_large`) |
| 422 | wrong extension, not UTF-8, or empty |

The file is stored server-side as `transcripts/{visit_id}.txt`; the name you send
is kept only for display in `transcript_filename`.

`GET /visits/{id}/transcript` returns the text with a `character_count`, so a
brief can be shown next to its source.

### Follow-ups

`GET /follow-ups` takes `status` (`open` default, or `resolved`/`dismissed`/`all`),
`due_before`, `due_after`, `needs_confirmation`, `patient_id`, `sort`
(`due_asc` default / `due_desc`), `limit`, `offset`. Rows carry the patient's
name, `external_id`, `phone`, and the visit's `chief_issue` — everything a queue
row needs in one request.

There is deliberately no `days_overdue` field: it depends on today and would go
stale in a cached response. Derive it from `due_earliest`.

`PATCH /follow-ups/{id}` accepts `status` (`open` or `dismissed` only),
`dismissed_reason` (required when dismissing), `note`, and `mark_contacted`.

**`status: "resolved"` is a 422.** A follow-up resolves as a consequence of
scheduling a visit for that patient, so schedule the visit instead.
`mark_contacted: true` records the call without changing status, which is how
"I phoned them, they're still deciding" is expressed.

#### Dismissing is a first-class action in the queue

Plenty of patients never come back — they feel better, or they simply decline. So
**Dismiss** is a visible action on every row of the Follow-ups page, not something
hidden in an overflow menu. Without it the queue silently fills with people who are
never returning and stops being trustworthy.

`dismissed_reason` is required by the API, so dismissal cannot be one click. Use a
small dialog offering the common reasons as presets, plus optional free text:

- Feeling better, no follow-up needed
- Declined to return
- Could not be reached
- Moved away or transferred care
- Other (free text)

Dismissal is reversible: `PATCH` with `status: "open"` clears the dismissal fields.
Offer **Undo** on the confirmation toast, and make `status=dismissed` a filter on
the queue so a row can be reopened later.

Three outcomes are therefore reachable from a queue row, and they are genuinely
distinct — do not collapse them:

| Doctor's situation | Action | Effect |
| --- | --- | --- |
| Patient is coming in | Schedule visit | follow-up auto-resolves on the new visit |
| Spoke to them, still deciding | Mark contacted | stays `open`, records the call |
| Not coming back | Dismiss + reason | leaves the queue, reversible |

### Uploading a transcript starts generation automatically

Product decision: the doctor should not have to press a second button. A
successful upload is immediately followed by `POST /visits/{id}/insights`, and the
UI moves straight into the progress state.

The chaining lives in the **frontend**, not the upload endpoint, on purpose.
Storing bytes always succeeds; generation costs money and hits a daily cap. Keeping
them as two calls means a quota failure reads as "transcript saved, compiling later"
rather than making a successful upload look like it failed.

Two things this has to get right.

**Pass `regenerate: true` when the visit already has insights.** Replacing a
transcript (`replace: true`) does not clear `insights_generated`, so a plain
generate call returns `409 conflict`. Send `regenerate: true` whenever
`has_insights` was already true.

**A stale brief is possible and must be surfaced.** Because a replace leaves the
old insight row in place, a failed regeneration leaves a brief that was compiled
from a *different* transcript, with nothing marking it as out of date. Detect it by
comparing `transcript_uploaded_at` against the insight's `generated_at`: if the
transcript is newer, show a warning on the brief and offer to recompile. This is a
clinical-safety issue, not a cosmetic one — the brief would describe a consultation
that is not the one on file.

**Fallback.** If the generate step fails for any reason — quota, network, or the
tab being closed between the two calls — the visit simply sits with a transcript
and no insights. That state already appears in the Visit schedule queue with a
**Generate insights** action, so the workflow is self-healing and no upload is ever
lost. Nothing extra is needed for the failure path beyond making that action
visible.

### Generation is asynchronous

`POST /visits/{id}/insights` returns `202` immediately, not the result. The chain
is three sequential LLM calls — roughly a minute. The response gives you where to
look:

```json
{
  "job_id": "5d52f9d1-...",
  "visit_id": "b76a1dfb-...",
  "status": "queued",
  "poll_url": "/api/v1/jobs/5d52f9d1-...",
  "insights_url": "/api/v1/visits/b76a1dfb-.../insights"
}
```

Poll `poll_url` every 2-3 seconds until `status` is terminal, then fetch
`insights_url`.

```
queued -> running -> succeeded | failed | failed_quota
```

`layers_completed` (0-3) is updated as the chain progresses, so a progress
indicator can show real movement rather than a spinner.

`failed_quota` deserves its own message. It means every configured API key is
rate limited, which is expected on the free tier and worth retrying unchanged
tomorrow — not a bug to report. Distinguish it from `failed`, which needs looking
at.

## Errors

Every failure uses one shape:

```json
{ "code": "duplicate_patient", "message": "human readable", "details": { } }
```

| Status | When | What the UI should do |
| --- | --- | --- |
| 404 | unknown id | not-found state |
| 409 | duplicate patient, visit number taken, no transcript attached, insights already exist, job already running | actionable message, often with a retry path |
| 422 | validation, bad transcript path, malformed uuid | field-level errors |
| 429 | `quota_exhausted` | "try again later", not an error toast |
| 500 | unexpected | generic failure |

Two 409s are worth special handling:

**Duplicate patient.** `POST /patients` refuses a registration that shares a name
and date of birth with an existing patient, and returns the existing one:

```json
{
  "code": "duplicate_patient",
  "message": "A patient with the same name and date of birth already exists (P-2026-00005).",
  "details": {
    "existing_patient_id": "afdc...",
    "existing_external_id": "P-2026-00005",
    "match_reason": "name_and_date_of_birth"
  }
}
```

The right UI is "did you mean this patient?" offering both the existing record and
a confirm that resubmits with `allow_duplicate: true`. Blocking outright would be
wrong — siblings sharing a name and birthday do exist.

**Job already running.** Guard the generate button on this rather than letting a
double-click spend two sets of API calls.

## Field notes

`date_of_birth` is required when registering, not age — the backend derives age as
at each visit date, so a patient shows the right age on historical visits.
`PatientOut` also carries `age` (as at today), `visit_count` and
`last_visit_date`, so none of that arithmetic belongs in the UI.

`visit_number` is assigned automatically when omitted; don't make the user pick.

`visit_date` must be the real consultation date. It anchors follow-up resolution,
so a wrong value silently produces a wrong appointment date.

`external_id` (`P-2026-00042`) is the human-facing patient number — show that, not
the UUID.

`VisitOut` carries `patient_name`, `first_name`, `last_name`, `external_id`,
`gender` and `age_at_visit`, so a visit list needs no second request.
`has_insights` mirrors `insights_generated` under a name that reads better.

**There is no diagnosis field.** `primary_diagnosis` was removed from every
response: the underlying `disease` value existed only as a prop for generating the
synthetic seed transcripts and was never clinical data. `chief_issue` from Layer 3
is the condition descriptor instead — a full sentence, not a label, so truncate it
to one line in queue rows and show the whole thing on the brief. Note the
consequence: there is no structured field to group or filter patients by
condition.

Regenerating insights **overwrites** the previous brief; there is no history.
Warn before offering it.

## Reading an insight

`GET /visits/{id}/insights` returns promoted fields for display plus the full
payload:

- `clinical_summary` — the doctor-facing brief, ready to render
- `chief_issue`, `next_visit_focus`, `patient_age`
- follow-up: `follow_up_type` (`scheduled` / `conditional_only` /
  `not_discussed`), `follow_up_date`, `follow_up_date_latest`,
  `follow_up_stated`, `follow_up_needs_confirmation`
- `insights_json` — `{ layer_1, layer_2, layer_3, follow_up }`, the whole run
- `total_tokens`, `model_name`, `generated_at`

On follow-up display: `follow_up_date` and `follow_up_date_latest` are equal
unless the doctor gave a range ("three to four months"), in which case show the
window. When `follow_up_needs_confirmation` is true the timing was vague or
hedged and a human should pick the actual appointment date — surface that rather
than presenting a computed date as settled. When `follow_up_type` is
`conditional_only` there is no date at all and the honest rendering is the
condition ("return if symptoms worsen"), not an empty date field.

`follow_up_stated` is the doctor's verbatim words. Worth showing next to the
computed date so the clinician can audit the interpretation.

## CORS

Defaults to all origins, so local development works with no setup. Set
`CORS_ORIGINS=http://localhost:3000` in `backend/.env` for development and the
real origin in production. `*` cannot be combined with credentialed requests, so
narrow this **before** enabling the API key, not after.

## Authentication

Off by default. Setting `API_KEY` in `backend/.env` makes every route except
`GET /health` require a matching `X-API-Key` header, returning `401`
`unauthorized` on mismatch.

Route all requests through a single API client module so adding the header is a
one-file change. **The key must never reach the browser bundle** — proxy calls
through a server-side route handler and read the key from a server-only env var.

## Other endpoints

`GET /stats` gives the home screen its counters in one request: patient count,
visit counts by status (including `awaiting_transcript` and `awaiting_insights`,
the two states needing action), and follow-up buckets (`open`, `overdue`,
`due_next_7_days`, `needs_confirmation`, `resolved`, `dismissed`). `overdue` and
`due_next_7_days` are relative to the server date, returned as `as_of` so the
label can be honest.

`GET /patients/{id}/timeline` returns the patient plus every visit newest-first,
each with an `insight` summary and `follow_up` summary attached (both `null` when
absent). It deliberately omits `insights_json`, which is 20-30 KB per visit.

`GET /visits/{id}/job` returns the latest job so a page reload can resume
polling, or `204` when the visit has never had one — distinguishable from a `404`
for an unknown visit.

Never poll `GET /health/api-keys`: each call spends one request from every key's
daily quota.
