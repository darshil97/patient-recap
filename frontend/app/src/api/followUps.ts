import { api, unwrap } from "./client";
import type { components } from "../types/api";

export type FollowUp = components["schemas"]["FollowUpOut"];
export type FollowUpUpdate = components["schemas"]["FollowUpUpdate"];

export interface ListFollowUpsParams {
  status?: "open" | "resolved" | "dismissed" | "all";
  dueBefore?: string;
  dueAfter?: string;
  needsConfirmation?: boolean;
  patientId?: string;
  sort?: "due_asc" | "due_desc";
  limit?: number;
  offset?: number;
}

export async function listFollowUps(
  params: ListFollowUpsParams = {},
): Promise<{ items: FollowUp[]; total: number }> {
  const result = await api.GET("/api/v1/follow-ups", {
    params: {
      query: {
        status: params.status,
        due_before: params.dueBefore,
        due_after: params.dueAfter,
        needs_confirmation: params.needsConfirmation,
        patient_id: params.patientId,
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

export async function getFollowUp(followUpId: string): Promise<FollowUp> {
  const result = await api.GET("/api/v1/follow-ups/{follow_up_id}", {
    params: { path: { follow_up_id: followUpId } },
  });
  return unwrap(result);
}

export async function updateFollowUp(
  followUpId: string,
  body: FollowUpUpdate,
): Promise<FollowUp> {
  const result = await api.PATCH("/api/v1/follow-ups/{follow_up_id}", {
    params: { path: { follow_up_id: followUpId } },
    body,
  });
  return unwrap(result);
}
