"use client";

import { useRef, useState, type DragEvent } from "react";

/**
 * The "Add transcript" dropzone. Per frontend/README.md, .txt only, 256 KB
 * ceiling — validated server-side; this component just previews the choice
 * and lets the caller decide what to do with the file.
 */
export function TranscriptDropzone({
  onFileSelected,
  busy,
}: {
  onFileSelected: (file: File) => void;
  busy: boolean;
}) {
  const inputRef = useRef<HTMLInputElement>(null);
  const [dragActive, setDragActive] = useState(false);

  function handleDrop(e: DragEvent<HTMLDivElement>) {
    e.preventDefault();
    setDragActive(false);
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
      <p style={{ fontWeight: 600, marginTop: 8 }}>Drop the transcript here</p>
      <p style={{ color: "var(--ink-2)", fontSize: 13, marginTop: 4 }}>
        Plain text <code>.txt</code> file, up to 256 KB.
      </p>
      <button
        type="button"
        className="btn btn--primary"
        style={{ marginTop: 12 }}
        disabled={busy}
        onClick={() => inputRef.current?.click()}
      >
        {busy ? "Uploading…" : "Choose file"}
      </button>
      <input
        ref={inputRef}
        type="file"
        accept=".txt,text/plain"
        onChange={(e) => {
          const file = e.target.files?.[0];
          if (file) onFileSelected(file);
          e.target.value = "";
        }}
      />
    </div>
  );
}
