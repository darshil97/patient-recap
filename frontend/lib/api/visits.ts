import { api, unwrap } from "./client";
import type { components } from "../types/api";

export type Visit = components["schemas"]["VisitOut"];
export type VisitCreate = components["schemas"]["VisitCreate"];
export type VisitUpdate = components["schemas"]["VisitUpdate"];
export type VisitCompleteRequest = components["schemas"]["VisitCompleteRequest"];
export type VisitCancelRequest = components["schemas"]["VisitCancelRequest"];
export type TranscriptOut = components["schemas"]["TranscriptOut"];

export interface ListVisitsParams {
  status?: string[];
  from?: string;
  to?: string;
  hasTranscript?: boolean;
  patientId?: string;
  pendingOnly?: boolean;
  sort?: string;
  limit?: number;
  offset?: number;
}

export async function listVisits(
  params: ListVisitsParams = {},
): Promise<{ items: Visit[]; total: number }> {
  const result = await api.GET("/api/v1/visits", {
    params: {
      query: {
        status: params.status,
        from: params.from,
        to: params.to,
        has_transcript: params.hasTranscript,
        patient_id: params.patientId,
        pending_only: params.pendingOnly,
        sort: params.sort,
        limit: params.limit,
        offset: params.offset,
      },
    },
  });
  const items = unwrap(result);
  const totalHeader = result.response.headers.get("X-Total-Count");
  return { items, total: totalHeader ? Number(totalHeader) : items.length };
}

export async function getVisit(visitId: string): Promise<Visit> {
  const result = await api.GET("/api/v1/visits/{visit_id}", {
    params: { path: { visit_id: visitId } },
  });
  return unwrap(result);
}

export async function scheduleVisit(body: VisitCreate): Promise<Visit> {
  const result = await api.POST("/api/v1/visits", { body });
  return unwrap(result);
}

export async function updateVisit(
  visitId: string,
  body: VisitUpdate,
): Promise<Visit> {
  const result = await api.PATCH("/api/v1/visits/{visit_id}", {
    params: { path: { visit_id: visitId } },
    body,
  });
  return unwrap(result);
}

/** scheduled -> in_progress. Idempotent; a completed or cancelled visit is a 409. */
export async function startVisit(visitId: string): Promise<Visit> {
  const result = await api.POST("/api/v1/visits/{visit_id}/start", {
    params: { path: { visit_id: visitId } },
  });
  return unwrap(result);
}

export async function completeVisit(
  visitId: string,
  body?: VisitCompleteRequest,
): Promise<Visit> {
  const result = await api.POST("/api/v1/visits/{visit_id}/complete", {
    params: { path: { visit_id: visitId } },
    body: body ?? null,
  });
  return unwrap(result);
}

export async function cancelVisit(
  visitId: string,
  body?: VisitCancelRequest,
): Promise<Visit> {
  const result = await api.POST("/api/v1/visits/{visit_id}/cancel", {
    params: { path: { visit_id: visitId } },
    body: body ?? null,
  });
  return unwrap(result);
}

export async function getTranscript(visitId: string): Promise<TranscriptOut> {
  const result = await api.GET("/api/v1/visits/{visit_id}/transcript", {
    params: { path: { visit_id: visitId } },
  });
  return unwrap(result);
}

export async function uploadTranscript(
  visitId: string,
  file: File,
  replace = false,
): Promise<Visit> {
  // openapi-fetch's default body serializer passes FormData through
  // untouched (letting the browser set the multipart boundary), so the typed
  // { file, replace } shape below is only used for its TypeScript type; at
  // runtime we build real FormData.
  const form = new FormData();
  form.append("file", file);
  form.append("replace", String(replace));
  const result = await api.POST("/api/v1/visits/{visit_id}/transcript", {
    params: { path: { visit_id: visitId } },
    body: form as unknown as { file: string; replace: boolean },
  });
  return unwrap(result);
}

export async function deleteTranscript(visitId: string): Promise<void> {
  const result = await api.DELETE("/api/v1/visits/{visit_id}/transcript", {
    params: { path: { visit_id: visitId } },
  });
  unwrap(result);
}
