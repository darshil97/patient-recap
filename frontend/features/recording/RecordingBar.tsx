"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { formatElapsed } from "@/lib/recording/level";
import { useLiveSession, type LiveSession } from "./LiveSessionProvider";

function phaseLabel(session: LiveSession): string {
  switch (session.phase) {
    case "starting":
      return "Starting recording";
    case "recording":
      return "Recording";
    case "paused":
      return "Recording paused";
    case "stopped":
      return "Recording stopped";
    case "finishing":
      return "Saving the recording";
    case "failed":
      return "Recording ended";
  }
}

/** One line on what is saved. Text, not colour alone. */
export function savedLabel(session: LiveSession): string {
  const { acknowledged, waiting, retrying } = session.uploader;
  if (retrying && waiting > 0) {
    return `Connection lost: recording continues, ${waiting} part${waiting === 1 ? "" : "s"} waiting to be saved`;
  }
  if (acknowledged === 0) return "Saved about once a minute";
  return `${acknowledged} part${acknowledged === 1 ? "" : "s"} saved`;
}

/**
 * Shown on every page except the session's own visit page, so a doctor who
 * opened the patient's history mid-consultation can see the recording is still
 * running and get back to it.
 */
export function RecordingBar() {
  const { session, elapsedMs } = useLiveSession();
  const pathname = usePathname();
  if (!session) return null;
  const href = `/visits/${session.visitId}`;
  if (pathname === href) return null;

  const live = session.phase === "recording";
  return (
    <div className={`rec-bar ${session.uploader.retrying ? "rec-bar--offline" : ""}`}>
      <span className="rec-status">
        <span className={`rec-dot ${live ? "rec-dot--live" : ""}`} aria-hidden="true" />
        {phaseLabel(session)}
      </span>
      <span>
        {session.patientName} &middot; visit {session.visitNumber}
      </span>
      <span className="rec-time">{formatElapsed(elapsedMs)}</span>
      <span className="rec-saved" role="status" aria-live="polite">
        {savedLabel(session)}
      </span>
      <span className="spacer" />
      <Link className="btn btn--sm" href={href}>
        Return to visit
      </Link>
    </div>
  );
}
