"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { getJob, getLatestJob, type JobOut } from "@/lib/api/insights";

const POLL_INTERVAL_MS = 2500;

const TERMINAL_STATUSES = new Set(["succeeded", "failed", "failed_quota"]);

/**
 * Polls an insight generation job until it reaches a terminal state.
 *
 * Per frontend/README.md: poll every 2-3 seconds, and a page reload should
 * resume polling via GET /visits/{id}/job rather than losing track of an
 * in-flight job. `layers_completed` (0-3) drives a real progress indicator.
 */
export function useInsightJob(visitId: string | undefined) {
  const [job, setJob] = useState<JobOut | null>(null);
  const [loading, setLoading] = useState(true);
  const timerRef = useRef<number | null>(null);

  const stopPolling = useCallback(() => {
    if (timerRef.current !== null) {
      window.clearTimeout(timerRef.current);
      timerRef.current = null;
    }
  }, []);

  const poll = useCallback(
    async (jobId: string) => {
      try {
        const latest = await getJob(jobId);
        setJob(latest);
        if (!TERMINAL_STATUSES.has(latest.status)) {
          timerRef.current = window.setTimeout(() => poll(jobId), POLL_INTERVAL_MS);
        }
      } catch {
        // Transient network error while polling: try again on the same
        // schedule rather than surfacing every blip as a failure.
        timerRef.current = window.setTimeout(() => poll(jobId), POLL_INTERVAL_MS);
      }
    },
    [],
  );

  const startTracking = useCallback(
    (jobId: string) => {
      stopPolling();
      poll(jobId);
    },
    [poll, stopPolling],
  );

  useEffect(() => {
    if (!visitId) return;
    let cancelled = false;
    setLoading(true);
    getLatestJob(visitId)
      .then((latest) => {
        if (cancelled) return;
        setJob(latest);
        if (latest && !TERMINAL_STATUSES.has(latest.status)) {
          timerRef.current = window.setTimeout(
            () => poll(latest.job_id),
            POLL_INTERVAL_MS,
          );
        }
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
      stopPolling();
    };
  }, [visitId, poll, stopPolling]);

  const isTerminal = job ? TERMINAL_STATUSES.has(job.status) : false;

  return { job, loading, isTerminal, startTracking, stopPolling };
}
