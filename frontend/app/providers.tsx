"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { NeonAuthUIProvider } from "@neondatabase/auth-ui";
import { authClient } from "@/lib/auth/client";
import { ToastProvider } from "@/components/Toast";
import { APP_HOME } from "@/lib/routes";

/**
 * Client-side providers shared by every page: the public home page, the auth
 * views and the app.
 *
 * NeonAuthUIProvider wraps the rest so the pre-built auth components work
 * anywhere, including the <UserButton> in the shell. It is given Next's router
 * and Link so the auth UI navigates client-side instead of doing full page loads.
 *
 * The /stats counters are deliberately not here; StatsProvider lives in the
 * (app) layout. At the root it fetched /api/v1/stats on mount on every page, so
 * the public home page and the sign-in screen sent an unauthenticated request
 * and got a 401 for every visitor. Root providers also survive client-side
 * navigation, so after an email sign-in (which navigates client-side) that
 * failed result stayed in the nav until some action happened to reload it.
 *
 * `redirectTo` is where every sign-in lands, and for Google it is also the OAuth
 * callback URL, which proxy.ts must see to finish the exchange. It used to be the
 * default "/", which was the queue; "/" is now the public home page, so it points
 * at APP_HOME directly rather than bouncing through a redirect.
 *
 * `social.providers` is what renders the "Continue with Google" button in
 * <AuthView>. Google is already enabled on this Neon project with shared
 * development credentials, so it works without any Google Cloud setup.
 *
 * `defaultTheme="light"` is load-bearing, not a preference. auth-ui bundles
 * next-themes and defaults to `"system"`, so on a machine set to dark mode the
 * auth card rendered dark while the rest of the app stayed light — the clinical
 * design system in app/design-system.css is light-only and has no dark variant.
 * Pinning it to light is what keeps the sign-in screen and the app one product.
 * If a dark theme is ever designed this is the switch to revisit, and it has to
 * change together with design-system.css rather than on its own.
 */
export function Providers({ children }: { children: React.ReactNode }) {
  const router = useRouter();

  return (
    <NeonAuthUIProvider
      authClient={authClient}
      navigate={(href: string) => router.push(href)}
      Link={Link}
      redirectTo={APP_HOME}
      social={{ providers: ["google"] }}
      defaultTheme="light"
    >
      <ToastProvider>{children}</ToastProvider>
    </NeonAuthUIProvider>
  );
}
