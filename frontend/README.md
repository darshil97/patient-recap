# Frontend

This is the API contract the UI is built against, plus the frontend-specific
gotchas. A session working here needs no knowledge of the Python backend beyond
what is below.

## What is here

A Next.js 16 App Router app (TypeScript `strict`, Tailwind v4), with a
server-side BFF proxy in front of the FastAPI backend.

```
frontend/
├── app/
│   ├── (app)/              authenticated area, wrapped in the clinical shell
│   │   ├── page.tsx        "/" -> the follow-up queue
│   │   ├── follow-ups/     same queue under its own name
│   │   ├── schedule/       visit schedule
│   │   ├── patients/       list, and [patientId] detail/timeline
│   │   └── visits/         [visitId]: transcript upload, generation, brief
│   ├── auth/[path]/        Neon Auth views, deliberately outside (app)
│   ├── api/
│   │   ├── auth/[...path]/ Neon Auth handler
│   │   └── [...path]/      the BFF proxy to FastAPI  ← all backend traffic
│   ├── globals.css         Tailwind + auth-ui theme + design-system.css
│   └── design-system.css   the approved clinical design (accent tokens in :root)
├── views/                  the six page components
├── features/  components/  hooks/
├── lib/
│   ├── api/                openapi-fetch wrappers, same-origin only
│   ├── auth/               server.ts (secrets) and client.ts (browser)
│   └── types/api.d.ts      generated — do not hand-edit
├── certificates/           self-signed dev cert, gitignored (see HTTPS below)
├── proxy.ts                route protection (middleware.ts on Next < 16)
├── legacy-vite/            the previous Vite SPA, reference only
└── design-history/         mockups and design alternatives
```

**Nothing in the browser talks to FastAPI.** The backend's address and API key are
server-only (`BACKEND_URL`, `BACKEND_API_KEY`) — there are no `NEXT_PUBLIC_`
variables at all, by design. The browser calls same-origin `/api/v1/...`, and
`app/api/[...path]/route.ts` verifies the Neon Auth session and forwards with the
secrets attached.

Three details in that proxy are load-bearing:

- multipart bodies are forwarded as **raw bytes** with the incoming `Content-Type`
  copied verbatim — re-encoding would mint a boundary that no longer matches the
  body, breaking transcript upload
- `X-Total-Count` is copied onto the response, or paginated lists lose their totals
- `204`/`205`/`304` are returned without a body, because constructing one throws

## Generate the client, don't hand-write it

The backend publishes OpenAPI at `/openapi.json` and every request and response
model is fully typed, so generating from it means the types cannot drift:

```bash
npm run gen:api        # needs the backend running
```

`lib/types/api.d.ts` is committed, so it can fall behind without anything failing
loudly — a stale copy still typechecks, it just describes an API that no longer
exists. Regenerate in the same piece of work as any backend model change. Browse
the schema interactively at http://127.0.0.1:8000/docs.

## The data is per-doctor

The backend is multi-tenant. Every response contains only the signed-in doctor's
patients, visits, insights and follow-ups — there is no way to ask for anyone
else's, and no endpoint accepts a doctor id. Two consequences for the UI:

