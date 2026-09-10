/**
 * Route protection.
 *
 * Next.js 16 renamed `middleware.ts` to `proxy.ts`; on Next 15 and earlier this
 * same file must be named middleware.ts instead.
 *
 * Everything is protected except the auth views themselves, Neon Auth's own API
 * routes, and static assets. Unauthenticated visitors are redirected to
 * /auth/sign-in.
 *
 * This guards page navigation. The BFF in app/api/[...path]/route.ts repeats the
 * session check for its own requests, because middleware running on a navigation
 * says nothing about a fetch made later from an already-loaded page.
 */
import { auth } from "@/lib/auth/server";

export default auth.middleware({
  loginUrl: "/auth/sign-in",
});

export const config = {
  matcher: [
    /*
     * Page navigation only. Everything except:
     *   /auth/*        the sign-in and sign-up views
     *   /api/*         see below
     *   Next internals and common static files
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
