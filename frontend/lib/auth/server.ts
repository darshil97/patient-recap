/**
 * The server-side Neon Auth (Managed Better Auth) instance.
 *
 * This module is server-only. It reads NEON_AUTH_COOKIE_SECRET, which must
 * never reach the browser bundle, so nothing here may be imported from a
 * "use client" file.
 *
 * It provides:
 *   auth.handler()     -> app/api/auth/[...path]/route.ts
 *   auth.middleware()  -> proxy.ts (Next 16 renamed middleware.ts to proxy.ts)
 *   auth.getSession()  -> server components, server actions, and the BFF proxy
 */
import { createNeonAuth } from "@neondatabase/auth/next/server";

function required(name: string): string {
  const value = process.env[name];
  if (!value) {
    throw new Error(
      `${name} is not set. Copy frontend/.env.local.example to ` +
        `frontend/.env.local and fill it in.`,
    );
  }
  return value;
}

export const auth = createNeonAuth({
  baseUrl: required("NEON_AUTH_BASE_URL"),
  cookies: {
    // HMAC-SHA256 signing key for the cached session_data cookie. Must be
    // 32+ characters; generate with `openssl rand -base64 32`.
    secret: required("NEON_AUTH_COOKIE_SECRET"),
  },
  // Raise to "debug" when diagnosing NETWORK_* errors from the auth server.
  logLevel: process.env.NODE_ENV === "development" ? "warn" : "error",
});
