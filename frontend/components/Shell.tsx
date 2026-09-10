"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { UserButton } from "@neondatabase/auth-ui";
import { useStatsContext } from "@/hooks/StatsContext";

/**
 * The top-navbar app shell from the approved design (frontend/design-history/
 * designs/alternative_3): brand mark, nav links with live counts, the signed-in
 * user menu, and a "does not diagnose" footer note.
 *
 * Ported from the Vite version: react-router's <NavLink> becomes next/link plus
 * usePathname for the active state, and <Outlet /> becomes the layout's
 * `children`.
 */
export function Shell({ children }: { children: React.ReactNode }) {
  const { stats } = useStatsContext();
  const pathname = usePathname();

  const openFollowUps = stats?.follow_ups.open;
  const needsAction =
    (stats?.visits.awaiting_transcript ?? 0) +
    (stats?.visits.awaiting_insights ?? 0) +
    (stats?.visits.scheduled ?? 0);
  const patientCount = stats?.patients;

  // "/" and "/follow-ups" render the same queue, so treat them as one tab.
  const isToday = pathname === "/" || pathname === "/follow-ups";

  return (
    <div className="app">
      <a className="skip-link" href="#main">
        Skip to main content
      </a>
      <header className="sidebar">
        <div className="brand">
          <div>
            <div className="brand-name">Consultation Insights</div>
          </div>
        </div>
        <nav className="nav" aria-label="Main">
          <Link href="/" className={isToday ? "active" : ""}>
            Today
            {needsAction !== undefined && <span className="count">{needsAction}</span>}
          </Link>
          <Link
            href="/patients"
            className={pathname.startsWith("/patients") ? "active" : ""}
          >
            Patients
            {patientCount !== undefined && <span className="count">{patientCount}</span>}
          </Link>
          <div className="nav-label">Records</div>
          <Link
            href="/schedule"
            className={pathname.startsWith("/schedule") ? "active" : ""}
          >
            Visit schedule
            {stats?.visits.scheduled !== undefined && (
              <span className="count">{stats.visits.scheduled}</span>
            )}
          </Link>
          <Link href="/follow-ups" className={isToday ? "active" : ""}>
            Follow-ups
            {openFollowUps !== undefined && <span className="count">{openFollowUps}</span>}
          </Link>
        </nav>
        <div className="sidebar-foot">
          {/* authClient comes from NeonAuthUIProvider in app/providers.tsx;
              UserButton reads it from context rather than taking a prop. */}
          <UserButton />
          <p style={{ marginTop: 10 }}>
            Records what was said.
            <br />
            Does not diagnose.
          </p>
        </div>
      </header>

      <main className="main" id="main">
        {children}
      </main>
    </div>
  );
}
