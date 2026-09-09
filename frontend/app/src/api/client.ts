/**
 * The single API client. Every request goes through here so:
 *  - the base URL and (future) X-API-Key header are set in one place
 *  - every error is normalised into ApiError with the backend's
 *    {code, message, details} shape
 *
 * See frontend/README.md "Authentication": the key must never reach the
 * browser bundle. This client reads a build-time base URL only; if API_KEY is
 * ever enabled server-side, requests must be proxied through a server-side
 * route instead of adding the header here.
 */
import createClient from "openapi-fetch";
import type { paths } from "../types/api";

// The server origin only. Every generated path already includes the
// /api/v1 prefix, so this must not also end in /api/v1 or requests double up.
export const API_BASE_URL: string =
  import.meta.env.VITE_API_BASE_URL ?? "http://127.0.0.1:8000";

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
