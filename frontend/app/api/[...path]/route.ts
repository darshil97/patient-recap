/**
 * Backend-for-frontend proxy.
 *
 * The browser only ever calls same-origin `/api/v1/...`. This route verifies the
 * caller has a Neon Auth session, then forwards to the FastAPI backend with the
 * shared API key attached. Neither BACKEND_URL nor BACKEND_API_KEY is ever sent
 * to the browser, and the backend never needs to be reachable from it.
 *
 * Why the paths keep their `/api/v1` prefix: the generated OpenAPI types already
 * include it, so the browser calling `/api/v1/patients` means this proxy is a
 * pure pass-through with no rewriting layer for bugs to hide in.
 *
 * Routing note: `app/api/auth/[...path]` wins over this catch-all because a
 * static segment outranks a dynamic one, so Neon Auth's own endpoints are not
 * proxied here. `assertNotAuthRoute` enforces that regardless.
 *
 * Three things this gets right that a naive proxy does not:
 *
 *   1. Multipart uploads. The transcript upload is multipart/form-data, whose
 *      Content-Type carries the boundary token. The body is forwarded as raw
 *      bytes and the incoming Content-Type is copied verbatim. Calling
 *      req.formData() and re-encoding would mint a new boundary that no longer
 *      matches the body, and the backend would reject the upload.
 *
 *   2. X-Total-Count. Paginated list endpoints return the total in that header.
 *      It has to be copied onto the response explicitly or the UI loses every
 *      result count.
 *
 *   3. Empty bodies. GET /api/v1/visits/{id}/job returns 204 when a visit has
 *      never had a job. 204/205/304 must carry no body at all, so constructing
 *      a Response with one throws.
 *
 *   4. A timeout. Without one, a backend that accepts the connection and then
 *      stalls holds the request until the platform kills it, which the UI cannot
 *      explain. See BACKEND_TIMEOUT_MS.
 */
import { NextResponse } from "next/server";
import { auth } from "@/lib/auth/server";

export const dynamic = "force-dynamic";
export const runtime = "nodejs";

/** Responses that must not carry a body. */
const BODILESS_STATUSES = new Set([204, 205, 304]);

/**
 * How long to wait on the backend before giving up.
 *
 * There was no timeout, so a backend that accepted the connection and then went
 * quiet held the request until the platform killed it — which surfaces as an
 * opaque function error rather than anything the UI can explain.
 *
 * 25s is chosen against the two real numbers: a Render free instance cold-starts
 * in 30-60s when it has been allowed to sleep, and a Vercel function has its own
 * hard ceiling. Waiting the full cold start would exceed that ceiling anyway, so
 * the useful thing is to fail *first*, with a message that names waking up as
 * the likely cause. UptimeRobot polls /api/v1/health every 12 minutes precisely
 * so this stays rare (§9.5).
 *
 * Neither insight generation nor recording transcription sits behind this:
 * POST /insights and POST /audio-uploads/{id}/finish return 202 in milliseconds
 * and the work happens on a background task the client polls. Recordings are
 * sent in chunks under Vercel's 4.5 MB request body limit, so no single request
 * here carries more than one chunk (backend speech.chunk_bytes).
 */
const BACKEND_TIMEOUT_MS = 25_000;

/**
 * Request headers worth forwarding. An allowlist rather than a copy-everything:
 * Cookie holds the Neon Auth session, which is ours and not the backend's
 * business, and Host/Origin would confuse it.
 *
 * The allowlist now carries a second job. Since the backend derives which
 * doctor's records to serve from the X-Doctor-* headers this route sets, a
 * copy-everything would let a browser send its own and read another doctor's
 * data. Keeping this list narrow is what makes those headers trustworthy:
 * anything a client sends under those names is dropped here, and the values the
 * backend sees are set below from the server-side session only.
 */
const FORWARD_REQUEST_HEADERS = ["content-type", "accept", "accept-language"];

/**
 * Response headers worth returning. X-Total-Count is why this list exists;
 * Content-Disposition matters if a transcript is ever served as a download.
 */
const FORWARD_RESPONSE_HEADERS = [
  "content-type",
  "content-disposition",
  "x-total-count",
];

function backendUrl(): string {
  const raw = process.env.BACKEND_URL;
  if (!raw) {
    throw new Error(
      "BACKEND_URL is not set. Copy frontend/.env.local.example to " +
        "frontend/.env.local and fill it in.",
    );
  }
  return raw.replace(/\/+$/, "");
}

function jsonError(status: number, code: string, message: string) {
  // Mirrors the backend's {code, message, details} shape so the existing
  // ApiError handling in lib/api/client.ts works for proxy failures too.
  return NextResponse.json({ code, message, details: {} }, { status });
}

