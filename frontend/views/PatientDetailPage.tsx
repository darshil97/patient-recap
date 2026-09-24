"use client";

import { useEffect, useState } from "react";
import Link from "next/link";
import { ToolNote } from "../components/ToolNote";
import { useToast } from "../components/Toast";
import { useStatsContext } from "../hooks/StatsContext";
import { getTimeline, updatePatient, type Timeline } from "@/lib/api/patients";
import { updateFollowUp } from "@/lib/api/followUps";
import { ScheduleVisitModal } from "../features/visits/ScheduleVisitModal";
import { DismissDialog } from "../features/followUps/DismissDialog";
import { formatDate, dueUrgency, parseDateOnly } from "../lib/dates";
import { ApiError } from "@/lib/api/client";

/** The three the backend accepts, plus "not specified". */
type Gender = "male" | "female" | "others" | "";

/** `patientId` arrives as a prop from app/patients/[patientId]/page.tsx, which
 *  awaits the route params on the server. */
export function PatientDetailPage({ patientId }: { patientId: string }) {
  const { stats, reload: reloadStats } = useStatsContext();
  const { showToast } = useToast();

  const [timeline, setTimeline] = useState<Timeline | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [showSchedule, setShowSchedule] = useState(false);
  const [showDismiss, setShowDismiss] = useState(false);
  const [editing, setEditing] = useState(false);
  const [busy, setBusy] = useState(false);

  async function load() {
    if (!patientId) return;
    setLoading(true);
    setError(null);
    try {
      const data = await getTimeline(patientId);
      setTimeline(data);
    } catch (err) {
      // 404 is now a routine outcome, not just a bad id: the backend scopes
      // every patient to the signed-in doctor and answers another doctor's
      // patient id with 404 rather than 403, deliberately, so ids cannot be
      // probed. Opening a link shared from a different account therefore lands
      // here. "Could not load" reads like a transient failure and invites a
      // pointless retry, so say what actually happened.
      setError(
        err instanceof ApiError && err.status === 404
          ? "This patient is not in your records."
          : "Could not load this patient's record.",
      );
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    load();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [patientId]);

  if (loading) {
    return (
      <div className="page">
        <div className="page-loading">
          <span className="spinner" aria-hidden="true" />
          Loading patient record&hellip;
        </div>
      </div>
    );
  }

  if (error || !timeline) {
    return (
      <div className="page">
        <div className="error-banner">{error ?? "Patient not found."}</div>
        <Link href="/patients">Back to patients</Link>
      </div>
    );
  }

  const { patient, visits } = timeline;
  const asOf = stats ? parseDateOnly(stats.as_of) : new Date();
  const openFollowUp = visits.find((v) => v.follow_up?.status === "open")?.follow_up ?? null;
  const latestVisit = visits[0];

  async function markContacted() {
    if (!openFollowUp) return;
    try {
      await updateFollowUp(openFollowUp.follow_up_id, { mark_contacted: true, status: null });
      showToast("Marked as contacted.");
      load();
    } catch (err) {
      showToast(err instanceof ApiError ? err.message : "Could not record the call.", {
        variant: "error",
      });
    }
  }

  async function confirmDismiss(reason: string) {
    if (!openFollowUp) return;
    setBusy(true);
    try {
      await updateFollowUp(openFollowUp.follow_up_id, {
        status: "dismissed",
        dismissed_reason: reason,
        mark_contacted: false,
      });
      setShowDismiss(false);
      showToast(`Dismissed ${patient.patient_name}.`, {
        action: {
          label: "Undo",
          onClick: async () => {
            await updateFollowUp(openFollowUp.follow_up_id, {
              status: "open",
              mark_contacted: false,
            });
            load();
          },
        },
      });
      load();
      reloadStats();
    } catch (err) {
      showToast(err instanceof ApiError ? err.message : "Could not dismiss the follow-up.", {
        variant: "error",
      });
    } finally {
      setBusy(false);
    }
  }

  return (
    <>
      <div className="record-head">
        <p style={{ fontSize: 12.5, marginBottom: 8 }}>
          <Link href="/patients">Patients</Link>
          <span className="muted"> / {patient.patient_name}</span>
        </p>
        <div className="record-head-top">
          <div>
            <h1 className="record-name">{patient.patient_name}</h1>
            <div className="record-meta">
              <span>
                {patient.age ?? "—"}
                {patient.gender ? patient.gender[0].toUpperCase() : ""}
              </span>
              <span className="sep">|</span>
              <span className="mrn">{patient.external_id}</span>
              <span className="sep">|</span>
              <span>DOB {formatDate(patient.date_of_birth)}</span>
              <span className="sep">|</span>
              {patient.phone ? (
                <span>{patient.phone}</span>
              ) : (
                <span className="none">No phone on file</span>
              )}
              <button className="btn btn--sm btn--subtle" onClick={() => setEditing(true)}>
                Edit details
              </button>
            </div>
          </div>
          <div className="record-actions">
            <button className="btn" onClick={() => setShowSchedule(true)}>
              Schedule visit
            </button>
          </div>
        </div>
      </div>

      <div className="page">
        <div className="brief">
          <div>
            {latestVisit?.insight && (
              <div className="card">
                <div className="card-head">
                  <h2 className="card-title">Where things stand</h2>
                  <span className="muted" style={{ fontSize: 12 }}>
                    After visit {latestVisit.visit_number} &middot; {formatDate(latestVisit.visit_date)}
                  </span>
                </div>
                <div className="card-body">
                  <p className="lede">{latestVisit.insight.chief_issue}</p>
                </div>
              </div>
            )}

            <h2 style={{ fontSize: 14, margin: "22px 0 12px" }}>
              Visit history{" "}
              <span className="muted" style={{ fontWeight: 400 }}>
                &middot; {visits.length} visit{visits.length === 1 ? "" : "s"}
              </span>
            </h2>

            {visits.length === 0 ? (
              <div className="card">
                <div className="empty">
                  <p className="empty-title">No visits yet</p>
                  <p className="empty-text">Schedule the first consultation to start this record.</p>
                </div>
              </div>
            ) : (
              <ul className="timeline">
                {visits.map((v, i) => (
                  <li key={v.visit_id} className={`tl-item ${i === 0 ? "tl-item--current" : ""}`}>
                    <span className="tl-dot" aria-hidden="true" />
                    <div className="tl-card">
                      <div className="tl-head">
                        <div>
                          <span className="tl-when">{formatDate(v.visit_date)}</span>
                          <span className="tl-visit">
                            {" "}
                            &middot; Visit {v.visit_number} &middot;{" "}
                            {v.visit_type === "initial" ? "Initial" : "Follow-up"}
                            {v.visit_time ? ` \u00b7 ${v.visit_time.slice(0, 5)}` : ""}
                          </span>
                        </div>
                        {v.has_insights ? (
                          <Link href={`/visits/${v.visit_id}`} style={{ fontSize: 12.5 }}>
                            Open brief
                          </Link>
                        ) : (
                          <Link href={`/visits/${v.visit_id}`} style={{ fontSize: 12.5 }}>
                            View status
                          </Link>
                        )}
                      </div>
                      {v.insight ? (
                        <p style={{ color: "var(--ink-2)" }}>{v.insight.chief_issue}</p>
                      ) : (
                        <p className="muted">No brief generated for this visit yet.</p>
                      )}
                      {v.follow_up && (
                        <dl className="kv" style={{ marginTop: 10 }}>
                          <dt>Follow-up</dt>
                          <dd>
                            {formatDate(v.follow_up.due_earliest)}
                            {v.follow_up.status === "open" && (
                              <span
                                className={`badge badge--dot ${
                                  dueUrgency(v.follow_up.due_earliest, asOf) === "overdue"
                                    ? "badge--overdue"
                                    : dueUrgency(v.follow_up.due_earliest, asOf) === "soon"
                                      ? "badge--soon"
                                      : ""
                                }`}
                                style={{ marginLeft: 6 }}
                              >
                                Open
                              </span>
                            )}
                            {v.follow_up.status === "resolved" && (
                              <span className="badge badge--ok" style={{ marginLeft: 6 }}>
                                Resolved
                              </span>
                            )}
                            {v.follow_up.status === "dismissed" && (
                              <span className="badge" style={{ marginLeft: 6 }}>
                                Dismissed
                              </span>
                            )}
                            {v.follow_up.needs_confirmation && (
                              <span className="badge badge--soon" style={{ marginLeft: 4 }}>
                                Confirm date
                              </span>
                            )}
                          </dd>
                        </dl>
                      )}
                    </div>
                  </li>
                ))}
              </ul>
            )}
          </div>

          <aside className="rail">
            {openFollowUp && (
              <div className="card">
                <div className="card-head">
                  <h2 className="card-title">Next follow-up</h2>
                </div>
                <div className="card-body">
                  <div style={{ fontSize: 17, fontWeight: 600 }}>
                    {formatDate(openFollowUp.due_earliest)}
                  </div>
                  {openFollowUp.needs_confirmation && (
                    <p className="notice" style={{ margin: "12px 0 0" }}>
                      <svg width="14" height="14" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.6" aria-hidden="true">
                        <path d="M8 2.8 14.2 13H1.8L8 2.8Z" />
                        <path d="M8 6.6v2.8M8 11.1h.01" />
                      </svg>
                      <span>
                        <span className="notice-title">Date needs confirming.</span> The
                        timing was hedged, so this is a best estimate. Set the real date
                        when booking.
                      </span>
                    </p>
                  )}
                  <button
                    className="btn btn--primary"
                    style={{ width: "100%", justifyContent: "center", marginTop: 12 }}
                    onClick={() => setShowSchedule(true)}
                  >
                    Schedule visit
                  </button>
                  <button
                    className="btn btn--subtle"
                    style={{ width: "100%", justifyContent: "center", marginTop: 6 }}
                    onClick={markContacted}
                  >
                    Mark contacted
                  </button>
                  <button
                    className="btn btn--subtle"
                    style={{ width: "100%", justifyContent: "center", marginTop: 6 }}
                    onClick={() => setShowDismiss(true)}
                  >
                    Not returning &mdash; dismiss
                  </button>
                </div>
              </div>
            )}

            {latestVisit?.insight?.next_visit_focus && (
              <div className="card">
                <div className="card-head">
                  <h2 className="card-title">Focus next time</h2>
                </div>
                <div className="card-body">
                  <p style={{ color: "var(--ink-2)" }}>{latestVisit.insight.next_visit_focus}</p>
                </div>
              </div>
            )}

            <div className="card">
              <div className="card-head">
                <h2 className="card-title">Record</h2>
              </div>
              <div className="card-body">
                <dl className="kv">
                  <dt>Visits</dt>
                  <dd>{patient.visit_count}</dd>
                  <dt>Last seen</dt>
                  <dd>{patient.last_visit_date ? formatDate(patient.last_visit_date) : "—"}</dd>
                  <dt>Briefs</dt>
                  <dd>
                    {visits.filter((v) => v.has_insights).length} of {visits.length} generated
                  </dd>
                </dl>
              </div>
            </div>
          </aside>
        </div>

        <ToolNote />
      </div>

      {showSchedule && (
        <ScheduleVisitModal
          patientId={patient.patient_id}
          patientName={patient.patient_name}
          onClose={() => setShowSchedule(false)}
          onScheduled={() => {
            setShowSchedule(false);
            showToast("Visit scheduled.");
            load();
            reloadStats();
          }}
        />
      )}

      {showDismiss && (
        <DismissDialog
          patientName={patient.patient_name}
          submitting={busy}
          onCancel={() => setShowDismiss(false)}
          onConfirm={confirmDismiss}
        />
      )}

      {editing && (
        <EditPatientModal
          patient={patient}
          onClose={() => setEditing(false)}
          onSaved={() => {
            setEditing(false);
            load();
          }}
        />
      )}
    </>
  );
}

function EditPatientModal({
  patient,
  onClose,
  onSaved,
}: {
  patient: Timeline["patient"];
  onClose: () => void;
  onSaved: () => void;
}) {
  const [firstName, setFirstName] = useState(patient.first_name);
  const [lastName, setLastName] = useState(patient.last_name ?? "");
  const [phone, setPhone] = useState(patient.phone ?? "");
  const [dob, setDob] = useState(patient.date_of_birth ?? "");
  const [gender, setGender] = useState<Gender>(
    (patient.gender as Gender) ?? "",
  );
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const { stats } = useStatsContext();

  // The server's date. `new Date().toISOString()` is UTC, so east of Greenwich
  // it reports yesterday for the first hours of every local day.
  const today = stats?.as_of ?? new Date().toISOString().slice(0, 10);

  async function save() {
    setSaving(true);
    setError(null);
    try {
      await updatePatient(patient.patient_id, {
        first_name: firstName,
        last_name: lastName || undefined,
        phone: phone || undefined,
        date_of_birth: dob || undefined,
        gender: gender || undefined,
      });
      onSaved();
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Could not save changes.");
    } finally {
      setSaving(false);
    }
  }

  return (
    <div className="modal-overlay" onMouseDown={(e) => e.target === e.currentTarget && onClose()}>
      <div className="modal" role="dialog" aria-modal="true" aria-label="Edit details">
        <div className="modal-head">
          <h2 className="modal-title">Edit details</h2>
        </div>
        <div className="modal-body">
          {error && <div className="error-banner">{error}</div>}
          <div className="form-row">
            <div className="form-group">
              <label className="form-label" htmlFor="ep-first">
                First name
              </label>
              <input id="ep-first" className="input" value={firstName} onChange={(e) => setFirstName(e.target.value)} />
            </div>
            <div className="form-group">
              <label className="form-label" htmlFor="ep-last">
                Last name
              </label>
              <input id="ep-last" className="input" value={lastName} onChange={(e) => setLastName(e.target.value)} />
            </div>
          </div>
          <div className="form-row">
            <div className="form-group">
              <label className="form-label" htmlFor="ep-dob">
                Date of birth
              </label>
              <input
                id="ep-dob"
                type="date"
                className="input"
                value={dob}
                onChange={(e) => setDob(e.target.value)}
                max={today}
              />
            </div>
            <div className="form-group">
              <label className="form-label" htmlFor="ep-phone">
                Phone
              </label>
              <input id="ep-phone" className="input" value={phone} onChange={(e) => setPhone(e.target.value)} />
            </div>
          </div>
          <div className="form-row">
            <div className="form-group">
              <label className="form-label" htmlFor="ep-gender">
                Gender
              </label>
              {/* A dropdown, matching registration. This form had no gender
                  field at all, so a value mistyped at registration could not be
                  corrected here — and the backend accepted free text on PATCH
                  while POST allowed only these three, so the one route that
                  could write it was the one with no validation. */}
              <select
                id="ep-gender"
                className="select"
                value={gender}
                onChange={(e) => setGender(e.target.value as Gender)}
              >
                <option value="">Not specified</option>
                <option value="male">Male</option>
                <option value="female">Female</option>
                <option value="others">Other</option>
              </select>
            </div>
            <div className="form-group" />
          </div>
        </div>
        <div className="modal-foot">
          <button type="button" className="btn" onClick={onClose}>
            Cancel
          </button>
          <button type="button" className="btn btn--primary" disabled={saving} onClick={save}>
            {saving ? "Saving…" : "Save"}
          </button>
        </div>
      </div>
    </div>
  );
}
