"use client";

import { useEffect, useState, useCallback } from "react";
import { getStats, type Stats } from "@/lib/api/stats";

/**
 * Home-screen / nav counters. Shared across the shell (nav badges) and the
 * Today page (stat chips) so both read the same numbers from one request.
 */
export function useStats() {
  const [stats, setStats] = useState<Stats | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<unknown>(null);

  const reload = useCallback(async () => {
    setLoading(true);
    try {
      const data = await getStats();
      setStats(data);
      setError(null);
    } catch (err) {
      setError(err);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    reload();
  }, [reload]);

  return { stats, loading, error, reload };
}
