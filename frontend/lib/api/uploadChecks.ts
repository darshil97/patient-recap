import { isAudioFile } from "./audioUploads";

/**
 * The server's upload rules, checked in the browser first.
 *
 * Only used where an upload also completes the visit: Upload transcript on a
 * visit that is scheduled or in session calls /complete and then uploads (the
 * server still sees two explicit transitions, §8.17). Checking here first means
 * an obviously wrong file is refused *before* the visit is marked complete. The
 * server still validates everything; these limits mirror backend config.yaml
 * (transcripts.max_upload_bytes, speech.max_upload_bytes) and only need to be
 * no stricter than it.
 */
export const MAX_TRANSCRIPT_BYTES = 256 * 1024;
export const MAX_RECORDING_BYTES = 100 * 1024 * 1024;

/** A reason the file would be refused, or null if it looks acceptable. */
export async function checkUploadFile(file: File): Promise<string | null> {
  if (file.size === 0) return `${file.name} is empty.`;

  if (isAudioFile(file)) {
    if (file.size > MAX_RECORDING_BYTES) {
      return `${file.name} is larger than the 100 MB limit for recordings.`;
    }
    return null;
  }

  if (!file.name.toLowerCase().endsWith(".txt")) {
    return "Choose an audio recording (MP3, M4A, WAV and similar) or a plain-text .txt transcript.";
  }
  if (file.size > MAX_TRANSCRIPT_BYTES) {
    return `${file.name} is larger than the 256 KB limit for transcripts.`;
  }
  try {
    const text = new TextDecoder("utf-8", { fatal: true }).decode(await file.arrayBuffer());
    if (!text.replace(/^\uFEFF/, "").trim()) return `${file.name} has no text in it.`;
  } catch {
    return `${file.name} is not plain UTF-8 text. Save it as UTF-8 and try again.`;
  }
  return null;
}
