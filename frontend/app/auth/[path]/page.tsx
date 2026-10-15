import Link from "next/link";
import { AuthView } from "@neondatabase/auth-ui";
import { authViewPaths } from "@neondatabase/auth-ui/server";
import { PUBLIC_HOME } from "@/lib/routes";

/**
 * Every auth view (sign-in, sign-up, callback, forgot-password, ...) rendered by
 * Neon Auth's own components rather than hand-rolled forms.
 *
 * This route sits outside the (app) group on purpose, so it does not get the
 * clinical shell and nav. proxy.ts also excludes /auth/* from the session check,
 * which is what stops a signed-out visitor from being redirected in a loop.
 *
 * The Google button appears here because `social.providers` is configured on
 * NeonAuthUIProvider in app/providers.tsx. It only renders inside <AuthView> —
 * the standalone <SignInForm> does not show OAuth buttons.
 */
export const dynamicParams = false;

export function generateStaticParams() {
  return Object.values(authViewPaths).map((path) => ({ path }));
}

export default async function AuthPage({
  params,
}: {
  params: Promise<{ path: string }>;
}) {
  const { path } = await params;

  return (
    <main className="auth">
      <div className="auth-panel">
        {/* The same .brand markup the shell uses, so the gradient tile and the
            wordmark cannot drift apart between signed-out and signed-in. It
            links back to the public home page, which is where most visitors
            arrive here from. */}
        <Link href={PUBLIC_HOME} className="brand" aria-label="Consultation Insights, home">
          <div>
            <div className="brand-name">Consultation Insights</div>
          </div>
        </Link>

        <AuthView path={path} />

        {/* The standing disclaimer from the shell's footer. It belongs here too:
            this is the first screen anyone sees, and it is the one claim about
            the product that should never be missable. */}
        <p className="auth-note">
          Records what was said. Does not diagnose.
        </p>
      </div>
    </main>
  );
}
