/**
 * Neon Auth API proxy: sign-in, sign-up, OAuth callbacks, session management.
 *
 * This is a static `auth` segment, so it takes precedence over the
 * app/api/[...path] BFF catch-all and auth traffic never reaches FastAPI.
 */
import { auth } from "@/lib/auth/server";

export const { GET, POST } = auth.handler();
