"use client";

import { useCallback, useEffect, useState } from "react";
import Link from "next/link";
import { getVisit, type Visit } from "@/lib/api/visits";
import { getInsights, generateInsights, type InsightOut } from "@/lib/api/insights";
import { useInsightJob } from "../hooks/useInsightJob";
import { useStatsContext } from "../hooks/StatsContext";
import { useToast } from "../components/Toast";
import { VisitLifecycle } from "../features/visits/VisitLifecycle";
import { InsightBrief } from "../features/visits/InsightBrief";
import { ErrorBoundary } from "../components/ErrorBoundary";
import { ScheduleVisitModal } from "../features/visits/ScheduleVisitModal";
import { ToolNote } from "../components/ToolNote";
import { formatDate, parseDateOnly } from "../lib/dates";
import { ApiError } from "@/lib/api/client";

/** `visitId` arrives as a prop from app/visits/[visitId]/page.tsx, which awaits
 *  the route params on the server. */
export function VisitPage({ visitId }: { visitId: string }) {
  const { stats, reload: reloadStats } = useStatsContext();
  const { showToast } = useToast();

  const [visit, setVisit] = useState<Visit | null>(null);
  const [insight, setInsight] = useState<InsightOut | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [showSchedule, setShowSchedule] = useState(false);

  const { job, error: jobError, startTracking } = useInsightJob(visitId);

  const load = useCallback(async () => {
    if (!visitId) return;
    setLoading(true);
    setError(null);
    try {
      const v = await getVisit(visitId);
      setVisit(v);
      if (v.has_insights) {
        try {
          const i = await getInsights(visitId);
          setInsight(i);
        } catch {
          setInsight(null);
        }
      } else {
        setInsight(null);
      }
    } catch {
      setError("Could not load this visit.");
    } finally {
      setLoading(false);
    }
  }, [visitId]);

  useEffect(() => {
    load();
  }, [load]);

  // When a job finishes successfully after we started tracking it, refresh
  // the visit + insight so the brief replaces the compiling state.
  const jobStatus = job?.status;
  useEffect(() => {
    if (jobStatus === "succeeded") {
      load();
      reloadStats();
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [jobStatus]);

  if (loading && !visit) {
    return (
      <div className="page">
        <div className="page-loading">
          <span className="spinner" aria-hidden="true" />
          Loading visit&hellip;
        </div>
      </div>
    );
  }

  if (error || !visit) {
    return (
      <div className="page">
        <div className="error-banner">{error ?? "Visit not found."}</div>
        <Link href="/schedule">Back to visit schedule</Link>
      </div>
    );
  }

  const asOf = stats ? parseDateOnly(stats.as_of) : new Date();

  // Gate on what is *in flight*, not on the last job having succeeded.
  //
  // Requiring "succeeded" meant a failed regeneration hid a brief that was still
  // sitting in the database, unchanged and perfectly good — the upsert only
  // replaces a row on success, so a failed rerun cannot have damaged it. The
  // doctor was shown a retry button instead of the brief they came for, which is
  // the wrong trade in a tool read before a consultation.
  const generating = job?.status === "queued" || job?.status === "running";
  const showBrief = Boolean(visit.has_insights && insight && !generating);

  // A failure worth mentioning above the brief rather than instead of it.
  const failedRegeneration =
    showBrief && (job?.status === "failed" || job?.status === "failed_quota")
      ? job
      : null;

  return (
    <>
      <div className="record-head">
        <p style={{ fontSize: 12.5, marginBottom: 8 }}>
          <Link href="/patients">Patients</Link>
          <span className="muted"> / </span>
          <Link href={`/patients/${visit.patient_id}`}>{visit.patient_name}</Link>
          <span className="muted"> / Visit {visit.visit_number}</span>
        </p>
        <div className="record-head-top">
          <div>
            <h1 className="record-name">
              {visit.patient_name}{" "}
              <span className="muted" style={{ fontWeight: 400, fontSize: 15 }}>
                &middot; Visit {visit.visit_number}
              </span>
            </h1>
            <div className="record-meta">
              <span>
                {visit.age_at_visit ?? "—"}
                {visit.gender ? visit.gender[0].toUpperCase() : ""}
              </span>
              <span className="sep">|</span>
              <span className="mrn">{visit.external_id}</span>
              <span className="sep">|</span>
              <span>{formatDate(visit.visit_date)}</span>
              <span className="sep">|</span>
              <span>{visit.visit_type === "initial" ? "Initial consultation" : "Follow-up consultation"}</span>
            </div>
          </div>
          {showBrief && (
            <div className="record-actions">
              <button
                className="btn"
                onClick={() => {
                  const el = document.getElementById("transcript");
                  el?.setAttribute("open", "");
                  el?.scrollIntoView({ behavior: "smooth" });
                }}
              >
                Transcript
              </button>
              <button
                className="btn"
                onClick={async () => {
                  if (!confirm("Regenerating overwrites the previous brief with no history kept. Continue?")) return;
                  try {
                    const accepted = await generateInsights(visit.visit_id, {
                      regenerate: true,
                    });
                    startTracking(accepted.job_id);
                    showToast("Recompiling the brief…");
                  } catch (err) {
                    showToast(err instanceof ApiError ? err.message : "Could not start regeneration.", {
                      variant: "error",
                    });
                  }
                }}
              >
                Regenerate
              </button>
            </div>
          )}
        </div>
      </div>

      <div className="page page--wide">
        {failedRegeneration && (
          <div
            className={
              failedRegeneration.status === "failed_quota"
                ? "notice"
                : "notice notice--critical"
            }
            style={{ marginBottom: 14 }}
          >
            <svg width="14" height="14" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.6" aria-hidden="true">
              <path d="M8 2.8 14.2 13H1.8L8 2.8Z" />
              <path d="M8 6.6v2.9M8 11.2h.01" />
            </svg>
            <span>
              <span className="notice-title">
                {failedRegeneration.status === "failed_quota"
                  ? "The last recompile hit the daily limit."
                  : "The last recompile failed."}
              </span>{" "}
              {failedRegeneration.status === "failed_quota"
                ? "Compiling uses an external service with a daily cap. Try again tomorrow."
                : failedRegeneration.error_message ??
                  "Something went wrong while rebuilding this brief."}{" "}
              The brief below is the last one that compiled successfully and is
              unchanged.
            </span>
          </div>
        )}

        {jobError && (
          <div className="notice notice--critical" style={{ marginBottom: 14 }}>
            <svg width="14" height="14" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.6" aria-hidden="true">
              <circle cx="8" cy="8" r="6" />
              <path d="M8 5v3.5M8 11h.01" />
            </svg>
            <span>{jobError}</span>
          </div>
        )}

        {showBrief && insight ? (
          // Boundary, not optimism. insights_json is whatever the model
          // returned: the backend stores it as Dict[str, Any] and the shape here
          // is a hand-written assertion that nothing validates at runtime. A
          // prompt edit is enough to change it. Without this, one unexpected
          // field took down the whole visit page — including the transcript,
          // which is exactly what a doctor needs when a brief looks wrong.
          <ErrorBoundary label="the visit brief" fallback={<BriefUnavailable />}>
            <InsightBrief
              visit={visit}
              insight={insight}
              asOf={asOf}
              onScheduleFollowUp={() => setShowSchedule(true)}
            />
          </ErrorBoundary>
        ) : (
          <VisitLifecycle
            visit={visit}
            job={job}
            onVisitChanged={load}
            onGenerationStarted={(jobId) => {
              startTracking(jobId);
              load();
            }}
          />
        )}

        <ToolNote />
      </div>

      {showSchedule && (
        <ScheduleVisitModal
          patientId={visit.patient_id}
          patientName={visit.patient_name}
          onClose={() => setShowSchedule(false)}
          onScheduled={() => {
            setShowSchedule(false);
            showToast("Visit scheduled.");
            reloadStats();
          }}
        />
      )}
    </>
  );
}

/** Shown when a stored brief cannot be rendered. Points at the two things that
 *  still work: the source transcript, and recompiling. */
function BriefUnavailable() {
  return (
    <div className="card">
      <div className="card-head">
        <h2 className="card-title">This brief could not be displayed</h2>
      </div>
      <div className="card-body">
        <p style={{ color: "var(--ink-2)", maxWidth: "62ch" }}>
          The stored brief is not in the shape this page expects, so it has not
          been shown rather than shown partially. Nothing has been lost: the
          consultation transcript is unchanged and is the source of record.
          Regenerating the brief will rebuild it from that transcript.
        </p>
      </div>
    </div>
  );
}
