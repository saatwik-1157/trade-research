"use client";

import { useEffect, useRef, useState } from "react";
import { usePathname } from "next/navigation";
import { Brand, NavLegend, NavList } from "./Sidebar";

/**
 * Navigation below `md`, where the sidebar is hidden.
 *
 * Nothing replaced it. Under 768px the app rendered one page and no way to
 * leave it -- every route reachable only by typing its URL. That is not a
 * styling gap; it is the whole application being unusable on a phone, which
 * is the sort of thing a desktop-only review never sees.
 *
 * It renders `NavList`, the same list the rail renders, so a route added to
 * `NAV` appears in both or in neither.
 */
export function MobileNav() {
  const pathname = usePathname();
  const panel = useRef<HTMLDivElement>(null);
  const trigger = useRef<HTMLButtonElement>(null);

  /**
   * Open is stored as the route it was opened ON, so a route change closes it
   * by arithmetic rather than by an effect that fires after the new page has
   * already painted underneath it. It also covers back and forward, which a
   * close-on-link-click would miss.
   */
  const [openedAt, setOpenedAt] = useState<string | null>(null);
  const open = openedAt !== null && openedAt === pathname;
  const setOpen = (next: boolean) => setOpenedAt(next ? pathname : null);

  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        // `setOpenedAt` rather than the `setOpen` helper: the setter is stable
        // across renders, so the effect does not need to re-subscribe.
        setOpenedAt(null);
        trigger.current?.focus();
      }
    };
    document.addEventListener("keydown", onKey);
    // The page behind a modal drawer must not scroll under it.
    const previous = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    panel.current?.focus();
    return () => {
      document.removeEventListener("keydown", onKey);
      document.body.style.overflow = previous;
    };
  }, [open]);

  return (
    <>
      <button
        ref={trigger}
        type="button"
        aria-label="Open navigation"
        aria-expanded={open}
        aria-controls="mobile-nav"
        onClick={() => setOpen(true)}
        className="-ml-1 rounded p-1.5 text-ink-2 transition-colors hover:bg-surface-2 hover:text-ink md:hidden"
      >
        {/* Drawn rather than imported: three rects cost nothing and this app
            has no icon dependency to justify adding for one glyph. */}
        <svg width="18" height="18" viewBox="0 0 18 18" aria-hidden focusable="false">
          <rect x="2" y="4" width="14" height="1.5" rx="0.75" fill="currentColor" />
          <rect x="2" y="8.25" width="14" height="1.5" rx="0.75" fill="currentColor" />
          <rect x="2" y="12.5" width="14" height="1.5" rx="0.75" fill="currentColor" />
        </svg>
      </button>

      {open && (
        <div className="fixed inset-0 z-50 md:hidden">
          <button
            type="button"
            aria-label="Close navigation"
            tabIndex={-1}
            onClick={() => setOpen(false)}
            className="absolute inset-0 h-full w-full cursor-default bg-plane/70"
          />
          <div
            id="mobile-nav"
            ref={panel}
            role="dialog"
            aria-modal="true"
            aria-label="Primary"
            tabIndex={-1}
            className="absolute inset-y-0 left-0 flex w-64 max-w-[85vw] flex-col border-r border-line bg-surface shadow-raised outline-none"
          >
            <div className="flex h-12 shrink-0 items-center justify-between gap-2 border-b border-line px-4">
              <span className="flex items-center gap-2">
                <Brand />
              </span>
              <button
                type="button"
                aria-label="Close navigation"
                onClick={() => setOpen(false)}
                className="rounded p-1 text-muted transition-colors hover:bg-surface-2 hover:text-ink"
              >
                <svg width="16" height="16" viewBox="0 0 16 16" aria-hidden focusable="false">
                  <path
                    d="M4 4l8 8M12 4l-8 8"
                    stroke="currentColor"
                    strokeWidth="1.5"
                    strokeLinecap="round"
                  />
                </svg>
              </button>
            </div>
            <nav className="flex-1 overflow-y-auto py-2">
              <NavList onNavigate={() => setOpen(false)} />
            </nav>
            <NavLegend />
          </div>
        </div>
      )}
    </>
  );
}
