"use client";

import { useEffect, useState } from "react";
import type { Visit } from "@/lib/api/visits";
import { completeVisit, cancelVisit, startVisit, uploadTranscript } from "@/lib/api/visits";
import {
  discardAudioUpload,
  finishAudioUpload,
  finishWithRecovery,
  isAudioFile,
  uploadRecording,
  type AudioUploadOut,
  type ChunkProgress,
} from "@/lib/api/audioUploads";
import { checkUploadFile } from "@/lib/api/uploadChecks";
import { generateInsights } from "@/lib/api/insights";
import type { JobOut } from "@/lib/api/insights";
import { TranscriptDropzone } from "./TranscriptDropzone";
import { TranscriptSection } from "./TranscriptSection";
import { lifecycleState } from "./lifecycleState";
import { useToast } from "../../components/Toast";
import { ApiError } from "@/lib/api/client";
import { useLiveSession } from "../recording/LiveSessionProvider";
import { LiveRecorderPanel } from "../recording/LiveRecorderPanel";

/**
 * The pre-brief lifecycle states from visit-pending.html: not yet held, in
 * session (recording live), awaiting transcript, transcribing a recording,
 * an interrupted live recording, compiling, and the daily-quota-reached state.
 * Which one applies is decided by lifecycleState(); this renders it.
 *
 * The recording upload is tracked by VisitPage, not here, because what happens
 * when it completes — following the insight job the backend starts — belongs to
 * the page that owns the job.
 */
