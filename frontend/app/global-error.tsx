"use client";

/**
 * The last resort: an error thrown by the root layout itself, which `error.tsx`
 * cannot catch because it renders *inside* that layout.
 *
 * It has to supply its own <html> and <body>, and it cannot rely on the app's
 * stylesheet having loaded — so the styling here is inline and deliberately
 * plain rather than reaching for design tokens that may not exist yet.
 */
export default function GlobalError({
  error,
  reset,
}: {
  error: Error & { digest?: string };
  reset: () => void;
}) {
  return (
    <html lang="en">
      <body
        style={{
          fontFamily: "system-ui, -apple-system, sans-serif",
          color: "#1a2b33",
          background: "#fff",
          margin: 0,
          padding: "48px 24px",
        }}
      >
        <main style={{ maxWidth: "48ch", margin: "0 auto" }}>
          <h1 style={{ fontSize: 20, marginBottom: 12 }}>
            Consultation Insights could not start
          </h1>
          <p style={{ lineHeight: 1.6, color: "#4a5b63" }}>
            The application failed to load. No patient data has been changed.
            Reload the page, and if it keeps happening the service may be down.
          </p>
          {error.digest && (
            <p style={{ fontSize: 12, color: "#7a8b93" }}>
              Reference: <code>{error.digest}</code>
            </p>
          )}
          <button
            onClick={reset}
            style={{
              marginTop: 20,
              padding: "8px 14px",
              fontSize: 14,
              cursor: "pointer",
              borderRadius: 6,
              border: "1px solid #1a2b33",
              background: "#1a2b33",
              color: "#fff",
            }}
          >
            Reload
          </button>
        </main>
      </body>
    </html>
  );
}
