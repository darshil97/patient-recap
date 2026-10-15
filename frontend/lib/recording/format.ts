/**
 * Which container the browser's MediaRecorder writes, and the extension that
 * tells the backend what it is (backend/app/domain/audio.py decides by
 * extension). Chrome, Edge and Firefox record WebM/Opus; Safari records MP4.
 */

export interface RecordingFormat {
  mimeType: string;
  extension: ".webm" | ".mp4" | ".ogg";
}

/** In order of preference. Opus in WebM is the smallest at speech bitrates. */
const CANDIDATES: RecordingFormat[] = [
  { mimeType: "audio/webm;codecs=opus", extension: ".webm" },
  { mimeType: "audio/webm", extension: ".webm" },
  { mimeType: "audio/mp4", extension: ".mp4" },
  { mimeType: "audio/ogg;codecs=opus", extension: ".ogg" },
];

/**
 * 32 kbps mono is plenty for speech, and keeps an hour near 14 MB. Browsers
 * default to several times that, which would reach the 100 MB ceiling in a long
 * consultation and make each chunk cover less of it.
 */
export const RECORDING_BITS_PER_SECOND = 32_000;

/**
 * How often the recorder hands over data. Small enough that a crash loses
 * little beyond the chunk being assembled, large enough not to churn.
 */
export const RECORDER_TIMESLICE_MS = 10_000;

/**
 * Raw audio, as unprocessed as the browser allows: echo cancellation and noise
 * suppression are tuned for calls and can clip the far voice in a room, which
 * is the patient. Automatic gain stays on so a quieter speaker is still heard.
 */
export const MICROPHONE_CONSTRAINTS: MediaStreamConstraints = {
  audio: {
    channelCount: 1,
    echoCancellation: false,
    noiseSuppression: false,
    autoGainControl: true,
  },
};

export function pickRecordingFormat(
  isSupported: (mimeType: string) => boolean = (type) =>
    typeof MediaRecorder !== "undefined" && MediaRecorder.isTypeSupported(type),
): RecordingFormat | null {
  return CANDIDATES.find((candidate) => isSupported(candidate.mimeType)) ?? null;
}

/** Whether this browser can record at all: a microphone API and MediaRecorder. */
export function canRecordInBrowser(): boolean {
  return (
    typeof window !== "undefined" &&
    typeof MediaRecorder !== "undefined" &&
    Boolean(navigator.mediaDevices?.getUserMedia) &&
    pickRecordingFormat() !== null
  );
}
