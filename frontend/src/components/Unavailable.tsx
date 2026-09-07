import { levelTag } from "@/lib/nav";

interface Props {
  level: number;
  reason: string;
  /** Existing CLI or file that does this job today, if any. */
  today?: string;
  /** Render as an overlay over a placeholder (e.g. an empty chart). */
  overlay?: boolean;
  /** Controls to render disabled beneath the notice. */
  children?: React.ReactNode;
}

/**
 * A feature that is not built yet. It says which level builds it and why it
 * is absent, and anything rendered inside is a real disabled control, so the
 * page never suggests a capability it does not have.
 */
export function Unavailable({ level, reason, today, overlay = false, children }: Props) {
  const notice = (
    <div
      role="note"
      className={`flex flex-col gap-1.5 rounded-md border border-dashed border-baseline px-3 py-2.5 text-body ${
        overlay ? "bg-surface/90 backdrop-blur-sm" : "bg-surface-2/40"
      }`}
    >
      <div className="flex items-center gap-2">
        <span className="rounded bg-baseline px-1.5 py-0.5 font-mono text-micro text-ink">
          {levelTag(level)}
        </span>
        <span className="font-medium text-ink-2">Not available yet</span>
      </div>
      <p className="text-muted">{reason}</p>
      {today && (
        <p className="text-muted">
          Today: <code className="font-mono text-ink-2">{today}</code>
        </p>
      )}
    </div>
  );

  if (overlay) {
    return (
      <div className="pointer-events-none absolute inset-0 flex items-center justify-center p-4">
        {notice}
      </div>
    );
  }
  return (
    <div className="flex flex-col gap-3">
      {notice}
      {children && (
        <fieldset disabled aria-disabled="true" className="m-0 min-w-0 border-0 p-0">
          {children}
        </fieldset>
      )}
    </div>
  );
}
