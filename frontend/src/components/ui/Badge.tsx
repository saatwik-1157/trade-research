export type BadgeTone = "neutral" | "good" | "warning" | "critical" | "accent";

const TONES: Record<BadgeTone, string> = {
  neutral: "border-line text-muted",
  good: "border-good text-good",
  warning: "border-warning text-warning",
  critical: "border-critical text-critical",
  accent: "border-accent text-accent",
};

export function Badge({
  tone = "neutral",
  children,
  title,
}: {
  tone?: BadgeTone;
  children: React.ReactNode;
  title?: string;
}) {
  return (
    <span
      title={title}
      className={`inline-block rounded border px-1.5 py-0.5 text-mini font-semibold uppercase tracking-wide ${TONES[tone]}`}
    >
      {children}
    </span>
  );
}
