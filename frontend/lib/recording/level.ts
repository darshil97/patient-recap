/**
 * Input level and silence, from an AnalyserNode's time-domain samples.
 *
 * The doctor cannot see whether the microphone hears the room. A meter that
 * moves, and a warning when it has not for a while, catches a muted or wrong
 * microphone before the whole consultation is recorded as silence.
 */

/** RMS of 8-bit time-domain samples (128 = silence), scaled to 0..1. */
export function rmsLevel(samples: Uint8Array): number {
  if (samples.length === 0) return 0;
  let sum = 0;
  for (let i = 0; i < samples.length; i++) {
    const centred = (samples[i] - 128) / 128;
    sum += centred * centred;
  }
  // Speech sits around 0.02-0.2 RMS; stretch it so the meter visibly moves.
  return Math.min(1, Math.sqrt(sum / samples.length) * 4);
}

/** Below this the input is treated as silence. Room noise is usually above it. */
export const SILENCE_LEVEL = 0.02;
export const SILENCE_WARNING_MS = 30_000;

/**
 * Says whether the input has been silent for `thresholdMs`. Paused time does
 * not count: call `reset` on resume so a pause does not trip the warning.
 */
export class SilenceDetector {
  private lastSoundAt: number;

  constructor(
    now: number,
    private readonly thresholdMs = SILENCE_WARNING_MS,
    private readonly level = SILENCE_LEVEL,
  ) {
    this.lastSoundAt = now;
  }

  update(level: number, now: number): boolean {
    if (level >= this.level) this.lastSoundAt = now;
    return now - this.lastSoundAt >= this.thresholdMs;
  }

  reset(now: number): void {
    this.lastSoundAt = now;
  }
}

/** "mm:ss", or "h:mm:ss" past an hour. */
export function formatElapsed(ms: number): string {
  const total = Math.max(0, Math.floor(ms / 1000));
  const h = Math.floor(total / 3600);
  const m = Math.floor((total % 3600) / 60);
  const s = total % 60;
  const mm = String(m).padStart(h > 0 ? 2 : 1, "0");
  const ss = String(s).padStart(2, "0");
  return h > 0 ? `${h}:${mm}:${ss}` : `${mm}:${ss}`;
}
