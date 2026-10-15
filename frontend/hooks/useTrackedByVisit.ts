"use client";

import { useCallback, useEffect, useRef, useState } from "react";

const POLL_INTERVAL_MS = 3000;
const MAX_FAILED_POLLS = 5;
/** One lookup request per row, so capped. The rows that need one are the
 * visits waiting on something, normally a handful. */
const MAX_LOOKUPS = 25;

export interface Tracked<T> {
  item: T;
  patientName: string;
}

interface Options<T> {
  /** The item's own id, for polling it. */
  idOf: (item: T) => string;
  /** Still in progress on the server, so worth polling. */
  isActive: (item: T) => boolean;
  fetchById: (id: string) => Promise<T>;
  /** The visit's latest item, or null when it has none. */
  fetchLatest: (visitId: string) => Promise<T | null>;
  /** Called once, when a polled item stops being active. */
  onSettled: (item: T, patientName: string) => void;
}

/**
 * Background work per visit, for a list page: a recording being transcribed,
 * an insight job compiling. Looks up each row's latest item, polls the active
 * ones, and reports each exactly once when it finishes.
 *
 * Same safeguards as useInsightJob: the poll is cancelled on unmount and gives
 * up after a few rounds in which nothing came back (offline, or the session
 * expired) rather than polling a dead endpoint all day.
 */
export function useTrackedByVisit<T extends { visit_id: string }>(options: Options<T>) {
  const [tracked, setTracked] = useState<Record<string, Tracked<T>>>({});
  const optionsRef = useRef(options);
  const failedPolls = useRef(0);

  useEffect(() => {
    optionsRef.current = options;
  });

  /** Start following an item this page just created. */
  const track = useCallback((item: T, patientName: string) => {
    failedPolls.current = 0;
    setTracked((prev) => ({ ...prev, [item.visit_id]: { item, patientName } }));
  }, []);

  /** Look up the latest item for each of these visits. */
  const refresh = useCallback(
    async (visits: { visit_id: string; patient_name: string }[]) => {
      const subset = visits.slice(0, MAX_LOOKUPS);
      if (subset.length === 0) return;
      const { fetchLatest, idOf, isActive } = optionsRef.current;
      // undefined = the lookup failed, so keep what is known; null = none.
      const latest = await Promise.all(
        subset.map((v) => fetchLatest(v.visit_id).catch(() => undefined)),
      );
      setTracked((prev) => {
        const next = { ...prev };
        subset.forEach((visit, i) => {
          const item = latest[i];
          if (item === undefined) return;
          const existing = prev[visit.visit_id];
          // One already being polled is left to the poll, so its completion is
          // reported exactly once.
          if (existing && isActive(existing.item) && item && idOf(item) === idOf(existing.item)) {
            return;
          }
          if (item === null) {
            delete next[visit.visit_id];
          } else {
            next[visit.visit_id] = { item, patientName: visit.patient_name };
          }
        });
        return next;
      });
    },
    [],
  );

  // Re-armed by every change to `tracked`, which each poll makes, so it stops by
  // itself once nothing is active.
  useEffect(() => {
    const { idOf, isActive, fetchById } = optionsRef.current;
    const active = Object.values(tracked).filter((t) => isActive(t.item));
    if (active.length === 0) return;
    let cancelled = false;
    const timer = window.setTimeout(async () => {
      const fresh = await Promise.all(
        active.map((t) => fetchById(idOf(t.item)).catch(() => null)),
      );
      if (cancelled) return;
      if (fresh.every((item) => item === null)) {
        failedPolls.current += 1;
        if (failedPolls.current < MAX_FAILED_POLLS) setTracked((prev) => ({ ...prev }));
        return;
      }
      failedPolls.current = 0;
      setTracked((prev) => {
        const next = { ...prev };
        for (const item of fresh) {
          const entry = item && next[item.visit_id];
          if (item && entry && idOf(entry.item) === idOf(item)) {
            next[item.visit_id] = { ...entry, item };
          }
        }
        return next;
      });
      fresh.forEach((item, i) => {
        if (item && !isActive(item)) optionsRef.current.onSettled(item, active[i].patientName);
      });
    }, POLL_INTERVAL_MS);
    return () => {
      cancelled = true;
      window.clearTimeout(timer);
    };
  }, [tracked]);

  return { tracked, track, refresh };
}
