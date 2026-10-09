import { api, unwrap, ApiError } from "./client";
import type { components } from "../types/api";

export type AudioUploadOut = components["schemas"]["AudioUploadOut"];
export type SpeakerOut = components["schemas"]["SpeakerOut"];

/**
 * Recording formats the backend transcribes (AUDIO_CONTENT_TYPES in
 * backend/app/domain/audio.py). The backend decides by extension, so the UI
 * does too; anything else is treated as a text upload and gets the .txt rules.
 */
export const AUDIO_EXTENSIONS = [
  ".mp3",
  ".m4a",
  ".mp4",
  ".aac",
  ".wav",
  ".flac",
  ".ogg",
  ".oga",
  ".opus",
  ".webm",
] as const;

export function isAudioFile(file: File): boolean {
  const name = file.name.toLowerCase();
  return AUDIO_EXTENSIONS.some((ext) => name.endsWith(ext));
}

export async function startAudioUpload(
  visitId: string,
  body: { filename: string; size_bytes: number; replace: boolean },
): Promise<AudioUploadOut> {
  const result = await api.POST("/api/v1/visits/{visit_id}/audio-uploads", {
    params: { path: { visit_id: visitId } },
    body,
  });
  return unwrap(result);
}

export async function putAudioChunk(
  uploadId: string,
  index: number,
  chunk: Blob,
): Promise<AudioUploadOut> {
  // FormData passes through openapi-fetch untouched, so the browser sets the
  // multipart boundary. The typed shape is only for the compiler.
  const form = new FormData();
  form.append("file", chunk, `chunk-${index}`);
  const result = await api.PUT("/api/v1/audio-uploads/{upload_id}/chunks/{index}", {
    params: { path: { upload_id: uploadId, index } },
    body: form as unknown as { file: string },
  });
  return unwrap(result);
}

export async function finishAudioUpload(uploadId: string): Promise<AudioUploadOut> {
  const result = await api.POST("/api/v1/audio-uploads/{upload_id}/finish", {
    params: { path: { upload_id: uploadId } },
  });
  return unwrap(result);
}

export async function getAudioUpload(uploadId: string): Promise<AudioUploadOut> {
  const result = await api.GET("/api/v1/audio-uploads/{upload_id}", {
    params: { path: { upload_id: uploadId } },
  });
  return unwrap(result);
}

/** The newest upload for a visit, or null when there has never been one (204). */
export async function getLatestAudioUpload(visitId: string): Promise<AudioUploadOut | null> {
  const result = await api.GET("/api/v1/visits/{visit_id}/audio-upload", {
    params: { path: { visit_id: visitId } },
  });
  return unwrap(result) ?? null;
}

// --------------------------------------------------------------------------- //
// Sending a whole file
// --------------------------------------------------------------------------- //

const MAX_ATTEMPTS = 4;
const RETRY_BASE_MS = 1000;

/**
 * Worth sending again: the network dropped, the BFF could not reach or wait
 * for the backend, or storage hiccuped. A 4xx is the request itself and will
 * fail the same way every time.
 */
function isTransient(err: unknown): boolean {
  if (err instanceof ApiError) {
    return [408, 429, 500, 502, 503, 504].includes(err.status);
  }
  // fetch rejects with a TypeError when the request never got a response.
  return err instanceof TypeError;
}

const sleep = (ms: number) => new Promise((resolve) => setTimeout(resolve, ms));

async function withRetry<T>(attempt: () => Promise<T>): Promise<T> {
  for (let tries = 1; ; tries++) {
    try {
      return await attempt();
    } catch (err) {
      if (tries >= MAX_ATTEMPTS || !isTransient(err)) throw err;
      await sleep(RETRY_BASE_MS * 2 ** (tries - 1));
    }
  }
}

export interface ChunkProgress {
  sent: number;
  total: number;
}

/**
 * Send a recording in the backend's chunk plan, then hand it over for
 * transcription. Resolves with the upload in `processing`; poll it from there.
 *
 * Chunks exist because every request goes through the BFF, and on Vercel a
 * request body is capped at 4.5 MB (backend `speech.chunk_bytes` stays under
 * it). Each chunk is retried on its own, and resending one is harmless — the
 * backend replaces it — so a flaky connection costs a chunk, not the file.
 */
export async function uploadRecording(
  visitId: string,
  file: File,
  options: { replace: boolean; onProgress?: (progress: ChunkProgress) => void },
): Promise<AudioUploadOut> {
  const upload = await startAudioUpload(visitId, {
    filename: file.name,
    size_bytes: file.size,
    replace: options.replace,
  });
  const { upload_id: uploadId, chunk_bytes: size, chunk_count: total } = upload;

  options.onProgress?.({ sent: 0, total });
  for (let index = 0; index < total; index++) {
    const chunk = file.slice(index * size, Math.min((index + 1) * size, file.size));
    await withRetry(() => putAudioChunk(uploadId, index, chunk));
    options.onProgress?.({ sent: index + 1, total });
  }

  // If Finish was accepted but its response was lost, a resend is a 409 — so on
  // any failure, check whether the server already has it before trying again.
  return withRetry(async () => {
    try {
      return await finishAudioUpload(uploadId);
    } catch (err) {
      const current = await getAudioUpload(uploadId).catch(() => null);
      if (current && (current.status === "processing" || current.status === "completed")) {
        return current;
      }
      throw err;
    }
  });
}
