/**
 * The single API client. Every request goes through here so:
 *  - the base URL is set in one place
 *  - every error is normalised into ApiError with the backend's
 *    {code, message, details} shape
 *
 * The browser never talks to FastAPI directly. Requests go to same-origin
 * `/api/v1/...`, which app/api/[...path]/route.ts verifies and forwards with the
 * backend's API key attached. That key, and the backend's address, exist only on
 * the server — there is deliberately no NEXT_PUBLIC_ variable for either.
 *
 * The base URL is therefore empty: the generated paths already begin with
 * `/api/v1`, so `fetch` resolves them against the current origin.
 */
import createClient from "openapi-fetch";
import type { paths } from "../types/api";

/** Same origin. See the note above about why this is not configurable. */
export const API_BASE_URL = "";

/** The error shape every backend failure uses (frontend/README.md "Errors"). */
export interface ApiErrorBody {
  code: string;
  message: string;
  details?: Record<string, unknown>;
}

export class ApiError extends Error {
  code: string;
  status: number;
  details: Record<string, unknown>;

  constructor(status: number, body: ApiErrorBody) {
    super(body.message || `Request failed (${status})`);
    this.name = "ApiError";
    this.status = status;
    this.code = body.code || "error";
    this.details = body.details ?? {};
  }

  /** True when the session has gone and the user needs to sign in again. */
  get isUnauthorized(): boolean {
    return this.status === 401;
  }
}

export const api = createClient<paths>({ baseUrl: API_BASE_URL });

/**
 * Unwraps an openapi-fetch response: returns `data` on success, throws
 * ApiError on failure. Every feature module calls its endpoints through this
 * so error handling isn't repeated at every call site.
 */
export function unwrap<T>(result: {
  data?: T;
  error?: unknown;
  response: Response;
}): T {
  if (result.error !== undefined) {
    const body = (result.error ?? {
      code: "error",
      message: "Request failed",
    }) as ApiErrorBody;
    throw new ApiError(result.response.status, body);
  }
  if (result.data === undefined) {
    // 204 No Content and similar: no body, not an error.
    return undefined as T;
  }
  return result.data;
}
