"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { getJob, getLatestJob, type JobOut } from "@/lib/api/insights";
import { ApiError } from "@/lib/api/client";

const POLL_INTERVAL_MS = 2500;

/**
 * How many consecutive failures to absorb before giving up. Generation takes
 * about a minute, so five tries at 2.5s covers a blip comfortably while still
 * ending rather than polling a broken endpoint until the tab closes.
 */
const MAX_CONSECUTIVE_FAILURES = 5;

const TERMINAL_STATUSES = new Set(["succeeded", "failed", "failed_quota"]);

/**
 * Polls an insight generation job until it reaches a terminal state.
 *
 * Per frontend/README.md: poll every 2-3 seconds, and a page reload should
 * resume polling via GET /visits/{id}/job rather than losing track of an
 * in-flight job. `layers_completed` (0-3) drives a real progress indicator.
 *
 * Two things this has to get right, and previously did not:
 *
 *   * **Stopping.** The cleanup cleared the pending timer, but an in-flight
 *     request resolving *after* cleanup scheduled a fresh one, so navigating
 *     away mid-generation left a request every 2.5 seconds for the rest of the
 *     session. A ref, checked immediately before every schedule, is what a
 *     cleared timeout alone cannot do — cleanup cannot reach into a promise that
 *     has already been handed a continuation.
 *   * **Giving up.** The catch re-armed unconditionally, so a request that could
 *     never succeed — a 401 after the session expired, most obviously — became
 *     an infinite loop that also looked, to the doctor, like a brief still
 *     compiling. A 401 stops immediately; anything else gets five tries.
 */
export function useInsightJob(visitId: string | undefined) {
  const [job, setJob] = useState<JobOut | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const timerRef = useRef<number | null>(null);
  const cancelledRef = useRef(false);
  const failuresRef = useRef(0);
  // Holds the latest `poll`. A self-referencing useCallback would be a
  // temporal-dead-zone reference inside its own initializer; a ref is read at
  // call time, which is the only time it is needed.
  const pollRef = useRef<(jobId: string) => void>(() => {});

  const clearTimer = useCallback(() => {
    if (timerRef.current !== null) {
      window.clearTimeout(timerRef.current);
      timerRef.current = null;
    }
  }, []);

  const stopPolling = useCallback(() => {
    cancelledRef.current = true;
    clearTimer();
  }, [clearTimer]);

  /** Schedule the next poll, unless this hook has been torn down. */
  const scheduleNext = useCallback((jobId: string) => {
    if (cancelledRef.current) return;
    timerRef.current = window.setTimeout(
      () => pollRef.current(jobId),
      POLL_INTERVAL_MS,
    );
  }, []);

  const poll = useCallback(
    async (jobId: string) => {
      if (cancelledRef.current) return;
      try {
        const latest = await getJob(jobId);
        if (cancelledRef.current) return;
        failuresRef.current = 0;
        setJob(latest);
        setError(null);
        if (!TERMINAL_STATUSES.has(latest.status)) {
          scheduleNext(jobId);
        }
      } catch (err) {
        if (cancelledRef.current) return;

        // A 401 will not fix itself by asking again: the session is gone and
        // every further poll is a request the doctor cannot authorise.
        if (err instanceof ApiError && err.isUnauthorized) {
          setError("Your session expired while this brief was compiling.");
          stopPolling();
          return;
        }

        failuresRef.current += 1;
        if (failuresRef.current >= MAX_CONSECUTIVE_FAILURES) {
          setError(
            "Lost contact while compiling. The job may still be running — " +
              "reload the page to pick it up again.",
          );
          stopPolling();
          return;
        }
        scheduleNext(jobId);
      }
    },
    [scheduleNext, stopPolling],
  );

  // Keep the ref pointing at the current closure. In an effect rather than
  // during render: a ref written while rendering is not safe under concurrent
  // rendering, and the timer that reads it can only fire after a commit anyway.
  useEffect(() => {
    pollRef.current = poll;
  }, [poll]);

  const startTracking = useCallback(
    (jobId: string) => {
      clearTimer();
      cancelledRef.current = false;
      failuresRef.current = 0;
      setError(null);
      poll(jobId);
    },
    [clearTimer, poll],
  );

  useEffect(() => {
    if (!visitId) return;
    cancelledRef.current = false;
    failuresRef.current = 0;
    setLoading(true);

    getLatestJob(visitId)
      .then((latest) => {
        if (cancelledRef.current) return;
        setJob(latest);
        if (latest && !TERMINAL_STATUSES.has(latest.status)) {
          scheduleNext(latest.job_id);
        }
      })
      .catch(() => {
        // No job history is not an error state for the page; the lifecycle view
        // renders from the visit itself.
        if (!cancelledRef.current) setJob(null);
      })
      .finally(() => {
        if (!cancelledRef.current) setLoading(false);
      });

    return () => {
      cancelledRef.current = true;
      clearTimer();
    };
  }, [visitId, poll, scheduleNext, clearTimer]);

  const isTerminal = job ? TERMINAL_STATUSES.has(job.status) : false;

  return { job, loading, error, isTerminal, startTracking, stopPolling };
}