export function VisitLifecycle({
  visit,
  job,
  recording,
  recordingError,
  onVisitChanged,
  onRecordingChanged,
  onGenerationStarted,
}: {
  visit: Visit;
  job: JobOut | null;
  recording: AudioUploadOut | null;
  recordingError: string | null;
  onVisitChanged: () => void;
  /** An upload changed here: handed over (to be polled until it settles), or
   *  discarded. */
  onRecordingChanged: (upload: AudioUploadOut) => void;
  onGenerationStarted: (jobId: string) => void;
}) {
  const { showToast } = useToast();
  const live = useLiveSession();
  const [busy, setBusy] = useState(false);
  // Chunks of a recording on their way up. Only this tab can send them, so this
  // is local state; once handed over, the backend's upload row takes over.
  const [sending, setSending] = useState<ChunkProgress | null>(null);

  async function startGeneration(regenerate: boolean) {
    try {
      const accepted = await generateInsights(visit.visit_id, { regenerate });
      onGenerationStarted(accepted.job_id);
    } catch (genErr) {
      showToast(
        genErr instanceof ApiError
          ? genErr.message
          : "Transcript saved, but generation could not start yet. Use Generate insights below.",
        { variant: "error" },
      );
    }
  }

  // Closing the tab mid-send loses the chunks still to go, so ask first. Once
  // the backend has the whole file, leaving is safe and this is removed.
  useEffect(() => {
    if (!sending) return;
    const warn = (e: BeforeUnloadEvent) => {
      e.preventDefault();
    };
    window.addEventListener("beforeunload", warn);
    return () => window.removeEventListener("beforeunload", warn);
  }, [sending]);

  async function handleComplete() {
    setBusy(true);
    try {
      await completeVisit(visit.visit_id);
      showToast("Visit marked complete.");
      onVisitChanged();
    } catch (err) {
      showToast(err instanceof ApiError ? err.message : "Could not complete the visit.", {
        variant: "error",
      });
    } finally {
      setBusy(false);
    }
  }

  async function handleCancel() {
    setBusy(true);
    try {
      await cancelVisit(visit.visit_id);
      showToast("Visit cancelled.");
      onVisitChanged();
    } catch (err) {
      showToast(err instanceof ApiError ? err.message : "Could not cancel the visit.", {
        variant: "error",
      });
    } finally {
      setBusy(false);
    }
  }

  async function handleUpload(file: File) {
    if (isAudioFile(file)) {
      await handleRecording(file);
      return;
    }
    setBusy(true);
    try {
      const updated = await uploadTranscript(visit.visit_id, file, visit.has_insights);
      showToast("Transcript saved. Starting insight generation…");
      onVisitChanged();
      await startGeneration(updated.has_insights);
    } catch (err) {
      showToast(err instanceof ApiError ? err.message : "Could not upload the transcript.", {
        variant: "error",
      });
    } finally {
      setBusy(false);
    }
  }

  async function handleRecording(file: File) {
    setBusy(true);
    setSending({ sent: 0, total: 0 });
    try {
      const accepted = await uploadRecording(visit.visit_id, file, {
        replace: Boolean(visit.transcript_path),
        onProgress: setSending,
      });
      onRecordingChanged(accepted);
      showToast("Recording uploaded. Transcribing, then compiling the brief…");
    } catch (err) {
      showToast(
        err instanceof ApiError
          ? err.message
          : "Could not upload the recording. Check the connection and try again.",
        { variant: "error" },
      );
    } finally {
      setBusy(false);
      setSending(null);
    }
  }

  async function handleRetryRecording(uploadId: string) {
    setBusy(true);
    try {
      const accepted = await finishAudioUpload(uploadId);
      onRecordingChanged(accepted);
    } catch (err) {
      showToast(err instanceof ApiError ? err.message : "Could not retry the recording.", {
        variant: "error",
      });
    } finally {
      setBusy(false);
    }
  }

  async function handleGenerate() {
    setBusy(true);
    try {
      const accepted = await generateInsights(visit.visit_id, {
        regenerate: visit.has_insights,
      });
      onGenerationStarted(accepted.job_id);
    } catch (err) {
      showToast(err instanceof ApiError ? err.message : "Could not start generation.", {
        variant: "error",
      });
    } finally {
      setBusy(false);
    }
  }

  /**
   * Upload to a visit not completed yet. The file is checked here first, so an
   * obviously wrong one is refused before the visit is marked complete; then two
   * explicit calls, /complete and the upload (the server rule is unchanged).
   */
  async function handleUploadAndComplete(file: File) {
    const problem = await checkUploadFile(file);
    if (problem) {
      showToast(problem, { variant: "error" });
      return;
    }
    setBusy(true);
    try {
      await completeVisit(visit.visit_id);
    } catch (err) {
      showToast(err instanceof ApiError ? err.message : "Could not complete the visit.", {
        variant: "error",
      });
      setBusy(false);
      return;
    }
    setBusy(false);
    onVisitChanged();
    await handleUpload(file);
  }

  async function handleStartSession() {
    setBusy(true);
    try {
      await startVisit(visit.visit_id);
      showToast("Session started.");
      onVisitChanged();
    } catch (err) {
      showToast(err instanceof ApiError ? err.message : "Could not start the session.", {
        variant: "error",
      });
    } finally {
      setBusy(false);
    }
  }

  /** A live session ended here: follow it into transcribing, like an upload. */
  function handleHandedOver(accepted: AudioUploadOut) {
    onRecordingChanged(accepted);
    onVisitChanged();
  }

  /** An interrupted live recording: complete the visit if needed, then finish
   *  with no chunk count, so whatever arrived is the recording. */
  async function handleProcessInterrupted(upload: AudioUploadOut) {
    setBusy(true);
    try {
      if (visit.visit_status !== "completed") await completeVisit(visit.visit_id);
      const accepted = await finishWithRecovery(upload.upload_id);
      onRecordingChanged(accepted);
      onVisitChanged();
      showToast("Transcribing what was recorded, then compiling the brief…");
    } catch (err) {
      showToast(err instanceof ApiError ? err.message : "Could not process the recording.", {
        variant: "error",
      });
    } finally {
      setBusy(false);
    }
  }

  async function handleDiscardInterrupted(upload: AudioUploadOut) {
    if (!confirm("Discard this recording? It is deleted and cannot be recovered.")) return;
    setBusy(true);
    try {
      onRecordingChanged(await discardAudioUpload(upload.upload_id));
      showToast("Recording discarded.");
    } catch (err) {
      showToast(err instanceof ApiError ? err.message : "Could not discard the recording.", {
        variant: "error",
      });
    } finally {
      setBusy(false);
    }
  }

  const state = lifecycleState({
    visit,
    job,
    recording,
    sending: sending !== null,
    recordingHere: live.session?.visitId === visit.visit_id,
  });

  if (state === "compiling" && job) return <CompilingState job={job} />;
  // The last job failed because the model provider's daily limit was reached.
  if (state === "quota") return <QuotaState onRetry={handleGenerate} busy={busy} />;
  if (state === "job_failed" && job) {
    return <FailedState job={job} onRetry={handleGenerate} busy={busy} />;
  }

  if (state === "recording_live" || state === "in_session") {
    return (
      <InSessionState
        visit={visit}
        busy={busy}
        recordingHere={state === "recording_live"}
        onHandedOver={handleHandedOver}
        onComplete={handleComplete}
        onCancel={handleCancel}
        onUpload={handleUploadAndComplete}
      />
    );
  }

  if (state === "transcribing") {
    return <RecordingProgressState sending={sending} recording={recording} />;
  }

  if (state === "interrupted" && recording) {
    return (
      <InterruptedState
        visit={visit}
        recording={recording}
        busy={busy}
        onProcess={() => handleProcessInterrupted(recording)}
        onDiscard={() => handleDiscardInterrupted(recording)}
      />
    );
  }

  if (state === "scheduled") {
    return (
      <ScheduledState
        visit={visit}
        busy={busy}
        onStartSession={handleStartSession}
        onComplete={handleComplete}
        onCancel={handleCancel}
        onUpload={handleUploadAndComplete}
      />
    );
  }

  if (state === "cancelled") {
    return (
      <div className="card">
        <div className="card-body">
          <p style={{ fontWeight: 600 }}>This visit was cancelled.</p>
          {visit.cancelled_at && (
            <p className="muted" style={{ fontSize: 12.5, marginTop: 4 }}>
              Cancelled {visit.cancelled_at}
            </p>
          )}
        </div>
      </div>
    );
  }

  // State: completed, no transcript yet.
  if (state === "add_transcript") {
    return (
      <div className="card">
        <div className="card-head">
          <h2 className="card-title">Add transcript</h2>
          <span className="status">
            <svg width="13" height="13" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.7" aria-hidden="true">
              <path d="M3.5 8.5l3 3 6-7" />
            </svg>
            Completed
          </span>
        </div>
        <div className="card-body">
          {recordingError && <div className="error-banner" style={{ marginBottom: 12 }}>{recordingError}</div>}
          {recording?.status === "failed" && (
            <div className="notice notice--critical" style={{ marginBottom: 14 }}>
              <svg width="14" height="14" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.6" aria-hidden="true">
                <path d="M8 2.8 14.2 13H1.8L8 2.8Z" />
                <path d="M8 6.6v2.9M8 11.2h.01" />
              </svg>
              <span>
                <span className="notice-title">
                  {recording.mode === "live"
                    ? "The live recording"
                    : (recording.filename ?? "The recording")}{" "}
                  could not be transcribed.
                </span>{" "}
                {recording.error_message ?? "Something went wrong while processing it."}
                {recording.retryable && (
                  <>
                    {" "}It is still stored, so trying again does not re-upload it.
                    <span style={{ display: "block", marginTop: 8 }}>
                      <button
                        className="btn btn--primary"
                        disabled={busy}
                        onClick={() => handleRetryRecording(recording.upload_id)}
                      >
                        {busy ? "Retrying…" : "Try again"}
                      </button>
                    </span>
                  </>
                )}
              </span>
            </div>
          )}
          {recording?.status === "receiving" && recording.mode === "file" && (
            <div className="notice" style={{ marginBottom: 14 }}>
              <svg width="14" height="14" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.6" aria-hidden="true">
                <circle cx="8" cy="8" r="6" />
                <path d="M8 5v3.5M8 11h.01" />
              </svg>
              <span>
                <span className="notice-title">An earlier upload did not finish.</span>{" "}
                {recording.filename ?? "The recording"} stopped at{" "}
                {recording.chunks_received} of {recording.chunk_count ?? "?"} parts. Choose
                the file again to start over.
              </span>
            </div>
          )}
          <TranscriptDropzone onFileSelected={handleUpload} busy={busy} />
          <p style={{ fontSize: 12.5, color: "var(--ink-2)", marginTop: 12 }}>
            A recording is stored with the visit, transcribed, and each voice is labelled
            as doctor, patient or someone else present. Word documents, PDFs and JSON are
            not accepted &mdash; a mis-read file would produce a confident but wrong
            record.
          </p>
        </div>
      </div>
    );
  }

  // State: transcript attached, insights never generated (and no job history).
  if (state === "generate") {
    return (
      <div className="card">
        <div className="card-body">
          <div style={{ display: "flex", gap: 16, alignItems: "flex-start", justifyContent: "space-between", flexWrap: "wrap" }}>
            <div>
              <p style={{ fontWeight: 600 }}>Transcript added</p>
              <p style={{ color: "var(--ink-2)", fontSize: 13, marginTop: 4, maxWidth: "60ch" }}>
                {visit.transcript_filename} is on file. Compile the visit brief when
                you&rsquo;re ready.
              </p>
            </div>
            <button className="btn btn--primary" disabled={busy} onClick={handleGenerate}>
              {busy ? "Starting…" : "Generate insights"}
            </button>
          </div>
          {/* Usually only seen briefly, or when generation could not be
              started: either way the transcript is worth having to hand. */}
          <div style={{ marginTop: 14 }}>
            <TranscriptSection visitId={visit.visit_id} filename={visit.transcript_filename} />
          </div>
        </div>
      </div>
    );
  }

  return null;
}

