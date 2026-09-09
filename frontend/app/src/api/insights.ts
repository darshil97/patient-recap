import { api, unwrap } from "./client";
import type { components } from "../types/api";
import type { InsightsJson } from "../types/insightsJson";

// InsightOut.insights_json is typed Record<string, never> by openapi-typescript
// (Pydantic's Dict[str, Any] carries no shape info). Recast it to the
// hand-written InsightsJson type here, in the one place that reads it.
export type InsightOut = Omit<
  components["schemas"]["InsightOut"],
  "insights_json"
> & { insights_json: InsightsJson };

export type JobAccepted = components["schemas"]["JobAccepted"];
export type JobOut = components["schemas"]["JobOut"];
export type GenerateInsightsRequest =
  components["schemas"]["GenerateInsightsRequest"];

export async function getInsights(visitId: string): Promise<InsightOut> {
  const result = await api.GET("/api/v1/visits/{visit_id}/insights", {
    params: { path: { visit_id: visitId } },
  });
  return unwrap(result) as unknown as InsightOut;
}

export async function generateInsights(
  visitId: string,
  body?: GenerateInsightsRequest,
): Promise<JobAccepted> {
  const result = await api.POST("/api/v1/visits/{visit_id}/insights", {
    params: { path: { visit_id: visitId } },
    body: body ?? null,
  });
  return unwrap(result);
}

export async function getLatestJob(visitId: string): Promise<JobOut | null> {
  const result = await api.GET("/api/v1/visits/{visit_id}/job", {
    params: { path: { visit_id: visitId } },
  });
  return unwrap(result) ?? null;
}

export async function getJob(jobId: string): Promise<JobOut> {
  const result = await api.GET("/api/v1/jobs/{job_id}", {
    params: { path: { job_id: jobId } },
  });
  return unwrap(result);
}
