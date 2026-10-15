"use client";

import { useEffect } from "react";
import { APP_HOME } from "@/lib/routes";

/**
 * Route-level error boundary.
 *
 * There was none, anywhere, which meant any render-time exception blanked the
 * whole application — no message, no way back. That is bad in any app and worse
 * in a clinical one, where the failure a doctor sees is indistinguishable from
 * the data being gone.
 *
 * Next calls this with the error and a `reset` that re-renders the segment.
 * Reset is offered first because the common causes here are transient: a fetch
 * that failed mid-render, a session that expired between navigation and paint.
 */
export default function Error({
  error,
  reset,
}: {
  error: Error & { digest?: string };
  reset: () => void;
}) {
  useEffect(() => {
    // Server-side digest only in production; the message is here in dev.
    console.error("Unhandled error in a route segment:", error);
  }, [error]);

  return (
    <div className="page">
      <div className="card">
        <div className="card-head">
          <h1 className="card-title">Something went wrong on this page</h1>
        </div>
        <div className="card-body">
          <p style={{ color: "var(--ink-2)", maxWidth: "60ch" }}>
            The page could not be displayed. Nothing has been changed or lost —
            this is a display failure, not a failure to save.
          </p>
          {error.digest && (
            <p className="muted" style={{ fontSize: 12, marginTop: 8 }}>
              Reference: <code>{error.digest}</code>
            </p>
          )}
          <div style={{ display: "flex", gap: 8, marginTop: 16 }}>
            <button className="btn btn--primary" onClick={reset}>
              Try again
            </button>
            {/* A plain <a>, not <Link>: a deliberate full page load. This is the
                escape hatch from a render that already failed, so client-side
                navigation is exactly what should not be relied on: <Link> keeps
                the same broken React tree alive, and the state that caused the
                throw with it. (@next/next/no-html-link-for-pages only inspects
                string literals, so it no longer needs disabling here now that
                the href is APP_HOME.) */}
            <a className="btn btn--subtle" href={APP_HOME}>
              Back to Today
            </a>
          </div>
        </div>
      </div>
    </div>
  );
}
