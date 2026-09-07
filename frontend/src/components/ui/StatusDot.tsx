/**
 * Service connection state.
 *
 * UNKNOWN is a first-class state, not a synonym for disconnected. The rule
 * this component exists to enforce: never render CONNECTED unless the
 * backend actually said so.
 */
export type ServiceState = "connected" | "degraded" | "disconnected" | "unknown";

const TONE: Record<ServiceState, { dot: string; text: string; label: string }> = {
  connected: { dot: "bg-good", text: "text-good", label: "CONNECTED" },
  degraded: { dot: "bg-warning", text: "text-warning", label: "DEGRADED" },
  disconnected: { dot: "bg-critical", text: "text-critical", label: "DISCONNECTED" },
  unknown: { dot: "bg-baseline", text: "text-muted", label: "UNKNOWN" },
};

export function StatusDot({
  state,
  label,
  detail,
}: {
  state: ServiceState;
  label?: string;
  detail?: string;
}) {
  const tone = TONE[state];
  return (
    <span className="flex items-center gap-1.5" title={detail}>
      <span className={`h-1.5 w-1.5 shrink-0 rounded-full ${tone.dot}`} aria-hidden />
      <span className={`text-micro font-semibold tracking-wide ${tone.text}`}>
        {label ?? tone.label}
      </span>
    </span>
  );
}
