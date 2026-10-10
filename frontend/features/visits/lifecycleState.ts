import { isInterruptedLiveRecording, type AudioUploadOut } from "@/lib/api/audioUploads";
import type { JobOut } from "@/lib/api/insights";
import type { Visit } from "@/lib/api/visits";

/**
 * Which card the visit page shows before there is a brief. Pure, so the order
 * of precedence (which is the whole difficulty) is tested on its own.
 */
export type LifecycleState =
  /** An insight job is queued or running. */
  | "compiling"
  | "quota"
  | "job_failed"
  | "cancelled"
  /** This tab is recording this visit right now. */
  | "recording_live"
  /** A file is going up from this tab, or the backend is transcribing. */
  | "transcribing"
  /** A live recording stopped without being finished (closed tab, crash). */
  | "interrupted"
  /** Not held yet: Start session, or upload afterwards. */
  | "scheduled"
  /** Session started, not recording here: the recorder, or upload instead. */
  | "in_session"
  | "add_transcript"
  | "generate"
  | "none";

export function lifecycleState({
  visit,
  job,
  recording,
  sending,
  recordingHere,
}: {
  visit: Pick<Visit, "visit_status" | "transcript_path" | "has_insights">;
  job: Pick<JobOut, "status"> | null;
  recording: Pick<AudioUploadOut, "status" | "mode"> | null;
  sending: boolean;
  /** The live session in this tab belongs to this visit. */
  recordingHere: boolean;
}): LifecycleState {
  if (job && !["succeeded", "failed", "failed_quota"].includes(job.status)) return "compiling";
  if (job?.status === "failed_quota") return "quota";
  if (job?.status === "failed") return "job_failed";
  if (visit.visit_status === "cancelled") return "cancelled";
  // Before anything about the visit's status: End session completes the visit
  // while the panel is still finishing, and it must stay on screen until the
  // recording has been handed over.
  if (recordingHere) return "recording_live";
  if (sending || recording?.status === "processing") return "transcribing";
  if (isInterruptedLiveRecording(recording)) return "interrupted";
  if (visit.visit_status === "scheduled") return "scheduled";
  if (visit.visit_status === "in_progress") return "in_session";
  if (!visit.transcript_path) return "add_transcript";
  if (!visit.has_insights) return "generate";
  return "none";
}
