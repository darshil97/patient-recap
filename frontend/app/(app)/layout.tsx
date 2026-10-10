import { Shell } from "@/components/Shell";
import { LiveSessionProvider } from "@/features/recording/LiveSessionProvider";

/**
 * The authenticated area. Everything in this route group gets the clinical shell
 * with its nav and counters; /auth/* deliberately sits outside it so the
 * sign-in page has no nav.
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
    <LiveSessionProvider>
      <Shell>{children}</Shell>
    </LiveSessionProvider>
  );
}