/** Defence in depth: auth endpoints must never be tunnelled to FastAPI. */
function isAuthRoute(segments: string[]): boolean {
  return segments[0] === "auth";
}

async function handle(
  req: Request,
  ctx: { params: Promise<{ path: string[] }> },
): Promise<Response> {
  const { path } = await ctx.params;
  const segments = path ?? [];

  if (isAuthRoute(segments)) {
    return jsonError(404, "not_found", "Not found.");
  }

  // --- gate on a real session -------------------------------------------- //
  // proxy.ts already redirects unauthenticated page loads, but a fetch to this
  // route has to be checked on its own: middleware protects navigation, not
  // this handler's own invocation.
  let session;
  try {
    const result = await auth.getSession();
    session = result.data;
  } catch {
    return jsonError(503, "auth_unavailable", "Could not verify your session.");
  }

  if (!session?.user) {
    return jsonError(401, "unauthorized", "Sign in to continue.");
  }

  // The backend keys tenancy on these, so a session without them cannot be
  // forwarded: it would arrive as a request the backend cannot attribute to a
  // doctor, and it answers those with a 401 anyway. Failing here gives a clearer
  // error than letting it round-trip.
  if (!session.user.id || !session.user.email) {
    return jsonError(
      401,
      "unauthorized",
      "Your session is missing an account id or email address. Sign out and in again.",
    );
  }

  // --- forward ------------------------------------------------------------ //
  const incoming = new URL(req.url);
  const target = `${backendUrl()}/api/${segments.join("/")}${incoming.search}`;

  const headers = new Headers();
  for (const name of FORWARD_REQUEST_HEADERS) {
    const value = req.headers.get(name);
    // Copied verbatim, so a multipart Content-Type keeps its boundary token.
    if (value) headers.set(name, value);
  }
  headers.set("X-API-Key", process.env.BACKEND_API_KEY ?? "");

  // --- who is asking ------------------------------------------------------ //
  // The API key above says "a trusted server is calling". These say "on behalf
  // of this doctor", and the backend scopes every query to whoever they name.
  //
  // Set AFTER the allowlist loop on purpose: FORWARD_REQUEST_HEADERS copies from
  // the incoming request, so a header set before it could be overwritten by a
  // client sending the same name. Set here, a client-supplied X-Doctor-* is
  // discarded — it is not in the allowlist — and cannot be used to act as
  // another doctor. This is the only place identity is asserted, and it comes
  // from the server-side session, never from the browser.
  //
  // Both are sent because the backend resolves on the id and falls back to the
  // email. The id is the stable key (Better Auth never changes it); the email is
  // what creates the doctor record on first sign-in, and what matches the row
  // seeded by backend/sql/7_multi_tenancy.sql before anyone has signed in.
  headers.set("X-Doctor-Auth-Id", session.user.id);
  headers.set("X-Doctor-Email", session.user.email);
  if (session.user.name) headers.set("X-Doctor-Name", session.user.name);

  // Raw bytes: never re-encode. Re-encoding multipart changes the boundary.
  const hasBody = req.method !== "GET" && req.method !== "HEAD";
  const body = hasBody ? await req.arrayBuffer() : undefined;

  let upstream: Response;
  try {
    upstream = await fetch(target, {
      method: req.method,
      headers,
      body: body && body.byteLength > 0 ? body : undefined,
      redirect: "manual",
      cache: "no-store",
      signal: AbortSignal.timeout(BACKEND_TIMEOUT_MS),
    });
  } catch (err) {
    // A timeout and a refused connection are different things to a reader: one
    // is "it is probably waking up, try again", the other is "it is not there".
    const timedOut = err instanceof DOMException && err.name === "TimeoutError";
    return timedOut
      ? jsonError(
          504,
          "backend_timeout",
          "The clinical records service did not respond in time. It may be " +
            "starting up — try again in a moment.",
        )
      : jsonError(
          502,
          "backend_unreachable",
          "The clinical records service is not responding.",
        );
  }

  const responseHeaders = new Headers();
  for (const name of FORWARD_RESPONSE_HEADERS) {
    const value = upstream.headers.get(name);
    if (value) responseHeaders.set(name, value);
  }

  if (BODILESS_STATUSES.has(upstream.status)) {
    // A 204 with a body is a runtime error, not a warning.
    return new Response(null, {
      status: upstream.status,
      headers: responseHeaders,
    });
  }

  return new Response(upstream.body, {
    status: upstream.status,
    headers: responseHeaders,
  });
}

export const GET = handle;
export const POST = handle;
export const PATCH = handle;
export const PUT = handle;
export const DELETE = handle;
