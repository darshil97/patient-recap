"use client";

import { useCallback, useEffect, useMemo, useState } from "react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { QueueTabs } from "../components/Tabs";
import { Menu } from "../components/Menu";
import { ToolNote } from "../components/ToolNote";
import { useStatsContext } from "../hooks/StatsContext";
import { useToast } from "../components/Toast";
import {
  listVisits,
  completeVisit,
  cancelVisit,
  startVisit,
  uploadTranscript,
  type Visit,
} from "@/lib/api/visits";
import {
  finishAudioUpload,
  getAudioUpload,
  getLatestAudioUpload,
  isAudioFile,
  isInterruptedLiveRecording,
  uploadRecording,
  type AudioUploadOut,
  type ChunkProgress,
} from "@/lib/api/audioUploads";
import { checkUploadFile } from "@/lib/api/uploadChecks";
import { useLiveSession } from "../features/recording/LiveSessionProvider";
import { generateInsights, getJob, getLatestJob, type JobOut } from "@/lib/api/insights";
import { useTrackedByVisit, type Tracked } from "../hooks/useTrackedByVisit";
import { formatDate } from "../lib/dates";
import { ApiError } from "@/lib/api/client";
import { TranscriptDropzone } from "../features/visits/TranscriptDropzone";
import { Modal, ModalBody } from "../components/Modal";

type NeedsAction = "needs_action" | "scheduled_only" | "all";

/** Matches the backend's default page size (app/services/pagination.py). */
const PAGE_SIZE = 100;

const ACTIVE_JOB_STATUSES = new Set(["queued", "running"]);
const isActiveJob = (job: JobOut) => ACTIVE_JOB_STATUSES.has(job.status);

