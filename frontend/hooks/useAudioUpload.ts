"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import {
  getAudioUpload,
  getLatestAudioUpload,
  type AudioUploadOut,
} from "@/lib/api/audioUploads";
import { ApiError } from "@/lib/api/client";

const POLL_INTERVAL_MS = 3000;
const MAX_CONSECUTIVE_FAILURES = 5;

/**
 * Tracks a visit's latest recording upload and polls it while the backend is
 * stitching and transcribing it (`processing`).
 *
 * Same shape and the same two safeguards as useInsightJob: a ref checked before
 * every schedule so an in-flight request cannot re-arm polling after unmount,
 * and giving up — immediately on a 401, after five tries otherwise — instead of
 * polling a broken endpoint forever. On mount it loads the latest upload, so a
 * reload mid-transcription picks the polling back up.
 */
export function useAudioUpload(
  visitId: string | undefined,
  /** Called once when a polled upload leaves `processing`. Not called for an
   * upload that was already finished when the page loaded. */
  onSettled?: (upload: AudioUploadOut) => void,
) {
  const [upload, setUpload] = useState<AudioUploadOut | null>(null);
  const [error, setError] = useState<string | null>(null);
  const timerRef = useRef<number | null>(null);
  const cancelledRef = useRef(false);
  const failuresRef = useRef(0);
  const pollRef = useRef<(uploadId: string) => void>(() => {});
  const onSettledRef = useRef(onSettled);

  useEffect(() => {
    onSettledRef.current = onSettled;
  }, [onSettled]);

  const clearTimer = useCallback(() => {
    if (timerRef.current !== null) {
      window.clearTimeout(timerRef.current);
      timerRef.current = null;
    }
  }, []);

  const scheduleNext = useCallback((uploadId: string) => {
    if (cancelledRef.current) return;
    timerRef.current = window.setTimeout(() => pollRef.current(uploadId), POLL_INTERVAL_MS);
  }, []);

  const poll = useCallback(
    async (uploadId: string) => {
      if (cancelledRef.current) return;
      try {
        const latest = await getAudioUpload(uploadId);
        if (cancelledRef.current) return;
        failuresRef.current = 0;
        setUpload(latest);
        setError(null);
        if (latest.status === "processing") {
          scheduleNext(uploadId);
        } else {
          onSettledRef.current?.(latest);
        }
      } catch (err) {
        if (cancelledRef.current) return;
        if (err instanceof ApiError && err.isUnauthorized) {
          setError("Your session expired while the recording was being transcribed.");
          return;
        }
        failuresRef.current += 1;
        if (failuresRef.current >= MAX_CONSECUTIVE_FAILURES) {
          setError(
            "Lost contact while transcribing. It may still be running — reload " +
              "the page to pick it up again.",
          );
          return;
        }
        scheduleNext(uploadId);
      }
    },
    [scheduleNext],
  );

  useEffect(() => {
    pollRef.current = poll;
  }, [poll]);

  /** Show an upload the caller already has, and poll it if it is processing. */
  const track = useCallback(
    (next: AudioUploadOut) => {
      clearTimer();
      cancelledRef.current = false;
      failuresRef.current = 0;
      setError(null);
      setUpload(next);
      if (next.status === "processing") scheduleNext(next.upload_id);
    },
    [clearTimer, scheduleNext],
  );

  useEffect(() => {
    if (!visitId) return;
    cancelledRef.current = false;
    failuresRef.current = 0;

    getLatestAudioUpload(visitId)
      .then((latest) => {
        if (cancelledRef.current) return;
        setUpload(latest);
        if (latest?.status === "processing") scheduleNext(latest.upload_id);
      })
      .catch(() => {
        // No upload history is not an error for the page; the lifecycle view
        // renders from the visit itself.
        if (!cancelledRef.current) setUpload(null);
      });

    return () => {
      cancelledRef.current = true;
      clearTimer();
    };
  }, [visitId, scheduleNext, clearTimer]);

  return { upload, error, track };
}
