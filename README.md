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
├── backend/      FastAPI service, pipeline, CLI tools, schema, sample data
├── frontend/     UI (not built yet — see frontend/README.md)
├── docs/         PROJECT_CONTEXT.md, the deep dive
└── README.md     this file
```

Each side owns its own dependencies and environment file. They talk over HTTP
only, so either can be worked on without touching the other.

---

## How it works

Three sequential LLM calls per consultation, each consuming the previous one's
output:

```
transcript.json
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

---

## Getting started

Requires Python 3.11+ and a Postgres database (this project uses Neon).

```bash
conda activate reels          # or your own venv
cd backend
pip install -r requirements.txt

cp .env.example .env          # then fill in CONNSTR and your API keys
```

Apply the schema in numeric order:

```bash
psql "$CONNSTR" -f sql/1_patient_schema.sql
psql "$CONNSTR" -f sql/2_visit_schema.sql          # includes seed visits
psql "$CONNSTR" -f sql/3_visit_insights_schema.sql
psql "$CONNSTR" -f sql/4_insights_generated.sql
psql "$CONNSTR" -f sql/5_api_support.sql
```

Check the keys work, then start the server:

```bash
python -m cli.check_keys
uvicorn app.main:app --reload
```

Interactive docs at http://127.0.0.1:8000/docs.

---

## The API

```
POST   /api/v1/patients                     register a patient
POST   /api/v1/visits                       schedule a consultation
PATCH  /api/v1/visits/{visit_id}            attach the transcript afterwards
POST   /api/v1/visits/{visit_id}/insights   start the chain -> 202 + job_id
GET    /api/v1/jobs/{job_id}                poll until terminal
GET    /api/v1/visits/{visit_id}/insights   read the result
GET    /api/v1/health                       service + database + key count
```

A visit is created *before* the consultation, so it starts with no transcript and
`visit_status: scheduled`. The PATCH fills that in afterwards and moves the visit
to `completed`.

Insight generation returns `202` rather than the finished result because the
chain is three sequential model calls, roughly a minute — past most proxy
timeouts. Poll the job, then read the insight.

A worked example, end to end:

```bash
# 1. register
curl -s -X POST localhost:8000/api/v1/patients -H 'content-type: application/json' -d '{
  "first_name": "Riya", "last_name": "Sharma",
  "date_of_birth": "2001-06-11", "gender": "female",
  "phone": "+91 98765 43210"
}'

# 2. schedule (visit_number is assigned for you)
curl -s -X POST localhost:8000/api/v1/visits -H 'content-type: application/json' -d '{
  "patient_id": "<patient_id>", "visit_date": "2026-02-16", "visit_time": "10:30:00"
}'

# 3. attach the transcript once the call has happened
curl -s -X PATCH localhost:8000/api/v1/visits/<visit_id> \
  -H 'content-type: application/json' \
  -d '{"transcript_path": "transcripts/riya_1.json"}'

# 4. generate, then poll
curl -s -X POST localhost:8000/api/v1/visits/<visit_id>/insights -d '{}' \
  -H 'content-type: application/json'
curl -s localhost:8000/api/v1/jobs/<job_id>
curl -s localhost:8000/api/v1/visits/<visit_id>/insights
```

Every error comes back in the same shape, so a client can handle them uniformly:

```json
{ "code": "duplicate_patient", "message": "...", "details": { } }
```

---

## Command line

For bulk work, run from `backend/`:

```bash
python -m cli.load_insights --list        # what is done, what is pending
python -m cli.load_insights --all --dry-run
python -m cli.load_insights --all         # process everything pending
python -m cli.load_insights --all --limit 2      # costed trial run
python -m cli.load_insights --all --pause        # confirm each visit

python -m cli.check_keys                  # which API keys are usable

python -m cli.render_prompt --transcript transcripts/riya_1.json \
  --age 24 --gender Female --call-date 2026-02-16 --layer 1
```

`render_prompt` calls neither the model nor the database, which makes it the
fastest way to iterate on prompt wording.

The batch runner prints a `check:` line per visit comparing the resolved
follow-up date against when the patient actually returned next. The seeded visit
dates were chained from each transcript's stated interval, so those should agree
— it is the most useful single signal that extraction is working.

Ctrl-C once stops after the visit in flight, twice abandons it. Nothing partial
is written either way, and re-running skips finished visits.

---

## Tests

```bash
cd backend && python -m unittest discover -s tests -t .
```

107 tests, all offline: no API calls, no database. They cover follow-up date
resolution against every timing pattern in the sample corpus, multi-key rotation
with stubbed clients, and the HTTP layer's routing, validation and error mapping.

---

## Configuration

`backend/.env`, see `.env.example`:

| Variable | Purpose |
| --- | --- |
| `CONNSTR` | Postgres connection string |
| `GOOGLE_API_KEY_1..8` | Gemini keys; the client rotates when one is rate limited |
| `GOOGLE_API_KEY` | fallback if no numbered keys are set |
| `GEMINI_MODEL` | model id, default `gemini-3.7-flash` |

Free-tier Gemini keys allow 20 requests per day per model per project, and each
consultation costs 3. Keys in the same Google Cloud project share one bucket, so
several keys do not guarantee proportionally more requests. `python -m
cli.check_keys` tells you where you stand.

---

## Two things to be aware of

**There is no authentication.** Every endpoint is open, and insight generation
both writes clinical records and spends money on API calls. That is fine locally
and not fine anywhere else. `backend/app/api/deps.py::require_auth` is the single
place to add a check.

**Transcripts are loaded by path from the payload**, so reads are confined to
`backend/transcripts/`. Anything resolving outside it is refused. This matters
more than usual given the point above.

---

## Further reading

`docs/PROJECT_CONTEXT.md` covers the architecture and, more usefully, the
reasoning behind the non-obvious decisions — several of which look like mistakes
until you know what broke without them. Worth reading before changing the
pipeline, the database layer, or the prompts.