export function SchedulePage() {
  const { stats, reload: reloadStats } = useStatsContext();
  const { showToast } = useToast();
  const router = useRouter();
  const recordingVisitId = useLiveSession().session?.visitId ?? null;

  const [visits, setVisits] = useState<Visit[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [search, setSearch] = useState("");
  const [filter, setFilter] = useState<NeedsAction>("needs_action");
  const [busyId, setBusyId] = useState<string | null>(null);
  const [total, setTotal] = useState(0);
  const [uploadFor, setUploadFor] = useState<Visit | null>(null);
  // Chunks of a recording on their way up from this tab, shown in the dialog.
  const [sending, setSending] = useState<ChunkProgress | null>(null);

  // Background work behind each row, so the Stage column tells the truth after
  // an upload — and after a reload — instead of offering buttons that would 409:
  // a recording being transcribed, then the brief compiling. Both start on the
  // server without the doctor, so they are looked up rather than remembered.
  const recordings = useTrackedByVisit<AudioUploadOut>({
    idOf: (upload) => upload.upload_id,
    isActive: (upload) => upload.status === "processing",
    fetchById: getAudioUpload,
    fetchLatest: getLatestAudioUpload,
    onSettled: handleRecordingSettled,
  });
  const jobs = useTrackedByVisit<JobOut>({
    idOf: (job) => job.job_id,
    isActive: isActiveJob,
    fetchById: getJob,
    fetchLatest: getLatestJob,
    onSettled: handleJobSettled,
  });
  const refreshRecordings = recordings.refresh;
  const refreshJobs = jobs.refresh;

  // "Needs action" is four states: scheduled but not held, in session, completed
  // without a transcript, and transcript present without a brief. The first two
  // are a status filter; the others are exactly what `pending_only` means
  // server-side. So the whole filter is expressible in query params.
  //
  // It used to fetch 200 rows and filter them in the browser, which quietly made
  // the schedule wrong above 200 visits: rows that needed action were dropped
  // before the filter ever saw them, and nothing said so.
  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const page = await listVisits({
        status:
          filter === "scheduled_only"
            ? ["scheduled", "in_progress"]
            : filter === "needs_action"
              ? ["scheduled", "in_progress", "completed"]
              : undefined,
        pendingOnly: filter === "needs_action" ? true : undefined,
        sort: "date_asc",
        limit: PAGE_SIZE,
      });
      setVisits(page.items);
      setTotal(page.total);
      const completed = page.items.filter((v) => v.visit_status === "completed");
      // A visit in session may hold an interrupted live recording.
      void refreshRecordings(
        page.items.filter(
          (v) =>
            v.visit_status === "in_progress" ||
            (v.visit_status === "completed" && !v.transcript_path),
        ),
      );
      void refreshJobs(completed.filter((v) => v.transcript_path && !v.has_insights));
    } catch {
      setError("Could not load the visit schedule.");
    } finally {
      setLoading(false);
    }
  }, [filter, refreshRecordings, refreshJobs]);

  useEffect(() => {
    load();
  }, [load]);

  // Function declarations, so the hooks above can be handed them before this
  // point; the hooks call them through a ref, after render.
  function openVisit(visitId: string, label: string) {
    return { label, onClick: () => router.push(`/visits/${visitId}`) };
  }

  /** A recording finished. On success the backend has already queued the
   * insight job, so this only reports it; the reload shows the row compiling. */
  function handleRecordingSettled(upload: AudioUploadOut, who: string) {
    if (upload.status === "failed") {
      showToast(
        `${who}'s recording could not be transcribed. ${upload.error_message ?? ""}`.trim(),
        { variant: "error", action: openVisit(upload.visit_id, "Open visit"), durationMs: 10_000 },
      );
    } else if (upload.status === "completed") {
      showToast(`${who}'s recording is transcribed. Compiling the brief…`);
    }
    load();
    reloadStats();
  }

  function handleJobSettled(job: JobOut, who: string) {
    if (job.status === "succeeded") {
      showToast(`Brief ready for ${who}.`, {
        action: openVisit(job.visit_id, "Open brief"),
        durationMs: 10_000,
      });
    } else if (job.status === "failed_quota") {
      showToast(
        `Could not compile ${who}'s brief: the daily limit has been reached. Try again later.`,
        { variant: "error", durationMs: 10_000 },
      );
    } else if (job.status === "failed") {
      showToast(
        `Could not compile ${who}'s brief. ${job.error_message ?? ""}`.trim(),
        { variant: "error", action: openVisit(job.visit_id, "Open visit"), durationMs: 10_000 },
      );
    }
    load();
    reloadStats();
  }

  // Closing the tab mid-send loses the chunks still to go, so ask first.
  useEffect(() => {
    if (!sending) return;
    const warn = (e: BeforeUnloadEvent) => e.preventDefault();
    window.addEventListener("beforeunload", warn);
    return () => window.removeEventListener("beforeunload", warn);
  }, [sending]);

  // Search stays client-side: /visits has no `q`, and adding one would need a
  // backend change this does not justify. It searches the loaded page only,
  // which the count below makes visible rather than silent.
  const filtered = useMemo(() => {
    const q = search.trim().toLowerCase();
    if (!q) return visits;
    return visits.filter(
      (v) =>
        v.patient_name.toLowerCase().includes(q) ||
        (v.external_id ?? "").toLowerCase().includes(q),
    );
  }, [visits, search]);

  const groups = useMemo(() => {
    // The server's date, not the browser's. toISOString() is UTC, so east of
    // Greenwich it reports yesterday for the first hours of every local day and
    // a visit awaiting action would not be grouped as overdue until mid-morning.
    const today = stats?.as_of ?? new Date().toISOString().slice(0, 10);
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
  }, [filtered, stats?.as_of]);

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

  /** Start the consultation and go to it, where the recorder is. */
  async function handleStartSession(visit: Visit) {
    setBusyId(visit.visit_id);
    try {
      await startVisit(visit.visit_id);
      reloadStats();
      router.push(`/visits/${visit.visit_id}`);
    } catch (err) {
      showToast(err instanceof ApiError ? err.message : "Could not start the session.", {
        variant: "error",
      });
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

  async function handleRecording(visit: Visit, file: File) {
    setBusyId(visit.visit_id);
    setSending({ sent: 0, total: 0 });
    try {
      const accepted = await uploadRecording(visit.visit_id, file, {
        // "Replace transcript" on a row that has one, but no insights yet.
        replace: Boolean(visit.transcript_path),
        onProgress: setSending,
      });
      recordings.track(accepted, visit.patient_name);
      setUploadFor(null);
      if (accepted.status === "processing") {
        showToast(
          `Recording uploaded. Transcribing ${visit.patient_name}'s consultation, then compiling the brief…`,
        );
      } else {
        // Finished before the response arrived (a lost-and-recovered Finish).
        handleRecordingSettled(accepted, visit.patient_name);
      }
    } catch (err) {
      showToast(
        err instanceof ApiError
          ? err.message
          : "Could not upload the recording. Check the connection and try again.",
        { variant: "error" },
      );
    } finally {
      setSending(null);
      setBusyId(null);
    }
  }

  async function handleRetryRecording(visit: Visit) {
    const entry = recordings.tracked[visit.visit_id];
    if (!entry) return;
    setBusyId(visit.visit_id);
    try {
      // The recording is still stored, so this transcribes it again without a
      // re-upload; the brief follows automatically as before.
      const accepted = await finishAudioUpload(entry.item.upload_id);
      recordings.track(accepted, visit.patient_name);
      showToast(`Transcribing ${visit.patient_name}'s recording again…`);
    } catch (err) {
      showToast(err instanceof ApiError ? err.message : "Could not retry the recording.", {
        variant: "error",
      });
    } finally {
      setBusyId(null);
    }
  }

  async function handleUpload(file: File) {
    if (!uploadFor) return;
    // Uploading to a visit not held yet completes it first: two explicit calls,
    // after checking the file so a wrong one does not complete the visit.
    if (uploadFor.visit_status !== "completed") {
      const problem = await checkUploadFile(file);
      if (problem) {
        showToast(problem, { variant: "error" });
        return;
      }
      setBusyId(uploadFor.visit_id);
      try {
        await completeVisit(uploadFor.visit_id);
      } catch (err) {
        showToast(err instanceof ApiError ? err.message : "Could not complete the visit.", {
          variant: "error",
        });
        setBusyId(null);
        return;
      }
      // Completed whatever happens to the upload next, so show it as such.
      load();
      reloadStats();
    }
    if (isAudioFile(file)) {
      await handleRecording(uploadFor, file);
      return;
    }
    setBusyId(uploadFor.visit_id);
    try {
      // replace follows whether a transcript exists, not has_insights: "Replace
      // transcript" is offered on rows with a transcript and no brief, where
      // has_insights is false and the upload would 409.
      const updated = await uploadTranscript(
        uploadFor.visit_id,
        file,
        Boolean(uploadFor.transcript_path),
      );
      showToast("Transcript saved. Starting insight generation…");
      setUploadFor(null);
      try {
        await generateInsights(updated.visit_id, {
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
  const scheduledCount = (stats?.visits.scheduled ?? 0) + (stats?.visits.in_progress ?? 0);

  return (
    <div className="page">
      <div className="page-head">
        <div>
          <h1 className="page-title">Visit schedule</h1>
          <p className="page-sub">{formatDate(stats?.as_of)}</p>
        </div>
      </div>

      {/* Not aria-hidden. These numbers are the reason the screen exists, and
          hiding them left a screen reader with no summary at all. Plain divs
          rather than a <dl>: the design puts the number above its label, and a
          <dl> would need the <dt> first to be valid, which inverts the reading
          order to fix markup nobody was reading. */}
      <div className="stat-row">
        <div className="stat-chip stat-chip--critical">
          <div className="stat-num">{scheduledCount}</div>
          <div className="stat-label">Not yet held, or in session</div>
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
              <option value="scheduled_only">Not yet held</option>
              <option value="all">All visits</option>
            </select>
          </div>
          <div className="spacer" />
          <span className="muted" style={{ fontSize: 12.5 }}>
            Showing {filtered.length} of {total}
            {visits.length < total && " — narrow the filter to see the rest"}
          </span>
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
                patient&rsquo;s page. It will appear here until the consultation is completed
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
                    recordings={recordings.tracked}
                    jobs={jobs.tracked}
                    recordingVisitId={recordingVisitId}
                    busyId={busyId}
                    onStartSession={handleStartSession}
                    onComplete={handleComplete}
                    onCancel={handleCancel}
                    onGenerate={handleGenerate}
                    onAddTranscript={setUploadFor}
                    onRetryRecording={handleRetryRecording}
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
          title={
            uploadFor.transcript_path
              ? "Replace transcript"
              : uploadFor.visit_status === "completed"
                ? "Add transcript"
                : "Upload transcript"
          }
          subtitle={`${uploadFor.patient_name} · visit ${uploadFor.visit_number}`}
          // Held open while a recording is going up: the progress lives here,
          // and closing would hide an upload that is still running.
          onClose={() => {
            if (!sending) setUploadFor(null);
          }}
        >
          <ModalBody>
            {sending ? (
              <UploadProgress sending={sending} />
            ) : (
              <TranscriptDropzone
                onFileSelected={handleUpload}
                busy={busyId === uploadFor.visit_id}
              />
            )}
            <p style={{ fontSize: 12.5, color: "var(--ink-2)", marginTop: 12 }}>
              {uploadFor.visit_status !== "completed" && "Uploading marks the visit complete. "}
              A recording is stored with the visit, transcribed, and each voice is labelled
              as doctor, patient or someone else present. Word documents, PDFs and JSON are
              not accepted &mdash; a mis-read file would produce a confident but wrong
              record.
            </p>
          </ModalBody>
        </Modal>
      )}
    </div>
  );
}

function UploadProgress({ sending }: { sending: ChunkProgress }) {
  const { sent, total } = sending;
  const pct = total > 0 ? Math.round((sent / total) * 100) : 0;
  return (
    <div className="dropzone" aria-busy="true">
      <p style={{ fontWeight: 600 }}>Uploading the recording&hellip;</p>
      <p role="status" aria-live="polite" style={{ color: "var(--ink-2)", fontSize: 13, marginTop: 4 }}>
        {total > 0 ? `Part ${Math.min(sent + 1, total)} of ${total}. ` : "Preparing. "}
        Keep this page open until it finishes; transcription then runs on its own.
      </p>
      <div
        className="bar"
        role="progressbar"
        aria-valuenow={sent}
        aria-valuemin={0}
        aria-valuemax={total}
        aria-label={`Upload progress: ${sent} of ${total} parts`}
        style={{ marginTop: 12 }}
      >
        <span style={{ width: `${Math.max(pct, 4)}%` }} />
      </div>
    </div>
  );
}

function VisitGroup({
  label,
  items,
  recordings,
  jobs,
  recordingVisitId,
  busyId,
  onStartSession,
  onComplete,
  onCancel,
  onGenerate,
  onAddTranscript,
  onRetryRecording,
}: {
  label: string;
  items: Visit[];
  recordings: Record<string, Tracked<AudioUploadOut>>;
  jobs: Record<string, Tracked<JobOut>>;
  /** The visit this tab is recording live, if any. */
  recordingVisitId: string | null;
  busyId: string | null;
  onStartSession: (v: Visit) => void;
  onComplete: (v: Visit) => void;
  onCancel: (v: Visit) => void;
  onGenerate: (v: Visit) => void;
  onAddTranscript: (v: Visit) => void;
  onRetryRecording: (v: Visit) => void;
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
              <Link className="who-name" href={`/patients/${v.patient_id}`}>
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
            <Stage
              visit={v}
              recording={recordings[v.visit_id]?.item}
              job={jobs[v.visit_id]?.item}
              recordingHere={recordingVisitId === v.visit_id}
            />
          </td>
          <td className="actions">
            <RowActions
              visit={v}
              recording={recordings[v.visit_id]?.item}
              job={jobs[v.visit_id]?.item}
              recordingHere={recordingVisitId === v.visit_id}
              busy={busyId === v.visit_id}
              onStartSession={onStartSession}
              onComplete={onComplete}
              onCancel={onCancel}
              onGenerate={onGenerate}
              onAddTranscript={onAddTranscript}
              onRetryRecording={onRetryRecording}
            />
          </td>
        </tr>
      ))}
    </>
  );
}

/** The row's job, when it is about the brief this row is waiting for. */
function pendingBriefJob(visit: Visit, job?: JobOut): JobOut | undefined {
  return visit.visit_status === "completed" && visit.transcript_path && !visit.has_insights
    ? job
    : undefined;
}

function Stage({
  visit,
  recording,
  job,
  recordingHere,
}: {
  visit: Visit;
  recording?: AudioUploadOut;
  job?: JobOut;
  recordingHere: boolean;
}) {
  const briefJob = pendingBriefJob(visit, job);
  if (recordingHere) {
    return (
      <>
        <span className="status">
          <span className="rec-dot rec-dot--live" aria-hidden="true" />
          Recording
        </span>
        <div className="who-meta">In this tab</div>
      </>
    );
  }
  if (isInterruptedLiveRecording(recording)) {
    return (
      <>
        <span className="status">
          <svg width="13" height="13" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.6" aria-hidden="true">
            <path d="M8 2.8 14.2 13H1.8L8 2.8Z" />
            <path d="M8 6.6v2.9M8 11.2h.01" />
          </svg>
          Recording interrupted
        </span>
        <div className="who-meta">Process or discard it</div>
      </>
    );
  }
  if (recording?.status === "processing") {
    return (
      <>
        <span className="status">
          <span className="spinner" aria-hidden="true" style={{ width: 12, height: 12 }} />
          Transcribing
        </span>
        <div className="who-meta">Recording uploaded</div>
      </>
    );
  }
  if (visit.visit_status === "completed" && !visit.transcript_path && recording?.status === "failed") {
    return (
      <>
        <span className="status">
          <svg width="13" height="13" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.6" aria-hidden="true">
            <path d="M8 2.8 14.2 13H1.8L8 2.8Z" />
            <path d="M8 6.6v2.9M8 11.2h.01" />
          </svg>
          Transcription failed
        </span>
        <div className="who-meta" title={recording.error_message ?? undefined}>
          {recording.retryable ? "Recording kept; try again" : "Upload it again"}
        </div>
      </>
    );
  }
  if (visit.visit_status === "scheduled") {
    return (
      <span className="status">
        <span className="badge badge--accent badge--dot">Scheduled</span>
      </span>
    );
  }
  if (visit.visit_status === "in_progress") {
    return (
      <span className="status">
        <span className="badge badge--soon badge--dot">In session</span>
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
  if (briefJob && isActiveJob(briefJob)) {
    return (
      <>
        <span className="status">
          <span className="spinner" aria-hidden="true" style={{ width: 12, height: 12 }} />
          Compiling brief
        </span>
        <div className="who-meta">Pass {Math.min(briefJob.layers_completed + 1, 3)} of 3</div>
      </>
    );
  }
  if (briefJob && (briefJob.status === "failed" || briefJob.status === "failed_quota")) {
    return (
      <>
        <span className="status">
          <svg width="13" height="13" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.6" aria-hidden="true">
            <path d="M8 2.8 14.2 13H1.8L8 2.8Z" />
            <path d="M8 6.6v2.9M8 11.2h.01" />
          </svg>
          Brief not compiled
        </span>
        <div className="who-meta" title={briefJob.error_message ?? undefined}>
          {briefJob.status === "failed_quota" ? "Daily limit reached" : "Generation failed"}
        </div>
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
  recording,
  job,
  recordingHere,
  busy,
  onStartSession,
  onComplete,
  onCancel,
  onGenerate,
  onAddTranscript,
  onRetryRecording,
}: {
  visit: Visit;
  recording?: AudioUploadOut;
  job?: JobOut;
  recordingHere: boolean;
  busy: boolean;
  onStartSession: (v: Visit) => void;
  onComplete: (v: Visit) => void;
  onCancel: (v: Visit) => void;
  onGenerate: (v: Visit) => void;
  onAddTranscript: (v: Visit) => void;
  onRetryRecording: (v: Visit) => void;
}) {
  const briefJob = pendingBriefJob(visit, job);
  if (recordingHere) {
    return (
      <Link className="btn btn--sm btn--primary" href={`/visits/${visit.visit_id}`}>
        Return to session
      </Link>
    );
  }
  // An interrupted live recording is resolved on the visit page, where what was
  // saved is shown before choosing to process or discard it.
  if (isInterruptedLiveRecording(recording)) {
    return (
      <Link className="btn btn--sm btn--primary" href={`/visits/${visit.visit_id}`}>
        Open visit
      </Link>
    );
  }
  if (recording?.status === "processing" || (briefJob && isActiveJob(briefJob))) {
    return (
      <Link className="btn btn--sm" href={`/visits/${visit.visit_id}`}>
        Open visit
      </Link>
    );
  }
  if (
    visit.visit_status === "completed" &&
    !visit.transcript_path &&
    recording?.status === "failed" &&
    recording.retryable
  ) {
    return (
      <>
        <button className="btn btn--sm btn--primary" disabled={busy} onClick={() => onRetryRecording(visit)}>
          {busy ? "Retrying…" : "Try again"}
        </button>
        <Menu
          label="More actions"
          items={[
            { label: "Add transcript", onClick: () => onAddTranscript(visit) },
            { label: "Cancel visit", danger: true, onClick: () => onCancel(visit) },
          ]}
        />
      </>
    );
  }
  if (visit.visit_status === "scheduled" || visit.visit_status === "in_progress") {
    const inSession = visit.visit_status === "in_progress";
    return (
      <>
        {inSession ? (
          <Link className="btn btn--sm btn--primary" href={`/visits/${visit.visit_id}`}>
            Open session
          </Link>
        ) : (
          <button
            className="btn btn--sm btn--primary"
            disabled={busy}
            onClick={() => onStartSession(visit)}
          >
            {busy ? "Starting…" : "Start session"}
          </button>
        )}
        <button className="btn btn--sm" disabled={busy} onClick={() => onAddTranscript(visit)}>
          Upload transcript
        </button>
        <Menu
          label="More actions"
          items={[
            { label: "Mark complete without a transcript", onClick: () => onComplete(visit) },
            { label: "Cancel visit", danger: true, onClick: () => onCancel(visit) },
          ]}
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
          {busy ? "Starting…" : briefJob ? "Try again" : "Generate insights"}
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
    <Link className="btn btn--sm" href={`/visits/${visit.visit_id}`}>
      Open brief
    </Link>
  );
}