/** Upload progress, then the server-side transcription, for a recording. */
function RecordingProgressState({
  sending,
  recording,
}: {
  sending: ChunkProgress | null;
  recording: AudioUploadOut | null;
}) {
  const uploading = sending !== null;
  const total = sending?.total ?? 0;
  const sent = sending?.sent ?? 0;
  const pct = uploading ? (total > 0 ? Math.round((sent / total) * 100) : 0) : 100;
  const steps = [
    {
      name: "Upload",
      detail: uploading
        ? total > 0
          ? `Part ${Math.min(sent + 1, total)} of ${total}`
          : "Preparing"
        : "Stored with the visit",
      state: uploading ? "active" : "done",
    },
    {
      name: "Transcribe",
      detail: uploading
        ? "Speech to text, then who is speaking"
        : "Speech to text, then working out who is speaking",
      state: uploading ? "todo" : "active",
    },
    {
      name: "Brief",
      detail: "Compiled automatically once transcribed",
      state: "todo",
    },
  ];
  return (
    <div className="card">
      <div className="card-head">
        <h2 className="card-title">{uploading ? "Uploading recording" : "Transcribing"}</h2>
      </div>
      <div className="card-body">
        <p role="status" aria-live="polite" style={{ color: "var(--ink-2)", fontSize: 13, maxWidth: "64ch" }}>
          {uploading
            ? "Keep this page open until the upload finishes."
            : `${recording?.filename ?? "The recording"} is uploaded. Transcribing a long consultation can take a few minutes. You can leave this page — it will keep going.`}
        </p>
        <ul className="steps" style={{ marginTop: 12 }}>
          {steps.map((step) => (
            <li key={step.name} className={`step--${step.state}`}>
              <span className="step-mark" aria-hidden="true">
                {step.state === "done" && (
                  <svg width="10" height="10" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="2.4" aria-hidden="true">
                    <path d="M3.5 8.5l3 3 6-7" />
                  </svg>
                )}
              </span>
              <div>
                <div className="step-name">{step.name}</div>
                <div className="step-detail">{step.state === "active" && !uploading ? "running" : step.detail}</div>
              </div>
            </li>
          ))}
        </ul>
        {uploading && (
          <div
            className="bar"
            role="progressbar"
            aria-valuenow={sent}
            aria-valuemin={0}
            aria-valuemax={total}
            aria-label={`Upload progress: ${sent} of ${total} parts`}
          >
            <span style={{ width: `${Math.max(pct, 4)}%` }} />
          </div>
        )}
      </div>
    </div>
  );
}

