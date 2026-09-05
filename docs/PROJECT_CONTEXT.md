# Project Context

Onboarding document for an AI coding assistant or a new developer. It describes
what this project is, how it is built, and — most importantly — *why* the
non-obvious parts are the way they are. Several decisions here look wrong until
you know what broke without them.

Deliberately excludes run state (how many transcripts have been processed, which
API key is currently rate limited). For that, run
`cd backend && python -m cli.load_insights --list`.

**Every path in this document is relative to `backend/` unless it starts with
`frontend/` or `docs/`.** All Python commands run from `backend/`.

---

## 1. What this is

A FastAPI service that turns recorded homeopathic doctor-patient consultations
into structured clinical records stored in Postgres.

A consultation transcript goes in. Out comes a machine-readable clinical
extraction, a homeopathic analysis, a doctor-facing care plan, and a resolved
follow-up appointment date — all persisted and queryable.

The intended user is a homeopathic practitioner who wants to open a patient's
record before the next call and see, in about 90 seconds, what happened last
time, what was prescribed, what to watch for, and what to ask next. A UI is
planned; the API exists to serve it.

**Not** a diagnostic tool. It records and organises what the doctor and patient
actually said. It does not infer diagnoses or make treatment recommendations.

---

## 2. The workflow

An explicit lifecycle, one step per doctor action:

```
Schedule Visit -> Complete Visit -> Add Transcript -> Generate Insights
                                                            |
                                    follow-up enters the Follow-ups queue
                                                            |
                                doctor calls patient -> Schedule Visit
                                                            |
                                      earlier follow-up marked resolved
```

```
GET    /api/v1/stats                          home-screen counters

POST   /api/v1/patients                       register a patient
GET    /api/v1/patients                       list / search (q, X-Total-Count)
GET    /api/v1/patients/{patient_id}          single patient
PATCH  /api/v1/patients/{patient_id}          correct demographics
GET    /api/v1/patients/{patient_id}/timeline whole record in one request

POST   /api/v1/visits                         schedule a consultation
GET    /api/v1/visits                         list (status/date filters, sort)
GET    /api/v1/visits/{visit_id}              single visit
PATCH  /api/v1/visits/{visit_id}              correct details
POST   /api/v1/visits/{visit_id}/complete     mark the consultation done
POST   /api/v1/visits/{visit_id}/cancel       cancel, reopening its follow-ups

POST   /api/v1/visits/{visit_id}/transcript   upload the .txt (multipart)
GET    /api/v1/visits/{visit_id}/transcript   read it back
DELETE /api/v1/visits/{visit_id}/transcript   remove it

POST   /api/v1/visits/{visit_id}/insights     start the chain -> 202 + job_id
GET    /api/v1/jobs/{job_id}                  poll until terminal
GET    /api/v1/visits/{visit_id}/job          latest job, to resume polling
GET    /api/v1/visits/{visit_id}/insights     read the result

GET    /api/v1/follow-ups                     the follow-up queue
GET    /api/v1/follow-ups/{follow_up_id}      single follow-up
PATCH  /api/v1/follow-ups/{follow_up_id}      outcome of a phone call
```

A visit is created *before* the consultation, so it starts with no transcript and
`visit_status: scheduled`. Completion is explicit: uploading a transcript
deliberately does **not** complete a visit (§8.17), and a transcript can only be
attached to a visit that is already `completed`.

Interactive docs at `/docs` once the server is running.

---

## 3. The three-layer pipeline

Each transcript passes through three sequential LLM calls. Each layer consumes
the previous layer's output, so a run is 3 API calls per consultation.

```
transcript.json  (loaded by reference from visits.transcript_path)
      |
      v
  Layer 1  clinical extraction     what was literally said, no interpretation
      |    prompts/layer_1_clinical_extraction.md
      |
      +--> resolve_follow_up()     Python, not the LLM: turns "in three weeks"
      |                            into a calendar date
      v
  Layer 2  homeopathic analysis    remedy reasoning, constitutional picture
      |    prompts/layer_2_homeopathic_analysis.md
      v
  Layer 3  care plan               skimmable visit brief for the doctor
           prompts/layer_3_care_plan.md
      |
      v
  visit_insights  (one row per visit)
```

