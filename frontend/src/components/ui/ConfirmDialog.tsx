"use client";

import { useEffect, useRef } from "react";
import { Button } from "./Button";

/**
 * Confirmation for a dangerous action.
 *
 * A native <dialog>, so Escape closes it and focus is trapped without a
 * library. The confirm button is never the default focus: a dangerous action
 * should not be one stray Enter away.
 */
export function ConfirmDialog({
  open,
  title,
  body,
  confirmLabel = "Confirm",
  danger = false,
  onConfirm,
  onCancel,
}: {
  open: boolean;
  title: string;
  body: React.ReactNode;
  confirmLabel?: string;
  danger?: boolean;
  onConfirm: () => void;
  onCancel: () => void;
}) {
  const ref = useRef<HTMLDialogElement>(null);
  const cancelRef = useRef<HTMLButtonElement>(null);

  useEffect(() => {
    const el = ref.current;
    if (!el) return;
    if (open && !el.open) {
      // jsdom implements <dialog> but not showModal; fall back to the open
      // attribute so the dialog is testable without a browser.
      if (typeof el.showModal === "function") el.showModal();
      else el.setAttribute("open", "");
      cancelRef.current?.focus();
    } else if (!open && el.open) {
      if (typeof el.close === "function") el.close();
      else el.removeAttribute("open");
    }
  }, [open]);

  return (
    <dialog
      ref={ref}
      aria-label={title}
      onCancel={(e) => {
        e.preventDefault();
        onCancel();
      }}
      className="rounded border border-line bg-surface p-0 text-ink backdrop:bg-plane/70"
    >
      <div className="w-80 p-4">
        <h2 className="text-sm font-semibold">{title}</h2>
        <div className="mt-2 text-body text-ink-2">{body}</div>
        <div className="mt-4 flex justify-end gap-2">
          {/* A plain button so the ref lands on the element; the confirm
              button never takes initial focus. */}
          <button
            ref={cancelRef}
            type="button"
            onClick={onCancel}
            className="rounded border border-line bg-surface-2 px-2 py-1 text-body font-medium text-ink-2 hover:text-ink focus-visible:outline focus-visible:outline-2 focus-visible:outline-accent"
          >
            Cancel
          </button>
          <Button variant={danger ? "danger" : "primary"} size="sm" onClick={onConfirm}>
            {confirmLabel}
          </Button>
        </div>
      </div>
    </dialog>
  );
}