/** Upload instead of recording: the dropzone, behind a button, with the note
 *  that uploading also completes the visit. */
function UploadInstead({
  busy,
  onUpload,
  label,
}: {
  busy: boolean;
  onUpload: (file: File) => void;
  label: string;
}) {
  const [open, setOpen] = useState(false);
  if (!open) {
    return (
      <button className="btn" disabled={busy} onClick={() => setOpen(true)}>
        {label}
      </button>
    );
  }
  return (
    <div style={{ marginTop: 14, flexBasis: "100%" }}>
      <TranscriptDropzone onFileSelected={onUpload} busy={busy} />
      <p style={{ fontSize: 12.5, color: "var(--ink-2)", marginTop: 10 }}>
        Uploading marks the visit complete. A recording is transcribed and each voice
        is labelled as doctor, patient or someone else present.{" "}
        <button className="btn btn--subtle btn--sm" onClick={() => setOpen(false)}>
          Hide
        </button>
      </p>
    </div>
  );
}

function ScheduledState({
  visit,
  busy,
  onStartSession,
  onComplete,
  onCancel,
  onUpload,
}: {
  visit: Visit;
  busy: boolean;
  onStartSession: () => void;
  onComplete: () => void;
  onCancel: () => void;
  onUpload: (file: File) => void;
}) {
  return (
    <div className="card">
      <div className="card-body">
        <div style={{ display: "flex", gap: 16, alignItems: "flex-start", justifyContent: "space-between", flexWrap: "wrap" }}>
          <div>
            <p style={{ fontWeight: 600 }}>Consultation not yet held</p>
            <p style={{ color: "var(--ink-2)", fontSize: 13, marginTop: 4, maxWidth: "60ch" }}>
              Start a session to record the consultation here as it happens, or upload a
              recording or transcript once it is over. Either way the brief is compiled
              from it.
            </p>
          </div>
          <div style={{ display: "flex", gap: 8, flexWrap: "wrap" }}>
            <button className="btn btn--primary" disabled={busy} onClick={onStartSession}>
              Start session
            </button>
            <UploadInstead busy={busy} onUpload={onUpload} label="Upload transcript" />
          </div>
        </div>

        <ul className="steps" style={{ marginTop: 16, borderTop: "1px solid var(--border)", paddingTop: 6 }}>
          <li className="step--active">
            <span className="step-mark" aria-hidden="true" />
            <div>
              <div className="step-name">Consultation</div>
              <div className="step-detail">Scheduled for {visit.visit_date}</div>
            </div>
          </li>
          <li className="step--todo">
            <span className="step-mark" aria-hidden="true" />
            <div>
              <div className="step-name">Transcript</div>
              <div className="step-detail">Recorded live, or uploaded afterwards</div>
            </div>
          </li>
          <li className="step--todo">
            <span className="step-mark" aria-hidden="true" />
            <div>
              <div className="step-name">Brief</div>
              <div className="step-detail">Compiled from the transcript</div>
            </div>
          </li>
        </ul>

        <div style={{ display: "flex", gap: 8, marginTop: 10, flexWrap: "wrap" }}>
          <button className="btn btn--subtle btn--sm" disabled={busy} onClick={onComplete}>
            Mark complete without a transcript
          </button>
          <button className="btn btn--subtle btn--sm" disabled={busy} onClick={onCancel}>
            Cancel visit
          </button>
        </div>
      </div>
    </div>
  );
}

