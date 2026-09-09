import { api, unwrap } from "./client";
import type { components } from "../types/api";

export type Stats = components["schemas"]["StatsOut"];

export async function getStats(): Promise<Stats> {
  const result = await api.GET("/api/v1/stats");
  return unwrap(result);
}