Why three layers rather than one prompt: extraction must be faithful and
uninterpreted, analysis is explicitly interpretive, and the care plan is a
summarisation task. Splitting them keeps each prompt's job narrow, makes a bad
output attributable to a specific stage, and means Layer 1's factual record
stays clean even if the analysis is re-run with a different prompt.

Every layer is asked to return JSON. All three outputs plus the resolved
follow-up are stored under `visit_insights.insights_json`.

---

## 4. Tech stack

| Concern | Choice |
| --- | --- |
| Language | Python 3.11 |
| Environment | conda env **`reels`** (`/opt/anaconda3/envs/reels/bin/python`) |
| API | FastAPI + uvicorn |
| Validation / settings | Pydantic 2, pydantic-settings |
| LLM | Google Gemini via `langchain-google-genai` |
| LLM framework | `langchain-core` (messages only — see §8.1) |
| Prompt templating | Jinja2 |
| Database | Neon serverless Postgres 18 |
| DB driver | `psycopg` 3 + `psycopg-pool` |
| Date maths | `python-dateutil` (`relativedelta`) |
| Tests | stdlib `unittest` (no pytest in the env) |

Pinned versions are in `requirements.txt`, matched to what is installed in
`reels` rather than the latest on PyPI.

Always run with the `reels` interpreter. The system `python3` is 3.9 and the
dependencies require 3.10+.

---

## 5. Folder structure

Top level splits by deployable unit. `backend` and `frontend` each own their
dependencies and environment file and communicate over HTTP only, so either can
be worked on — or replaced — without touching the other.

```
doctor-patient/
├── backend/          Python service (everything in §5.1)
├── frontend/         UI; see frontend/README.md for the API contract
├── docs/             this file
├── README.md         getting started
├── .kiro/            workspace config, steering, MCP
└── .vscode/
```

### 5.1 backend/

```
backend/
├── app/
│   ├── main.py                  FastAPI app factory, lifespan, CORS
│   ├── config.py                all settings in one typed place
│   ├── api/
│   │   ├── deps.py              DI: pooled connection, auth placeholder
│   │   ├── errors.py            AppError -> HTTP status, in one place
│   │   └── v1/                  patients, visits, jobs, health routers
│   ├── schemas/                 Pydantic request/response models
│   ├── domain/                  pure logic, zero I/O
│   │   ├── follow_up/           models.py + resolver.py
│   │   ├── transcripts.py       loading + path-traversal guard
│   │   └── identity.py          external_id, name/phone normalisation
│   ├── pipeline/                layers, prompts, parsing, results, runner
│   ├── llm/                     gemini.py (multi-key client), errors.py
│   ├── db/
│   │   ├── pool.py              connection pool
│   │   ├── models.py            row dataclasses
│   │   └── repositories/        patients, visits, insights, jobs
│   ├── services/                the only place db + llm + pipeline meet
│   │   ├── patient_service.py
│   │   ├── visit_service.py
│   │   ├── insight_service.py   generation + persistence
│   │   └── job_service.py       enqueue, run, reap orphans
│   └── core/                    errors.py, logging.py
│
├── cli/
│   ├── load_insights.py         batch runner over the same services
│   ├── render_prompt.py         render a prompt, no model, no DB
│   ├── check_keys.py            which API keys are usable
│   └── convert_transcripts.py   one-off JSON -> .txt migration (already run)
│
├── archive/transcripts_json/    the 16 original synthetic transcripts, tracked
│
├── prompts/                     Jinja2 templates, one per layer
├── transcripts/                 source data: <patient>_<visit number>.json
├── sql/                         schema, applied in numeric order
│   ├── 1_patient_schema.sql
│   ├── 2_visit_schema.sql       + seed data for all 16 visits
│   ├── 3_visit_insights_schema.sql
│   ├── 4_insights_generated.sql migration
│   ├── 5_api_support.sql        migration: external_id, insight_jobs
│   ├── 6_frontend_support.sql   migration: lifecycle columns, follow_ups
│   └── extra.sql                future tables, all commented out
├── tests/                       unittest suites
├── legacy/                      pre-refactor scripts, safe to delete
├── requirements.txt
└── .env / .env.example
```

