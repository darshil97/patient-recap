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

**Every endpoint below returns only the signed-in doctor's data** (§8.12, §8.20).
`GET /health` is the sole exception and needs no identity at all.

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
transcript  (loaded via visits.transcript_path + transcript_provider)
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
| Transcript storage | Neon Object Storage (S3-compatible) via `boto3`, or local disk |
| Date maths | `python-dateutil` (`relativedelta`) |
| Tests | stdlib `unittest` (no pytest in the env) |

Frontend:

| Concern | Choice |
| --- | --- |
| Framework | Next.js 16, App Router |
| Language | TypeScript 5.9, `strict` on |
| Styling | Tailwind v4 + the ported clinical design system |
| API types | generated from `/openapi.json` by `openapi-typescript` |
| API calls | `openapi-fetch`, same-origin only |
| Auth | Neon Auth / Managed Better Auth (`@neondatabase/auth`), Google OAuth |
| Auth UI | `@neondatabase/auth-ui` pre-built components |
| Node | 26.x, npm 11 |

The two Neon Auth packages are **beta-only on npm**, and `auth-ui` peer-pins
`auth` to an exact version, so both are pinned exactly in `package.json` rather
than with carets: caret ranges resolve prereleases unpredictably.

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
├── frontend/         Next.js app + BFF proxy (§5.2); README.md is the API contract
├── docs/             this file
├── README.md         getting started
├── run_local.sh      backgrounds both dev servers; `start|stop|status|restart|logs`
├── .run_local/       pid files and logs it writes (gitignored)
├── .kiro/            workspace config, steering, MCP
└── .vscode/
```

`run_local.sh start` preflights `backend/.env`, `frontend/.env.local`, the
`API_KEY` / `BACKEND_API_KEY` match, `node_modules`, and the dev TLS certificate,
generating the last two if absent. `stop` reaps by pid *and* by port, because
`npm run dev` sits between the script and the Next server so the recorded pid is
sometimes a wrapper whose child outlives it.

### 5.1 backend/

```
backend/
├── app/
│   ├── main.py                  FastAPI app factory, lifespan, CORS
│   ├── config.py                all settings in one typed place
│   ├── api/
│   │   ├── deps.py              DI: pooled connection, API key + doctor identity
│   │   ├── errors.py            AppError -> HTTP status, in one place
│   │   └── v1/                  patients, visits, jobs, health routers
│   ├── schemas/                 Pydantic request/response models
│   ├── domain/                  pure logic, zero I/O
│   │   ├── follow_up/           models.py + resolver.py
│   │   ├── transcripts.py       loading + path-traversal guard
│   │   └── identity.py          external_id, name/phone normalisation
│   ├── pipeline/                layers, prompts, parsing, results, runner
│   ├── storage/                 where the .txt lives: local disk or a Neon bucket
│   │   ├── base.py              the store contract + the locator guard
│   │   ├── local.py             backend/transcripts/{visit_id}.txt
│   │   ├── object_store.py      Neon Object Storage via boto3
│   │   └── factory.py           picks a backend from visits.transcript_provider
│   ├── llm/                     gemini.py (multi-key client), errors.py
│   ├── db/
│   │   ├── pool.py              connection pool
│   │   ├── models.py            row dataclasses
│   │   ├── tenancy.py           DoctorScope + ALL_DOCTORS: the tenancy boundary
│   │   └── repositories/        doctors, patients, visits, insights, follow_ups, jobs
│   ├── services/                the only place db + llm + pipeline meet
│   │   ├── patient_service.py
│   │   ├── visit_service.py
│   │   ├── insight_service.py   generation + persistence
│   │   └── job_service.py       enqueue, run, reap orphans
│   └── core/                    errors.py, logging.py
│
├── cli/
│   ├── load_insights.py         batch runner over the same services
│   ├── migrate_transcripts.py   move transcripts between storage backends
│   ├── render_prompt.py         render a prompt, no model, no DB
│   ├── check_keys.py            which API keys are usable
│   └── convert_transcripts.py   one-off JSON -> .txt migration (already run)
│
├── archive/transcripts_json/    the 16 original synthetic transcripts, tracked
│
├── prompts/                     Jinja2 templates, one per layer
├── transcripts/                 the 18 originals, <visit_id>.txt (§8.22)
├── sql/                         schema, applied in numeric order
│   ├── 1_patient_schema.sql
│   ├── 2_visit_schema.sql       + seed data for all 16 visits
│   ├── 3_visit_insights_schema.sql
│   ├── 4_insights_generated.sql migration
│   ├── 5_api_support.sql        migration: external_id, insight_jobs
│   ├── 6_frontend_support.sql   migration: lifecycle columns, follow_ups
│   ├── 7_multi_tenancy.sql      migration: doctors, patients.doctor_id
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

A Next.js 16 App Router app (TypeScript, Tailwind v4), plus the contract and the
design history:

