"use client";

import { useEffect, useMemo, useState } from "react";
import Link from "next/link";
import { QueueTabs } from "../components/Tabs";
import { DueBadge } from "../components/DueBadge";
import { Menu } from "../components/Menu";
import { ToolNote } from "../components/ToolNote";
import { NewPatientModal } from "../features/patients/NewPatientModal";
import { ScheduleVisitModal } from "../features/visits/ScheduleVisitModal";
import { DismissDialog } from "../features/followUps/DismissDialog";
import { useStatsContext } from "../hooks/StatsContext";
import { useToast } from "../components/Toast";
import {
  listFollowUps,
  updateFollowUp,
  type FollowUp,
} from "@/lib/api/followUps";
import { formatDate, dueUrgency, parseDateOnly } from "../lib/dates";
import { ApiError } from "@/lib/api/client";

type StatusFilter = "open" | "resolved" | "dismissed" | "all";

export function TodayPage() {
  const { stats, reload: reloadStats } = useStatsContext();
  const { showToast } = useToast();

  const [followUps, setFollowUps] = useState<FollowUp[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [search, setSearch] = useState("");
  const [statusFilter, setStatusFilter] = useState<StatusFilter>("open");
  const [sort, setSort] = useState<"due_asc" | "due_desc">("due_asc");

  const [showNewPatient, setShowNewPatient] = useState(false);
  const [scheduleFor, setScheduleFor] = useState<FollowUp | null>(null);
  const [dismissFor, setDismissFor] = useState<FollowUp | null>(null);
  const [busyId, setBusyId] = useState<string | null>(null);

  async function load() {
    setLoading(true);
    setError(null);
    try {
      const { items } = await listFollowUps({ status: statusFilter, sort });
      setFollowUps(items);
    } catch {
      setError("Could not load the follow-ups queue.");
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    load();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [statusFilter, sort]);

  const asOf = stats ? parseDateOnly(stats.as_of) : new Date();

  const filtered = useMemo(() => {
    const q = search.trim().toLowerCase();
    if (!q) return followUps;
    return followUps.filter(
      (f) =>
        f.patient_name.toLowerCase().includes(q) ||
        (f.external_id ?? "").toLowerCase().includes(q),
    );
  }, [followUps, search]);

  const groups = useMemo(() => {
    if (statusFilter !== "open") return [{ label: null, items: filtered }];
    const overdue: FollowUp[] = [];
    const soon: FollowUp[] = [];
    const later: FollowUp[] = [];
    for (const f of filtered) {
      const urgency = dueUrgency(f.due_earliest, asOf);
      if (urgency === "overdue") overdue.push(f);
      else if (urgency === "soon") soon.push(f);
      else later.push(f);
    }
    return [
      { label: "Overdue", items: overdue },
      { label: "Next 7 days", items: soon },
      { label: "Later", items: later },
    ].filter((g) => g.items.length > 0);
  }, [filtered, statusFilter, asOf]);

  async function markContacted(followUp: FollowUp) {
    setBusyId(followUp.follow_up_id);
    try {
      await updateFollowUp(followUp.follow_up_id, { mark_contacted: true, status: null });
      showToast(`Marked ${followUp.patient_name} as contacted.`);
      load();
    } catch (err) {
      showToast(
        err instanceof ApiError ? err.message : "Could not record the call.",
        { variant: "error" },
      );
    } finally {
      setBusyId(null);
    }
  }

  async function reopen(followUp: FollowUp) {
    setBusyId(followUp.follow_up_id);
    try {
      await updateFollowUp(followUp.follow_up_id, {
        status: "open",
        mark_contacted: false,
      });
      showToast(`Reopened ${followUp.patient_name}.`);
      load();
      reloadStats();
    } catch (err) {
      showToast(
        err instanceof ApiError ? err.message : "Could not reopen the follow-up.",
        { variant: "error" },
      );
    } finally {
      setBusyId(null);
    }
  }

  async function confirmDismiss(reason: string) {
    if (!dismissFor) return;
    setBusyId(dismissFor.follow_up_id);
    try {
      await updateFollowUp(dismissFor.follow_up_id, {
        status: "dismissed",
        dismissed_reason: reason,
        mark_contacted: false,
      });
      const name = dismissFor.patient_name;
      const id = dismissFor.follow_up_id;
      setDismissFor(null);
      showToast(`Dismissed ${name}.`, {
        action: {
          label: "Undo",
          onClick: async () => {
            try {
              await updateFollowUp(id, { status: "open", mark_contacted: false });
              showToast(`Reopened ${name}.`);
              load();
            } catch {
              showToast("Could not undo the dismissal.", { variant: "error" });
            }
          },
        },
      });
      load();
      reloadStats();
    } catch (err) {
      showToast(
        err instanceof ApiError ? err.message : "Could not dismiss the follow-up.",
        { variant: "error" },
      );
    } finally {
      setBusyId(null);
    }
  }

  const overdueCount = followUps.filter(
    (f) => dueUrgency(f.due_earliest, asOf) === "overdue",
  ).length;
  const needsConfirmationCount = followUps.filter((f) => f.needs_confirmation).length;

  return (
    <div className="page">
      <div className="page-head">
        <div>
          <h1 className="page-title">Today</h1>
          <p className="page-sub">{formatDate(stats?.as_of)}</p>
        </div>
        <button className="btn btn--primary" onClick={() => setShowNewPatient(true)}>
          <svg width="13" height="13" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.6" aria-hidden="true">
            <path d="M8 3v10M3 8h10" />
          </svg>
          New patient
        </button>
      </div>

      <div className="stat-row" aria-hidden="true">
        <div className="stat-chip">
          <div className="stat-num">{stats?.follow_ups.open ?? "—"}</div>
          <div className="stat-label">Open follow-ups</div>
        </div>
        <div className="stat-chip stat-chip--critical">
          <div className="stat-num">{stats?.follow_ups.overdue ?? "—"}</div>
          <div className="stat-label">Overdue</div>
        </div>
        <div className="stat-chip stat-chip--caution">
          <div className="stat-num">{stats?.follow_ups.needs_confirmation ?? "—"}</div>
          <div className="stat-label">Needs date confirmation</div>
        </div>
      </div>

      <QueueTabs
        active="followUps"
        followUpsCount={stats?.follow_ups.open}
        scheduleCount={
          (stats?.visits.scheduled ?? 0) +
          (stats?.visits.awaiting_transcript ?? 0) +
          (stats?.visits.awaiting_insights ?? 0)
        }
      />

      <div role="tabpanel">
        <div className="toolbar">
          <div className="field">
            <svg width="13" height="13" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.6" aria-hidden="true">
              <circle cx="7" cy="7" r="4.2" />
              <path d="M10.2 10.2 14 14" />
            </svg>
            <input
              type="search"
              placeholder="Search patient or ID"
              aria-label="Search follow-ups"
              value={search}
              onChange={(e) => setSearch(e.target.value)}
            />
          </div>
          <div className="field">
            <label htmlFor="f-status" className="visually-hidden">
              Status
            </label>
            <select
              id="f-status"
              value={statusFilter}
              onChange={(e) => setStatusFilter(e.target.value as StatusFilter)}
            >
              <option value="open">Open</option>
              <option value="resolved">Resolved</option>
              <option value="dismissed">Dismissed</option>
              <option value="all">All</option>
            </select>
          </div>
          <div className="field">
            <label htmlFor="f-sort" className="visually-hidden">
              Sort
            </label>
            <select
              id="f-sort"
              value={sort}
              onChange={(e) => setSort(e.target.value as "due_asc" | "due_desc")}
            >
              <option value="due_asc">Due date, earliest first</option>
              <option value="due_desc">Due date, latest first</option>
            </select>
          </div>
          <div className="spacer" />
          {statusFilter === "open" && (
            <span className="muted" style={{ fontSize: 12.5 }}>
              {followUps.length} open &middot; {overdueCount} overdue &middot;{" "}
              {needsConfirmationCount} needs confirmation
            </span>
          )}
        </div>

        {error && <div className="error-banner">{error}</div>}

        {loading ? (
          <div className="page-loading">
            <span className="spinner" aria-hidden="true" />
            Loading follow-ups&hellip;
          </div>
        ) : filtered.length === 0 ? (
          <div className="card">
            <div className="empty">
              <p className="empty-title">No follow-ups here</p>
              <p className="empty-text">
                {statusFilter === "open"
                  ? "Nothing is waiting on a call right now."
                  : `No ${statusFilter} follow-ups match your search.`}
              </p>
            </div>
          </div>
        ) : (
          <div className="card">
            <table className="table table--stack">
              <caption className="visually-hidden">
                {statusFilter === "open" ? "Open follow-ups, earliest due first" : "Follow-ups"}
              </caption>
              <thead>
                <tr>
                  <th scope="col">Patient</th>
                  <th scope="col">Phone</th>
                  <th scope="col">Follow-up due</th>
                  <th scope="col">Last seen</th>
                  <th scope="col">Where things stood</th>
                  <th scope="col">
                    <span className="visually-hidden">Actions</span>
                  </th>
                </tr>
              </thead>
              <tbody>
                {groups.map((group) => (
                  <FollowUpGroup
                    key={group.label ?? "all"}
                    label={group.label}
                    items={group.items}
                    asOf={asOf}
                    busyId={busyId}
                    onSchedule={setScheduleFor}
                    onMarkContacted={markContacted}
                    onDismiss={setDismissFor}
                    onReopen={reopen}
                  />
                ))}
              </tbody>
            </table>
          </div>
        )}

        <ToolNote />
      </div>

      {showNewPatient && (
        <NewPatientModal
          onClose={() => setShowNewPatient(false)}
          onCreated={() => {
            setShowNewPatient(false);
            showToast("Patient registered.");
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
            setScheduleFor(null);
            showToast(`Visit scheduled for ${scheduleFor.patient_name}.`);
            load();
            reloadStats();
          }}
        />
      )}

      {dismissFor && (
        <DismissDialog
          patientName={dismissFor.patient_name}
          submitting={busyId === dismissFor.follow_up_id}
          onCancel={() => setDismissFor(null)}
          onConfirm={confirmDismiss}
        />
      )}
    </div>
  );
}

function FollowUpGroup({
  label,
  items,
  asOf,
  busyId,
  onSchedule,
  onMarkContacted,
  onDismiss,
  onReopen,
}: {
  label: string | null;
  items: FollowUp[];
  asOf: Date;
  busyId: string | null;
  onSchedule: (f: FollowUp) => void;
  onMarkContacted: (f: FollowUp) => void;
  onDismiss: (f: FollowUp) => void;
  onReopen: (f: FollowUp) => void;
}) {
  return (
    <>
      {label && (
        <tr className="rowgroup">
          <th scope="colgroup" colSpan={6}>
            {label} <span className="rowgroup-count">&middot; {items.length}</span>
          </th>
        </tr>
      )}
      {items.map((f) => (
        <tr key={f.follow_up_id}>
          <td>
            <div className="who">
              <Link className="who-name" href={`/patients/${f.patient_id}`}>
                {f.patient_name}
              </Link>
              <span className="who-meta">
                {f.patient_age ?? "—"}
                {f.patient_gender ? f.patient_gender[0].toUpperCase() : ""} &middot;{" "}
                <span className="mrn">{f.external_id ?? "—"}</span>
              </span>
            </div>
          </td>
          <td>{f.phone ? f.phone : <span className="none">&mdash;</span>}</td>
          <td>
            <div className="due">
              <span className="due-date">{formatDate(f.due_earliest)}</span>
              <DueBadge dueEarliest={f.due_earliest} asOf={asOf} />
              {f.needs_confirmation && (
                <span className="badge badge--soon">Confirm date</span>
              )}
            </div>
          </td>
          <td>
            {formatDate(f.source_visit_date)}
            <div className="who-meta">Visit {f.source_visit_number}</div>
          </td>
          <td>
            <span className="clip clip--2">{f.chief_issue ?? "—"}</span>
          </td>
          <td className="actions">
            {f.status === "open" && (
              <>
                <button
                  className="btn btn--sm btn--primary"
                  disabled={busyId === f.follow_up_id}
                  onClick={() => onSchedule(f)}
                >
                  Schedule visit
                </button>
                <Menu
                  label={`More actions for ${f.patient_name}`}
                  items={[
                    {
                      label: "Mark contacted",
                      onClick: () => onMarkContacted(f),
                    },
                    {
                      label: "Dismiss",
                      danger: true,
                      onClick: () => onDismiss(f),
                    },
                    {
                      label: "Open record",
                      onClick: () => {
                        window.location.href = `/patients/${f.patient_id}`;
                      },
                    },
                  ]}
                />
              </>
            )}
            {f.status === "dismissed" && (
              <>
                <span className="badge" style={{ marginRight: 8 }}>
                  {f.dismissed_reason ?? "dismissed"}
                </span>
                <button
                  className="btn btn--sm btn--subtle"
                  disabled={busyId === f.follow_up_id}
                  onClick={() => onReopen(f)}
                >
                  Reopen
                </button>
              </>
            )}
            {(f.status === "resolved") && <span className="badge badge--ok">resolved</span>}
          </td>
        </tr>
      ))}
    </>
  );
}
