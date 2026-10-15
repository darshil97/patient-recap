import { Shell } from "@/components/Shell";
import { LiveSessionProvider } from "@/features/recording/LiveSessionProvider";
import { StatsProvider } from "@/hooks/StatsContext";

/**
 * The authenticated area. Everything in this route group gets the clinical shell
 * with its nav and counters; /auth/* and the public home page deliberately sit
 * outside it so neither has the nav.
 *
 * StatsProvider is here rather than in app/providers.tsx because /stats needs a
 * signed-in doctor: at the root it fired on the public pages too, and 401'd.
 * Every reader of it (the shell's counters, the views, the two modals) is inside
 * this group.
 *
 * The live recorder sits here, above every page, so a consultation keeps
 * recording while the doctor looks at the patient's history (§8.24). A layout
 * persists across client-side navigation within the group; a full reload or
 * sign-out ends it, which its leave-page warning asks about first.
 *
 * Access is enforced in proxy.ts, not here — a layout cannot reliably block a
 * request.
 */
export default function AppLayout({ children }: { children: React.ReactNode }) {
  return (
    <StatsProvider>
      <LiveSessionProvider>
        <Shell>{children}</Shell>
      </LiveSessionProvider>
    </StatsProvider>
  );
}
