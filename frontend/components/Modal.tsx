"use client";

import { useEffect, useRef, type ReactNode } from "react";

/** Everything focusable, in DOM order. Used to find the trap's two ends. */
const FOCUSABLE =
  'a[href], button:not([disabled]), input:not([disabled]), select:not([disabled]), ' +
  'textarea:not([disabled]), [tabindex]:not([tabindex="-1"])';

export function Modal({
  title,
  subtitle,
  onClose,
  children,
  wide = false,
}: {
  title: string;
  subtitle?: string;
  onClose: () => void;
  children: ReactNode;
  wide?: boolean;
}) {
  const dialogRef = useRef<HTMLDivElement>(null);

  // `aria-modal` tells assistive tech that everything behind is inert. It does
  // not make it so: without moving focus, a keyboard user opening this stayed on
  // the trigger behind the overlay and tabbed through a form they could not see.
  // Three things are needed and none of them are the attribute — move focus in,
  // keep Tab inside, and put focus back where it came from on close.
  useEffect(() => {
    const opener = document.activeElement as HTMLElement | null;
    const dialog = dialogRef.current;

    const focusable = () =>
      Array.from(dialog?.querySelectorAll<HTMLElement>(FOCUSABLE) ?? []).filter(
        (el) => el.offsetParent !== null,
      );

    // The first field, not the close button: the point of opening this is to
    // fill it in.
    const first = focusable()[0];
    first?.focus();

    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        onClose();
        return;
      }
      if (e.key !== "Tab") return;

      const items = focusable();
      if (items.length === 0) return;
      const start = items[0];
      const end = items[items.length - 1];
      const active = document.activeElement;

      // Wrap at both ends. Focus that has escaped the dialog entirely -- which
      // happens if the element it was on is removed -- is pulled back in.
      if (e.shiftKey && (active === start || !dialog?.contains(active))) {
        e.preventDefault();
        end.focus();
      } else if (!e.shiftKey && (active === end || !dialog?.contains(active))) {
        e.preventDefault();
        start.focus();
      }
    };

    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("keydown", onKey);
      // Back to whatever opened the dialog, so closing does not dump a keyboard
      // user at the top of the document.
      opener?.focus?.();
    };
  }, [onClose]);

  return (
    <div
      className="modal-overlay"
      onMouseDown={(e) => {
        if (e.target === e.currentTarget) onClose();
      }}
    >
      <div
        ref={dialogRef}
        className={`modal ${wide ? "modal--wide" : ""}`}
        role="dialog"
        aria-modal="true"
        aria-label={title}
      >
        <div className="modal-head">
          <div>
            <h2 className="modal-title">{title}</h2>
            {subtitle && <p className="modal-sub">{subtitle}</p>}
          </div>
          <button
            type="button"
            className="btn btn--sm btn--subtle btn--icon"
            aria-label="Close dialog"
            onClick={onClose}
          >
            <svg
              width="14"
              height="14"
              viewBox="0 0 16 16"
              fill="none"
              stroke="currentColor"
              strokeWidth="1.6"
              aria-hidden="true"
            >
              <path d="M4 4l8 8M12 4l-8 8" />
            </svg>
          </button>
        </div>
        {children}
      </div>
    </div>
  );
}

export function ModalBody({ children }: { children: ReactNode }) {
  return <div className="modal-body">{children}</div>;
}

export function ModalFoot({ children }: { children: ReactNode }) {
  return <div className="modal-foot">{children}</div>;
}
