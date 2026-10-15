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
PATCH  /api/v1/visits/{visit_id}              correct date/time/type only (§8.17)
POST   /api/v1/visits/{visit_id}/start        start the session -> in_progress (§8.24)
POST   /api/v1/visits/{visit_id}/complete     mark the consultation done
POST   /api/v1/visits/{visit_id}/cancel       cancel, reopening its follow-ups

POST   /api/v1/visits/{visit_id}/transcript   upload the .txt (multipart)
POST   /api/v1/visits/{visit_id}/audio-uploads   or a recording: start, get chunk plan (§8.23)
POST   /api/v1/visits/{visit_id}/audio-uploads/live   or record live, sent while recording (§8.24)
PUT    /api/v1/audio-uploads/{upload_id}/chunks/{index}   one chunk, idempotent
POST   /api/v1/audio-uploads/{upload_id}/finish   stitch + transcribe -> 202 (also retries)
POST   /api/v1/audio-uploads/{upload_id}/discard  throw an unfinished one away
GET    /api/v1/audio-uploads/{upload_id}          poll until completed | failed
GET    /api/v1/visits/{visit_id}/audio-upload     latest, to resume polling (204 if none)
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

`PATCH /visits/{id}` corrects a visit's own details — date, time, type — and
**cannot** change `visit_status` or touch the transcript. `/start`, `/complete`
and `/cancel` are the only status transitions (§8.17); `/start` (scheduled →
`in_progress`) is optional and exists for live recording (§8.24). A transcript is
attached only by uploading one — `POST /transcript`, or a recording that becomes
one (§8.19, §8.23).

Every list endpoint returns the total in `X-Total-Count`, and it counts the same
filters as the list (§7). A total larger than the array means there are more
pages, not that something is missing.

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
| Validation / settings | Pydantic 2; `config.yaml` (PyYAML) + `.env` secrets (python-dotenv) |
| LLM | Google Gemini via `langchain-google-genai`, or Amazon Bedrock via `langchain-aws` (`llm.provider`) |
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
│   ├── config.py                loads config.yaml (+ .env secrets) into typed settings
│   ├── api/
│   │   ├── deps.py              DI: pooled connection, API key + doctor identity
│   │   ├── errors.py            AppError -> HTTP status, in one place
│   │   └── v1/                  patients, visits, jobs, health routers
│   ├── schemas/                 Pydantic request/response models
│   ├── domain/                  pure logic, zero I/O
│   │   ├── follow_up/           models.py + resolver.py
│   │   ├── transcripts.py       loading + path-traversal guard
│   │   ├── diarization.py       speaker turns -> DOCTOR:/PATIENT:/OTHER: text (§8.23)
│   │   └── identity.py          external_id, name/phone normalisation
│   ├── pipeline/                layers, prompts, parsing, results, runner
│   ├── storage/                 where the .txt lives: local disk or a Neon bucket
│   │   ├── base.py              the store contract + the locator guard
│   │   ├── local.py             backend/transcripts/{visit_id}.txt
│   │   ├── object_store.py      Neon Object Storage via boto3
│   │   ├── factory.py           picks a backend from visits.transcript_provider
│   │   └── audio.py             upload chunks + recordings (bytes), same two backends
│   ├── speech/deepgram.py       recording -> diarized utterances, over REST (§8.23)
│   ├── llm/                     factory.py (reads llm.provider), errors.py, base.py
│   │   └── providers/           gemini.py, bedrock.py: LangChain chat models
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
│   ├── check_keys.py            does the model provider's key work
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
│   ├── 8_cancellation_reason.sql migration: visits.cancelled_reason
│   ├── 9_audio_uploads.sql      migration: audio_uploads, audio_upload_chunks
│   ├── 10_live_recordings.sql   migration: audio_uploads.mode, plan nullable while live
│   └── extra.sql                future tables, all commented out
├── tests/                       unittest suites
├── legacy/                      pre-refactor scripts, safe to delete
├── requirements.txt
├── config.yaml                  all non-secret settings, committed
└── .env / .env.example          secrets only, referenced from config.yaml as ${NAME}
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
│   ├── (home)/             "/" -> the public home page: what the product is, sign-in/sign-up CTAs
│   ├── (app)/              authenticated area, wrapped in the clinical shell
│   │   ├── today/          "/today" -> the follow-up queue, where sign-in lands
│   │   ├── follow-ups/     same queue under its own name
│   │   ├── schedule/       visit schedule
│   │   ├── patients/       list, and [patientId] detail/timeline
│   │   └── visits/         [visitId]: transcript upload, generation, brief
│   ├── auth/[path]/        Neon Auth views, deliberately outside (app)
│   ├── api/
│   │   ├── auth/[...path]/ Neon Auth handler
│   │   └── [...path]/      the BFF proxy to FastAPI
│   ├── error.tsx           route-level error boundary
│   ├── global-error.tsx    for a throw in the root layout itself
│   ├── globals.css         Tailwind + auth-ui theme + design-system.css
│   └── design-system.css   the approved clinical design (accent tokens in :root)
├── views/                  the six page components, ported from the Vite app
├── features/  components/  hooks/
│   └── features/recording/ the live recorder: provider (in the (app) layout), panel, bar (§8.24)
├── lib/
│   ├── recording/          pure: format picker, exact-size chunker, uploader, level meter maths
│   ├── api/                openapi-fetch wrappers, same-origin only
│   ├── auth/               server.ts (secrets) and client.ts (browser)
│   ├── routes.ts           PUBLIC_HOME, APP_HOME, SIGN_IN, SIGN_UP: paths several modules must agree on
│   └── types/api.d.ts      generated from /openapi.json
├── tests/                  Vitest: dates, the polling hook, the BFF proxy
├── eslint.config.mjs       Next's rule set; `npm run lint`
├── vitest.config.ts
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
protects every route except `/`, `/auth/*` and `/api/auth/*`; the BFF repeats the
session check for its own requests, since middleware guards navigation and says
nothing about a later fetch.

