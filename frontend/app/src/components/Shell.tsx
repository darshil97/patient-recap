import { NavLink, Outlet } from "react-router-dom";
import { useStatsContext } from "../hooks/StatsContext";

/**
 * The top-navbar app shell from the approved design (frontend/designs/
 * alternative_3): brand mark, nav links with live counts, "does not
 * diagnose" footer note, and an <Outlet /> for the current page.
 */
export function Shell() {
  const { stats } = useStatsContext();

  const openFollowUps = stats?.follow_ups.open;
  const needsAction =
    (stats?.visits.awaiting_transcript ?? 0) +
    (stats?.visits.awaiting_insights ?? 0) +
    (stats?.visits.scheduled ?? 0);
  const patientCount = stats?.patients;

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
          <NavLink to="/" end className={({ isActive }) => (isActive ? "active" : "")}>
            Today
            {needsAction !== undefined && (
              <span className="count">{needsAction}</span>
            )}
          </NavLink>
          <NavLink
            to="/patients"
            className={({ isActive }) => (isActive ? "active" : "")}
          >
            Patients
            {patientCount !== undefined && (
              <span className="count">{patientCount}</span>
            )}
          </NavLink>
          <div className="nav-label">Records</div>
          <NavLink
            to="/follow-ups"
            className={({ isActive }) => (isActive ? "active" : "")}
          >
            Follow-ups
            {openFollowUps !== undefined && (
              <span className="count">{openFollowUps}</span>
            )}
          </NavLink>
        </nav>
        <div className="sidebar-foot">
          Records what was said.
          <br />
          Does not diagnose.
        </div>
      </header>

      <main className="main" id="main">
        <Outlet />
      </main>
    </div>
  );
}
