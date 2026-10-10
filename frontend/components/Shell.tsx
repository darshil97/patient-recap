"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { UserButton } from "@neondatabase/auth-ui";
import { useStatsContext } from "@/hooks/StatsContext";
import { RecordingBar } from "@/features/recording/RecordingBar";

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

  // "Today" renders the follow-up queue, so it badges the follow-up count. It
  // used to badge a *visit* count -- scheduled plus awaiting transcript plus
  // awaiting insights -- which is the Visit schedule tab's number, on a link
  // that opens a different list. Two nav items claiming to be the same screen is
  // confusing enough without the counts disagreeing about which one you are on.
  const openFollowUps = stats?.follow_ups.open;
  const scheduleNeedsAction =
    (stats?.visits.awaiting_transcript ?? 0) +
    (stats?.visits.awaiting_insights ?? 0) +
    (stats?.visits.scheduled ?? 0) +
    (stats?.visits.in_progress ?? 0);
  const patientCount = stats?.patients;

  // "/" and "/follow-ups" render the same queue page, but they are still two
  // distinct nav links — each highlights only when it is the current route.
  // (Previously both used one shared flag, which marked Follow-ups active
  // whenever you were on "/", giving it a permanently solid teal pill.)
  const isTodayActive = pathname === "/";
  const isFollowUpsActive = pathname === "/follow-ups";

  return (
    <div className="app">
      <a className="skip-link" href="#main">
        Skip to main content
      </a>
      {/* Header and recording bar stick together, so a recording in progress
          stays visible however far the doctor scrolls a patient's history. */}
      <div className="app-top">
        <header className="sidebar">
          <div className="brand">
            <div>
              <div className="brand-name">Consultation Insights</div>
            </div>
          </div>
          <nav className="nav" aria-label="Main">
            <Link href="/" className={isTodayActive ? "active" : ""}>
              Today
              {openFollowUps !== undefined && (
                <span className="count">{openFollowUps}</span>
              )}
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
              {stats?.visits !== undefined && (
                <span className="count">{scheduleNeedsAction}</span>
              )}
            </Link>
            <Link href="/follow-ups" className={isFollowUpsActive ? "active" : ""}>
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

        <RecordingBar />
      </div>

      <main className="main" id="main">
        {children}
      </main>
    </div>
  );
}
