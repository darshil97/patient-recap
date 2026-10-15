# Consultation Insights

Turns recorded homeopathic doctor-patient consultations into structured clinical
records: a faithful extraction of what was said, a homeopathic analysis, a
doctor-facing visit brief, and a follow-up appointment date resolved to a real
calendar date.

The point is that a practitioner can open a patient's record before the next call
and see in about 90 seconds what happened last time, what was prescribed, what to
watch for, and what to ask next.

It records and organises what the doctor and patient actually said. It does not
diagnose or recommend treatment.

---

## Layout

```
doctor-patient/
├── backend/        FastAPI service, pipeline, CLI tools, schema, sample data
├── frontend/       Next.js app + BFF proxy — see frontend/README.md
├── docs/           PROJECT_CONTEXT.md, the deep dive
├── run_local.sh    start/stop both dev servers in the background
└── README.md       this file
```

Each side owns its dependencies and environment file. They talk over HTTP only, so
either can be worked on without touching the other.

---

## How it works

Three sequential LLM calls per consultation, each consuming the previous one's
output:

```
transcript (.txt, in Neon Object Storage)
      |
      v
  Layer 1  clinical extraction     what was literally said, no interpretation
      |
      +--> resolve_follow_up()     Python, not the LLM: "in three weeks" -> a date
      |
      v
  Layer 2  homeopathic analysis    remedy reasoning, constitutional picture
      v
  Layer 3  care plan               the visit brief
      v
  Postgres (visit_insights)
```

Splitting it into three keeps each prompt's job narrow, makes a bad output
attributable to a stage, and means the factual record survives re-running the
interpretive parts with a different prompt.

The date arithmetic deliberately happens in Python rather than in the model:
deterministic, auditable, and unit-tested, with the doctor's verbatim phrase kept
alongside the computed date.

