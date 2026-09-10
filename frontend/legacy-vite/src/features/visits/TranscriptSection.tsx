import { useState } from "react";
import { getTranscript, type TranscriptOut } from "../../api/visits";

/**
 * The transcript, rendered as plain text so any line in the brief can be
 * checked against what was actually said (frontend/README.md: "the only
 * way the doctor can audit an extraction"). Loaded lazily on first expand.
 */
export function TranscriptSection({
  visitId,
  filename,
}: {
  visitId: string;
  filename?: string | null;
}) {
  const [open, setOpen] = useState(false);
  const [transcript, setTranscript] = useState<TranscriptOut | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function handleToggle(nowOpen: boolean) {
    setOpen(nowOpen);
    if (nowOpen && !transcript && !loading) {
      setLoading(true);
      setError(null);
      try {
        const data = await getTranscript(visitId);
        setTranscript(data);
      } catch {
        setError("Could not load the transcript.");
      } finally {
        setLoading(false);
      }
    }
  }

  return (
    <details
      className="section"
      id="transcript"
      open={open}
      onToggle={(e) => handleToggle(e.currentTarget.open)}
    >
      <summary>
        <svg className="chev" width="12" height="12" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.8" aria-hidden="true">
          <path d="M6 3.5 10.5 8 6 12.5" />
        </svg>
        Transcript
        <span className="section-note">
          {filename ?? "transcript"}
          {transcript ? ` \u00b7 ${transcript.character_count.toLocaleString()} characters` : ""}
        </span>
      </summary>
      <div className="section-body">
        {loading && (
          <div className="page-loading" style={{ padding: "20px 0" }}>
            <span className="spinner" aria-hidden="true" />
            Loading transcript&hellip;
          </div>
        )}
        {error && <div className="error-banner">{error}</div>}
        {transcript && (
          <pre
            style={{
              whiteSpace: "pre-wrap",
              fontFamily: "var(--mono)",
              fontSize: 12.5,
              lineHeight: 1.6,
              color: "var(--ink-2)",
              margin: 0,
            }}
          >
            {transcript.text}
          </pre>
        )}
      </div>
    </details>
  );
}
