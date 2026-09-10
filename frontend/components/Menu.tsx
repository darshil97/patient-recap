"use client";

import { useEffect, useRef, useState, type ReactNode } from "react";

export interface MenuItemDef {
  label: string;
  onClick: () => void;
  danger?: boolean;
  disabled?: boolean;
}

/** The "..." more-actions dropdown used on queue rows. */
export function Menu({
  label,
  items,
}: {
  label: string;
  items: MenuItemDef[];
}) {
  const [open, setOpen] = useState(false);
  const ref = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!open) return;
    const onClick = (e: MouseEvent) => {
      if (ref.current && !ref.current.contains(e.target as Node)) {
        setOpen(false);
      }
    };
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") setOpen(false);
    };
    document.addEventListener("mousedown", onClick);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("mousedown", onClick);
      document.removeEventListener("keydown", onKey);
    };
  }, [open]);

  return (
    <div className="menu-wrap" ref={ref}>
      <button
        type="button"
        className="btn btn--sm btn--subtle"
        aria-label={label}
        aria-haspopup="menu"
        aria-expanded={open}
        onClick={() => setOpen((o) => !o)}
      >
        &hellip;
      </button>
      {open && (
        <div className="menu" role="menu">
          {items.map((item, i) => (
            <MenuButton key={i} item={item} onClose={() => setOpen(false)} />
          ))}
        </div>
      )}
    </div>
  );
}

function MenuButton({
  item,
  onClose,
}: {
  item: MenuItemDef;
  onClose: () => void;
}) {
  return (
    <button
      type="button"
      role="menuitem"
      className={`menu-item ${item.danger ? "menu-item--danger" : ""}`}
      disabled={item.disabled}
      onClick={() => {
        onClose();
        item.onClick();
      }}
    >
      {item.label}
    </button>
  );
}

export function MenuSeparator(): ReactNode {
  return <div className="menu-sep" />;
}
