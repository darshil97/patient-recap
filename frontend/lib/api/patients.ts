import { api, unwrap } from "./client";
import type { components } from "../types/api";

export type Patient = components["schemas"]["PatientOut"];
export type PatientCreate = components["schemas"]["PatientCreate"];
export type PatientUpdate = components["schemas"]["PatientUpdate"];
export type Timeline = components["schemas"]["TimelineOut"];

export interface ListPatientsParams {
  q?: string;
  /** Server-side, so it orders the whole list and not just the page. */
  sort?: "name" | "created_desc";
  limit?: number;
  offset?: number;
}

export async function listPatients(
  params: ListPatientsParams = {},
): Promise<{ items: Patient[]; total: number }> {
  const result = await api.GET("/api/v1/patients", {
    params: {
      query: {
        q: params.q,
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

export async function getPatient(patientId: string): Promise<Patient> {
  const result = await api.GET("/api/v1/patients/{patient_id}", {
    params: { path: { patient_id: patientId } },
  });
  return unwrap(result);
}

export async function getTimeline(patientId: string): Promise<Timeline> {
  const result = await api.GET("/api/v1/patients/{patient_id}/timeline", {
    params: { path: { patient_id: patientId } },
  });
  return unwrap(result);
}

export async function createPatient(body: PatientCreate): Promise<Patient> {
  const result = await api.POST("/api/v1/patients", { body });
  return unwrap(result);
}

export async function updatePatient(
  patientId: string,
  body: PatientUpdate,
): Promise<Patient> {
  const result = await api.PATCH("/api/v1/patients/{patient_id}", {
    params: { path: { patient_id: patientId } },
    body,
  });
  return unwrap(result);
}
