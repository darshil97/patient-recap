"use client";

import Link from "next/link";
import { useId, useState } from "react";
import type { AudioUploadOut } from "@/lib/api/audioUploads";
import { canRecordInBrowser } from "@/lib/recording/format";
import { formatElapsed } from "@/lib/recording/level";
import { useToast } from "@/components/Toast";
import { LevelMeter } from "./LevelMeter";
import { savedLabel } from "./RecordingBar";
import { useLiveSession, type StartableVisit } from "./LiveSessionProvider";

const WarningIcon = () => (
  <svg width="14" height="14" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.6" aria-hidden="true">
    <path d="M8 2.8 14.2 13H1.8L8 2.8Z" />
    <path d="M8 6.6v2.9M8 11.2h.01" />
  </svg>
);

/**
 * Record the consultation in the browser (docs/PROJECT_CONTEXT.md §8.24).
 *
 * Before: a consent confirmation, then Start recording. During: elapsed time, a
 * level meter, what is saved, Pause and End session. End session sends the last
 * piece, marks the visit complete and hands the recording over; `onHandedOver`
 * then takes it into the existing transcribing -> brief progress.
 */
export function LiveRecorderPanel({
  visit,
  onHandedOver,
}: {
  visit: StartableVisit;
  onHandedOver: (upload: AudioUploadOut) => void;
}) {
  const live = useLiveSession();
  const { showToast } = useToast();
  const consentId = useId();
  const [consented, setConsented] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const session = live.session;
  const here = session?.visitId === visit.visit_id ? session : null;

  async function handleStart() {
    setBusy(true);
    setError(null);
    const problem = await live.start(visit);
    setBusy(false);
    if (problem) setError(problem);
  }

  async function handleEnd() {
    if (!confirm("End the session? Recording stops, the visit is marked complete, and the recording is transcribed.")) {
      return;
    }
    const accepted = await live.end();
    if (accepted) {
      showToast("Session ended. Transcribing the recording, then compiling the brief…");
      onHandedOver(accepted);
    }
  }

  async function handleDiscard() {
    if (!confirm("Discard this recording? Everything recorded so far is deleted and cannot be recovered.")) {
      return;
    }
    const problem = await live.discard();
    showToast(problem ?? "Recording discarded.", problem ? { variant: "error" } : undefined);
  }

  // --- someone else's session, or none yet --------------------------------- //

  if (!here) {
    if (session) {
      return (
        <div className="notice notice--info" style={{ marginBottom: 0 }}>
          <WarningIcon />
          <span>
            <span className="notice-title">A recording is already in progress</span> for{" "}
            {session.patientName}, visit {session.visitNumber}. End it before recording
            here. <Link href={`/visits/${session.visitId}`}>Go to that visit</Link>
          </span>
        </div>
      );
    }
    if (!canRecordInBrowser()) {
      return (
        <div className="notice" style={{ marginBottom: 0 }}>
          <WarningIcon />
          <span>
            <span className="notice-title">This browser cannot record audio.</span> Use a
            current version of Chrome, Edge, Firefox or Safari, or upload the recording
            after the consultation.
          </span>
        </div>
      );
    }
    return (
      <div>
        {error && (
          <div className="error-banner" role="alert" style={{ marginBottom: 12 }}>
            {error}
          </div>
        )}
        <label htmlFor={consentId} className="consent">
          <input
            id={consentId}
            type="checkbox"
            checked={consented}
            onChange={(e) => setConsented(e.target.checked)}
          />
          <span>The patient has agreed to this consultation being recorded.</span>
        </label>
        <button
          className="btn btn--primary"
          style={{ marginTop: 12 }}
          disabled={!consented || busy}
          onClick={handleStart}
        >
          <span className="rec-dot" aria-hidden="true" />
          {busy ? "Starting…" : "Start recording"}
        </button>
        <p className="muted" style={{ fontSize: 12.5, marginTop: 10, maxWidth: "64ch" }}>
          The recording is saved to the visit about once a minute while you talk, so
          closing the page loses at most the last minute. Keep this browser tab open and
          the laptop awake during the consultation.
        </p>
      </div>
    );
  }

  // --- this visit's session -------------------------------------------------- //

  if (here.phase === "starting") {
    return (
      <p role="status" className="status">
        <span className="spinner" aria-hidden="true" style={{ width: 12, height: 12 }} />
        Starting the microphone&hellip;
      </p>
    );
  }

  if (here.phase === "failed") {
    return (
      <div>
        <div className="notice notice--critical" role="alert">
          <WarningIcon />
          <span>{here.error}</span>
        </div>
        <button className="btn" onClick={live.dismiss}>
          Close
        </button>
      </div>
    );
  }

  if (here.phase === "finishing") {
    const { waiting, retrying } = here.uploader;
    return (
      <div>
        <p role="status" aria-live="polite" className="status">
          <span className="spinner" aria-hidden="true" style={{ width: 12, height: 12 }} />
          {retrying
            ? `Waiting for the connection to save the last ${waiting} part${waiting === 1 ? "" : "s"}…`
            : waiting > 0
              ? `Saving the last ${waiting === 1 ? "part" : `${waiting} parts`}…`
              : "Handing the recording over for transcription…"}
        </p>
        <p className="muted" style={{ fontSize: 12.5, marginTop: 8 }}>
          Keep this page open until it finishes.
        </p>
      </div>
    );
  }

  const recording = here.phase === "recording";
  return (
    <div>
      {here.notice && (
        <div className="notice" role="alert">
          <WarningIcon />
          <span>{here.notice}</span>
        </div>
      )}
      <div className="rec-panel-head">
        <span className="rec-status">
          <span className={`rec-dot ${recording ? "rec-dot--live" : ""}`} aria-hidden="true" />
          {recording ? "Recording" : here.phase === "paused" ? "Paused" : "Stopped"}
        </span>
        <span className="rec-time rec-time--large" role="timer" aria-label="Recorded time">
          {formatElapsed(live.elapsedMs)}
        </span>
      </div>
      <LevelMeter analyser={live.analyser} listening={recording} />
      <p
        className="muted"
        role="status"
        aria-live="polite"
        style={{ fontSize: 12.5, marginTop: 10 }}
      >
        {savedLabel(here)}
      </p>
      <div style={{ display: "flex", gap: 8, marginTop: 14, flexWrap: "wrap" }}>
        <button className="btn btn--primary" onClick={handleEnd}>
          End session
        </button>
        {here.phase === "recording" && (
          <button className="btn" onClick={live.pause}>
            Pause
          </button>
        )}
        {here.phase === "paused" && (
          <button className="btn" onClick={live.resume}>
            Resume
          </button>
        )}
        <button className="btn btn--subtle" onClick={handleDiscard}>
          Discard recording
        </button>
      </div>
    </div>
  );
}