```
frontend/
├── app/                    App Router
│   ├── (app)/              authenticated area, wrapped in the clinical shell
│   │   ├── page.tsx        "/" -> the follow-up queue
│   │   ├── follow-ups/     same queue under its own name
│   │   ├── schedule/       visit schedule
│   │   ├── patients/       list, and [patientId] detail/timeline
│   │   └── visits/         [visitId]: transcript upload, generation, brief
│   ├── auth/[path]/        Neon Auth views, deliberately outside (app)
│   ├── api/
│   │   ├── auth/[...path]/ Neon Auth handler
│   │   └── [...path]/      the BFF proxy to FastAPI
│   ├── globals.css         Tailwind + auth-ui theme + design-system.css
│   └── design-system.css   the approved clinical design (accent tokens in :root)
├── views/                  the six page components, ported from the Vite app
├── features/  components/  hooks/
├── lib/
│   ├── api/                openapi-fetch wrappers, same-origin only
│   ├── auth/               server.ts (secrets) and client.ts (browser)
│   └── types/api.d.ts      generated from /openapi.json
├── proxy.ts                route protection (middleware.ts on Next < 16)
├── legacy-vite/            the previous Vite SPA, kept for reference
├── design-history/         mockups and the design alternatives
└── README.md               the API contract to build against
```

**Nothing in the browser talks to FastAPI.** The backend's address and API key are
server-only environment variables (`BACKEND_URL`, `BACKEND_API_KEY`); there are no
`NEXT_PUBLIC_` variables at all, by design. The browser calls same-origin
`/api/v1/...`, and `frontend/app/api/[...path]/route.ts` verifies a Neon Auth
session and
forwards with `X-API-Key` attached. Three details in that proxy are load-bearing:
multipart bodies are forwarded as raw bytes with the incoming `Content-Type`
copied verbatim (re-encoding would mint a boundary that no longer matches the
body and break transcript upload), `X-Total-Count` is copied onto the response or
paginated lists lose their totals, and 204/205/304 are returned without a body
because constructing one throws.

Auth is Neon Auth (Managed Better Auth, `@neondatabase/auth`), already enabled on
this project with Google OAuth on shared development credentials. `proxy.ts`
protects every route except `/auth/*` and `/api/auth/*`; the BFF repeats the
session check for its own requests, since middleware guards navigation and says
nothing about a later fetch.

