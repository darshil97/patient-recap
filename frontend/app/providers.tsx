"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { NeonAuthUIProvider } from "@neondatabase/auth-ui";
import { authClient } from "@/lib/auth/client";
import { ToastProvider } from "@/components/Toast";
import { StatsProvider } from "@/hooks/StatsContext";

/**
 * Every client-side provider, in the order the Vite app used: toasts outermost
 * so anything inside can raise one, then the shared stats used by the nav
 * counters.
 *
 * NeonAuthUIProvider wraps them so the pre-built auth components work anywhere,
 * including the <UserButton> in the shell. It is given Next's router and Link so
 * the auth UI navigates client-side instead of doing full page loads.
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
      social={{ providers: ["google"] }}
      defaultTheme="light"
    >
      <ToastProvider>
        <StatsProvider>{children}</StatsProvider>
      </ToastProvider>
    </NeonAuthUIProvider>
  );
}
