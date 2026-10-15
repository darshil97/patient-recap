/**
 * Route protection.
 *
 * Next.js 16 renamed `middleware.ts` to `proxy.ts`; on Next 15 and earlier this
 * same file must be named middleware.ts instead.
 *
 * Everything is protected except the public home page ("/"), the auth views
 * themselves, Neon Auth's own API routes, and static assets. Unauthenticated
 * visitors to any other page are redirected to /auth/sign-in.
 *
 * This guards page navigation. The BFF in app/api/[...path]/route.ts repeats the
 * session check for its own requests, because middleware running on a navigation
 * says nothing about a fetch made later from an already-loaded page.
 */
import { NextResponse, type NextRequest } from "next/server";
import { auth } from "@/lib/auth/server";
import { APP_HOME, PUBLIC_HOME, SIGN_IN } from "@/lib/routes";

const protect = auth.middleware({ loginUrl: SIGN_IN });

export default async function proxy(request: NextRequest) {
  if (request.nextUrl.pathname === PUBLIC_HOME) return home(request);
  return protect(request);
}

/**
 * "/" is public, but it still goes through Neon Auth rather than being excluded
 * from the matcher, for two reasons:
 *
 * - Neon Auth's middleware is what completes Google sign-in. The OAuth callback
 *   returns to `redirectTo` with a session verifier in the query string, and the
 *   middleware exchanges it for the session cookie on whatever page it lands on.
 *   Sign-in is pointed at APP_HOME (app/providers.tsx), but a callback that
 *   reaches "/" — an old link, a hand-typed `?redirectTo=/` — must still
 *   complete rather than silently drop the visitor back on the home page.
 * - It is how we learn whether the visitor is signed in, so a doctor who opens
 *   the bare domain goes straight to the queue as they always have.
 *
 * Neon Auth's Next adapter has no option to add a public route (its skip list is
 * fixed to the auth views), so its answer is interpreted instead:
 *
 *   redirect to the sign-in page  ->  signed out: serve the home page instead
 *   no redirect (allowed through) ->  signed in: send them to APP_HOME
 *   any other redirect            ->  the OAuth exchange: pass it through as-is
 *
 * "Allowed through" can only mean a live session here, because "/" is not on
 * Neon Auth's skip list. Cookies it set are carried across in every case: it
 * clears stale session data on the signed-out path and refreshes the session on
 * the signed-in one.
 */
async function home(request: NextRequest): Promise<NextResponse> {
  const decision = await protect(request);
  const location = decision.headers.get("location");

  if (location === null) {
    return withCookiesFrom(
      decision,
      NextResponse.redirect(new URL(APP_HOME, request.url)),
    );
  }

  if (new URL(location, request.url).pathname === SIGN_IN) {
    return withCookiesFrom(decision, NextResponse.next());
  }

  return decision;
}

function withCookiesFrom(source: NextResponse, target: NextResponse): NextResponse {
  for (const cookie of source.headers.getSetCookie()) {
    target.headers.append("Set-Cookie", cookie);
  }
  return target;
}

export const config = {
  matcher: [
    /*
     * Page navigation only. Everything except:
     *   /auth/*        the sign-in and sign-up views
     *   /api/*         see below
     *   Next internals and common static files
     *
     * "/" stays in the matcher on purpose even though it is public; see home().
     *
     * /api/* is excluded deliberately. This middleware answers an unauthenticated
     * request with a 307 to the sign-in page, which is right for a navigation and
     * wrong for a fetch: openapi-fetch would receive an HTML login page and fail
     * trying to parse it as JSON, turning "your session expired" into a parse
     * error. The BFF checks the session itself and returns a proper
     * {code:"unauthorized"} 401, so the API path loses nothing by being excluded.
     * /api/auth/* must be public regardless, since that is how sign-in happens.
     */
    "/((?!auth/|api/|_next/static|_next/image|favicon.ico|.*\\.(?:svg|png|jpg|jpeg|gif|webp|ico)$).*)",
  ],
};