function InSessionState({
  visit,
  busy,
  recordingHere,
  onHandedOver,
  onComplete,
  onCancel,
  onUpload,
}: {
  visit: Visit;
  busy: boolean;
  /** This tab is recording this visit: no alternatives while it does. */
  recordingHere: boolean;
  onHandedOver: (upload: AudioUploadOut) => void;
  onComplete: () => void;
  onCancel: () => void;
  onUpload: (file: File) => void;
}) {
  return (
    <div className="card">
      <div className="card-head">
        <h2 className="card-title">{recordingHere ? "Recording the consultation" : "Session in progress"}</h2>
        <span className="badge badge--accent badge--dot">In session</span>
      </div>
      <div className="card-body">
        <LiveRecorderPanel visit={visit} onHandedOver={onHandedOver} />
        {!recordingHere && (
          <div
            style={{
              display: "flex",
              gap: 8,
              marginTop: 16,
              paddingTop: 14,
              borderTop: "1px solid var(--border)",
              flexWrap: "wrap",
              alignItems: "center",
            }}
          >
            <span className="muted" style={{ fontSize: 12.5 }}>Not recording here?</span>
            <UploadInstead busy={busy} onUpload={onUpload} label="Upload a recording or transcript" />
            <button className="btn btn--subtle btn--sm" disabled={busy} onClick={onComplete}>
              Mark complete without a transcript
            </button>
            <button className="btn btn--subtle btn--sm" disabled={busy} onClick={onCancel}>
              Cancel visit
            </button>
          </div>
        )}
      </div>
    </div>
  );
}

