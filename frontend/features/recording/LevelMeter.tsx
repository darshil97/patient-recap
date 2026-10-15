"use client";

import { useEffect, useState } from "react";
import { SilenceDetector, rmsLevel } from "@/lib/recording/level";

/** Updates per second. The meter only needs to look alive. */
const RATE_HZ = 8;

/**
 * Shows that the microphone hears the room, and warns when it has not for a
 * while (muted, the wrong device, unplugged without the browser noticing).
 * `listening` is false while paused, so a pause does not trip the warning.
 */
export function LevelMeter({
  analyser,
  listening,
}: {
  analyser: AnalyserNode | null;
  listening: boolean;
}) {
  const [level, setLevel] = useState(0);
  const [silent, setSilent] = useState(false);

  useEffect(() => {
    if (!analyser || !listening) return;
    const samples = new Uint8Array(analyser.fftSize);
    const detector = new SilenceDetector(performance.now());
    let frame = 0;
    let last = 0;
    const tick = (time: number) => {
      frame = requestAnimationFrame(tick);
      if (time - last < 1000 / RATE_HZ) return;
      last = time;
      analyser.getByteTimeDomainData(samples);
      const value = rmsLevel(samples);
      setLevel(value);
      setSilent(detector.update(value, time));
    };
    frame = requestAnimationFrame(tick);
    return () => {
      cancelAnimationFrame(frame);
      setLevel(0);
      setSilent(false);
    };
  }, [analyser, listening]);

  if (!analyser) return null;
  const pct = Math.round(level * 100);
  return (
    <div style={{ marginTop: 12 }}>
      <div
        className="meter"
        role="meter"
        aria-label="Microphone level"
        aria-valuemin={0}
        aria-valuemax={100}
        aria-valuenow={pct}
      >
        <span style={{ width: `${Math.max(pct, 2)}%` }} />
      </div>
      {silent && (
        <div className="notice" role="alert" style={{ marginTop: 10, marginBottom: 0 }}>
          <svg width="14" height="14" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.6" aria-hidden="true">
            <path d="M8 2.8 14.2 13H1.8L8 2.8Z" />
            <path d="M8 6.6v2.9M8 11.2h.01" />
          </svg>
          <span>
            <span className="notice-title">No sound for 30 seconds.</span> Check that the
            microphone is switched on, not muted, and is the one the browser is using.
          </span>
        </div>
      )}
    </div>
  );
}
