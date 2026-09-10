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

  const { job, startTracking } = useInsightJob(visitId);

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
  const showBrief = visit.has_insights && insight && (!job || job.status === "succeeded" || job.status === undefined);

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
                      stop_after: 3,
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
        {showBrief ? (
          <InsightBrief
            visit={visit}
            insight={insight}
            asOf={asOf}
            onScheduleFollowUp={() => setShowSchedule(true)}
          />
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
