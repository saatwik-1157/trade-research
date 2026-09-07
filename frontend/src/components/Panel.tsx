import { levelTag } from "@/lib/nav";

interface Props {
  title: string;
  level?: number;
  right?: React.ReactNode;
  className?: string;
  children: React.ReactNode;
}

/**
 * A panel separates from the page by its edge and its shadow, not by its fill.
 * The surface is 1.12:1 against the page and cannot usefully be lifted -- even
 * a pure-black page reaches only 1.21 -- so the border does the work, which is
 * why `--line` was raised from 1.24:1 to 1.87:1 against this surface.
 */
export function Panel({ title, level, right, className = "", children }: Props) {
  return (
    <section
      aria-label={title}
      className={`flex min-h-0 flex-col rounded-md border border-line bg-surface shadow-panel ${className}`}
    >
      <header className="flex h-9 shrink-0 items-center justify-between gap-2 border-b border-line bg-surface-2/30 px-3">
        <h2 className="truncate text-mini font-semibold uppercase tracking-wide text-ink-2">
          {title}
        </h2>
        <div className="flex shrink-0 items-center gap-2">
          {right}
          {level !== undefined && (
            <span className="font-mono text-micro text-muted">{levelTag(level)}</span>
          )}
        </div>
      </header>
      <div className="min-h-0 flex-1 p-3">{children}</div>
    </section>
  );
}