function timeOf(iso: string | null | undefined): string | null {
  if (!iso) return null;
  return new Date(iso).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
}

/** A live recording that stopped without End session: a closed tab, a crash. */
function InterruptedState({
  visit,
  recording,
  busy,
  onProcess,
  onDiscard,
}: {
  visit: Visit;
  recording: AudioUploadOut;
  busy: boolean;
  onProcess: () => void;
  onDiscard: () => void;
}) {
  const from = timeOf(recording.created_at);
  const to = timeOf(recording.last_chunk_at);
  const saved = recording.bytes_received > 0;
  // Pieces arrive about once a minute while recording, so one this recent means
  // it may still be recording in another tab, where it should be ended instead.
  const [openedAt] = useState(() => Date.now());
  const lastPieceAt = recording.last_chunk_at ? Date.parse(recording.last_chunk_at) : null;
  const maybeStillRecording = lastPieceAt !== null && openedAt - lastPieceAt < 3 * 60_000;
  return (
    <div className="card">
      <div className="card-head">
        <h2 className="card-title">Recording interrupted</h2>
      </div>
      <div className="card-body">
        <div className="notice">
          <svg width="14" height="14" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.6" aria-hidden="true">
            <circle cx="8" cy="8" r="6" />
            <path d="M8 5v3.5M8 11h.01" />
          </svg>
          <span>
            <span className="notice-title">
              The live recording stopped before the session was ended.
            </span>{" "}
            {saved ? (
              <>
                What was saved runs from {from ?? "the start"}
                {to ? ` to ${to}` : ""}. Up to the last minute before it stopped may be
                missing. It is kept for a day unless you process or discard it.
              </>
            ) : (
              <>Nothing was saved before it stopped.</>
            )}
          </span>
        </div>
        {maybeStillRecording && (
          <p style={{ fontSize: 13, marginBottom: 12, maxWidth: "64ch" }}>
            The last part arrived a few minutes ago. If this consultation is still being
            recorded in another tab or on another computer, end the session there instead:
            processing or discarding it here stops that recording.
          </p>
        )}
        <div style={{ display: "flex", gap: 8, flexWrap: "wrap" }}>
          {saved && (
            <button className="btn btn--primary" disabled={busy} onClick={onProcess}>
              {busy ? "Working…" : "Process what was saved"}
            </button>
          )}
          <button className={saved ? "btn" : "btn btn--primary"} disabled={busy} onClick={onDiscard}>
            {visit.visit_status === "in_progress" ? "Discard and record again" : "Discard"}
          </button>
        </div>
        {saved && visit.visit_status !== "completed" && (
          <p className="muted" style={{ fontSize: 12.5, marginTop: 10 }}>
            Processing marks the visit complete.
          </p>
        )}
      </div>
    </div>
  );
}

