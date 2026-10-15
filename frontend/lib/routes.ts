/**
 * The handful of paths more than one module has to agree on.
 *
 * APP_HOME in particular is named by proxy.ts (where a signed-in visitor to "/"
 * is sent), by NeonAuthUIProvider's redirectTo (where every sign-in lands, and
 * so where Google's OAuth callback returns), and by the shell's "Today" link. If
 * those disagree, sign-in lands on the public home page and immediately
 * redirects, or the nav highlights nothing — so they read it from here.
 *
 * Safe to import from both server and "use client" modules: plain constants.
 */

/** The public home page that describes the product. Not behind sign-in. */
export const PUBLIC_HOME = "/";

/** Where a signed-in doctor's day starts: the follow-up queue. */
export const APP_HOME = "/today";

/** Neon Auth's views, rendered by app/auth/[path]/page.tsx. */
export const SIGN_IN = "/auth/sign-in";
export const SIGN_UP = "/auth/sign-up";