- **Ids are only meaningful inside one account.** A patient or visit id from another
  doctor returns `404`, not `403` (see [404, not 403](#404-not-403)). Don't build
  deep links or copy-paste flows that assume an id is globally addressable.
- **A new account sees a completely empty app** — no patients, no visits, an empty
  queue, all counters zero. Nothing distinguishes that from a broken backend, so
  empty states carry real weight.

## The workflow

Base URL: **`/api/v1`, same-origin.** Paths below are written without that
prefix. The browser never uses `http://127.0.0.1:8000` — the BFF does.

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

The file is stored server-side as `transcripts/{visit_id}.txt` — currently an
object in a Neon Object Storage bucket, though that is opaque to the UI and the
address is the same either way. The name you send is kept only for display in
`transcript_filename`; it never reaches the stored address.

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

**It does not affect this app.** All backend traffic is server-to-server
through the BFF, so the browser never makes a cross-origin request and CORS is
never consulted. `CORS_ORIGINS` still defaults to `*` in `backend/.env`, which is
fine for that reason — but it must be narrowed before deployment, since `*`
cannot be combined with credentialed requests.

## Running the dev server — HTTPS, not HTTP

```bash
npm run dev          # https://localhost:3000
```

**Note the `s`.** Neon Auth's session-challenge cookie is `__Secure-` prefixed
with the `Secure` flag, and Safari refuses to store `Secure` cookies over plain
HTTP even on localhost. Chrome and Firefox make an exception for local
development; Safari does not. Over `http://` the cookie is dropped and Google
sign-in fails in the most confusing way available: you authenticate successfully,
come back with a session verifier, `/api/auth/get-session` returns
`400 SESSION_CHALLENGE_COOKIE_NOT_FOUND`, and you land on the sign-in screen with
**no visible error and nothing in the backend log** — the backend is never
called.

The SDK's `SessionCookieConfig` exposes `secret`, `sessionDataTtl`, `domain` and
`sameSite` only, so the flag cannot be turned off. The origin has to be secure.

The cert in `certificates/` is self-signed and gitignored, so the browser warns
once per origin: **Show Details → visit this website**. Alternatives:

| script | what it does |
| --- | --- |
| `dev` | HTTPS with the checked-in self-signed cert. Warns once. |
| `dev:mkcert` | HTTPS with a trusted local CA. No warning, needs sudo. |
| `dev:http` | Plain HTTP. Fine for Chrome and for `gen:api`. |

Regenerating the cert, if it expires or goes missing, is in `package.json` under
`//dev-regen-certs`.

One thing to remember when changing the dev URL: Neon Auth's trusted-domain
allowlist is per **origin**, and `https://localhost:3000` is a different origin
from `http://localhost:3000`.

## Theming the auth screens

`<AuthView>` is a pre-built component tree, and out of the box it looks like a
different product — shadcn's near-black primary button, its own greys, softer
radii, system font.

It is restyled by mapping its own theming API onto the design system rather than
by forking components or overriding utility classes. auth-ui is
shadcn-flavoured: every colour resolves through a `--neon-*` custom property, so
`app/globals.css` redefines that layer against `design-system.css` tokens, scoped
to `.neon-auth-ui`. That survives internal class changes, which matters for a
package on `0.3.0-beta`, and keeps the accent in one place.

Two things there are load-bearing and should not be "cleaned up":

- **`defaultTheme="light"` on `NeonAuthUIProvider`.** auth-ui defaults to
  `"system"`, so on a dark-mode machine the auth card rendered dark while the
  app — which is light-only — stayed light.
- **`--neon-accent` and `--neon-accent-foreground`.** auth-ui reads a bare
  `--accent` if one exists, and `design-system.css` defines `--accent` as the
  brand teal. In shadcn's vocabulary `--accent` is a muted *hover surface* with
  near-black text on it, so the teal was being painted into hover states. That
  override fixes a real bug, invisible until you hover something.

`suppressHydrationWarning` on `<html>` in `app/layout.tsx` is also required, not a
patch over a bug: auth-ui bundles next-themes, which sets `class` and
`color-scheme` on `<html>` from a blocking script before React hydrates.

## Authentication — two headers, not one

**The backend is multi-tenant.** Every endpoint returns only the signed-in
doctor's patients, visits, insights and follow-ups. `GET /health` is the only
route that needs nothing.

Two separate things have to be true for a request to succeed, and they answer
different questions:

| Header | Question it answers | Value |
| --- | --- | --- |
| `X-API-Key` | may this caller reach the API at all? | `API_KEY` from `backend/.env` |
| `X-Doctor-Auth-Id` | whose data is this about? | `session.user.id` |
| `X-Doctor-Email` | ...and who is that, if we have not seen them before? | `session.user.email` |

Missing either gate is `401` with `{"code": "unauthorized", ...}`. A valid key
with no doctor identity is **also** 401 — the backend will not serve patient data
without knowing whose it is, rather than falling back to a default account.

`X-Doctor-Name` (`session.user.name`) is optional and display-only.

**Why two identity headers.** The backend resolves on the id and falls back to the
email. The id is the stable key — Better Auth never changes it — whereas email is
mutable, so keying on email alone would turn a doctor who changed their address
into a brand-new doctor with an empty patient list. The email is still needed: it
is what creates the doctor record on first sign-in, and what matches the row
seeded by `backend/sql/7_multi_tenancy.sql` before anyone has signed in.

**All three are set server-side in `app/api/[...path]/route.ts`, from the Neon
Auth session.** Never from the browser, and never from an incoming header. The
proxy's `FORWARD_REQUEST_HEADERS` allowlist is what makes that safe: a client
sending its own `X-Doctor-Auth-Id` has it dropped, so it cannot be used to read
another doctor's records. If you ever widen that allowlist, this is the thing you
are putting at risk.

They do **not** appear in `lib/types/api.d.ts`, and should not. The backend
declares them as OpenAPI security schemes rather than per-endpoint parameters, so
`npm run gen:api` leaves every path's `header?: never` alone. Declared as ordinary
parameters they would regenerate as *required* headers on every operation and
break every call site in `lib/api/`, none of which can supply them — they are
injected server-side. Same arrangement `X-API-Key` already had.

**The key must never reach the browser bundle.** Anything in `NEXT_PUBLIC_*` or
imported into client code is shipped to every visitor. Read it from a server-only
env var and proxy through a route handler:

```ts
// app/api/[...path]/route.ts — server-side only
const res = await fetch(`${process.env.BACKEND_URL}/${path}`, {
  headers: {
    "X-API-Key": process.env.BACKEND_API_KEY!,   // no NEXT_PUBLIC_ prefix
    "X-Doctor-Auth-Id": session.user.id,          // from the server session
    "X-Doctor-Email": session.user.email,
  },
  ...init,
});
```

The browser then calls your own `/api/...` routes and never sees the secret. That
also means CORS stops mattering for those calls, since they are server-to-server.

`GET /openapi.json` and `/docs` are deliberately left open so you can generate
types without the key.

Over plain HTTP the key is readable in transit. Fine on localhost; a real
deployment needs TLS. Note the key now carries more weight than it used to:
because FastAPI trusts the identity headers that accompany it, anyone holding
`API_KEY` can act as any doctor. It is a tenancy-bearing secret, not just a gate.

**Not yet implemented, deliberately:** FastAPI does not verify the Neon Auth token
itself. It trusts this proxy's assertion of who is signed in, which is sound only
because the browser cannot reach FastAPI directly, cannot obtain `API_KEY`, and
this route re-checks the session on every request. Verifying the JWT against
Neon Auth's JWKS endpoint is a later increment and changes nothing here.

### The stale-brief warning is currently noisy

`InsightBrief.tsx` warns "This brief may be out of date" when
`visit.transcript_uploaded_at > insight.generated_at`. **15 of the 18 seeded visits
trip it, and all 15 are false positives** — `transcript_uploaded_at` records when
the transcript *columns* were last written, and a format migration bumped it after
those briefs were generated without changing a word. The stored text has been
verified byte-identical to the archived originals.

Known and accepted for now. Don't remove the warning — a genuinely replaced
transcript *should* invalidate a brief — and don't "fix" the data by recompiling,
which costs 3 model calls per visit against a 20/day quota to rebuild briefs from
identical input.

The fix, when it happens, is backend-side: compare a content hash instead of a
timestamp. The UI change would be to prefer `insight.transcript_sha256` over the
timestamp comparison once that field exists.

### 404, not 403

A patient, visit, follow-up or job id that belongs to **another** doctor returns
`404`, not `403`. That is intentional: a 403 would confirm the id exists and leak
that some other doctor holds a record under it, so "unknown id" and "not yours"
are made deliberately indistinguishable.

Two consequences for the UI:

- A 404 on a detail page is now a routine outcome, not necessarily a bug — a link
  shared from a different account produces one. `PatientDetailPage` distinguishes
  it ("This patient is not in your records.") from a genuine failure, and new
  detail pages should do the same rather than showing a generic "could not load".
- Do not treat ids as globally addressable in UI copy or deep links. They are only
  meaningful within one doctor's account.

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

Vercel Deployment: https://patient-recap.vercel.app
