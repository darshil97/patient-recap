"use client";

import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useRef,
  useState,
  type ReactNode,
} from "react";
import {
  discardAudioUpload,
  finishWithRecovery,
  isTransient,
  putAudioChunk,
  startLiveRecording,
  withRetry,
  type AudioUploadOut,
} from "@/lib/api/audioUploads";
import { completeVisit } from "@/lib/api/visits";
import { ApiError } from "@/lib/api/client";
import { ExactChunker } from "@/lib/recording/chunker";
import {
  MICROPHONE_CONSTRAINTS,
  RECORDER_TIMESLICE_MS,
  RECORDING_BITS_PER_SECOND,
  pickRecordingFormat,
} from "@/lib/recording/format";
import { LiveUploader, type UploaderSnapshot } from "@/lib/recording/liveUploader";

/**
 * The live consultation recorder (docs/PROJECT_CONTEXT.md §8.24).
 *
 * Lives in the authenticated layout rather than on the visit page, so the
 * doctor can open the patient's previous brief mid-consultation without
 * stopping the recording. One session at a time: there is one microphone and
 * one consultation in the room.
 *
 * Audio is cut into exact pieces and sent while recording, through the same BFF
 * and chunk route as a file upload, so a closed tab costs at most the piece
 * being assembled. Nothing here talks to anything but this app's own /api.
 */

export type LivePhase =
  /** Asking for the microphone, opening the upload. */
  | "starting"
  | "recording"
  | "paused"
  /** The recorder stopped by itself (microphone lost, size ceiling); what was
   *  saved can still be processed with End session. */
  | "stopped"
  /** End session: last piece, wait for every piece, complete, finish. */
  | "finishing"
  /** Nothing more can be done here; `error` says why. */
  | "failed";

export interface LiveSession {
  visitId: string;
  patientName: string;
  visitNumber: number;
  phase: LivePhase;
  upload: AudioUploadOut | null;
  /** Recorded time before the current segment; paused time is not counted. */
  activeMs: number;
  /** When the current recording segment started, or null while not recording. */
  segmentStart: number | null;
  uploader: UploaderSnapshot;
  notice: string | null;
  error: string | null;
}

export interface StartableVisit {
  visit_id: string;
  patient_name: string;
  visit_number: number;
  transcript_path?: string | null;
}

interface LiveSessionContextValue {
  session: LiveSession | null;
  /** Recorded time so far, updated about once a second. */
  elapsedMs: number;
  /** For the level meter; null when not recording. */
  analyser: AnalyserNode | null;
  /** Resolves with an error to show, or null once recording. */
  start: (visit: StartableVisit) => Promise<string | null>;
  pause: () => void;
  resume: () => void;
  /** End session. Resolves with the upload handed to processing, or null if it
   *  could not finish (the session then says why). */
  end: () => Promise<AudioUploadOut | null>;
  /** Stop and throw the recording away. Resolves with an error to show, or null. */
  discard: () => Promise<string | null>;
  /** Close a session that has failed. */
  dismiss: () => void;
}

const EMPTY_SNAPSHOT: UploaderSnapshot = {
  acknowledged: 0,
  acknowledgedBytes: 0,
  waiting: 0,
  retrying: false,
  failure: null,
};

/** Everything that is not render state: devices, the recorder, the queue. */
interface Runtime {
  visitId: string;
  uploadId: string;
  stream: MediaStream;
  recorder: MediaRecorder;
  chunker: ExactChunker;
  uploader: LiveUploader;
  audioContext: AudioContext | null;
  wakeLock: WakeLockSentinel | null;
  /** Resolves once the recorder has stopped and its last piece is queued. */
  stopped: Promise<void>;
  discarding: boolean;
}

const LiveSessionContext = createContext<LiveSessionContextValue | null>(null);

export function useLiveSession(): LiveSessionContextValue {
  const ctx = useContext(LiveSessionContext);
  if (!ctx) throw new Error("useLiveSession must be used within LiveSessionProvider");
  return ctx;
}

