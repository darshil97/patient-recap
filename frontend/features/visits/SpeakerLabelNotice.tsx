"use client";

import type { AudioUploadOut } from "@/lib/api/audioUploads";
import type { Visit } from "@/lib/api/visits";

/**
 * Whether a recording's speaker-label warnings apply to the transcript on file.
 *
 * They stop applying once a different transcript replaces it — a corrected .txt,
 * typically. The backend writes the transcript just before it marks the upload
 * completed, so a transcript stamped *after* completion came from somewhere else.
 */
export function speakerWarningsApply(
  recording: AudioUploadOut | null | undefined,
  visit: Visit,
): recording is AudioUploadOut {
  if (!recording || recording.status !== "completed" || recording.warnings.length === 0) {
    return false;
  }
  if (!visit.transcript_uploaded_at || !recording.completed_at) return false;
  return (
    recording.filename === visit.transcript_filename &&
    new Date(visit.transcript_uploaded_at) <= new Date(recording.completed_at)
  );
}

/**
 * What the speaker labelling was unsure about. Informational: insights are
 * generated straight after transcription regardless, so this sits above the
 * brief (or its progress) telling the doctor which parts to check against the
 * transcript, rather than holding generation back.
 */
export function SpeakerLabelNotice({
  recording,
  onShowTranscript,
}: {
  recording: AudioUploadOut;
  onShowTranscript?: () => void;
}) {
  const labels = Array.from(new Set(recording.speakers.map((s) => s.label)));
  return (
    <div className="notice" role="note" style={{ marginBottom: 14 }}>
      <svg width="14" height="14" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.6" aria-hidden="true">
        <circle cx="8" cy="8" r="6" />
        <path d="M8 5v3.5M8 11h.01" />
      </svg>
      <span>
        <span className="notice-title">Check the speaker labels.</span> This transcript
        came from a recording, labelled {labels.join(", ")}. Some labels may be wrong,
        which would attribute words to the wrong person in the brief.
        <ul style={{ margin: "6px 0 0", paddingLeft: 18 }}>
          {recording.warnings.map((warning) => (
            <li key={warning}>{warning}</li>
          ))}
        </ul>
        {onShowTranscript && (
          <button
            type="button"
            className="btn btn--sm"
            style={{ marginTop: 8 }}
            onClick={onShowTranscript}
          >
            Read the transcript
          </button>
        )}
      </span>
    </div>
  );
}
