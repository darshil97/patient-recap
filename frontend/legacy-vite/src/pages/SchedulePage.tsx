import { useEffect, useMemo, useState } from "react";
import { Link } from "react-router-dom";
import { QueueTabs } from "../components/Tabs";
import { Menu } from "../components/Menu";
import { ToolNote } from "../components/ToolNote";
import { useStatsContext } from "../hooks/StatsContext";
import { useToast } from "../components/Toast";
import { listVisits, completeVisit, cancelVisit, uploadTranscript, type Visit } from "../api/visits";
import { generateInsights } from "../api/insights";
import { formatDate } from "../lib/dates";
import { ApiError } from "../api/client";
import { TranscriptDropzone } from "../features/visits/TranscriptDropzone";
import { Modal, ModalBody } from "../components/Modal";

type NeedsAction = "needs_action" | "scheduled_only" | "all";

export function SchedulePage() {
  const { stats, reload: reloadStats } = useStatsContext();
  const { showToast } = useToast();

  const [visits, setVisits] = useState<Visit[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [search, setSearch] = useState("");
  const [filter, setFilter] = useState<NeedsAction>("needs_action");
  const [busyId, setBusyId] = useState<string | null>(null);
  const [uploadFor, setUploadFor] = useState<Visit | null>(null);

  async function load() {
    setLoading(true);
    setError(null);
    try {
      const { items } = await listVisits({
        status: filter === "scheduled_only" ? ["scheduled"] : undefined,
        sort: "date_asc",
        limit: 200,
      });
      setVisits(items);
    } catch {
      setError("Could not load the visit schedule.");
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    load();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [filter]);

  const filtered = useMemo(() => {
    let base = visits;
    if (filter === "needs_action") {
      base = base.filter(
        (v) =>
          v.visit_status === "scheduled" ||
          (v.visit_status === "completed" && !v.transcript_path) ||
          (v.visit_status === "completed" && v.transcript_path && !v.has_insights),
      );
    }
    const q = search.trim().toLowerCase();
    if (!q) return base;
    return base.filter(
      (v) =>
        v.patient_name.toLowerCase().includes(q) ||
        (v.external_id ?? "").toLowerCase().includes(q),
    );
  }, [visits, search, filter]);

  const groups = useMemo(() => {
    const today = new Date().toISOString().slice(0, 10);
    const overdue: Visit[] = [];
    const byDate = new Map<string, Visit[]>();
    for (const v of filtered) {
      const isOverdueAction =
        v.visit_status === "completed" && (!v.transcript_path || !v.has_insights) && v.visit_date < today;
      if (isOverdueAction) {
        overdue.push(v);
        continue;
      }
      const list = byDate.get(v.visit_date) ?? [];
      list.push(v);
      byDate.set(v.visit_date, list);
    }
    const dateGroups = Array.from(byDate.entries())
      .sort(([a], [b]) => a.localeCompare(b))
      .map(([date, items]) => ({ label: formatDate(date), items }));
    const result = [];
    if (overdue.length) result.push({ label: "Overdue action", items: overdue });
    result.push(...dateGroups);
    return result;
  }, [filtered]);

  async function handleComplete(visit: Visit) {
    setBusyId(visit.visit_id);
    try {
      await completeVisit(visit.visit_id);
      showToast(`${visit.patient_name}'s visit marked complete.`);
      load();
      reloadStats();
    } catch (err) {
      showToast(err instanceof ApiError ? err.message : "Could not complete the visit.", {
        variant: "error",
      });
    } finally {
      setBusyId(null);
    }
  }

  async function handleCancel(visit: Visit) {
    setBusyId(visit.visit_id);
    try {
      await cancelVisit(visit.visit_id);
      showToast(`${visit.patient_name}'s visit cancelled.`);
      load();
      reloadStats();
    } catch (err) {
      showToast(err instanceof ApiError ? err.message : "Could not cancel the visit.", {
        variant: "error",
      });
    } finally {
      setBusyId(null);
    }
  }

  async function handleGenerate(visit: Visit) {
    setBusyId(visit.visit_id);
    try {
      await generateInsights(visit.visit_id, {
        stop_after: 3,
        regenerate: visit.has_insights,
      });
      showToast(`Compiling the brief for ${visit.patient_name}…`);
      load();
      reloadStats();
    } catch (err) {
      showToast(err instanceof ApiError ? err.message : "Could not start generation.", {
        variant: "error",
      });
    } finally {
      setBusyId(null);
    }
  }

  async function handleUpload(file: File) {
    if (!uploadFor) return;
    setBusyId(uploadFor.visit_id);
    try {
      const updated = await uploadTranscript(uploadFor.visit_id, file, uploadFor.has_insights);
      showToast("Transcript saved. Starting insight generation…");
      setUploadFor(null);
      try {
        await generateInsights(updated.visit_id, {
          stop_after: 3,
          regenerate: updated.has_insights,
        });
      } catch (genErr) {
        // Per README: generation failing after a successful upload is
        // self-healing — the visit sits with a transcript and a visible
        // "Generate insights" action, nothing is lost.
        showToast(
          genErr instanceof ApiError
            ? genErr.message
            : "Transcript saved, but generation could not start. Use Generate insights below.",
          { variant: "error" },
        );
      }
      load();
      reloadStats();
    } catch (err) {
      showToast(err instanceof ApiError ? err.message : "Could not upload the transcript.", {
        variant: "error",
      });
    } finally {
      setBusyId(null);
    }
  }

  const awaitingTranscript = stats?.visits.awaiting_transcript ?? 0;
  const awaitingInsights = stats?.visits.awaiting_insights ?? 0;
  const scheduledCount = stats?.visits.scheduled ?? 0;

  return (
    <div className="page">
      <div className="page-head">
        <div>
          <h1 className="page-title">Today</h1>
          <p className="page-sub">{formatDate(stats?.as_of)}</p>
        </div>
      </div>

      <div className="stat-row" aria-hidden="true">
        <div className="stat-chip stat-chip--critical">
          <div className="stat-num">{scheduledCount}</div>
          <div className="stat-label">Scheduled, not yet held</div>
        </div>
        <div className="stat-chip stat-chip--caution">
          <div className="stat-num">{awaitingTranscript}</div>
          <div className="stat-label">Awaiting transcript</div>
        </div>
        <div className="stat-chip stat-chip--accent">
          <div className="stat-num">{awaitingInsights}</div>
          <div className="stat-label">Awaiting insights</div>
        </div>
      </div>

      <QueueTabs
        active="schedule"
        followUpsCount={stats?.follow_ups.open}
        scheduleCount={scheduledCount + awaitingTranscript + awaitingInsights}
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
              aria-label="Search visits"
              value={search}
              onChange={(e) => setSearch(e.target.value)}
            />
          </div>
          <div className="field">
            <label htmlFor="f-need" className="visually-hidden">
              Filter
            </label>
            <select
              id="f-need"
              value={filter}
              onChange={(e) => setFilter(e.target.value as NeedsAction)}
            >
              <option value="needs_action">Needs action</option>
              <option value="scheduled_only">Scheduled only</option>
              <option value="all">All visits</option>
            </select>
          </div>
          <div className="spacer" />
        </div>

        {error && <div className="error-banner">{error}</div>}

        {loading ? (
          <div className="page-loading">
            <span className="spinner" aria-hidden="true" />
            Loading visits&hellip;
          </div>
        ) : filtered.length === 0 ? (
          <div className="card">
            <div className="empty">
              <svg
                className="empty-icon"
                width="30"
                height="30"
                viewBox="0 0 24 24"
                fill="none"
                stroke="currentColor"
                strokeWidth="1.4"
                aria-hidden="true"
              >
                <rect x="3.5" y="5" width="17" height="15" rx="1.5" />
                <path d="M3.5 9.5h17M8 3.5v3M16 3.5v3" />
              </svg>
              <p className="empty-title">No visits scheduled</p>
              <p className="empty-text">
                When a patient confirms they are coming in, schedule the visit from a
                patient's page. It will appear here until the consultation is completed
                and its transcript is in.
              </p>
            </div>
          </div>
        ) : (
          <div className="card">
            <table className="table table--stack">
              <caption className="visually-hidden">Visits needing action, earliest first</caption>
              <thead>
                <tr>
                  <th scope="col">Patient</th>
                  <th scope="col">Time</th>
                  <th scope="col">Visit</th>
                  <th scope="col">Stage</th>
                  <th scope="col">
                    <span className="visually-hidden">Actions</span>
                  </th>
                </tr>
              </thead>
              <tbody>
                {groups.map((group) => (
                  <VisitGroup
                    key={group.label}
                    label={group.label}
                    items={group.items}
                    busyId={busyId}
                    onComplete={handleComplete}
                    onCancel={handleCancel}
                    onGenerate={handleGenerate}
                    onAddTranscript={setUploadFor}
                  />
                ))}
              </tbody>
            </table>
          </div>
        )}

        <ToolNote />
      </div>

      {uploadFor && (
        <Modal
          title="Add transcript"
          subtitle={`${uploadFor.patient_name} · visit ${uploadFor.visit_number}`}
          onClose={() => setUploadFor(null)}
        >
          <ModalBody>
            <TranscriptDropzone onFileSelected={handleUpload} busy={busyId === uploadFor.visit_id} />
            <p style={{ fontSize: 12.5, color: "var(--ink-2)", marginTop: 12 }}>
              Save the recording as plain text before uploading. Word documents, PDFs and
              JSON are not accepted &mdash; a mis-read file would produce a confident but
              wrong record.
            </p>
          </ModalBody>
        </Modal>
      )}
    </div>
  );
}

function VisitGroup({
  label,
  items,
  busyId,
  onComplete,
  onCancel,
  onGenerate,
  onAddTranscript,
}: {
  label: string;
  items: Visit[];
  busyId: string | null;
  onComplete: (v: Visit) => void;
  onCancel: (v: Visit) => void;
  onGenerate: (v: Visit) => void;
  onAddTranscript: (v: Visit) => void;
}) {
  return (
    <>
      <tr className="rowgroup">
        <th scope="colgroup" colSpan={5}>
          {label} <span className="rowgroup-count">&middot; {items.length}</span>
        </th>
      </tr>
      {items.map((v) => (
        <tr key={v.visit_id}>
          <td>
            <div className="who">
              <Link className="who-name" to={`/patients/${v.patient_id}`}>
                {v.patient_name}
              </Link>
              <span className="who-meta">
                {v.age_at_visit ?? "—"}
                {v.gender ? v.gender[0].toUpperCase() : ""} &middot;{" "}
                <span className="mrn">{v.external_id ?? "—"}</span>
              </span>
            </div>
          </td>
          <td>{v.visit_time ? v.visit_time.slice(0, 5) : <span className="none">&mdash;</span>}</td>
          <td>
            Visit {v.visit_number}
            <div className="who-meta">{v.visit_type === "initial" ? "Initial" : "Follow-up"}</div>
          </td>
          <td>
            <Stage visit={v} />
          </td>
          <td className="actions">
            <RowActions
              visit={v}
              busy={busyId === v.visit_id}
              onComplete={onComplete}
              onCancel={onCancel}
              onGenerate={onGenerate}
              onAddTranscript={onAddTranscript}
            />
          </td>
        </tr>
      ))}
    </>
  );
}

function Stage({ visit }: { visit: Visit }) {
  if (visit.visit_status === "scheduled") {
    return (
      <span className="status">
        <span className="badge badge--accent badge--dot">Scheduled</span>
      </span>
    );
  }
  if (visit.visit_status === "cancelled") {
    return <span className="badge">Cancelled</span>;
  }
  if (visit.visit_status === "completed" && !visit.transcript_path) {
    return (
      <>
        <span className="status">
          <svg width="13" height="13" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.6" aria-hidden="true">
            <path d="M3.5 8.5l3 3 6-7" />
          </svg>
          Completed
        </span>
        <div className="who-meta">No transcript yet</div>
      </>
    );
  }
  if (visit.visit_status === "completed" && visit.transcript_path && !visit.has_insights) {
    return (
      <>
        <span className="status">
          <svg width="13" height="13" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.5" aria-hidden="true">
            <circle cx="8" cy="8" r="6" />
            <path d="M8 5v3.4l2.2 1.6" />
          </svg>
          Transcript added
        </span>
        <div className="who-meta">Insights not generated</div>
      </>
    );
  }
  return (
    <span className="status">
      <svg width="13" height="13" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.6" aria-hidden="true">
        <path d="M3.5 8.5l3 3 6-7" />
      </svg>
      Brief ready
    </span>
  );
}

function RowActions({
  visit,
  busy,
  onComplete,
  onCancel,
  onGenerate,
  onAddTranscript,
}: {
  visit: Visit;
  busy: boolean;
  onComplete: (v: Visit) => void;
  onCancel: (v: Visit) => void;
  onGenerate: (v: Visit) => void;
  onAddTranscript: (v: Visit) => void;
}) {
  if (visit.visit_status === "scheduled") {
    return (
      <>
        <button className="btn btn--sm btn--primary" disabled={busy} onClick={() => onComplete(visit)}>
          Complete visit
        </button>
        <Menu
          label="More actions"
          items={[{ label: "Cancel visit", danger: true, onClick: () => onCancel(visit) }]}
        />
      </>
    );
  }
  if (visit.visit_status === "completed" && !visit.transcript_path) {
    return (
      <>
        <button className="btn btn--sm btn--primary" disabled={busy} onClick={() => onAddTranscript(visit)}>
          Add transcript
        </button>
        <Menu
          label="More actions"
          items={[{ label: "Cancel visit", danger: true, onClick: () => onCancel(visit) }]}
        />
      </>
    );
  }
  if (visit.visit_status === "completed" && visit.transcript_path && !visit.has_insights) {
    return (
      <>
        <button className="btn btn--sm btn--primary" disabled={busy} onClick={() => onGenerate(visit)}>
          {busy ? "Starting…" : "Generate insights"}
        </button>
        <Menu
          label="More actions"
          items={[
            { label: "Replace transcript", onClick: () => onAddTranscript(visit) },
          ]}
        />
      </>
    );
  }
  return (
    <Link className="btn btn--sm" to={`/visits/${visit.visit_id}`}>
      Open brief
    </Link>
  );
}