Two things this depends on staying true: the OpenAPI schema at `/openapi.json` is
the source of truth for types (`npm run gen:api`, don't hand-write), and
`cors_origins` in `app/config.py` still allows everything. CORS no longer matters
for the app itself now that traffic is same-origin through the BFF, but it must
still be narrowed before deployment.

`legacy-vite/` is the previous Vite SPA. It is kept only for reference and is
excluded from the Next build and typecheck. It cannot reach the API any more,
because it has no server able to hold `API_KEY`.

### Transcript format and where it is stored

Plain UTF-8 text, one per visit, addressed by visit id:

```
transcripts/{visit_id}.txt
```

That string is the visit's `transcript_path`, and it is **identical on every
storage backend**. `visits.transcript_provider` says which backend actually holds
the bytes:

| provider | meaning |
| --- | --- |
| `local` | a file at `backend/transcripts/{visit_id}.txt` |
| `neon_object_storage` | an object at that key in a Neon Object Storage bucket |

So the provider is the scheme and the path is the address. Keeping the string the
same across backends is what lets a migration flip one column and rewrite no
paths, and it means `get_by_transcript_path` (used by `load_insights
--transcript`) works against both. See §8.22.

Converted historical files and new uploads are identical, so replace, read and
delete need no special cases. The doctor's own filename lives in
`visits.transcript_filename` for display and never reaches a path or an object
key.

Transcripts were originally JSON with `patient_name`, `disease` and `transcript`
keys. `cli/convert_transcripts.py` migrated all 16 to `.txt`; the originals are
archived in `backend/archive/transcripts_json/` and tracked in git so a fresh
clone can rebuild the text files. `patient_name` was redundant (the pipeline
takes the name from the database) and `disease` was only ever a prop for
generating the synthetic transcripts, so neither survives (§8.3).

---

## 6. Data model

Six tables. `doctors` 1—N `patients` 1—N `visits` 1—1 `visit_insights`, plus
`follow_ups` and `insight_jobs`.

**`doctors` is the tenancy root, and it is the only place a doctor id is
stored.** Everything else reaches its owner by following foreign keys it already
had. That is the single most important thing to understand about this schema, so
it gets its own subsection below.

### doctors

`doctor_id` UUID PK, `email`, `auth_user_id`, `name`, `created_at`.

**Not a user-management system.** No password, session, roles or verification —
Neon Auth owns all of that (§8.12). This table exists so `patients.doctor_id` has
something to foreign-key against, and so a patient's owner survives an email
change.

| Column | Role |
| --- | --- |
| `auth_user_id` | Neon Auth's subject id. The **resolution key**. `TEXT`, not UUID: an opaque provider string whose format we must not assume. Nullable, because the migration seeded the first doctor before that doctor had signed in. |
| `email` | The label and the fallback. Unique via a functional index on `lower(btrim(email))`, so `Doctor@x.com` and `doctor@x.com` cannot become two tenants. |

Resolution prefers `auth_user_id` because email is mutable (§8.12). A row found by
email gets its `auth_user_id` stamped on once — `claim()` has
`AND auth_user_id IS NULL` in its `WHERE`, so a row can never be silently
re-pointed at a different account.

### Why tenancy is derived, not denormalised

`visits`, `visit_insights` and `follow_ups` deliberately have **no `doctor_id`
column**. Each already carries a foreign key that leads to `patients`:

```
visit_insights.visit_id -> visits.patient_id -> patients.doctor_id
follow_ups.patient_id ----------------------> patients.doctor_id
insight_jobs.visit_id  -> visits.patient_id -> patients.doctor_id
```

Denormalising would create four places that must agree about ownership, and
disagreement is silent: a stale `follow_ups.doctor_id` after a patient is
reassigned shows one doctor another doctor's patient, with no error anywhere.

`follow_ups` *does* denormalise `patient_id`, because the queue is the most-read
query — so it is worth saying why that does not carry over. The queue's `SELECT`
already joins `patients` unconditionally for `first_name`, `last_name`, `phone`
and `date_of_birth`. The join a denormalised `doctor_id` would avoid is one this
query performs anyway. It buys no read that is not already paid for, and costs an
update anomaly.

The one place this cost something: `count_follow_ups` used to read `FROM
follow_ups f` alone and now joins `patients` purely to reach `doctor_id`. One
indexed lookup on a primary key, against being right about who owns a row.

### patients
`patient_id` UUID PK. `doctor_id` FK -> `doctors(doctor_id)`, **NOT NULL**, `ON
DELETE RESTRICT` — deleting a doctor who still owns patients must fail loudly,
because cascading would delete clinical records because an account was removed,
and `SET NULL` would silently produce untenanted patients.

`external_id` is the clinic patient number, `UNIQUE NOT NULL`, generated by the
database as `P-2026-00042`, and **globally unique across all doctors** (§8.10).
Stores `date_of_birth`, not age, so age can be computed as at any visit date.

`doctor_id` is not updatable through the API. Moving a patient between doctors is
a transfer of clinical custody that would carry every visit, insight and
follow-up with it, not a demographics correction, so it is absent from
`PatientUpdate` and from the repository's update whitelist.

### visits
One row per consultation, i.e. per transcript file.

| Column | Notes |
| --- | --- |
| `visit_id` | UUID PK |
| `patient_id` | FK -> `patients(patient_id)` |
| `visit_number` | 1-based, `UNIQUE(patient_id, visit_number)`, auto-assigned |
| `visit_date` | **Anchors all follow-up resolution.** Must be the real date. |
| `visit_status` | scheduled / in_progress / completed / cancelled |
| `transcript_path` | always `transcripts/<visit_id>.txt`; null until attached |
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
api  ->  services  ->  { db, llm, storage, pipeline, domain }
cli  ->  services
```

`domain` and `pipeline` are I/O-free and unit-testable with stubs. `db`, `llm` and
`storage` are the edges that touch the outside world. `services` is the only place
they meet — which is why the HTTP API and the batch CLI cannot drift apart in
behaviour: they call the same functions.

A `doctor_id` travels down that whole chain as an ordinary argument. `api`
extracts it from the authenticated request context, `services` pass it through,
`db` puts it in the `WHERE` clause. Nothing below `api` knows or cares where it
came from, which is what makes replacing the trusted header with verified JWT
claims (§8.12) a one-file change.

Dependencies must stay acyclic. `app/pipeline/` must never import from `app/db/`
or `app/storage/` — it receives an already-loaded `Transcript` and does no I/O of
its own, which is what keeps the three-layer chain testable against stubs.

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

### 8.10 Patient uniqueness is layered, and each layer has a different scope

| Layer | Rule | Scope |
| --- | --- | --- |
| hard | `external_id`, UNIQUE NOT NULL, database-generated | **global**, all doctors |
| soft | normalised (first_name, last_name, date_of_birth) -> 409, overridable | **per doctor** |
| none | phone is normalised and indexed but **not** unique | n/a |

Phone is not a key because families share one mobile. Name is not a key because
spelling and transliteration vary. Together they both over-block and under-catch,
which is why identity is anchored on a system-owned id and demographics only
*flag* a probable duplicate for a human to confirm.

**The scope column is the part that was wrong.** The soft check had no doctor
filter, so it matched across every doctor. Two doctors having a patient with the
same name and DOB is ordinary, not a coincidence, so this did two wrong things at
once: it refused a legitimate registration with a 409 the doctor could do nothing
about, and the 409 body carried the *other* doctor's `patient_id` and
`external_id`. Now keyed on `(doctor_id, first_name, last_name, date_of_birth)`,
with `idx_patients_identity` replaced by `idx_patients_doctor_identity`. Still
advisory, still not a constraint.

**`external_id` stays globally unique**, and that is correct: it is generated from
one shared sequence, so collisions cannot happen in practice, and a global
constraint keeps a clinic number unambiguous. The disclosure risk was never the
constraint but what the API said when it was hit, so `add_patient` now has two
paths — a collision with **this doctor's own** patient returns the reusable
`patient_id`; a collision with **another doctor's** reports only
`match_reason: "external_id_unavailable"`, naming nothing. The repository helper
returns a bare boolean, not a row, so no version of the call can leak one.

Checked for other leaks through this key: there is **no lookup-by-external_id
route**, and the `q` search on `GET /patients` runs *inside* the doctor filter, so
clinic numbers cannot be probed by prefix.

### 8.11 Transcript paths are confined to one directory

Paths arrive from API payloads and are loaded by reference, so
`resolve_within()` fully resolves both the candidate and the allowed directory
before comparing. Without it, a payload of `../../etc/passwd` would be honoured.
It is not a substitute for auth and never was; it is what stops an authenticated
caller reading files outside `transcripts/`, which per-doctor login does nothing
to prevent.

### 8.12 Auth is two gates: a shared secret, and a doctor identity

Two mechanisms answering different questions. Both must pass on every route except
`GET /health`.

| Dependency | Question | Fails with |
| --- | --- | --- |
| `require_auth` | may this caller reach the API at all? | 401 |
| `require_doctor` | whose data is this request about? | 401 |

`require_auth` enforces `X-API-Key` when `API_KEY` is set in settings, and is a
no-op when it is not, so a checkout without the variable stays open. It uses
`hmac.compare_digest` rather than `==` so the check cannot leak the key's length
or content through timing; there is a test asserting that, because a refactor to
`==` would look harmless.

`require_doctor` depends on `require_auth`, so the key is checked **before** any
identity is trusted and before any row is written to `doctors` — otherwise an
unauthenticated request could create rows by presenting a new email. There is a
test for that ordering.

**The doctor-identity headers**, forwarded by the Next.js BFF from the signed-in
Neon Auth session:

| Header | From | Role |
| --- | --- | --- |
| `X-Doctor-Auth-Id` | `session.user.id` | resolution key, preferred |
| `X-Doctor-Email` | `session.user.email` | creates the row on first sign-in; matches the seeded row |
| `X-Doctor-Name` | `session.user.name` | display only |

Why two identity headers: `session.user.id` never changes for an account, so it is
the identity. Email is mutable — Better Auth ships a change-email flow — so keying
on it alone would resolve a doctor who changed address as a brand-new doctor with
an empty patient list. But email is the only thing knowable *before* a first
sign-in, which is what let `sql/7_multi_tenancy.sql` seed the first doctor. So:
resolve by `auth_user_id`, fall back to email, and stamp the auth id onto the row
the first time the two meet (§6).

Neither is ever accepted as a body or query param — `doctor_id` appears in no
request schema, and a structural test asserts that.

Missing or unresolvable identity is **401, not a fallback to a default tenant**.
An anonymous tenant would mean a misconfigured BFF silently pooling several
doctors' patients into one account instead of failing.

`GET /health` is exempt from both gates: a liveness probe is not a doctor.
`GET /health/api-keys` needs the key but not a doctor — it reports on the server's
own credentials and touches no patient data.

**The trust chain**

```
browser --(Neon Auth session cookie)--> Next.js BFF --(X-API-Key
          + X-Doctor-Auth-Id + X-Doctor-Email)--> FastAPI
```

FastAPI does **not** verify a Neon Auth token in this increment; it trusts the
BFF's assertion. That is sound only because the browser cannot reach FastAPI
directly, cannot obtain `API_KEY`, and the BFF re-checks the session on every
forwarded request. Change any one of those and it stops being sound.

**Consequence: `API_KEY` is now tenancy-bearing.** Anyone holding it can claim to
be any doctor. Before this it granted access to one undifferentiated dataset; now
it grants impersonation of a practitioner.

The later increment: verify the JWT against
`{NEON_AUTH_BASE_URL}/.well-known/jwks.json` and take `doctor_id` from the
verified `sub`, keeping the API key as the CLI's route in. The seam is
`require_doctor` and nothing else — every route, service and repository already
takes a `doctor_id` and does not care where it came from.

**Both key and identity headers are declared as `APIKeyHeader` security schemes,
not `Header(...)` params.** As params they would appear in every endpoint's
`parameters` and `npm run gen:api` would emit them as *required* per-endpoint
headers in `frontend/lib/types/api.d.ts`, breaking every browser-side call site —
which cannot supply them, because they are injected server-side. As security
schemes they land in `components.securitySchemes`, which openapi-typescript
ignores, and `/docs` still gets Authorize fields.

API tests bypass auth via `dependency_overrides`; enforcement has its own suites
(§10).

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

### 8.16 Visit dates are chained from the transcripts

Seed `visit_date` values in `sql/2_visit_schema.sql` are not arbitrary. Each
transcript ends with a stated interval, so each visit is dated by applying the
previous visit's interval. Visit N+1 lands exactly on the date visit N promised.

This gives a free end-to-end correctness check: a visit's resolved
`follow_up_date` should equal the next visit's actual `visit_date`.
`cli/load_insights.py` prints this per visit as a `check:` line and flags
`MISMATCH`. It is the single most useful signal that extraction is working.

---

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

### 8.20 Repositories cannot be called without naming a tenant

Every repository function touching `patients`, `visits`, `visit_insights`,
`follow_ups` or `insight_jobs` takes a `DoctorScope` as its **second positional
argument**, right after the connection. A call site that forgets it raises
`TypeError` before any SQL runs, rather than quietly returning another doctor's
records.

"No filter" is a value, not an absence:

```python
patients_repo.list_all(conn, doctor_id)      # one doctor
patients_repo.list_all(conn, ALL_DOCTORS)    # deliberately every doctor
```

`ALL_DOCTORS` is a distinct sentinel, not `None`, so an unset variable cannot
silently widen a query — `None` raises `ValueError`. `doctor_id_of()` refuses it
outright, because a write has no owner to record.

Reads put the filter in `WHERE`; **writes put it in their own `WHERE` too**, rather
than checking ownership first and then updating. Check-then-write leaves a window
and two places that must both be right. Two writes go further and resolve their
foreign key from a scoped `SELECT` — `visits.create` and `follow_ups.upsert` are
`INSERT ... SELECT FROM patients WHERE doctor_id = ...` — so another doctor's
patient id inserts nothing and returns `None`, which the caller turns into a 404.

Path-param lookups return **404, not 403**, for a row that exists but belongs to
someone else. A 403 would confirm the id is real. The repositories return `None`
for both "unknown" and "not yours", so the service layer cannot tell them apart
even if it wanted to.

Three places are cross-doctor on purpose, and only three:

| Where | Why |
| --- | --- |
| `jobs_repo.fail_orphans` | runs at startup, no request, no doctor. Reaping one doctor's jobs would leave every other doctor's stuck in `running`. |
| `jobs_repo.owner_doctor_id` | it is what *establishes* a scope — see below. |
| `cli/load_insights.py` | §8.21 |

Nothing under `app/api/` may use `ALL_DOCTORS`; a test greps for it.

**The async path.** `run_job` executes after the response is sent, holding only a
job id. Rather than query unscoped it resolves the owning doctor from the job's
visit (`owner_doctor_id`) and then does every read and write under that scope,
including re-reading the job row. So no code path touches patient data without
naming a tenant, including the asynchronous one.

### 8.21 The CLI stays privileged, on purpose

`cli/load_insights.py` runs across **every** doctor's visits unless given
`--doctor <email|uuid>`. That is the opposite of the API's rule, so it needs
justifying: a mandatory flag would not be a security boundary. The CLI is invoked
from a shell that already holds `CONNSTR`, so whoever runs it can reach every row
with `psql` regardless. Requiring the flag would buy the appearance of a boundary
without the substance.

What the flag is good for is cost and safety, so it is an optional narrowing. Each
visit is three model calls against a 20/day quota. The run header always prints
which doctors are in scope and `--list` grows a `doctor` column when unscoped, so
a cross-doctor run is visible rather than assumed. A `--doctor` matching nothing
exits non-zero instead of falling back to everyone.

`--doctor` narrows every selection mode, not just `--all`. In the unscoped path
each visit is generated under **its own owner's** id (`visit.doctor_id`), so the
privileged path cannot produce a row whose tenancy is ambiguous.

`cli/convert_transcripts.py` and `cli/migrate_transcripts.py` are cross-doctor
too, and correctly so: both are operator tasks over the whole database.

### 8.22 Transcripts live in object storage, behind a pre-existing seam

Transcripts are objects in a Neon Object Storage bucket, not files on the app's
disk. The notable part is not S3: `visits.transcript_provider` had been in the
schema since `5_api_support.sql` carrying `'local'`, so this was a second
implementation behind an existing seam.

**Why move off local disk: branching, not durability.** A Neon branch inherits its
parent's buckets *and their objects at fork time*, copy-on-write, then diverges.
So a preview branch gets a consistent pair — the visit rows and the transcripts
they point at. With local disk, branching the database produced visit rows whose
`transcript_path` pointed at files that either did not exist on that machine or,
worse, were the *live* ones shared by every branch, where a test upload would
overwrite production evidence.

**Reads follow the row; writes follow the setting.** `store_for_visit(visit)` routes
on `visit.transcript_provider`; `default_provider()` reads `TRANSCRIPT_STORAGE`.
That asymmetry is the design: a transcript stored before the setting changed stays
readable, and the two backends coexist, so a migration runs in batches, can be
interrupted, and resumes. A read that consulted the setting would have made
switching backends a flag-day over clinical data.

**An unknown provider raises rather than falling back.** A row naming a backend this
build lacks means a downgrade or a restore from a newer deployment. Reading local
disk instead would either 404 confusingly or — if a stale file were sitting there —
serve an older version of a clinical record as current.

**The key is `transcripts/{visit_id}.txt` with no `doctor_id`.** Same reasoning as
§6: a second place ownership is recorded, going stale on reassignment. It buys
nothing either, since every path to an object passes a doctor-scoped visit lookup
first (§8.20).

**The locator guard replaces a guarantee the filesystem gave.** On disk a malformed
path was caught by `resolve_within()`. Object storage has no parent directory to be
confined to — a key containing `../` is just a key, written without complaint. So
`app/storage/base.py` validates every locator against `transcripts/<uuid>.txt` on
every read, write and delete in both backends. The local backend keeps
`resolve_within` as well, because a well-shaped relative path can still land
somewhere unexpected through a symlink.

**A migration deletes nothing.** `cli/migrate_transcripts.py` reads the source,
writes the destination, reads *back* and compares text exactly, and only then flips
the column. Any failure leaves the row pointing at the source, which still has the
bytes. `--prune-local` is a separate step that refuses to delete unless the new
copy is verifiably readable.

`Transcript.path` is `Optional[Path]` and is `None` for any non-filesystem backend.
Nothing outside the local loader reads it — the pipeline uses `text` and
`relative_path` — but a new caller must not assume a filesystem location.

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
python -m cli.load_insights --all --doctor me@example.com   # narrow it (§8.21)

# Where transcripts are stored, and moving them between backends (§8.22)
python -m cli.migrate_transcripts --status
python -m cli.migrate_transcripts --to neon_object_storage --dry-run
python -m cli.migrate_transcripts --to neon_object_storage
python -m cli.migrate_transcripts --verify
python -m cli.migrate_transcripts --prune-local      # only after --verify passes

# Which API keys are usable (one small request each)
python -m cli.check_keys

# Render a prompt without calling the model — best way to iterate on prompts
python -m cli.render_prompt --transcript transcripts/<visit_id>.txt \
  --name Riya --age 24 --gender Female --call-date 2026-02-16 --layer 1

# Tests
python -m unittest discover -s tests -t .
```

Other `load_insights` flags: `--doctor <email|uuid>` to narrow to one doctor
(cross-doctor by default, §8.21), `--sleep N` to pace under per-minute limits,
`--log-file run.log` to tee output, `--model` to switch model (each has its own
daily quota bucket), `--stop-after`, `--stop-on-error`.

Ctrl-C once stops after the visit in flight; twice abandons it immediately. Either
way nothing partial is written and re-running skips completed visits.

Schema is applied in numeric order, and migrations 1 through 7 are all applied
to the current database. `sql/7_multi_tenancy.sql` seeds the first doctor from an email set at the
top of its section 2 and aborts on the placeholder, so a fresh database needs that
line edited before running.

### Frontend

From `frontend/`, with Node 26 (no conda involved):

```bash
cd frontend
cp .env.local.example .env.local     # then fill it in, see below
npm install

npm run dev          # https://localhost:3000  — note the s
npm run build        # production build
npm run typecheck    # tsc --noEmit
npm run gen:api      # regenerate lib/types/api.d.ts from the live schema
```

**`npm run dev` serves HTTPS, and it has to.** Neon Auth's session-challenge cookie
is `__Secure-` prefixed with the `Secure` flag, and **Safari refuses to store
`Secure` cookies over plain HTTP even on localhost** — Chrome and Firefox make an
exception for local development, Safari does not. Over `http://` the cookie is
dropped, so the OAuth callback returns a session verifier that cannot be exchanged,
`/api/auth/get-session` answers `400 SESSION_CHALLENGE_COOKIE_NOT_FOUND`, and you
land back on the sign-in screen **with no error shown and nothing in the backend
log** — the backend is never called. It looks like a broken login and is a dropped
cookie. The SDK's `SessionCookieConfig` exposes `secret`, `sessionDataTtl`,
`domain` and `sameSite` only, so the flag cannot be turned off; the origin has to be
secure.

`frontend/certificates/` holds a self-signed, gitignored cert, so the browser warns
once per origin (**Show Details → visit this website**). `npm run dev:mkcert`
installs a trusted local CA instead but needs sudo; `npm run dev:http` is the old
behaviour, fine for Chrome and for `gen:api`.

Neon Auth's trusted-domain allowlist is per **origin**, and `https://localhost:3000`
is a different origin from `http://localhost:3000`.

`npm run gen:api` needs the backend running and reads `BACKEND_URL` (default
`http://127.0.0.1:8000`). Regenerate after any change to a request or response
model; the committed file goes stale silently otherwise, and a stale one is worse
than none because it typechecks.

Both services must run for the app to work: uvicorn on 8000 and Next on 3000. The
frontend has no fallback if the backend is down — the BFF returns
`502 backend_unreachable`.

`.env.local` holds four values, all server-only:

| Variable | Where it comes from |
| --- | --- |
| `NEON_AUTH_BASE_URL` | Neon Console → branch → Auth → Configuration |
| `NEON_AUTH_COOKIE_SECRET` | `openssl rand -base64 32`, 32+ chars |
| `BACKEND_URL` | `http://127.0.0.1:8000` locally |
| `BACKEND_API_KEY` | must match `API_KEY` in `backend/.env` |

If `BACKEND_API_KEY` and the backend's `API_KEY` disagree, every screen fails with
a 401 that looks like a session problem but is not. First thing to check when the
UI is empty but the API works from curl.

## 10. Testing approach

Six files, 295 tests, all offline — no API calls, no database, no object storage.

`test_transcripts.py` covers the loader, the traversal guard, and upload
validation, including the BOM case and that the client filename never reaches a
path.

`test_storage.py` covers transcript storage (§8.22): the locator guard, both
backends, and provider routing. The backends share a `StoreContractTests` mixin
rather than parallel assertions, so they cannot diverge on what read/write/delete
mean. The object backend takes its S3 client by injection, so an in-memory fake
covers key handling and error mapping — including that a `403` is a storage error
and **not** a missing transcript — with no network, no credential, and without
boto3 installed.

`test_follow_up_tracking.py` covers which follow-ups get queued, that the upsert
preserves human decisions, and the resolve/reopen transitions.

`test_follow_up.py` covers date resolution against every timing pattern the corpus
contains, with hand-checked expected dates, plus malformed input, bad anchor
dates, and prompt wiring.

`test_gemini_llm.py` covers key rotation with stubbed clients: fall-through,
stickiness, permanent disabling of rejected keys, request errors not touching
other keys, and total exhaustion.

`test_api.py` covers routing, validation, status codes and error mapping with the
DB dependency overridden and the services patched. It also holds the four
auth/tenancy suites, which are structured so they fail for the right reasons:

- **`TestApiKeyAuth`** — the shared secret with enforcement on. It deliberately does
  *not* override `require_doctor`: that would replace the `require_auth` sub-tree
  beneath it and the key check would never run, so every assertion would pass
  vacuously. It stubs the doctors *repository* instead.
- **`TestDoctorIdentityAuth`** — the tenancy gate with the key gate off, so a 401 can
  only come from a missing or unresolvable doctor. The mirror image.
- **`TestCrossTenantIsolation`** — **the most important suite in the project.** A
  regression here is a real patient data leak. Doctor B is signed in throughout and
  every id belongs to doctor A. It does not merely assert "the route returned 404",
  which would still pass if a refactor dropped the SQL filter and relied on the
  route to check afterwards; each test asserts on the doctor id the API layer
  *hands the service*.
- **`TestScopedRepositorySql`** — the other half of that boundary, asserting the
  queries themselves carry a doctor filter, so a refactor that keeps the argument
  but drops it from the `WHERE` fails here. Sources are read with whitespace
  collapsed, because these helpers are called across line breaks.

Conventions:

- stdlib `unittest`; `tests/__init__.py` must exist for discovery to work
- tests insert the project root on `sys.path`
- when reading settings in a test, pass `_env_file=None` **and** scrub
  `GOOGLE_API_KEY*`, `AWS_*` and `TRANSCRIPT_*` from `os.environ`; a value exported
  in the developer's shell will otherwise make assertions machine-dependent
- fixtures carry a `doctor_id`. `test_api.py` defines two tenants, `DOCTOR_A_*` and
  `DOCTOR_B_*`, plus the header dicts the BFF would send for each
- database write semantics are verified against a **throwaway Neon branch**, never
  the default branch
- verify LLM-independent behaviour with stubs rather than live calls

## 11. Constraints and gotchas

**Free-tier quota.** 20 requests per day, per model, per Google Cloud project. Each
visit costs 3. Keys sharing a project share a bucket, so N keys do not guarantee
20N requests. Switching model gets a separate bucket.

**Live data is 1 doctor, 7 patients, 18 visits.** All owned by
`owner@example.com`, created by `sql/7_multi_tenancy.sql`. **"16" throughout
the older parts of this document means seeded *transcripts*, i.e. visits, not
patients.** Five original patients plus Jay Patel (`P-2026-00010`, no visits — the
empty-patient case) plus Mitesh Shah, and two visits added through the API since
seeding. Don't assert a count in a test or migration without checking; the tenancy
backfill deliberately used `WHERE doctor_id IS NULL` with no expected row count.

**A second doctor sees an empty application, and that is correct.** No patients, no
visits, empty queue, zero counters. Nothing in the UI distinguishes "new account"
from "backend is broken", so the empty states matter more than they did when the
only user had every patient.

**`API_KEY` is tenancy-bearing** (§8.12). Rotating it is no longer just hygiene.

**Object storage is per-branch, and so is its credential.**
`AWS_ENDPOINT_URL_S3` differs per branch, and a credential minted on the default
branch is valid there and on descendants only — elsewhere it is `403 AccessDenied`.
The secret is shown **once** at creation and cannot be retrieved; a lost one must be
revoked and replaced. Object storage is beta, currently `aws-us-east-2` and
`aws-eu-central-1`; this project is in `aws-us-east-2`.

**15 of 18 briefs show "This brief may be out of date", and that is a known false
positive we have decided to live with.** `features/visits/InsightBrief.tsx` flags a
brief stale when `visit.transcript_uploaded_at > insight.generated_at`. That column
records when the transcript *columns were last written*, not when the *content*
changed — and `cli/convert_transcripts.py` bumped it on 2026-09-05 when it
converted JSON to `.txt`, after those 15 briefs were generated on 2026-08-24. The
words are unchanged: all 18 stored transcripts have been verified byte-identical to
`backend/archive/transcripts_json/`.

Do **not** "fix" this by recompiling — that is 3 model calls per visit against a
20/day quota to rebuild briefs from identical input. Do not backdate the timestamps
either; that falsifies when the files were actually written.

The real fix, deliberately deferred, is to compare content rather than time: add
`visit_insights.transcript_sha256`, populate it at generation, and have the UI
compare hashes with the timestamp as a fallback. Then the warning fires only when
the words actually change, which is the question it is trying to answer. Until then
the warning is noise on those 15, and the feature is still correct in principle —
a genuinely replaced transcript *should* invalidate a brief.

A related bug is already fixed: `cli/migrate_transcripts.py` originally reused
`visits_repo.set_transcript()`, which stamps `transcript_uploaded_at = now()`, so
the move to object storage briefly flagged all 18. It now uses
`set_transcript_provider()`, which touches the provider only, and
`tests/test_storage.py::TestMigrationDoesNotClaimAnUpload` holds that line.

**`backend/transcripts/` still holds the 18 originals.** All 18 transcripts were
migrated to object storage and verified byte-for-byte; the local copies are
redundant but deliberately kept, and `--prune-local` removes them when you decide
to. `TRANSCRIPT_STORAGE` governs only *new* uploads (§8.22), so changing it moves
nothing.

**`npm run dev` serves HTTPS, and Safari is why** (§9). Reverting to HTTP gives a
sign-in that silently loops back to the login screen with nothing in any log.

**The auth screens are themed by mapping `--neon-*` onto the design tokens.**
`defaultTheme="light"` on `NeonAuthUIProvider` is load-bearing — auth-ui defaults
to `"system"`, so on a dark-mode machine the card rendered dark against a
light-only app. There is also a real name collision: our `--accent` is the brand
teal, shadcn's `--accent` is a muted hover surface, so removing those overrides as
"redundant" reintroduces teal hover blocks. See `frontend/README.md`.

**Google's consent screen says "continue to neon.tech"**, because the project uses
Neon's *shared* Google OAuth credentials (`social_providers[].isShared = true`).
Fixing it needs your own Google OAuth client plus consent-screen branding, with the
redirect URI `{NEON_AUTH_BASE_URL}/callback/google` — not the app's URL.
Deliberately deferred; a new client also starts in Testing status where only listed
test users can sign in.

**`CORS_ORIGINS` still defaults to `*`.** It does not affect the app, whose traffic
is same-origin through the BFF, but it must be narrowed before deployment: `*`
cannot be combined with credentialed requests.

**Corpus blind spot.** Every follow-up in the seeded transcripts is *relative*
("three weeks"), so the absolute-date branch of the resolver has unit-test coverage
only. All of them resolve to `scheduled`, so the `conditional_only` and
`not_discussed` queue paths have no real data behind them.

**Every seeded visit is `completed`**, so the Visit Schedule tab starts empty and
that empty state needed designing rather than discovering.

**Seeded patients have no phone number.** The follow-up queue exposes `phone`
because telephoning is the action on that screen, but it is NULL for the five
original patients.

**`riya_6` has swapped speaker labels.** Handled, not fixed in the data — §8.4.

**`backend/runner.ipynb` is stale.** It imports the pre-refactor `pipeline` module,
now in `legacy/`. Update it to call `app.services.insight_service` or delete it;
`cli/` covers what it was for.

**Prompt edits invalidate provenance.** `prompt_version` is a hash of the three
template files, so editing a prompt changes it. Intended — it tells you which rows
came from which prompts — but old rows are not comparable to new ones. Bump
`EXTRACTION_VERSION` in `app/services/insight_service.py` when the *shape* of what
you extract changes.

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
- **A new repository function takes `DoctorScope` as its second positional
  argument.** No exceptions without a comment saying why, next to the code. A new
  route gets `doctor: CurrentDoctor = Depends(require_doctor)` and passes
  `doctor.doctor_id` down; it never accepts a doctor id from the client.
- A new table owned by a doctor should reach `patients.doctor_id` through a
  foreign key rather than carrying its own `doctor_id` column (§6).
- Anything that reads or writes transcript bytes goes through `app/storage/`, not
  through `open()` or boto3 directly. A new backend implements the four methods on
  `TranscriptStore` and is registered in `factory.get_store`; everything
  provider-independent (upload validation, the completed-visit rule, the
  insights-exist rule) stays in `transcript_service` (§8.22).
- Comment the *why*, not the *what*, especially for the counter-intuitive items
  in §8. Several of them look like mistakes without their rationale.

### Working across backend and frontend

The HTTP API is the only contract between them. No code is shared at the file
level: no imported modules, no common build step.

There is one generated artefact, though, and it is the thing most likely to bite:
`frontend/lib/types/api.d.ts` is produced from `/openapi.json` by
`npm run gen:api`. It is committed, so it can fall behind the backend without
anything failing loudly — a stale copy still typechecks, it just describes an API
that no longer exists. This has already happened once: the file kept declaring
`X-API-Key` as an ordinary per-endpoint parameter long after §8.12 moved it to a
security scheme.

So a backend change is breaking for the frontend if it changes a route, a field
name, a status code, or the error shape. When one has to change:

1. Add a new field rather than renaming one.
2. Run `npm run gen:api` in the same piece of work.
3. Update `frontend/README.md`, which is what a frontend session reads instead of
   this document.
4. Update `README.md`, whose worked end-to-end example otherwise goes quietly out
   of date.

Two operational couplings that are not visible in the API contract. The
frontend's `BACKEND_API_KEY` must equal the backend's `API_KEY` (§8.12) or every
screen 401s in a way that resembles a session bug. And enabling auth on a new
deployment means registering that origin as a Neon Auth trusted domain, or the
OAuth callback is refused with no useful error.