`app/config.py` derives `BASE_DIR` as its own parent's parent, which is
`backend/`. That is what makes `prompts/` and `transcripts/` resolve, and why
they live inside `backend/` rather than at the repository root.

### 5.2 frontend/

Empty apart from `frontend/README.md`, which is the contract to build against:
endpoints, the async polling flow, the error shape, and the two 409 cases that
need real UI treatment. A session working there needs nothing from this document.

Two things it depends on staying true: the OpenAPI schema at `/openapi.json` is
the source of truth for types (generate, don't hand-write), and `cors_origins` in
`app/config.py` currently allows everything, which is why local development needs
no setup and why it must be narrowed before deployment.

### Transcript format

Plain UTF-8 text, one file per visit, named by visit id:

```
backend/transcripts/{visit_id}.txt
```

stored on the visit as `transcripts/{visit_id}.txt`. That is the only convention;
converted historical files and new uploads are identical, so replace, read and
delete need no special cases. The doctor's own filename lives in
`visits.transcript_filename` for display and never touches the filesystem.

Transcripts were originally JSON with `patient_name`, `disease` and `transcript`
keys. `cli/convert_transcripts.py` migrated all 16 to `.txt`; the originals are
archived in `backend/archive/transcripts_json/` and tracked in git so a fresh
clone can rebuild the text files. `patient_name` was redundant (the pipeline
takes the name from the database) and `disease` was only ever a prop for
generating the synthetic transcripts, so neither survives (§8.3).

---

## 6. Data model

Four tables. `patients` 1—N `visits` 1—1 `visit_insights`, and `insight_jobs`
tracking generation runs.

### patients
`patient_id` UUID PK. `external_id` is the clinic patient number, `UNIQUE NOT
NULL`, generated by the database as `P-2026-00042`. Stores `date_of_birth`, not
age, so age can be computed as at any visit date.

### visits
One row per consultation, i.e. per transcript file.

| Column | Notes |
| --- | --- |
| `visit_id` | UUID PK |
| `patient_id` | FK -> `patients(patient_id)` |
| `visit_number` | 1-based, `UNIQUE(patient_id, visit_number)`, auto-assigned |
| `visit_date` | **Anchors all follow-up resolution.** Must be the real date. |
| `visit_status` | scheduled / in_progress / completed / cancelled |
| `transcript_path` | e.g. `transcripts/riya_1.json`; null until attached |
| `insights_generated` | TRUE only after all layers succeed *and* the row is written |

### visit_insights
One row per visit, `UNIQUE(visit_id)`. Promoted scalar columns for querying,
plus the full payload.

- **Doctor-facing:** `clinical_summary`, `chief_issue`, `next_visit_focus`
- **Searchable:** `patient_age`, `patient_gender`
- **Follow-up:** `follow_up_type`, `follow_up_date`, `follow_up_date_latest`,
  `follow_up_stated`, `follow_up_needs_confirmation`
- **Payload:** `insights_json` (`{layer_1, layer_2, layer_3, follow_up}`), GIN indexed
- **Provenance:** `model_name`, `model_version`, `prompt_version`, `extraction_version`
- **Cost:** `input_tokens`, `output_tokens`, `total_tokens`
- **Timing:** `created_at` (first generation), `generated_at` (latest)

Constraints worth knowing: `follow_up_type_matches_date` (a `scheduled`
follow-up must have a date, the other types must not) and
`follow_up_window_ordered`.

`is_current` is **vestigial**. It predates update-in-place and can no longer be
FALSE. Don't build on it.

`primary_diagnosis` still exists as a column with its original 16 values, but is
no longer written (new rows are NULL) and is **not serialised by the API** — see
§8.3. Regeneration overwrites a brief in place, so there is no history; warn
before offering it.

### follow_ups
One row per source visit, `UNIQUE(source_visit_id)`, which is also the upsert
conflict target so regeneration refreshes rather than duplicates.

`status` is open / resolved / dismissed. `resolved` is only ever reached by
scheduling a later visit — never settable through the API (§8.18).
`last_contacted_at` is independent of status, so "I phoned them and they are
still deciding" stays open while recording the call. Two CHECK constraints keep
transitions whole: a resolved row must carry `resolved_at` and
`resolved_reason`, and a non-resolved row must carry neither.

Patient id is denormalised onto the table because the queue is the most-read
query in the application.

### insight_jobs
`status` is queued / running / succeeded / failed / **failed_quota**. A partial
unique index on `(visit_id) WHERE status IN ('queued','running')` means one visit
cannot be processed twice at once, which would pay for two sets of API calls.
`worker_id` identifies the process that claimed a job so orphans are detectable.

---

## 7. Layering rules

```
api  ->  services  ->  { db, llm, pipeline, domain }
cli  ->  services
```

`domain` and `pipeline` are I/O-free and unit-testable with stubs. `db` and `llm`
are the only edges that touch the outside world. `services` is the only place
they meet — which is why the HTTP API and the batch CLI cannot drift apart in
behaviour: they call the same functions.

Dependencies must stay acyclic. `app/pipeline/` must never import from `app/db/`.

---

## 8. Design decisions and their rationale

The section to read before changing anything. Each of these was a bug or a
deliberate trade-off.

### 8.1 Jinja2 for prompts, not LangChain's templating

The prompts contain JSON schema examples full of `{` and `}`.
`ChatPromptTemplate` would read those as template variables and raise. So
prompts are rendered by Jinja2 and passed to the model as a plain
`HumanMessage`. **Never** route a rendered prompt through a LangChain prompt
template.

Jinja is configured with `undefined=StrictUndefined` so a missing variable
raises instead of silently rendering an empty string into a clinical prompt, and
`autoescape=False` because these are plain-text prompts — escaping would mangle
quotes in the transcript.

Prompt placeholders use `{{ variable }}`.

### 8.2 The LLM never does date arithmetic

Layer 1 captures the follow-up as *stated* ("come back in three weeks") broken
into parts: `min_value`, `max_value`, `unit`, `absolute_date`, `approximate`.
`resolve_follow_up()` turns that into calendar dates in Python.

LLMs are unreliable at date maths and it isn't auditable. Doing it in Python
makes it deterministic and unit-testable, and keeping the verbatim phrase
alongside the computed date means the doctor can see what was actually said.

`relativedelta` is used rather than `timedelta` so month arithmetic is
calendar-correct (31 Jan + 1 month = 28 Feb, not a 30-day drift).

### 8.3 There is no diagnosis field, on purpose

The seeded transcripts carried a `disease` key, but it existed only as a prop for
generating those synthetic conversations — it was never clinical data. So it must
not reach the doctor: it is gone from the transcript format, no longer written to
`primary_diagnosis` (new rows are NULL), and removed from `InsightOut`. Deleting
it from the schema is a stronger guarantee than asking the frontend not to render
it, because it cannot then leak through a generated type.

The column and its original 16 values are kept — dropping a populated column to
hide a field is destructive for no gain, and the archived JSON holds the same
information anyway.

Consequence worth knowing: **Layer 3's `chief_issue` is now the condition
descriptor everywhere.** It is a full sentence derived from what was actually
said, not a label, which is arguably better — but it means there is no structured
field to group or filter patients by condition. Adding one is a clinical-coding
decision, not a free-text column.

The original reasoning still holds for the prompts: Layer 1 must capture only
what was said, and handing the model a confirmed diagnosis would bias that
extraction.

### 8.4 Follow-up is two independent slots, not an enum

All 16 transcripts contain **both** a scheduled return ("come back in three
weeks") **and** a conditional trigger ("if you notice a flare-up, contact the
clinic"). Modelling this as one `type: definite | conditional` field would
discard half of every transcript. Hence `scheduled_visit` and
`return_earlier_if[]` side by side.

Related distinctions the prompt calls out explicitly, each from a real
transcript:

- A PRN dosing instruction ("take five drops if you feel an aura") is **not** a
  follow-up trigger.
- "Note down the flow so we can discuss it" is `record_and_report` (patient
  homework), not `contact_clinic` (escalation).
- One transcript (`riya_6`) has **inverted speaker labels** — the follow-up
  instruction appears under `PATIENT:`. The schema has a `stated_by` field so
  this surfaces as a data-quality warning instead of being silently
  mis-attributed.

`confidence` describes the resolved *date only*. A confidently-absent date
(conditional-only, or never discussed) stays `high`; a range or hedged timing
drops to `medium` and sets `needs_human_confirmation`.

### 8.5 `visit_date` is required and never defaulted

It anchors every relative follow-up. Defaulting it would silently produce wrong
appointment dates. It is required in `VisitCreate` and read from the database at
generation time — never client-supplied alongside a transcript.

### 8.6 `autocommit=True` on the pool is load-bearing

With psycopg 3's default (`autocommit=False`), *any* query — including the
`SELECT` that loads the visit list — implicitly opens a transaction that stays
open. A later `with conn.transaction():` then degrades into a **savepoint**
inside it rather than an independent transaction. Every visit's write
accumulated in one long transaction that only committed when the connection
closed, so interrupting a long batch discarded all of it.

Confirmed by observing the same `txid_current()` across two supposedly separate
transaction blocks, `now()` frozen at one value, and the connection still
`INTRANS` after each block exited.

With autocommit on, `conn.transaction()` emits a real `BEGIN`/`COMMIT`, which is
what makes each visit independently durable. **Do not remove it.**

### 8.7 Upsert, plus a flag set in the same transaction

`insights.save()` does `INSERT ... ON CONFLICT (visit_id) DO UPDATE`, so
regenerating a visit updates its row in place and reuses the `insight_id`.
`created_at` is excluded from the update list so it keeps the first-generation
time while `generated_at` advances.

`visits.insights_generated` is set TRUE in the *same transaction* as the insight
row, so the flag can never claim a visit is done while its data is missing. And
because the whole chain must succeed before the save is reached, a failure at
any layer leaves the flag FALSE and the visit pending.

The pending query keys off the flag but also treats a visit as pending if the
flag is TRUE with no insight row — otherwise a deleted row would make a visit
invisible forever.

### 8.8 Multi-key rotation with three-way error classification

Free-tier Gemini keys are capped per day per model, and one full backfill needs
more than a single key allows. `app/llm/gemini.py` holds several keys and sorts
errors three ways, because they need different responses:

| Kind | Example | Response |
| --- | --- | --- |
| quota / transient | 429, 503, deadline exceeded | try the next key |
| key rejected | `API_KEY_INVALID`, 403, 401 | disable that key, try next |
| request problem | unknown model, safety block | raise — another key fails alike |

The middle row is the important one. An invalid key is a problem with the *key*,
not the request, so it must not abort a run that other keys could finish.

Rotation is **sticky**: once a key works it keeps being used, wrapping around
only when it fails. Restarting from key 1 every call would waste a round trip
rediscovering that key 1 is exhausted.

### 8.9 Insight generation is asynchronous

The chain is three sequential model calls, around a minute. That is past common
proxy and load-balancer timeouts, so `POST /visits/{id}/insights` returns `202`
with a `job_id` to poll rather than blocking.

Jobs run in-process on a FastAPI background task but their *state* lives in
Postgres, so it survives a restart and stays queryable without adding Redis or
Celery. The trade-off: a process that dies mid-job would leave a row stuck in
`running`, so `reap_orphans()` runs at startup and fails anything a previous
worker abandoned. If concurrency ever matters, this is the seam to replace with
a real queue.

`failed_quota` is a distinct status from `failed` because it is worth retrying
unchanged once the daily limit resets, rather than investigating.

### 8.10 Patient uniqueness is layered

| Layer | Rule |
| --- | --- |
| hard | `external_id`, UNIQUE NOT NULL, database-generated |
| soft | normalised (first_name, last_name, date_of_birth) -> 409, overridable |
| none | phone is normalised and indexed but **not** unique |

Phone is not a key because families share one mobile, so a unique constraint
would reject the second family member. Name is not a key because spelling and
transliteration vary, so it yields a different key for the same person. Together
they manage to both over-block and under-catch, which is why identity is
anchored on a system-owned id and demographics only *flag* a probable duplicate
for a human to confirm.

### 8.11 Transcript paths are confined to one directory

Paths arrive from API payloads and are loaded by reference, so
`resolve_within()` fully resolves both the candidate and the allowed directory
before comparing. Without it, a payload of `../../etc/passwd` would be honoured.
This matters more than usual because there is no auth (§8.12).

### 8.12 Auth is an optional shared secret

`require_auth` enforces an `X-API-Key` header when `API_KEY` is set in settings,
and is a no-op when it is not — so local development stays open while deployment
only needs an env var. `GET /health` is exempt so a monitor can check liveness
without holding the secret; `GET /health/api-keys` is **not** exempt, because
each call spends one request from every key's daily quota.

The comparison uses `hmac.compare_digest` rather than `==`, so the check does not
leak the key's length or content through response timing.

This is a shared secret, not per-user identity, so it gives no audit trail of who
did what. Worth replacing if more than one person ever uses the system.

`CORS_ORIGINS` still defaults to `*`. It must be narrowed **before** enabling
`API_KEY`, not after: `*` cannot be combined with credentialed requests.

### 8.13 `response_mime_type="application/json"`

Set on the client so the API itself constrains replies to JSON, rather than
relying on stripping markdown fences. `extract_json()` still keeps
fence-and-brace fallbacks for robustness.

### 8.14 `AIMessage.text`, and why the isinstance check comes first

Gemini 3.x can return content as a list of blocks rather than a string, so
`response_text()` prefers `.text` over `.content`. In langchain-core 1.2.x
`.text` returns a str subclass that is *also* callable for backwards
compatibility, so the `isinstance(text, str)` check must precede the
`callable(text)` check or merely reading it emits a deprecation warning.

### 8.15 Ids are validated as UUIDs at the edge

Path params and body id fields are typed `UUID`, not `str`. Otherwise a
malformed id reaches Postgres, which fails to parse it, and the caller gets a
500 instead of a clear 422. A well-formed but unknown id is a 404.

### 8.17 Attaching a transcript does not complete a visit

`PATCH /visits/{id}` used to force `visit_status = 'completed'` as a side effect
of setting `transcript_path`. That contradicts an explicit Complete button and
makes the real state depend on the order of two unrelated actions, so it was
removed. Status changes go through `/complete` and `/cancel` only.

The dependency runs the other way instead: **upload requires
`visit_status = 'completed'`**, which is what makes an Add Transcript button
legitimately disabled until the consultation is marked done.

`/complete` is idempotent — `completed_at` is set with `coalesce(completed_at,
now())`, so a double click changes nothing.

`/cancel` **reopens any follow-up it had resolved**, in the same transaction.
Without that, cancelling a visit silently drops a patient who still needs calling.
Cancelling is refused once insights exist, because a generated brief is a record
of a consultation that demonstrably happened.

### 8.18 A follow-up cannot be resolved through the API

`PATCH /follow-ups/{id}` accepts `open` and `dismissed` only; `resolved` is a
422. Resolution is *caused* by scheduling a visit, so allowing a client to set it
directly would create resolved rows with no visit behind them.

Resolution keys on the **source visit's date**, not the due date: any open
follow-up from an earlier visit is closed by booking the next one, whether or not
the new date matches what was suggested.

Regeneration must not undo a human decision, so the upsert's `DO UPDATE` list
excludes `status`, the `resolved_*` and `dismissed_*` fields,
`last_contacted_at` and `note`. Only the model-derived dates and text refresh.
There is a test asserting this, because the failure would be silent and would
quietly reopen closed work.

Only `scheduled` follow-ups enter the queue. `conditional_only` has no date and
cannot be sorted into a due-date list — its return triggers render on the visit
brief instead — and `not_discussed` produces nothing. Either case *deletes* any
existing row, so a regeneration that no longer finds a follow-up does not leave a
stale one in the queue.

### 8.19 Transcript uploads are validated, never coerced

Wrong extension, over 256 KB, not UTF-8, or empty after stripping are all
rejected. Silently mis-parsing a Word document or a mis-encoded file would
produce a confident, wrong clinical record, which is worse than a clear failure.
The size ceiling exists so a large paste fails immediately instead of an hour
later as an opaque pipeline error.

The file is written to `transcripts/{visit_id}.txt` and **never** under the
client's filename, which is attacker controlled. Deriving the path from the visit
id removes the whole class of problem rather than trying to sanitise it.

One subtlety in the decoder: `utf-8-sig` is tried **before** `utf-8`. A byte-order
mark is itself valid UTF-8, so plain `utf-8` succeeds and silently leaves a
U+FEFF at the start of the text, which would then be sent to the model as
transcript content.

### 8.16 Visit dates are chained from the transcripts

Seed `visit_date` values in `sql/2_visit_schema.sql` are not arbitrary. Each
transcript ends with a stated interval, so each visit is dated by applying the
previous visit's interval. Visit N+1 lands exactly on the date visit N promised.

This gives a free end-to-end correctness check: a visit's resolved
`follow_up_date` should equal the next visit's actual `visit_date`.
`cli/load_insights.py` prints this per visit as a `check:` line and flags
`MISMATCH`. It is the single most useful signal that extraction is working.

---

## 9. Commands

All with the `reels` interpreter, run from `backend/`.

```bash
conda activate reels
cd backend

# Serve the API (docs at http://127.0.0.1:8000/docs)
uvicorn app.main:app --reload

# Batch: status, plan, run
python -m cli.load_insights --list
python -m cli.load_insights --all --dry-run
python -m cli.load_insights --all
python -m cli.load_insights --all --limit 2         # costed trial
python -m cli.load_insights --all --pause           # confirm each visit
python -m cli.load_insights --visit <visit_id>
python -m cli.load_insights --transcript transcripts/riya_1.json --regenerate

# Which API keys are usable (one small request each)
python -m cli.check_keys

# Render a prompt without calling the model — best way to iterate on prompts
python -m cli.render_prompt --transcript transcripts/riya_1.json \
  --age 24 --gender Female --call-date 2026-02-16 --layer 1

# Tests
python -m unittest discover -s tests -t .
```

Other `load_insights` flags: `--sleep N` to pace under per-minute limits,
`--log-file run.log` to tee output, `--model` to switch model (each has its own
daily quota bucket), `--stop-after`, `--stop-on-error`.

Ctrl-C once stops after the visit in flight; twice abandons it immediately.
Either way nothing partial is written and re-running skips completed visits.

---

## 10. Testing approach

Five suites, 157 tests, all offline — no API calls, no database.

`test_transcripts.py` covers the loader, the traversal guard, and upload
validation, including the BOM case and that the client filename never reaches a
path.

`test_follow_up_tracking.py` covers which follow-ups get queued, that the upsert
preserves human decisions, and the resolve/reopen transitions.

`test_follow_up.py` covers date resolution against every timing pattern the
corpus contains, with hand-checked expected dates, plus malformed input, bad
anchor dates, and prompt wiring.

`test_gemini_llm.py` covers key rotation with stubbed clients: fall-through,
stickiness, permanent disabling of rejected keys, request errors not touching
other keys, and total exhaustion.

`test_api.py` covers routing, validation, status codes and error mapping with the
DB dependency overridden and the services patched.

Conventions:

- stdlib `unittest`; `tests/__init__.py` must exist for discovery to work
- tests insert the project root on `sys.path`
- when reading settings in a test, pass `_env_file=None` **and** scrub
  `GOOGLE_API_KEY*` from `os.environ`; a key exported in the developer's shell
  will otherwise leak in and make assertions machine-dependent
- database write semantics are verified against a **throwaway Neon branch**, never
  the main branch
- verify LLM-independent behaviour with stubs rather than live calls

---

## 11. Constraints and gotchas

**Free-tier quota.** 20 requests per day, per model, per Google Cloud project.
Each visit costs 3. Keys sharing a project share a bucket, so N keys do not
guarantee 20N requests. Switching model gets a separate bucket.

**Corpus blind spot.** Every follow-up in all 16 transcripts is *relative*
("three weeks"). The absolute-date branch of the resolver is covered by unit
tests only. A synthetic transcript saying "come on the 15th of March" would
close that gap. Similarly all 16 resolve to `scheduled`, so the
`conditional_only` and `not_discussed` queue paths have no real data behind them.

**Every seeded visit is `completed`**, so the Visit Schedule tab starts empty and
that empty state needs designing rather than discovering. Jay Patel
(`P-2026-00010`, no visits) exercises the empty patient case.

**Seeded patients have no phone number.** The follow-up queue exposes `phone`
because telephoning is the action on that screen, but it is NULL for all five
original patients.

**`riya_6` has swapped speaker labels.** Handled, not fixed in the data — see §8.4.

**Not under version control.** `git init` is worth doing; the pre-refactor
scripts are parked in `backend/legacy/` only because deletions would otherwise be
unrecoverable.

**`backend/runner.ipynb` is stale.** It imports the pre-refactor `pipeline`
module, which now lives in `legacy/`. Either update it to call
`app.services.insight_service` or delete it; `cli/` covers what it was for.

**Prompt edits invalidate provenance.** `prompt_version` is a hash of the three
template files, so editing a prompt changes it. That is intended — it lets you
tell which rows came from which prompts — but old rows are not comparable to new
ones. Bump `EXTRACTION_VERSION` in `app/services/insight_service.py` when the
*shape* of what you extract changes.

---

## 12. Conventions for future work

- Prompts are data, not code. Change behaviour by editing `prompts/*.md` where
  possible.
- Anything deterministic — dates, arithmetic, validation, id lookups — belongs in
  Python, not in a prompt.
- The database is the source of truth for patient facts (age, gender, dates).
  Never hand-supply them alongside a transcript.
- Services raise `AppError` subclasses; only `app/api/errors.py` knows about HTTP
  status codes. This is what lets the CLI reuse the same services.
- New scalar columns on `visit_insights` should be *promoted* from
  `insights_json`, not stored only in the JSON, when they need indexing.
- Repositories take and return plain dataclasses, not Pydantic models. Pydantic
  lives at the API boundary only.
- Comment the *why*, not the *what*, especially for the counter-intuitive items
  in §8. Several of them look like mistakes without their rationale.

### Working across backend and frontend

The HTTP API is the only contract between them. Nothing is shared at the file
level: no imported types, no common build step.

That means a backend change is breaking for the frontend only if it changes a
route, a field name, a status code, or the error shape. When one of those has to
change, add a new field rather than renaming, and update `frontend/README.md` in
the same edit — that file is what a frontend session reads instead of this one.

Endpoint or schema changes should also be reflected in `README.md`, which carries
a worked end-to-end example that will otherwise quietly go out of date.