function microphoneError(err: unknown): string {
  const name = err instanceof DOMException ? err.name : "";
  if (name === "NotAllowedError" || name === "SecurityError") {
    return "Microphone access is blocked for this site. Allow it from the browser's address bar, then try again.";
  }
  if (name === "NotFoundError" || name === "OverconstrainedError") {
    return "No microphone was found. Connect one and try again.";
  }
  if (name === "NotReadableError") {
    return "The microphone could not be started. Another application may be using it.";
  }
  return "The microphone could not be started.";
}

function stopAfter(session: LiveSession, now: number): LiveSession {
  return {
    ...session,
    activeMs: session.activeMs + (session.segmentStart !== null ? now - session.segmentStart : 0),
    segmentStart: null,
  };
}

export function LiveSessionProvider({ children }: { children: ReactNode }) {
  const [session, setSession] = useState<LiveSession | null>(null);
  const [analyser, setAnalyser] = useState<AnalyserNode | null>(null);
  const [now, setNow] = useState(0);
  const runtimeRef = useRef<Runtime | null>(null);

  const update = useCallback((change: (s: LiveSession) => LiveSession) => {
    setSession((s) => (s ? change(s) : s));
  }, []);

  /** Microphone, audio graph and wake lock off. Safe to call more than once. */
  const releaseDevices = useCallback((rt: Runtime) => {
    rt.stream.getTracks().forEach((track) => track.stop());
    void rt.audioContext?.close().catch(() => {});
    rt.audioContext = null;
    void rt.wakeLock?.release().catch(() => {});
    rt.wakeLock = null;
    setAnalyser(null);
  }, []);

  const stopRecorder = useCallback(async (rt: Runtime) => {
    if (rt.recorder.state !== "inactive") rt.recorder.stop();
    await rt.stopped;
  }, []);

  const teardown = useCallback(() => {
    const rt = runtimeRef.current;
    runtimeRef.current = null;
    if (!rt) return;
    rt.discarding = true;
    if (rt.recorder.state !== "inactive") rt.recorder.stop();
    rt.uploader.stop();
    releaseDevices(rt);
  }, [releaseDevices]);

  /** The server refused a piece for good. */
  const handleFatal = useCallback(
    (err: unknown) => {
      const rt = runtimeRef.current;
      if (!rt) return;
      const status = err instanceof ApiError ? err.status : 0;
      void stopRecorder(rt);
      const at = Date.now();
      if (status === 409) {
        update((s) => ({
          ...stopAfter(s, at),
          phase: "failed",
          error:
            "This recording was ended elsewhere: the visit was cancelled, or the recording " +
            "discarded in another tab. Nothing more can be saved to it.",
        }));
      } else if (status === 401) {
        update((s) => ({
          ...stopAfter(s, at),
          phase: "failed",
          error:
            "Your sign-in expired, so recording stopped. Sign in again and open this visit: " +
            "everything saved so far can still be processed.",
        }));
      } else {
        update((s) => ({
          ...stopAfter(s, at),
          phase: "stopped",
          notice:
            status === 413
              ? "The recording reached the 100 MB limit, so recording stopped. End the session to process what was saved."
              : `Recording stopped: ${err instanceof ApiError ? err.message : "a part could not be saved"}. End the session to process what was saved.`,
        }));
      }
    },
    [stopRecorder, update],
  );

  const start = useCallback(
    async (visit: StartableVisit): Promise<string | null> => {
      if (runtimeRef.current) {
        return "A recording is already in progress. End it before starting another.";
      }
      const format = pickRecordingFormat();
      if (!format || !navigator.mediaDevices?.getUserMedia) {
        return "This browser cannot record audio. Use a current version of Chrome, Edge, Firefox or Safari.";
      }

      setSession({
        visitId: visit.visit_id,
        patientName: visit.patient_name,
        visitNumber: visit.visit_number,
        phase: "starting",
        upload: null,
        activeMs: 0,
        segmentStart: null,
        uploader: EMPTY_SNAPSHOT,
        notice: null,
        error: null,
      });

      let stream: MediaStream;
      try {
        stream = await navigator.mediaDevices.getUserMedia(MICROPHONE_CONSTRAINTS);
      } catch (err) {
        setSession(null);
        return microphoneError(err);
      }

      let upload: AudioUploadOut;
      try {
        upload = await startLiveRecording(visit.visit_id, {
          filename: `consultation${format.extension}`,
          replace: Boolean(visit.transcript_path),
        });
      } catch (err) {
        stream.getTracks().forEach((track) => track.stop());
        setSession(null);
        return err instanceof ApiError
          ? err.message
          : "Could not start the recording. Check the connection and try again.";
      }

      const uploadId = upload.upload_id;
      const chunker = new ExactChunker(upload.chunk_bytes);
      const uploader = new LiveUploader((index, blob) => putAudioChunk(uploadId, index, blob), {
        isTransient,
        onChange: (snapshot) => update((s) => ({ ...s, uploader: snapshot })),
        onFatal: (err) => handleFatal(err),
      });

      let recorder: MediaRecorder;
      try {
        recorder = new MediaRecorder(stream, {
          mimeType: format.mimeType,
          audioBitsPerSecond: RECORDING_BITS_PER_SECOND,
        });
      } catch {
        stream.getTracks().forEach((track) => track.stop());
        void discardAudioUpload(uploadId).catch(() => {});
        setSession(null);
        return "This browser could not start recording audio.";
      }

      let resolveStopped: () => void = () => {};
      const rt: Runtime = {
        visitId: visit.visit_id,
        uploadId,
        stream,
        recorder,
        chunker,
        uploader,
        audioContext: null,
        wakeLock: null,
        stopped: new Promise<void>((resolve) => {
          resolveStopped = resolve;
        }),
        discarding: false,
      };
      runtimeRef.current = rt;

      recorder.addEventListener("dataavailable", (event: BlobEvent) => {
        if (rt.discarding || !event.data || event.data.size === 0) return;
        for (const chunk of rt.chunker.push(event.data)) rt.uploader.enqueue(chunk);
      });
      // The last dataavailable fires before stop, so the remainder is complete
      // here. Also reached when the browser stops the recorder itself (the
      // microphone was unplugged or its permission revoked).
      recorder.addEventListener(
        "stop",
        () => {
          if (!rt.discarding) {
            const last = rt.chunker.flush();
            if (last) rt.uploader.enqueue(last);
          }
          releaseDevices(rt);
          resolveStopped();
        },
        { once: true },
      );

      for (const track of stream.getAudioTracks()) {
        track.addEventListener("ended", () => {
          if (runtimeRef.current !== rt || rt.discarding) return;
          void stopRecorder(rt);
          const at = Date.now();
          update((s) =>
            s.phase === "recording" || s.phase === "paused"
              ? {
                  ...stopAfter(s, at),
                  phase: "stopped",
                  notice:
                    "The microphone was disconnected, so recording stopped. Everything recorded " +
                    "up to then is saved: end the session to process it.",
                }
              : s,
          );
        });
      }

      // The level meter. Optional: recording does not depend on it.
      try {
        if (typeof AudioContext !== "undefined") {
          const context = new AudioContext();
          const node = context.createAnalyser();
          node.fftSize = 1024;
          context.createMediaStreamSource(stream).connect(node);
          rt.audioContext = context;
          setAnalyser(node);
        }
      } catch {
        rt.audioContext = null;
      }

      try {
        recorder.start(RECORDER_TIMESLICE_MS);
      } catch {
        teardown();
        void discardAudioUpload(uploadId).catch(() => {});
        setSession(null);
        return "This browser could not start recording audio.";
      }

      // Keep the screen awake: a sleeping laptop suspends the recorder.
      navigator.wakeLock
        ?.request("screen")
        .then((lock) => {
          if (runtimeRef.current === rt) rt.wakeLock = lock;
          else void lock.release();
        })
        .catch(() => {});

      const startedAt = Date.now();
      setNow(startedAt);
      update((s) => ({ ...s, phase: "recording", upload, segmentStart: startedAt }));
      return null;
    },
    [handleFatal, releaseDevices, stopRecorder, teardown, update],
  );

  const pause = useCallback(() => {
    const rt = runtimeRef.current;
    if (!rt || rt.recorder.state !== "recording") return;
    rt.recorder.pause();
    // Hand over what is buffered, so a pause is also a save point.
    rt.recorder.requestData();
    const at = Date.now();
    update((s) => ({ ...stopAfter(s, at), phase: "paused" }));
  }, [update]);

  const resume = useCallback(() => {
    const rt = runtimeRef.current;
    if (!rt || rt.recorder.state !== "paused") return;
    rt.recorder.resume();
    const at = Date.now();
    setNow(at);
    update((s) => ({ ...s, phase: "recording", segmentStart: at }));
  }, [update]);

  const end = useCallback(async (): Promise<AudioUploadOut | null> => {
    const rt = runtimeRef.current;
    if (!rt) return null;
    const at = Date.now();
    update((s) => ({ ...stopAfter(s, at), phase: "finishing", notice: null }));

    await stopRecorder(rt);

    let count: number;
    try {
      await rt.uploader.drain();
      count = rt.chunker.emitted;
    } catch (err) {
      const status = err instanceof ApiError ? err.status : 0;
      // 409 and 401 have already failed the session with an explanation.
      if (status === 409 || status === 401) return null;
      // Anything else stopped the queue part-way: what was confirmed is the
      // recording, and the backend works the plan out from exactly that.
      count = rt.uploader.snapshot().acknowledged;
    }

    if (count === 0) {
      teardown();
      void discardAudioUpload(rt.uploadId).catch(() => {});
      update((s) => ({
        ...s,
        phase: "failed",
        error: "No audio reached the clinic record, so there is nothing to process.",
      }));
      return null;
    }

    try {
      await withRetry(() => completeVisit(rt.visitId));
      const accepted = await finishWithRecovery(rt.uploadId, count);
      teardown();
      setSession(null);
      return accepted;
    } catch (err) {
      // Everything is stored; End session again retries just these two steps.
      update((s) => ({
        ...s,
        phase: "stopped",
        notice:
          `The recording is saved, but it could not be handed over for transcription` +
          `${err instanceof ApiError ? `: ${err.message}` : ""}. Try End session again.`,
      }));
      return null;
    }
  }, [stopRecorder, teardown, update]);

  const discard = useCallback(async (): Promise<string | null> => {
    const rt = runtimeRef.current;
    if (!rt) return null;
    teardown();
    setSession(null);
    try {
      await withRetry(() => discardAudioUpload(rt.uploadId));
      return null;
    } catch (err) {
      return err instanceof ApiError
        ? err.message
        : "The recording stopped, but could not be discarded on the server. Discard it from the visit page.";
    }
  }, [teardown]);

  const dismiss = useCallback(() => {
    teardown();
    setSession(null);
  }, [teardown]);

  // The clock, while recording.
  const ticking = session?.phase === "recording";
  useEffect(() => {
    if (!ticking) return;
    const timer = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(timer);
  }, [ticking]);

  // Leaving the app mid-session loses whatever has not been sent, and stops
  // recording. Ask first.
  const guard = Boolean(session && session.phase !== "failed");
  useEffect(() => {
    if (!guard) return;
    const warn = (e: BeforeUnloadEvent) => e.preventDefault();
    window.addEventListener("beforeunload", warn);
    return () => window.removeEventListener("beforeunload", warn);
  }, [guard]);

  // A wake lock is dropped whenever the tab is hidden; take it again on return.
  useEffect(() => {
    if (!ticking) return;
    const onVisible = () => {
      const rt = runtimeRef.current;
      if (!rt || document.visibilityState !== "visible") return;
      if (rt.wakeLock && !rt.wakeLock.released) return;
      navigator.wakeLock
        ?.request("screen")
        .then((lock) => {
          if (runtimeRef.current === rt) rt.wakeLock = lock;
          else void lock.release();
        })
        .catch(() => {});
    };
    document.addEventListener("visibilitychange", onVisible);
    return () => document.removeEventListener("visibilitychange", onVisible);
  }, [ticking]);

  // Signing out unmounts the app: release the microphone with it.
  useEffect(() => () => teardown(), [teardown]);

  const elapsedMs = session
    ? session.activeMs +
      (session.segmentStart !== null ? Math.max(0, now - session.segmentStart) : 0)
    : 0;

  const value = useMemo<LiveSessionContextValue>(
    () => ({ session, elapsedMs, analyser, start, pause, resume, end, discard, dismiss }),
    [session, elapsedMs, analyser, start, pause, resume, end, discard, dismiss],
  );

  return <LiveSessionContext.Provider value={value}>{children}</LiveSessionContext.Provider>;
}