**It is multi-tenant.** Every patient belongs to a doctor, and every query is
scoped to the signed-in one. See [Multi-tenancy](#multi-tenancy) below.

---

## Getting started

Requires Python 3.11+, Node 26+, and a Neon Postgres project.

```bash
conda activate reels          # or your own venv
cd backend
pip install -r requirements.txt
cp .env.example .env          # then fill it in — see Configuration
```

Apply the schema in numeric order:

```bash
psql "$CONNSTR" -f sql/1_patient_schema.sql
psql "$CONNSTR" -f sql/2_visit_schema.sql          # includes seed visits
psql "$CONNSTR" -f sql/3_visit_insights_schema.sql
psql "$CONNSTR" -f sql/4_insights_generated.sql
psql "$CONNSTR" -f sql/5_api_support.sql
psql "$CONNSTR" -f sql/6_frontend_support.sql
psql "$CONNSTR" -f sql/7_multi_tenancy.sql         # EDIT IT FIRST — see below
```

`7_multi_tenancy.sql` creates the `doctors` table and assigns every existing
patient to you. **Set `FIRST_DOCTOR_EMAIL` at the top of its section 2 to the email
you sign in to Neon Auth with** — it aborts on the placeholder rather than guessing.
If it does not match, your first sign-in creates a second, empty doctor and every
screen looks blank.

Then start both services. Either run them yourself in two terminals:

```bash
# terminal 1
cd backend && uvicorn app.main:app --reload

# terminal 2
cd frontend && cp .env.local.example .env.local && npm install && npm run dev
```

…or use the helper, which backgrounds both and returns the terminal:

```bash
./run_local.sh start      # also runs npm install and generates the dev cert if missing
./run_local.sh status
./run_local.sh logs       # tail both
./run_local.sh stop
```

`start` preflights the two things that break local dev most often: a missing
`.env`/`.env.local`, and `API_KEY` disagreeing with `BACKEND_API_KEY` (which makes
every screen 401 in a way that looks like a session bug). `stop` also reaps
anything still holding either port, so a server started outside the script — or a
child that outlived its wrapper — does not silently linger and get attached to by
the next `start`.

Backend docs at http://127.0.0.1:8000/docs. App at **https://localhost:3000** —
note the `s`; the dev server is HTTPS because Safari will not store Neon Auth's
`Secure` session cookie over plain HTTP. The cert is self-signed, so accept the
browser warning once. See `frontend/README.md`.

---

## Multi-tenancy

`doctors` 1—N `patients` 1—N `visits`. Only `patients` carries a `doctor_id`;
visits, insights, follow-ups and jobs derive their owner by following existing
foreign keys, so there is one place ownership is recorded rather than five that
must agree.

Two things follow from that, and both matter when writing code:

- **Every repository function takes a `DoctorScope` as its second positional
  argument.** Forgetting it is a `TypeError`, not a silent leak. `ALL_DOCTORS` is an
  explicit sentinel for the two places that are legitimately cross-doctor (startup
  job reaping, and the CLI).
- **A row belonging to another doctor is a `404`, never a `403`.** A 403 would
  confirm the id exists.

Requests carry two things: `X-API-Key` (is this a trusted caller?) and
`X-Doctor-Auth-Id` + `X-Doctor-Email` (whose data is this?). The Next.js BFF adds
all three from the server-side Neon Auth session; the browser never sees them and
cannot supply them. Missing either is a 401.

Because FastAPI trusts the BFF's identity assertion rather than verifying a token,
**`API_KEY` can impersonate any doctor**. Treat it accordingly.

---

## The API

```
GET    /api/v1/stats                          home-screen counters
GET    /api/v1/health                         service + database + model provider
GET    /api/v1/health/api-keys                does the model key work — never poll

POST   /api/v1/patients                       register a patient
GET    /api/v1/patients                       list / search (q, X-Total-Count)
GET    /api/v1/patients/{id}                  single patient
PATCH  /api/v1/patients/{id}                  correct demographics
GET    /api/v1/patients/{id}/timeline         whole record in one request

POST   /api/v1/visits                         schedule a consultation
GET    /api/v1/visits                         list (status/date filters, sort)
GET    /api/v1/visits/{id}                    single visit
PATCH  /api/v1/visits/{id}                    correct details
POST   /api/v1/visits/{id}/start              start the session (in_progress)
POST   /api/v1/visits/{id}/complete           mark the consultation done
POST   /api/v1/visits/{id}/cancel             cancel, reopening its follow-ups

POST   /api/v1/visits/{id}/transcript         upload the .txt (multipart)
POST   /api/v1/visits/{id}/audio-uploads      or a recording: start, get chunk plan
POST   /api/v1/visits/{id}/audio-uploads/live or record live: sent while recording
PUT    /api/v1/audio-uploads/{id}/chunks/{i}  one chunk, idempotent
POST   /api/v1/audio-uploads/{id}/finish      stitch + transcribe -> 202
POST   /api/v1/audio-uploads/{id}/discard     throw an unfinished one away
GET    /api/v1/audio-uploads/{id}             poll until completed | failed
GET    /api/v1/visits/{id}/transcript         read it back
DELETE /api/v1/visits/{id}/transcript         remove it

POST   /api/v1/visits/{id}/insights           start the chain -> 202 + job_id
GET    /api/v1/jobs/{job_id}                  poll until terminal
GET    /api/v1/jobs                           recent jobs, newest first
GET    /api/v1/visits/{id}/job                latest job, to resume polling
GET    /api/v1/visits/{id}/insights           read the result

GET    /api/v1/follow-ups                     the follow-up queue
GET    /api/v1/follow-ups/{id}                single follow-up
PATCH  /api/v1/follow-ups/{id}                outcome of a phone call
```

A visit is created *before* the consultation, so it starts with no transcript and
`visit_status: scheduled`. Completion is explicit — uploading a transcript does
**not** complete a visit, and a transcript can only be attached to a visit that is
already `completed`.

Insight generation returns `202` rather than the finished result because the chain
is three sequential model calls, roughly a minute — past most proxy timeouts. Poll
the job, then read the insight.

A worked example, end to end. `$K` is `API_KEY`; the two doctor headers are what
the BFF would normally add:

```bash
K=$(grep -m1 '^API_KEY=' backend/.env | cut -d= -f2-)
H=(-H "X-API-Key: $K"
   -H "X-Doctor-Auth-Id: <your neon auth user id>"
   -H "X-Doctor-Email: you@example.com"
   -H 'content-type: application/json')

# 1. register
curl -s "${H[@]}" -X POST localhost:8000/api/v1/patients -d '{
  "first_name": "Riya", "last_name": "Sharma",
  "date_of_birth": "2001-06-11", "gender": "female",
  "phone": "+91 98765 43210"
}'

# 2. schedule (visit_number is assigned for you)
curl -s "${H[@]}" -X POST localhost:8000/api/v1/visits -d '{
  "patient_id": "<patient_id>", "visit_date": "2026-02-16", "visit_time": "10:30:00"
}'

# 3. mark it done, then attach the transcript
curl -s "${H[@]}" -X POST localhost:8000/api/v1/visits/<visit_id>/complete
curl -s -H "X-API-Key: $K" \
  -H "X-Doctor-Auth-Id: <id>" -H "X-Doctor-Email: you@example.com" \
  -F 'file=@transcript.txt' \
  localhost:8000/api/v1/visits/<visit_id>/transcript

# 4. generate, then poll
curl -s "${H[@]}" -X POST localhost:8000/api/v1/visits/<visit_id>/insights -d '{}'
curl -s "${H[@]}" localhost:8000/api/v1/jobs/<job_id>
curl -s "${H[@]}" localhost:8000/api/v1/visits/<visit_id>/insights
```

Every error comes back in the same shape, so a client can handle them uniformly:

```json
{ "code": "duplicate_patient", "message": "...", "details": { } }
```

---

## Transcript storage

Transcripts are plain UTF-8 `.txt`, one per visit, addressed as
`transcripts/{visit_id}.txt`. That string is the same whether the bytes are on
local disk or in a Neon Object Storage bucket — `visits.transcript_provider` says
which, so both can coexist and a migration flips one column.

They currently live in object storage. The reason is branching, not durability: a
Neon branch inherits its parent's buckets and their objects, so a preview branch
gets a consistent pair — the visit rows *and* the transcripts they point at. With
local disk, branching gave you rows pointing at files that either did not exist or
were the live ones shared by every branch.

```bash
python -m cli.migrate_transcripts --status
python -m cli.migrate_transcripts --to neon_object_storage --dry-run
python -m cli.migrate_transcripts --to neon_object_storage
python -m cli.migrate_transcripts --verify
python -m cli.migrate_transcripts --prune-local   # only after --verify passes
```

The migration reads the source, writes the destination, reads *back* and compares
exactly, and only then flips the column. It deletes nothing — `--prune-local` is a
separate decision.

---

## Command line

For bulk work, run from `backend/`:

```bash
python -m cli.load_insights --list        # what is done, what is pending
python -m cli.load_insights --all --dry-run
python -m cli.load_insights --all         # process everything pending
python -m cli.load_insights --all --limit 2      # costed trial run
python -m cli.load_insights --all --pause        # confirm each visit
python -m cli.load_insights --all --doctor me@example.com   # one doctor only
python -m cli.check_keys                  # does the configured provider's key work
python -m cli.check_keys --provider bedrock      # check the other provider's key
python -m cli.render_prompt --transcript transcripts/<visit_id>.txt \
  --age 24 --gender Female --call-date 2026-02-16 --layer 1
```

`render_prompt` calls neither the model nor the database, which makes it the
fastest way to iterate on prompt wording.

CLI tooling is **cross-doctor by default**. That is deliberate: it runs from a shell
that already holds `CONNSTR`, so a mandatory `--doctor` would buy the appearance of
a boundary without the substance. The flag exists for cost and safety, and the run
header always prints which doctors are in scope.

The batch runner prints a `check:` line per visit comparing the resolved follow-up
date against when the patient actually returned next. The seeded visit dates were
chained from each transcript's stated interval, so those should agree — it is the
most useful single signal that extraction is working.

Ctrl-C once stops after the visit in flight, twice abandons it. Nothing partial is
written either way, and re-running skips finished visits.

---

## Tests

```bash
cd backend && python -m unittest discover -s tests -t .
```

All offline: no API calls, no database, no object storage. They cover follow-up
date resolution against every timing pattern in the sample corpus, configuration
loading, provider construction and error classification, transcript storage on
both backends, and the HTTP layer's routing, validation and error mapping.

The most important suite is `TestCrossTenantIsolation` in `tests/test_api.py`: a
regression there is a real patient data leak, not a cosmetic bug.

---

## Configuration

Two files with separate jobs:

- **`backend/config.yaml`** (committed) holds everything about *what to run*:
  provider, models, timeouts, pool sizes, upload limit, storage backend. It is
  loaded into a typed tree by `app/config.py`, and an unknown key is a startup
  error.
- **`backend/.env`** (gitignored, see `.env.example`) holds secrets only. The YAML
  reads them by name as `${NAME}` or `${NAME:-fallback}`; on a host without a
  `.env`, set the same names as environment variables.

```yaml
llm:
  provider: gemini            # gemini | bedrock
  gemini:  { model: gemini-3.7-flash, api_key: ${GOOGLE_API_KEY} }
  bedrock: { model: openai.gpt-oss-120b-1:0, api_key: ${AWS_BEARER_TOKEN_BEDROCK}, region: us-west-2 }
```

| Secret / per-host variable | Read as |
| --- | --- |
| `GOOGLE_API_KEY` | `llm.gemini.api_key` |
| `AWS_BEARER_TOKEN_BEDROCK` | `llm.bedrock.api_key` (a Bedrock API key) |
| `DEEPGRAM_API_KEY` | `speech.deepgram.api_key`; only needed for audio uploads |
| `CONNSTR` | `database.url` |
| `API_KEY` | `api.key`; shared secret for the API, unset leaves it open |
| `CORS_ORIGINS` | `api.cors_origins`, default `*`. A bare value, a comma-separated list, or JSON |
| `AWS_ENDPOINT_URL_S3` | Neon branch storage endpoint |
| `AWS_ACCESS_KEY_ID` | the credential's `token_id` |
| `AWS_SECRET_ACCESS_KEY` | the credential's `s3_secret_access_key` |
| `AWS_REGION` | Neon storage region, default `us-east-2` |

The variables the old flat configuration read (`LLM_PROVIDER`, `GEMINI_MODEL`,
`GOOGLE_API_KEY_1..8`, `TRANSCRIPT_STORAGE`, `MAX_TRANSCRIPT_BYTES`, ...) are no
longer read; if one is still set, startup logs a warning naming its config.yaml
key.

The `AWS_*` names are standard on purpose, so `neon env pull` writes them directly.
Create the credential in the Neon Console under the branch's **Credentials**, with
`storage:read` and `storage:write`. **The secret is shown once and cannot be
retrieved**, and credentials are branch-scoped: one minted on the default branch
works there and on descendants only.

Both providers go through LangChain (`app/llm/providers/`): Gemini through
`ChatGoogleGenerativeAI`, Bedrock through `ChatBedrockConverse` with the API key as
a bearer token. Each has one key. Throttling is retried by the provider's client and
then surfaces as `quota_exhausted` (HTTP 429). Free-tier Gemini allows 20 requests
per day per model per project, and each consultation costs 3. `python -m
cli.check_keys` sends one request to confirm the selected provider's key.

---

## Things to be aware of

**Two headers, not one.** Every route except `GET /health` needs `X-API-Key`, and
every route that touches patient data also needs `X-Doctor-Auth-Id` /
`X-Doctor-Email`. Missing either is a `401`. `/openapi.json` and `/docs` stay open
so a client can generate types. Over plain HTTP the key travels in clear text, so a
real deployment needs TLS.

**Transcript addresses are always derived from the visit id**, never from the
client's filename and never from the request body. On disk that is enforced by
resolving paths against the transcripts directory; in object storage there is no
parent directory to be confined to, so a strict `transcripts/<uuid>.txt` pattern is
checked on every read, write and delete. `transcript_path` is not a request field
on any endpoint — uploading is the only way a transcript is attached (§8.19).

**Status is a transition, not a field.** `PATCH /visits/{id}` corrects date, time
and type only. `/start`, `/complete` and `/cancel` are the only ways `visit_status` changes,
because each does more than set a column — `/cancel` reopens the follow-ups that
visit had resolved, and refuses once insights exist (§8.17).

**`CORS_ORIGINS` defaults to `*` locally** and is narrowed to the Vercel origin on
Render. It does not affect the app, whose traffic is same-origin through the BFF —
`*` cannot be combined with credentialed requests in any case.

---

## Further reading

`docs/PROJECT_CONTEXT.md` covers the architecture and, more usefully, the reasoning
behind the non-obvious decisions — several of which look like mistakes until you
know what broke without them. Worth reading before changing the pipeline, the
database layer, the tenancy scoping, or the prompts.

`frontend/README.md` is the API contract the UI is built against, plus the frontend
gotchas (HTTPS dev, auth theming).

## To Dos
  **Frontend | Backend**
  - [ ] Verify the Neon Auth JWT against the JWKS endpoint and take `doctor_id`
        from the verified `sub`, so `API_KEY` stops being tenancy-bearing (§8.12).
        The seam is `require_doctor` and nothing else.
  - [ ] Validate `insights_json` against a schema at the write, so the frontend's
        hand-written types stop being an unenforced assertion (§11)
  - [ ] Move data loading off fetch-on-mount (Suspense + `use()`, or a query
        library). Seven `react-hooks/set-state-in-effect` warnings track this.
  - [ ] `visit_insights.transcript_sha256`, so the stale-brief warning compares
        content rather than timestamps (§11)
  - [ ] Privacy, announced as "Next, we're..." on the public home page. As each
        one ships, move it into the present-tense lists in
        `frontend/app/(home)/page.tsx` and out of the "Next" sentence:
        - drop `patient_name` and `doctor_name` from every prompt
        - mask the patient's and doctor's names (`[Patient]` / `[Doctor]`),
          phone numbers and emails in the transcript at prompt time
        - send `mip_opt_out=true` on every Deepgram request
  - [ ] Add transcript audio functionality
  - [ ] Add recording functionality
  - [ ] Deploy frontend on Vercel
  - [ ] Deploy backend
  

  **Data Science**
  - [ ] Family history to be preserved
  - [ ] Open-source LLM
  - [ ] Tracing