**`/` is public, but still runs through Neon Auth's middleware.** That middleware
is what completes Google sign-in: the OAuth callback returns to the provider's
`redirectTo` with a session verifier in the query string, and the middleware
exchanges it on whatever page it lands on. Excluding `/` from the matcher would
break any callback that arrives there. Neon Auth's Next adapter cannot be given
extra public routes, so `proxy.ts` interprets its answer for `/` instead: a
redirect to the sign-in page means signed out (serve the home page), being let
through means signed in (redirect to `/today`), and any other redirect is the
OAuth exchange (pass through). `NeonAuthUIProvider`'s `redirectTo` is `/today`
(`APP_HOME` in `lib/routes.ts`), so sign-in lands on the queue directly rather
than bouncing off the home page.

`NeonAuthUIProvider`'s `.neon-auth-ui` wrapper encloses the whole app, not just
auth-ui's components, so an element selector under it in `globals.css` reaches
every page. A `.neon-auth-ui a` rule once outranked `.btn--primary` and drew teal
text on every teal `<Link>` button; it is now scoped to auth-ui's
`[data-slot="card"]`.

One thing this depends on staying true: the OpenAPI schema at `/openapi.json` is
the source of truth for types (`npm run gen:api`, don't hand-write).

`cors_origins` still defaults to `*` for local development, but the deployment
narrows it to the Vercel origin (§9.5). CORS does not matter for the app itself
— traffic is same-origin through the BFF — so this is defence for the browser
path nothing currently uses, not something the app relies on.

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

Eight tables. `doctors` 1—N `patients` 1—N `visits` 1—1 `visit_insights`, plus
`follow_ups`, `insight_jobs`, and `audio_uploads` with its `audio_upload_chunks`.

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
| `insights_generated` | TRUE only after all *three* layers succeed *and* the row is written |
| `cancelled_reason` | why, in the doctor's words. NULL means not recorded, which is every visit cancelled before `sql/8_cancellation_reason.sql` |

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

### audio_uploads, audio_upload_chunks
One row per recording upload (§8.23), tenancy derived through `visit_id` like
`insight_jobs`. `status` is receiving / processing / completed / failed /
abandoned / superseded. Two partial unique indexes: one upload in flight per visit,
and one stored recording per visit (`completed` with `audio_path`). The completed
row is *the* visit recording; `speakers` and `warnings` hold its labelling for the
UI. `audio_upload_chunks` lists the pieces received and is emptied once they are
stitched.

`mode` is `file` or `live` (§8.24). A live recording has `total_bytes` and
`chunk_count` NULL while it is receiving; finish writes them. The
`audio_uploads_plan_known` CHECK allows a NULL plan only for a live row that is
`receiving` or `abandoned`, so nothing reaches processing without one, and the
existing cover constraint still holds once it is set.

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

**That rule now holds for reads as well as writes.** Every list endpoint used to
import its repository and call it directly, doing its own `count` for
`X-Total-Count` alongside. Nothing was leaking — the repositories are scoped
either way — but it left the page size, the cap, and the pairing of a list with
its count written out once per endpoint and held together by nothing, which is
how three screens each ended up choosing their own limit and filtering the
remainder in the browser. Lists go through `services` now and return a `Page`
(`app/services/pagination.py`): the rows, and the total for the *same* filters.
`tests/test_api.py::test_routers_go_through_services_not_repositories` greps for
regressions.

`app/api/deps.py` is the one module under `api` that may touch a repository, and
it is not a route: `require_doctor` is what *establishes* the scope every service
below it takes as an argument, so it has to reach `doctors` before any service
exists to ask.

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

### 8.8 Two providers, one key each, three-way error classification

`llm.provider` in `config.yaml` picks Gemini or Amazon Bedrock. Both are LangChain
chat models built in `app/llm/providers/` (`ChatGoogleGenerativeAI`,
`ChatBedrockConverse`), and `app/llm/factory.py` is the only code that reads the
setting. Each provider has a single API key; the earlier multi-key rotation for
Gemini's free tier is gone. Retries on throttling are left to each provider's own
client (`llm.max_retries`).

`app/llm/errors.py` still sorts failures three ways, by message text:

| Kind | Example | Response |
| --- | --- | --- |
| quota / transient | 429, `RESOURCE_EXHAUSTED`, `ThrottlingException`, 503 | `QuotaExhaustedError` → 429, job `failed_quota` |
| key rejected | `API_KEY_INVALID`, `AccessDeniedException`, 401 | reported as `bad_key` by `cli.check_keys` |
| request problem | unknown model, safety block | raised as-is |

Bedrock's gpt-oss returns its reasoning as separate `reasoning_content` blocks.
`AIMessage.text` leaves those out, so JSON parsing sees only the answer.

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

**The API always runs all three layers.** `GenerateInsightsRequest` has no
`stop_after`, and `insight_service.require_complete_brief()` refuses to persist a
run that did not reach Layer 3. Both are needed and they guard different things.

Layer 3 is the doctor-facing part and the frontend renders it straight out of
`insights_json`, so a partial run wrote a row with no `layer_3` while
`insights_repo.save` set `visits.insights_generated` TRUE in the same transaction
(§8.7) — a visit that *claimed* to have a brief, and blanked the page when
opened. Removing the request field closes the route a client could take; the
guard at the write is what makes it true for the CLI too, which still has
`--stop-after` and should, because iterating on an early prompt without paying
for the whole chain is exactly what it is for. A partial CLI run now fails with
`layer_output_error` and writes nothing, which is the honest outcome — the layer
output is still printed as it goes.

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

**The email fallback is one-way, and that matters more than it looks.** It may
*claim* an unclaimed row; it may not serve a row already bound to a different
`auth_user_id`. Without the second half, the fallback quietly inverted the whole
preference order: a request presenting auth id B with doctor A's email resolved
as doctor A, so anyone naming the right address got the tenant regardless of
which account they were signed in as, and `auth_user_id` was decorative. `claim()`
had `AND auth_user_id IS NULL` and so refused to re-*point* a claimed row — but
nothing stopped that row being *read*. A mismatch is now `None`, which the
dependency turns into 401, and is logged at warning: it means either an address
moved between accounts or someone is presenting an email that is not theirs, and
both need a human.

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

### 8.17 Status is a transition, not a field

`PATCH /visits/{id}` used to force `visit_status = 'completed'` as a side effect
of setting `transcript_path`. That contradicts an explicit Complete button and
makes the real state depend on the order of two unrelated actions, so it was
removed. Status changes go through `/complete` and `/cancel` only.

**`visit_status` itself stayed settable through PATCH long after that, which
undid most of the above.** `VisitUpdate` accepted it, the service forwarded it,
and the repository's whitelist included it — directly under a docstring saying it
deliberately did not. So `{"visit_status": "cancelled"}` reached the column while
skipping everything `/cancel` exists to do: the refusal once insights exist, the
reopening of the follow-ups that visit had resolved, and `cancelled_at`. A
patient who still needed calling dropped off the queue with no error anywhere.
`{"visit_status": "completed"}` left `completed_at` NULL and could revive a
cancelled visit that `/complete` would have refused.

It is gone from all three places. `VisitUpdate` is `visit_date`, `visit_time` and
`visit_type`, and is the **one model in the API with `extra="forbid"`**: every
field removed from it was previously *honoured*, so silently ignoring one would
answer 200 to "cancel this visit" and do nothing. Elsewhere an unknown field is
still ignored, which is right where it was never honoured (`transcript_provider`
on `VisitCreate`).

The test that should have caught this mocked `visit_service.update_visit`
wholesale, so it only ever proved the *route* did not invent a status.
`TestVisitLifecycleIsTransitionsOnly` asserts on the service, the schema, the
OpenAPI document and the repository's whitelist source.

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

**And the address is no longer client-supplied at all.** `VisitCreate` and
`VisitUpdate` both used to accept a `transcript_path`, checked by resolving it
inside the transcripts directory and stat-ing the file. That proves a file
exists; it does not prove it is *yours*. The locator is
`transcripts/<visit-uuid>.txt` with no `doctor_id` in it, deliberately (§8.22),
so every valid visit uuid passes the check — including another doctor's. Pointing
your own visit at their locator and reading it back through
`GET /visits/{id}/transcript` returned their verbatim consultation.

It was not reachable in the deployed configuration: object storage is the
provider there and the local transcripts directory does not survive a Render
restart, so the `is_file()` check failed. But that is an accident of deployment,
not a boundary, and it would come back the first time a second doctor used
local transcript storage. Both fields are removed. Uploading is the only way a
transcript is attached, and it derives the key from the visit id.

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

These places are cross-doctor on purpose, and only these:

| Where | Why |
| --- | --- |
| `jobs_repo.fail_orphans` | runs at startup, no request, no doctor. Reaping one doctor's jobs would leave every other doctor's stuck in `running`. |
| `audio_uploads_repo.fail_orphans`, `.stale_receiving` | the same startup sweep (called from `job_service.reap_orphans`) for recording uploads, §8.23. |
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
on `visit.transcript_provider`; `default_provider()` reads `transcripts.storage`.
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

### 8.23 Recordings: sent in chunks, stored once, transcribed into an ordinary transcript

A recording ends up as exactly what a `.txt` upload produces —
`transcripts/{visit_id}.txt` in the `DOCTOR:` / `PATIENT:` format — so reading
back, the three-layer chain and delete have no audio branch. The recording itself
is kept too, as one file per visit.

```
browser  POST /visits/{id}/audio-uploads   checks + chunk plan        -> receiving
         PUT  /audio-uploads/{id}/chunks/i  x N, 4 MB each, retried
         POST /audio-uploads/{id}/finish    202                        -> processing
backend  stitch chunks -> recordings/<visit>/<upload><ext>, verify size, delete chunks
         Deepgram (diarize_model=latest) -> utterances by "Speaker 0/1/2"
         LLM, prompts/speaker_identification.md -> a role per voice
         app/domain/diarization.py -> DOCTOR: / PATIENT: / OTHER (patient's mother):
         transcript_service.save_transcript_text
         job_service.enqueue (insight job queued)           -> completed | failed
         job_service.run_job, same background task, own connection
browser  GET  /audio-uploads/{id}  polls; on completed, follows GET /visits/{id}/job
```

**Insights start on the server, with no click.** Unlike a `.txt` upload, where
the browser chains `POST /insights`, the background task queues the job itself
once the transcript is stored, then runs it. So a brief is compiled even if the
uploading tab is closed. The job is queued *before* the upload is marked
`completed`, so a poller that sees `completed` always finds the job. Queueing is
best-effort: if it is refused (a job already running), the upload still
completes and the visit offers Generate insights. A failed or quota-limited job
is retried from the UI as for any other brief.

**Why chunks.** Every browser request goes through the BFF, and on Vercel a
function's request body is capped at 4.5 MB; consultation recordings are larger.
`speech.chunk_bytes` (4,000,000) stays under it with room for the multipart
envelope. Each chunk must be *exactly* its planned size — every one
`chunk_bytes` except the remainder — and the plan must cover the file (a CHECK
constraint), so "every chunk arrived" means "the stitched file is byte-for-byte
the original". Resending a chunk replaces it, which is what makes the browser's
per-chunk retry safe.

**Stitching** happens in a temporary file on disk and goes up as one PUT, not an
S3 multipart upload: the chunks are below S3's 5 MB part minimum, and a single PUT
does not depend on the endpoint supporting multipart. The stored size is checked
against the plan before any chunk is deleted, because until then the chunks are
the only copy.

**One recording per visit**, enforced by a partial unique index. The key carries
the upload id, so a replacement is written beside the current recording, and the
old one is deleted only once the new upload has completed. A failed replacement
therefore never costs the visit the recording its transcript came from.

**Processing is a background task**, the §8.9 pattern: state in `audio_uploads`,
`finish` returns 202, the worker resolves the owning doctor from the upload id and
works under that scope. Stitching plus speech-to-text plus a model call can
outlast both the BFF's 25 s timeout and a Vercel function's duration limit. A
failed upload keeps its chunks or recording, so Finish again retries without a
re-upload (`retryable`). Startup (`job_service.reap_orphans`) fails uploads a dead
process left in `processing` and discards ones nobody has sent a chunk to for a
day. Starting a new upload for a visit discards any unfinished one.

**Everything checkable is checked before a byte is sent**: visit completed,
`replace`, format, size, and that `DEEPGRAM_API_KEY` and the LLM key are set. The
LLM client is built before Deepgram is called, the 256 KB text ceiling is checked
before the identification call, and the visit is re-read before writing.

**The model labels voices; it never writes the transcript.** It returns
`{speaker, role, description, confidence}` per voice and Python assembles the text
from Deepgram's words. A confused or prompt-injected answer can mislabel a voice
but cannot add, drop or reword speech — the same instinct as §8.2. Descriptions
are sanitised before they become a label.

**OTHER is a real role, and PATIENT is not required.** A parent speaking for a
child is `OTHER (patient's mother)`. What is refused (stored as `failed`, no
transcript written): one voice only, no doctor, or nobody but the doctor — each
means the conversation was not separated. Doubtful-but-usable labellings complete
with `warnings`, stored on the upload. They do not hold generation back (a
product decision: the doctor should never have to press Generate after a
recording); the visit page shows them above the brief as a "check the speaker
labels" notice, until a different transcript replaces this one. Two voices mapped to the
same person are merged, which repairs Deepgram splitting one speaker in two.

Errors by who can fix them: unsupported format or unreadable audio 422, missing
or rejected `DEEPGRAM_API_KEY` 500, storage hiccup on a chunk 503 (resend),
Deepgram throttled/down 503, other Deepgram failure 502, LLM throttled 429. During
processing these become the upload's `error_code` / `error_message`.

### 8.24 Live recordings: sent while recording, planned at the end

A doctor can record the consultation in the browser instead of uploading a file
afterwards. Every scheduled visit offers **Start session** and **Upload
transcript**. Upload completes the visit and then uploads: two explicit calls,
with the file checked in the browser first (`lib/api/uploadChecks.ts`).

```
browser  POST /visits/{id}/start                  scheduled -> in_progress
         POST /visits/{id}/audio-uploads/live     mode=live, no plan  -> receiving
         PUT  /audio-uploads/{id}/chunks/i        every ~minute while recording
         POST /visits/{id}/complete               End session
         POST /audio-uploads/{id}/finish {chunk_count: n}   plan fixed -> processing
backend  unchanged from §8.23: stitch -> Deepgram -> speakers -> transcript -> job
```

**Chunks go up during the consultation, not after it.** A consultation cannot be
repeated. Kept only in the tab's memory until the end, it would be lost to a
closed tab, a crash, or a laptop going to sleep. Sent as it is recorded, a crash
costs at most the piece not yet sent. A Render restart mid-consultation costs
nothing: the browser keeps retrying until the backend is back.

**Same security, by construction.** Each chunk is an ordinary authenticated
`PUT` through the BFF, the same route a file upload uses, so the session check,
`X-API-Key` and `X-Doctor-*` all apply (§8.12). Deepgram is still called only by
FastAPI. A WebSocket was ruled out: Vercel cannot proxy one, and connecting the
browser straight to Render would bypass the BFF.

**The plan is written last, not weakened.** A file upload declares its size and
gets its plan at start. A live one cannot, so `total_bytes` and `chunk_count`
are NULL while it is receiving. The pieces are still exact-size —
`speech.live_chunk_bytes`, 256 KiB, about a minute at 32 kbps, every one except
the last. The browser cuts the recorder's byte stream to that size rather than
sending `MediaRecorder` blobs as they come.

At finish, `domain/audio.live_plan` works the plan out from the chunk rows:
- indices 0..n-1 all present (409 `details.missing` otherwise);
- every chunk except the last full size;
- `n` equal to the `chunk_count` the browser declares.

`claim_live` then writes the plan and moves the upload to `processing` in one
UPDATE. Its WHERE re-checks the chunk count, the byte total and that nothing sits
past index n-1, so a chunk landing in between matches nothing rather than
producing a wrong plan. From there the row is an ordinary upload, with no live
branch: the cover constraint, `_stitch`'s byte-for-byte check, retry and the
reaper all see a known plan. A short middle chunk is refused because it would
make the stitched file differ from the recording.

The ceiling is enforced as the recording grows. Every earlier chunk is full, so
chunk `i` ends at `i * chunk_bytes + size`, and anything past
`speech.max_upload_bytes` is a 413 without counting rows.

**`in_progress` is what recording needs.** Uploads still require `completed`
(§8.17). Recording is the one thing allowed before that, because it happens
during the consultation. `/start` is the explicit transition into that state.
Finish still applies `check_can_attach`, so End session completes the visit and
then finishes. Finishing on an in-progress visit is a 409 while the chunks are
still intact, not a failure after paying for speech-to-text.

**A live recording is never silently replaced.** Starting a file upload used to
discard anything unfinished for the visit. Applied to a live recording, that
would delete a consultation still being recorded in another tab. Now any upload
start, live or file, is a 409 while a live recording is `receiving`. The doctor
resolves it explicitly:
- *process*: finish with no `chunk_count`, so whatever arrived is the recording;
- *discard*: `POST /audio-uploads/{id}/discard`.

`/cancel` discards the visit's unfinished uploads after it commits, so a
recorder still sending gets a 409 on its next chunk and stops. An interrupted
recording nobody resolves is kept for 24 hours after its last chunk, then
removed by the startup reaper, like any stale `receiving` upload.

**Formats.** `.webm` (Chrome, Edge, Firefox), `.mp4` (Safari) and `.ogg` were
already in the allowlist. The extension of the filename sent at start declares
the recorder's format, as for a file.

**The recorder lives in the `(app)` layout, not on the visit page.**
`features/recording/LiveSessionProvider.tsx` owns the microphone, the
MediaRecorder, the chunker and the uploader. A layout survives client-side
navigation, so the doctor can open the patient's previous brief
mid-consultation without stopping the recording. `RecordingBar`, in the sticky
header, shows the session on every other page. There is one session at a time,
because there is one microphone and one consultation in the room. A full reload
or sign-out ends the session, and the `beforeunload` warning asks first.

**Browser half of the plan rule.** The recorder emits a blob every 10 s at
32 kbps mono (`lib/recording/format.ts`). Browser defaults are several times
higher, which would reach the 100 MB ceiling in a long consultation.
`ExactChunker` joins those blobs and re-cuts them into exact 256 KiB pieces. The
pieces join back into the same bytes, so the stitched file is the recording.

`LiveUploader` sends pieces one at a time and in order, which is what keeps the
server's chunk rows gap-free. It retries transient failures forever with backoff
capped at 30 s: giving up would only throw away audio that will get through
later, and recording carries on meanwhile. It stops on a failure the server
means:
- 409 (discarded, or the visit cancelled elsewhere): the session fails with an
  explanation;
- 413 (the size ceiling): recording stops and End session processes what was
  acknowledged.

**End session order:** stop the recorder, flush the last piece, wait for every
piece, `/complete`, then `finish {chunk_count}`, using `finishWithRecovery`'s
lost-response handling. If either of the last two fails, the session stays
open, nothing is lost, and End session retries only those two steps. An empty
recording is discarded rather than finished.

**Microphone settings.** Echo cancellation and noise suppression are off: they
are tuned for calls and can clip the far voice in a room, which is the
patient's. Automatic gain stays on. A level meter and a 30-second silence
warning catch a muted or wrong microphone. A screen wake lock is taken while
recording and re-taken when the tab becomes visible again, because a sleeping
laptop suspends the recorder.

**Interrupted sessions** are worked out from the server's row, not remembered:
the latest upload is `mode: live, status: receiving` and not this tab's
(`features/visits/lifecycleState.ts`). The visit page shows when it ran
(`created_at` → `last_chunk_at`) and offers *Process what was saved* (complete
if needed, then finish with no count) or *Discard*. If the last piece arrived in
the past three minutes, it warns that the recording may still be running in
another tab, where processing it here would stop it.

**Consent** is a checkbox in the UI ("The patient has agreed to this
consultation being recorded") that enables Start recording. It is not stored on
the server; storing it would be a small follow-up.

**Not verified in a real browser yet** (Vitest covers the logic with a fake
MediaRecorder): that Deepgram decodes the joined WebM from Chrome and Firefox and
the joined MP4 from Safari, including a recording cut off part-way. If a cut-off
Safari MP4 cannot be decoded, crash recovery on Safari falls back to Discard
only. A normal End session is unaffected.

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

# Does the configured provider's key work (one small request)
python -m cli.check_keys

# Two-voice test recording from a transcript, via Deepgram text-to-speech (§8.23)
python -m cli.synthesize_transcript archive/shalin_1.txt          # -> archive/shalin_1.wav
python -m cli.synthesize_transcript archive/shalin_1.txt --dry-run  # character count only

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

Schema is applied in numeric order. Migrations 1 through 9 are applied to the
current database. `sql/10_live_recordings.sql` is required by the code from
§8.24 onwards: every audio upload query selects `mode`. Its `DO` blocks need a
real client (psql or psycopg), not a tool that splits on semicolons. `sql/7_multi_tenancy.sql` seeds the first doctor from an email set at the
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
npm run lint         # eslint
npm test             # vitest run
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

## 9.5 Deployment

Deployed as two separate services, matching the local two-process shape: no
shared runtime, HTTP only between them.

| Service | Host | URL |
| --- | --- | --- |
| Frontend | Vercel | `https://patient-recap.vercel.app` |
| Backend | Render (free tier) | `https://YOUR-SERVICE.onrender.com` |

### Backend on Render

Root Directory is `backend`, so `BASE_DIR` in `app/config.py` still resolves
correctly (§5.1) — `prompts/` and `transcripts/` are found the same way they are
locally. Build and start commands mirror the local invocation:

```
Build:  pip install -r requirements.txt
Start:  uvicorn app.main:app --host 0.0.0.0 --port $PORT
```

`--host 0.0.0.0 --port $PORT` is required and does not match the local `--reload`
invocation: Render assigns the port at runtime via `$PORT`, and the default
`127.0.0.1` bind is unreachable from outside the container.

**Health check path is `/api/v1/health`, not `/health`.** The route function
itself is declared as `/health` in `app/api/v1/health.py`, but the router is
mounted under the `/api/v1` prefix in `main.py` alongside every other router, so
the path Render must poll is the full `/api/v1/health`. This tripped the first
deploy: Render's health check 404'd against the bare `/health` path even though
the service was running correctly.

**Configuration on Render.** `config.yaml` is committed and deployed with the
code, so provider, models and the storage backend come from it. Render's
environment carries only what `config.yaml` references as `${NAME}`: the secrets
(`GOOGLE_API_KEY`, `AWS_BEARER_TOKEN_BEDROCK` if Bedrock is selected, `CONNSTR`,
`API_KEY`, the four Neon `AWS_*` storage values) and `CORS_ORIGINS`. Variables
from the old flat configuration (`TRANSCRIPT_STORAGE`, `GEMINI_MODEL`,
`GOOGLE_API_KEY_1..8`, ...) are ignored, and startup logs a warning for each one
still set, so they can be removed from the dashboard. A Render service still
holding only `GOOGLE_API_KEY_1` needs `GOOGLE_API_KEY` added.

`CORS_ORIGINS` accepts a bare value (`*`), a comma-separated list (`a,b`) and JSON
(`["a","b"]`) alike, and trailing slashes are stripped (`ApiConfig._split_origins`
in `app/config.py`). The old pydantic-settings trap, where a list-typed setting
ran `json.loads()` on the raw value and `CORS_ORIGINS=*` crashed startup, no
longer exists: the YAML loader hands the string to that validator directly.

**Free-tier constraints that interact with this project's design:**

- No persistent disk. `backend/transcripts/` (the 18 kept-for-reference original
  files, §11) does not survive a restart or redeploy on Render — this is fine
  only because `transcripts.storage` in `config.yaml` is `neon_object_storage`,
  so no runtime read or write depends on that local directory. Don't commit it
  as `local`.
- Free instances spin down after ~15 minutes idle and cold-start (30–60s) on the
  next request. Mitigated with UptimeRobot pinging
  `https://YOUR-SERVICE.onrender.com/api/v1/health` every 12 minutes, which
  keeps the instance warm and, as a side effect, keeps the Neon DB from
  suspending too, since the health route runs `SELECT 1` against Postgres on
  every call.
- This does **not** fully protect the async job model (§8.9). Render can still
  recycle a free instance independent of idle-based sleep (periodic restarts,
  monthly hour caps), and a restart mid-job leaves an `insight_jobs` row that
  `reap_orphans()` marks failed on the next boot rather than resuming it — that
  row's 3 model calls are wasted against Gemini's 20/day quota (§11). Prefer
  running `cli.load_insights` batch jobs from a local machine against the same
  Neon database rather than triggering bulk generation through the deployed API
  while on the free tier.

### Frontend on Vercel

Root Directory is `frontend`. Single service — the repo's `backend/` folder is
deliberately **not** deployed to Vercel. FastAPI's background-task job model
(§8.9) assumes a long-running process; Vercel's serverless functions are
request-scoped and would not keep a job alive after the response returns, so
deploying the backend there would silently break insight generation rather than
just duplicate it.

Environment variables (Production and Preview scopes) mirror the four local
`.env.local` values from §9, with two changes for production:

- `NEON_AUTH_COOKIE_SECRET` is a **separate** value from the local one, freshly
  generated with `openssl rand -base64 32`, not reused across environments.
- `BACKEND_URL` points at the Render URL above instead of `127.0.0.1:8000`.

**Auto-deploy is scoped to `frontend/` only.** Since backend and frontend share
one repo, an unscoped Vercel project would rebuild on every commit anywhere in
the repo, including pure backend changes. This is handled by Vercel's built-in
**"Skip deployments when there are no changes to the root directory or its
dependencies"** toggle (Settings → Build and Deployment → Root Directory,
Enabled), *not* by a custom Ignored Build Step. "Include files outside the root
directory in the Build Step" is also Enabled.

With Root Directory `frontend`, the toggle skips any commit whose changed files
are all outside `frontend/` — backend-only, docs-only, or root-file-only commits
never deploy the frontend. A commit that changes frontend source builds.

Two properties of this toggle are worth knowing, because they are Vercel
heuristics rather than a rule we control:

- It is **not a plain folder diff.** Vercel does affected-file analysis ("or its
  dependencies"), so a change inside `frontend/` that does not affect the build —
  a README-only edit is the likely case — may be treated as non-affecting and
  skipped. That is acceptable: a README does not change the built app. It does
  mean a doc-only frontend commit is not a valid test of auto-deploy.
- The diff baseline is the **last deployment's** commit (`VERCEL_GIT_PREVIOUS_SHA`),
  not the pushed commit's parent. After a manual redeploy of an older commit, the
  "changed files" set is computed from that older base, which can make a single
  push's skip/build decision surprising.

We chose this over the deterministic alternative — a custom Ignored Build Step of
`git diff --quiet HEAD^ HEAD -- .`, which builds on *any* change under `frontend/`
including non-code files — because it needs no configuration to maintain and
skipping a README-only redeploy is desirable, not a problem. If a future
requirement is "every change under `frontend/` must deploy, predictably," switch
to that command (Settings → Git → Ignored Build Step) and turn this toggle off;
do not run both.

**The Ignored Build Step "Behavior" must be "Automatic", not "Only build if there
are changes in a folder".** The folder helper *overrides* the Skip-deployments
toggle, so the toggle silently stops governing. It had been set to that folder
behaviour, which generated `git diff HEAD^ HEAD --quiet -- ./frontend` and
cancelled **every** deployment: Vercel runs the Ignored Build Step *from the Root
Directory* (already `frontend/`), so `./frontend` resolved to `frontend/frontend`,
matched nothing, and `--quiet` exited 0 — and exit 0 means skip. The two features
stack: Root Directory `frontend` and a folder filter of `./frontend` both assume
the same directory. The build log showed `Running "git diff HEAD^ HEAD --quiet --
./frontend"` then `canceled because the Ignored Build Step command returned exit
code 0` on a commit that genuinely changed frontend source. Setting Behavior to
Automatic hands control back to the toggle, which understands `frontend` without
nesting. If you ever want the folder filter instead of the toggle, its folder must
be `.` (not `frontend`), because the command already runs inside the root — but
then the toggle is moot, and note `HEAD^ HEAD` diffs only the latest commit, so a
multi-commit push can miss earlier changes.

To verify auto-deploy actually works, push a change to a real frontend **source**
file (a page or component), not the README, and confirm it produces a build. A
build that does not appear is then a Git-integration problem (Settings → Git:
repository connected, production branch `main`), not this toggle.

**A build can fail during static generation, not just during compile or
typecheck.** Next.js executes page-module top-level code while prerendering
static pages (`app/main.py`'s deploy-log equivalent: "Generating static pages
using 1 worker"), which is after `next build`'s compile and typecheck stages
already report success. A malformed or missing auth-related env var (e.g. a
mis-pasted `NEON_AUTH_COOKIE_SECRET` with stray whitespace) can surface here
instead of at those earlier stages, so a green compile/typecheck does not by
itself mean the deploy will succeed.

**Neon Auth's trusted-domain allowlist did not need the Vercel domain added to
work.** This is believed to be because the project is still on Neon Auth's
*shared* Beta Google OAuth credentials (§11's "continue to neon.tech" consent
screen), which appear to allow broader default origins than a project's own
Google Cloud OAuth client would. This is a soft observation, not a documented
guarantee — add the Vercel production domain in Neon Console → Auth →
Configuration → trusted domains before or when migrating off the shared
credentials (§11), since that migration is exactly the point at which strict
allowlist enforcement is expected to start mattering.

## 10. Testing approach

### Backend

Eight files, 429 tests, all offline — no API calls, no database, no object storage.

`test_audio_uploads.py` covers live recordings (§8.24). The pure part is
`live_plan` and `check_live_chunk`, including a check that every plan
`live_plan` returns satisfies the file-upload rules. The service rules are
tested against a mocked repository:
- start needs a session;
- a second live recording is a conflict, not a replacement;
- a file upload cannot discard a live recording;
- finish needs a completed visit and names missing chunks;
- discard and cancel.

One test stitches real files from a live plan and compares them byte-for-byte
with what was sent. The routes, cross-tenant isolation and SQL scoping of the
new endpoints are in `test_api.py` beside the others.

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

`test_llm.py` covers error classification for both providers, building each
provider's LangChain model from config (no network), the missing-key error, and
the Bedrock path end to end against a stubbed botocore client. `test_config.py`
covers config.yaml loading: `${...}` substitution, `.env` precedence, unknown
keys rejected, and `CORS_ORIGINS` parsing.

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
  collapsed, because these helpers are called across line breaks. It also holds
  `test_routers_go_through_services_not_repositories` (§7).

Four suites were added with the review fixes, each pinning a failure that had
already happened rather than a hypothetical one:

- **`TestVisitLifecycleIsTransitionsOnly`** — §8.17. Asserts on the service, the
  schema, the OpenAPI document and the repository whitelist, because the test it
  replaces mocked the service out and therefore could not see the bug.
- **`TestTranscriptLocationIsServerDecided`** — §8.19 and §8.22 together: neither
  `transcript_path` nor `transcript_provider` is a request field.
- **`TestPartialBriefIsNotPersisted`** — §8.9. Includes an ordering assertion
  that the guard runs before the upsert, since it is useless after it.
- **`TestDoctorResolutionPrefersTheStableId`** — §8.12, both directions: an
  unclaimed row is still claimed, a row claimed by another account is refused.

Plus `TestCancellationReasonIsStored` and `TestHealthDoesNotLeakOrLie`.

### Frontend

Six files, 67 tests, `npm test` (Vitest + jsdom). There were none before, which
is why every bug the review found here was found by reading.

Deliberately narrow: these modules hold logic rather than markup, and logic is
what regresses silently.

- `tests/dates.test.ts` — the overdue boundaries and the local-vs-UTC parsing
  that decides whether the queue says "overdue".
- `tests/useInsightJob.test.tsx` — that polling stops: on a terminal status, on
  unmount (including when a request resolves *after* unmount, which is the actual
  regression), after repeated failures, and immediately on a 401.
- `tests/bffProxy.test.ts` — the header allowlist above all: that a
  browser-supplied `X-Doctor-*` is dropped and the session's own values are what
  reach FastAPI. Plus `X-Total-Count`, the verbatim multipart Content-Type, the
  bodiless 204, and 504-vs-502.
- `tests/recording.test.ts` (§8.24) covers:
  - `ExactChunker`: exact sizes across arbitrary blob sizes, an exact-multiple
    ending, and joined bytes equal to the recording;
  - `LiveUploader`: one piece at a time in order, retrying without skipping
    ahead, stopping on a 409 with `drain` rejecting, and `stop` releasing a
    waiter while a send is in flight. That last one was a real bug, caught here.
- `tests/liveSession.test.tsx` runs the provider against a fake MediaRecorder:
  - the End session order: every piece, then `/complete`, then `finish` with the
    right count;
  - a blocked microphone;
  - the microphone released when the server refuses to start;
  - one session at a time;
  - a 409 mid-recording;
  - an empty recording discarded;
  - paused time not counted.
- `tests/lifecycleState.test.ts` covers which card the visit page shows when two
  states hold at once (recording here while the visit is already completed, an
  interrupted recording versus an unfinished file upload, and so on), plus the
  pre-completion file checks.

`npm run lint` is ESLint with Next's config. It reports seven warnings, all
`react-hooks/set-state-in-effect` and all the same shape — fetch on mount, then
setState — which is how all six views load their data. The rule is right; fixing
it means moving to Suspense with `use()` or a query library, which is a change to
every view and not one to make in passing. Downgraded to a warning with that
reasoning in `eslint.config.mjs` rather than silenced per line.

Conventions:

- stdlib `unittest`; `tests/__init__.py` must exist for discovery to work
- tests insert the project root on `sys.path`
- when a test needs settings, build them explicitly: `Settings()` for code
  defaults, or `load_settings(env_file=None, environ={...})` for config.yaml with a
  controlled environment. A value exported in the developer's shell or a real
  `.env` will otherwise make assertions machine-dependent
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

**`insights_json` has no runtime validation anywhere, and the frontend now
assumes that.** It is `Dict[str, Any]` in Pydantic, `Record<string, never>` out
of openapi-typescript, and hand-typed in `frontend/lib/types/insightsJson.ts`
from a real row. Every field in that file is optional on purpose: declaring them
required did not make them present, it only stopped the compiler asking, and one
absent key threw on first render and blanked the page. Prompts are data (§12), so
editing one is enough to change what arrives. `InsightBrief` is additionally
wrapped in an error boundary, so a payload it cannot render degrades to a card
instead of taking the visit page — and its transcript — down with it.

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
to. `transcripts.storage` governs only *new* uploads (§8.22), so changing it moves
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

**`CORS_ORIGINS` defaults to `*`** locally and is narrowed to
`https://patient-recap.vercel.app` on Render. Any of the three syntaxes works
now (§9.5), so a bare origin is fine — it does not have to be JSON.

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
- **The public home page states what reaches the model.** Its privacy section
  (`AI_WORKS_FROM`, `AI_KEPT_FROM` and `PRIVACY_POINTS` in
  `frontend/app/(home)/page.tsx`) is a statement to practitioners about patient
  data. Any change to what a prompt or the speech-to-text request receives must
  update it in the same change. The lists are present tense and must stay
  true; planned work is the note's "Next, we're..." sentence (name and
  contact masking, removing `patient_name` / `doctor_name`, Deepgram's
  `mip_opt_out`). Move each into the lists only when it ships (README To
  Dos). "The AI model we use does not train on your consultations" holds for
  Bedrock and not for Gemini's free tier, so revisit it if `llm.provider`
  changes.
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
- A new list endpoint goes through a service and returns a `Page`
  (`app/services/pagination.py`), never a bare list plus a separate count in the
  route. A test greps for routers importing repositories (§7).
- A field a client may send must be one the server will *honour*. If a request
  field is removed because it was dangerous, prefer refusing it to ignoring it —
  a 200 that silently did nothing is worse than a 422 (§8.17).
- Anything the model produced is optional on the frontend until something
  validates it. See §11 on `insights_json`.
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