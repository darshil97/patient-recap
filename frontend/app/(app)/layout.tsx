import { Shell } from "@/components/Shell";

/**
 * The authenticated area. Everything in this route group gets the clinical shell
 * with its nav and counters; /auth/* deliberately sits outside it so the
 * sign-in page has no nav.
 *
 * Access is enforced in proxy.ts, not here — a layout cannot reliably block a
 * request.
 */
export default function AppLayout({ children }: { children: React.ReactNode }) {
  return <Shell>{children}</Shell>;
}
