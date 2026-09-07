interface Props {
  label: string;
  /** Undefined means "no measurement exists"; rendered as an em dash. */
  value?: string | number | null;
  note?: string;
  tone?: "default" | "good" | "warning" | "critical";
}

const toneCls = {
  default: "text-ink",
  good: "text-good",
  warning: "text-warning",
  critical: "text-critical",
} as const;

/**
 * The value is the reason the tile exists, so it is sized like it: 24px
 * against the 10px label. At the previous 18px it was two steps from body
 * text and a row of tiles read as a paragraph.
 */
export function StatTile({ label, value, note, tone = "default" }: Props) {
  const empty = value === undefined || value === null || value === "";
  return (
    <div className="flex flex-col gap-1 rounded-md border border-line bg-surface px-3 py-2.5 shadow-panel">
      <div className="text-mini font-semibold uppercase tracking-wider text-muted">{label}</div>
      <div
        className={`tabular text-2xl font-semibold leading-none ${empty ? "text-muted" : toneCls[tone]}`}
        aria-label={empty ? `${label}: no data` : undefined}
      >
        {empty ? "—" : value}
      </div>
      {note && <div className="text-body leading-snug text-muted">{note}</div>}
    </div>
  );
}
