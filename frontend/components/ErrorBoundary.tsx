"use client";

import { Component, type ErrorInfo, type ReactNode } from "react";

/**
 * A boundary narrow enough to keep the rest of a page alive.
 *
 * `app/error.tsx` catches a throw by replacing the whole route segment. That is
 * right for a page that genuinely cannot render, and wrong for one section of a
 * page that can't — losing the visit header, the lifecycle actions and the
 * transcript because the brief's payload was an unexpected shape is a worse
 * outcome than showing the brief as unavailable.
 *
 * React only offers this as a class component; there is no hook equivalent.
 */
export class ErrorBoundary extends Component<
  { children: ReactNode; fallback: ReactNode; label?: string },
  { failed: boolean }
> {
  state = { failed: false };

  static getDerivedStateFromError() {
    return { failed: true };
  }

  componentDidCatch(error: Error, info: ErrorInfo) {
    console.error(
      `Render failed in ${this.props.label ?? "a section"}:`,
      error,
      info.componentStack,
    );
  }

  render() {
    return this.state.failed ? this.props.fallback : this.props.children;
  }
}
