"use client";

import { createContext, useContext, type ReactNode } from "react";
import { useStats } from "./useStats";
import type { Stats } from "@/lib/api/stats";

interface StatsContextValue {
  stats: Stats | null;
  loading: boolean;
  error: unknown;
  reload: () => Promise<void>;
}

const StatsContext = createContext<StatsContextValue | null>(null);

/** Provides the /stats counters once at the app root; nav badges and the
 * Today/Schedule stat chips all read from here instead of each fetching
 * their own copy. */
export function StatsProvider({ children }: { children: ReactNode }) {
  const value = useStats();
  return <StatsContext.Provider value={value}>{children}</StatsContext.Provider>;
}

export function useStatsContext(): StatsContextValue {
  const ctx = useContext(StatsContext);
  if (!ctx) throw new Error("useStatsContext must be used within StatsProvider");
  return ctx;
}
