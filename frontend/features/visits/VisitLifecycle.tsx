"use client";

import { useState } from "react";
import type { Visit } from "@/lib/api/visits";
import { completeVisit, cancelVisit, uploadTranscript } from "@/lib/api/visits";
import { generateInsights } from "@/lib/api/insights";
import type { JobOut } from "@/lib/api/insights";
import { TranscriptDropzone } from "./TranscriptDropzone";
import { useToast } from "../../components/Toast";
import { ApiError } from "@/lib/api/client";

/**
 * The pre-brief lifecycle states from visit-pending.html: not yet held,
 * awaiting transcript, compiling, and the daily-quota-reached state.
 * Renders whichever state the visit (and its latest job) is currently in.
 */
export function VisitLifecycle({
  visit,
  job,
  onVisitChanged,
  onGenerationStarted,
}: {
  visit: Visit;
  job: JobOut | null;
  onVisitChanged: () => void;
  onGenerationStarted: (jobId: string) => void;
}) {
  const { showToast } = useToast();
  const [busy, setBusy] = useState(false);

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
    setBusy(true);
    try {
      const updated = await uploadTranscript(visit.visit_id, file, visit.has_insights);
      showToast("Transcript saved. Starting insight generation…");
      onVisitChanged();
      try {
        const accepted = await generateInsights(updated.visit_id, {
          stop_after: 3,
          regenerate: updated.has_insights,
        });
        onGenerationStarted(accepted.job_id);
      } catch (genErr) {
        showToast(
          genErr instanceof ApiError
            ? genErr.message
            : "Transcript saved, but generation could not start yet. Use Generate insights below.",
          { variant: "error" },
        );
      }
    } catch (err) {
      showToast(err instanceof ApiError ? err.message : "Could not upload the transcript.", {
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
        stop_after: 3,
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

  // State: an in-flight job that hasn't reached a terminal status yet.
  if (job && !["succeeded", "failed", "failed_quota"].includes(job.status)) {
    return <CompilingState job={job} />;
  }

  // State: the last job failed because every configured API key is rate limited.
  if (job && job.status === "failed_quota") {
    return <QuotaState onRetry={handleGenerate} busy={busy} />;
  }

  // State: the last job failed for a real reason.
  if (job && job.status === "failed") {
    return <FailedState job={job} onRetry={handleGenerate} busy={busy} />;
  }

  // State: not yet held.
  if (visit.visit_status === "scheduled") {
    return (
      <NotYetHeldState
        visit={visit}
        busy={busy}
        onComplete={handleComplete}
        onCancel={handleCancel}
      />
    );
  }

  if (visit.visit_status === "cancelled") {
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
  if (!visit.transcript_path) {
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
          <TranscriptDropzone onFileSelected={handleUpload} busy={busy} />
          <p style={{ fontSize: 12.5, color: "var(--ink-2)", marginTop: 12 }}>
            Save the recording as plain text before uploading. Word documents, PDFs and
            JSON are not accepted &mdash; a mis-read file would produce a confident but
            wrong record.
          </p>
        </div>
      </div>
    );
  }

  // State: transcript attached, insights never generated (and no job history).
  if (!visit.has_insights) {
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
        </div>
      </div>
    );
  }

  return null;
}

function NotYetHeldState({
  visit,
  busy,
  onComplete,
  onCancel,
}: {
  visit: Visit;
  busy: boolean;
  onComplete: () => void;
  onCancel: () => void;
}) {
  return (
    <div className="card">
      <div className="card-body">
        <div style={{ display: "flex", gap: 16, alignItems: "flex-start", justifyContent: "space-between", flexWrap: "wrap" }}>
          <div>
            <p style={{ fontWeight: 600 }}>Consultation not yet held</p>
            <p style={{ color: "var(--ink-2)", fontSize: 13, marginTop: 4, maxWidth: "60ch" }}>
              Mark the visit complete once you have seen the patient. You can then add
              the recording transcript and compile the brief.
            </p>
          </div>
          <div style={{ display: "flex", gap: 8 }}>
            <button className="btn btn--primary" disabled={busy} onClick={onComplete}>
              Complete visit
            </button>
            <button className="btn btn--subtle" disabled={busy} onClick={onCancel}>
              Cancel visit
            </button>
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
              <div className="step-detail">Upload after the consultation</div>
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
