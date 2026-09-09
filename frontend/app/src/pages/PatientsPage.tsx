import { useEffect, useMemo, useState } from "react";
import { Link } from "react-router-dom";
import { ToolNote } from "../components/ToolNote";
import { useStatsContext } from "../hooks/StatsContext";
import { useToast } from "../components/Toast";
import { listPatients, type Patient } from "../api/patients";
import { formatDate, dueUrgency, parseDateOnly } from "../lib/dates";
import { NewPatientModal } from "../features/patients/NewPatientModal";
import { ScheduleVisitModal } from "../features/visits/ScheduleVisitModal";
import { listFollowUps, type FollowUp } from "../api/followUps";

type SortOption = "name" | "created_desc";

export function PatientsPage() {
  const { stats, reload: reloadStats } = useStatsContext();
  const { showToast } = useToast();

  const [patients, setPatients] = useState<Patient[]>([]);
  const [followUpByPatient, setFollowUpByPatient] = useState<Map<string, FollowUp>>(new Map());
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [search, setSearch] = useState("");
  const [sort, setSort] = useState<SortOption>("name");
  const [showNewPatient, setShowNewPatient] = useState(false);
  const [scheduleFor, setScheduleFor] = useState<Patient | null>(null);

  async function load() {
    setLoading(true);
    setError(null);
    try {
      const [{ items }, { items: openFollowUps }] = await Promise.all([
        listPatients({ q: search || undefined }),
        listFollowUps({ status: "open", limit: 500 }),
      ]);
      const sorted = [...items].sort((a, b) => {
        if (sort === "created_desc") {
          return (b.created_at ?? "").localeCompare(a.created_at ?? "");
        }
        return a.patient_name.localeCompare(b.patient_name);
      });
      setPatients(sorted);
      setFollowUpByPatient(new Map(openFollowUps.map((f) => [f.patient_id, f])));
    } catch {
      setError("Could not load patients.");
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    load();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [sort]);

  useEffect(() => {
    const handle = setTimeout(load, 250);
    return () => clearTimeout(handle);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [search]);

  const asOf = stats ? parseDateOnly(stats.as_of) : new Date();

  const rows = useMemo(() => patients, [patients]);

  return (
    <div className="page">
      <div className="page-head">
        <div>
          <h1 className="page-title">Patients</h1>
          <p className="page-sub">{stats?.patients ?? rows.length} registered</p>
        </div>
        <button className="btn btn--primary" onClick={() => setShowNewPatient(true)}>
          <svg width="13" height="13" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.6" aria-hidden="true">
            <path d="M8 3v10M3 8h10" />
          </svg>
          New patient
        </button>
      </div>

      <div className="toolbar">
        <div className="field">
          <svg width="13" height="13" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.6" aria-hidden="true">
            <circle cx="7" cy="7" r="4.2" />
            <path d="M10.2 10.2 14 14" />
          </svg>
          <input
            type="search"
            placeholder="Name, patient ID or phone"
            aria-label="Search patients"
            value={search}
            onChange={(e) => setSearch(e.target.value)}
          />
        </div>
        <div className="field">
          <label htmlFor="p-sort" className="visually-hidden">
            Sort
          </label>
          <select id="p-sort" value={sort} onChange={(e) => setSort(e.target.value as SortOption)}>
            <option value="name">Name (A&ndash;Z)</option>
            <option value="created_desc">Recently added</option>
          </select>
        </div>
        <div className="spacer" />
        <span className="muted" style={{ fontSize: 12.5 }}>
          Showing {rows.length} of {stats?.patients ?? rows.length}
        </span>
      </div>

      {error && <div className="error-banner">{error}</div>}

      {loading ? (
        <div className="page-loading">
          <span className="spinner" aria-hidden="true" />
          Loading patients&hellip;
        </div>
      ) : rows.length === 0 ? (
        <div className="card">
          <div className="empty">
            <p className="empty-title">No patients found</p>
            <p className="empty-text">Try a different search, or register a new patient.</p>
          </div>
        </div>
      ) : (
        <div className="card">
          <table className="table table--stack">
            <caption className="visually-hidden">All patients</caption>
            <thead>
              <tr>
                <th scope="col">Patient</th>
                <th scope="col">Patient ID</th>
                <th scope="col">Phone</th>
                <th scope="col" className="num">
                  Visits
                </th>
                <th scope="col">Last seen</th>
                <th scope="col">Next follow-up</th>
                <th scope="col">
                  <span className="visually-hidden">Actions</span>
                </th>
              </tr>
            </thead>
            <tbody>
              {rows.map((p) => {
                const followUp = followUpByPatient.get(p.patient_id);
                return (
                  <tr key={p.patient_id}>
                    <td>
                      <div className="who">
                        <Link className="who-name" to={`/patients/${p.patient_id}`}>
                          {p.patient_name}
                        </Link>
                        <span className="who-meta">
                          {p.age ?? "—"}
                          {p.gender ? p.gender[0].toUpperCase() : ""}
                        </span>
                      </div>
                    </td>
                    <td>
                      <span className="mrn">{p.external_id}</span>
                    </td>
                    <td>{p.phone ? p.phone : <span className="none">&mdash;</span>}</td>
                    <td className="num">{p.visit_count}</td>
                    <td>
                      {p.last_visit_date ? (
                        formatDate(p.last_visit_date)
                      ) : (
                        <span className="none">Never seen</span>
                      )}
                    </td>
                    <td>
                      {followUp ? (
                        <>
                          <span
                            className={`badge badge--dot ${
                              dueUrgency(followUp.due_earliest, asOf) === "overdue"
                                ? "badge--overdue"
                                : dueUrgency(followUp.due_earliest, asOf) === "soon"
                                  ? "badge--soon"
                                  : ""
                            }`}
                          >
                            {formatDate(followUp.due_earliest)}
                          </span>
                          {followUp.needs_confirmation && (
                            <span className="badge badge--soon">Confirm date</span>
                          )}
                        </>
                      ) : (
                        <span className="none">&mdash;</span>
                      )}
                    </td>
                    <td className="actions">
                      <button className="btn btn--sm" onClick={() => setScheduleFor(p)}>
                        Schedule visit
                      </button>
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}

      <ToolNote />

      {showNewPatient && (
        <NewPatientModal
          onClose={() => setShowNewPatient(false)}
          onCreated={() => {
            setShowNewPatient(false);
            showToast("Patient registered.");
            load();
            reloadStats();
          }}
        />
      )}

      {scheduleFor && (
        <ScheduleVisitModal
          patientId={scheduleFor.patient_id}
          patientName={scheduleFor.patient_name}
          onClose={() => setScheduleFor(null)}
          onScheduled={() => {
            const name = scheduleFor.patient_name;
            setScheduleFor(null);
            showToast(`Visit scheduled for ${name}.`);
            load();
            reloadStats();
          }}
        />
      )}
    </div>
  );
}
