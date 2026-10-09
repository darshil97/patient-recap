"use client";

import { useRef, useState, type DragEvent } from "react";
import { AUDIO_EXTENSIONS } from "@/lib/api/audioUploads";

/** Text transcripts plus every recording format the backend transcribes. */
const ACCEPT = [".txt", "text/plain", ...AUDIO_EXTENSIONS].join(",");

/**
 * The "Add transcript" dropzone. Takes either a plain-text transcript (.txt,
 * 256 KB) or a consultation recording, which the backend transcribes and labels
 * by speaker. Both are validated server-side; this component just previews the
 * choice and lets the caller decide what to do with the file.
 */
export function TranscriptDropzone({
  onFileSelected,
  busy,
  busyLabel = "Uploading…",
}: {
  onFileSelected: (file: File) => void;
  busy: boolean;
  busyLabel?: string;
}) {
  const inputRef = useRef<HTMLInputElement>(null);
  const [dragActive, setDragActive] = useState(false);

  function handleDrop(e: DragEvent<HTMLDivElement>) {
    e.preventDefault();
    setDragActive(false);
    if (busy) return;
    const file = e.dataTransfer.files?.[0];
    if (file) onFileSelected(file);
  }

  return (
    <div
      className={`dropzone ${dragActive ? "dropzone--active" : ""}`}
      onDragOver={(e) => {
        e.preventDefault();
        setDragActive(true);
      }}
      onDragLeave={() => setDragActive(false)}
      onDrop={handleDrop}
      aria-busy={busy}
    >
      <svg
        width="26"
        height="26"
        viewBox="0 0 24 24"
        fill="none"
        stroke="currentColor"
        strokeWidth="1.4"
        style={{ color: "var(--ink-3)" }}
        aria-hidden="true"
      >
        <path d="M12 15.5V4.5M8 8l4-3.5L16 8" />
        <path d="M4.5 14v4a1.5 1.5 0 0 0 1.5 1.5h12a1.5 1.5 0 0 0 1.5-1.5v-4" />
      </svg>
      <p style={{ fontWeight: 600, marginTop: 8 }}>Drop the recording or transcript here</p>
      <p style={{ color: "var(--ink-2)", fontSize: 13, marginTop: 4 }}>
        An audio recording (MP3, M4A, WAV and similar), or a plain text{" "}
        <code>.txt</code> transcript up to 256 KB.
      </p>
      <button
        type="button"
        className="btn btn--primary"
        style={{ marginTop: 12 }}
        disabled={busy}
        onClick={() => inputRef.current?.click()}
      >
        {busy ? busyLabel : "Choose file"}
      </button>
      <input
        ref={inputRef}
        type="file"
        accept={ACCEPT}
        aria-label="Choose a recording or transcript file"
        onChange={(e) => {
          const file = e.target.files?.[0];
          if (file) onFileSelected(file);
          e.target.value = "";
        }}
      />
    </div>
  );
}