const LAYER_NAMES = [
  { name: "Clinical extraction", detail: "What was said, without interpretation" },
  { name: "Homeopathic analysis", detail: "Remedy reasoning and pattern" },
  { name: "Visit brief", detail: "The summary you read before the next call" },
];

function CompilingState({ job }: { job: JobOut }) {
  const completed = job.layers_completed;
  const pct = Math.round((completed / 3) * 100);
  return (
    <div className="card">
      <div className="card-head">
        <h2 className="card-title">Compiling</h2>
      </div>
      <div className="card-body">
        <p style={{ color: "var(--ink-2)", fontSize: 13, maxWidth: "64ch" }}>
          Reading the transcript in three passes. This takes about a minute. You can
          leave this page &mdash; it will keep going.
        </p>

        <ul className="steps" style={{ marginTop: 12 }}>
          {LAYER_NAMES.map((layer, i) => {
            const state = i < completed ? "done" : i === completed ? "active" : "todo";
            return (
              <li key={layer.name} className={`step--${state}`}>
                <span className="step-mark" aria-hidden="true">
                  {state === "done" && (
                    <svg width="10" height="10" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="2.4" aria-hidden="true">
                      <path d="M3.5 8.5l3 3 6-7" />
                    </svg>
                  )}
                </span>
                <div>
                  <div className="step-name">{layer.name}</div>
                  <div className="step-detail">
                    {state === "active" ? "running" : layer.detail}
                  </div>
                </div>
              </li>
            );
          })}
        </ul>
        <div
          className="bar"
          role="progressbar"
          aria-valuenow={completed}
          aria-valuemin={0}
          aria-valuemax={3}
          aria-label={`Compilation progress: pass ${completed} of 3`}
        >
          <span style={{ width: `${Math.max(pct, 8)}%` }} />
        </div>
        <p className="muted" style={{ fontSize: 12, marginTop: 8 }}>
          Pass {Math.min(completed + 1, 3)} of 3
        </p>
      </div>
    </div>
  );
}

function QuotaState({ onRetry, busy }: { onRetry: () => void; busy: boolean }) {
  return (
    <div className="card">
      <div className="card-body">
        <div className="notice" style={{ marginBottom: 14 }}>
          <svg width="14" height="14" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.6" aria-hidden="true">
            <circle cx="8" cy="8" r="6" />
            <path d="M8 5v3.5M8 11h.01" />
          </svg>
          <span>
            <span className="notice-title">Daily limit reached.</span> The transcript is
            saved. Compiling uses an external service with a daily cap, which has been
            reached &mdash; try again tomorrow, or now if capacity has freed up. Nothing
            has been lost.
          </span>
        </div>
        <button className="btn btn--primary" disabled={busy} onClick={onRetry}>
          {busy ? "Retrying…" : "Try again"}
        </button>
      </div>
    </div>
  );
}

function FailedState({
  job,
  onRetry,
  busy,
}: {
  job: JobOut;
  onRetry: () => void;
  busy: boolean;
}) {
  return (
    <div className="card">
      <div className="card-body">
        <div className="notice notice--critical" style={{ marginBottom: 14 }}>
          <svg width="14" height="14" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.6" aria-hidden="true">
            <path d="M8 2.8 14.2 13H1.8L8 2.8Z" />
            <path d="M8 6.6v2.9M8 11.2h.01" />
          </svg>
          <span>
            <span className="notice-title">Generation failed.</span>{" "}
            {job.error_message ?? "Something went wrong while compiling this brief."}
          </span>
        </div>
        <button className="btn btn--primary" disabled={busy} onClick={onRetry}>
          {busy ? "Retrying…" : "Try again"}
        </button>
      </div>
    </div>
  );
}
