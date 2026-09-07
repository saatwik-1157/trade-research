import type { TradingMode } from "@/lib/types";

interface Props {
  mode?: TradingMode;
  liveAllowed?: boolean;
  blockers?: number;
}

/**
 * The one badge that must never be ambiguous: PAPER, DEMO or LIVE, and for
 * LIVE whether execution is actually allowed. Unknown (API down) is shown as
 * unknown, never as a default mode.
 *
 * LIVE·ARMED carries a dark label on the red fill because a white one measures
 * 3.87:1 against it and a dark one 5.02:1. Of every string in this app it is
 * the one that must survive a bad monitor.
 */
export function ModeBadge({ mode, liveAllowed, blockers }: Props) {
  if (!mode) {
    return (
      <span className="rounded border border-line px-2 py-0.5 text-body text-muted" role="status">
        MODE UNKNOWN
      </span>
    );
  }
  if (mode === "live") {
    const blocked = !liveAllowed;
    return (
      <span
        role="status"
        className={`rounded border px-2 py-0.5 text-body font-semibold ${
          blocked ? "border-critical text-critical" : "border-critical bg-critical text-plane"
        }`}
        title={
          blocked
            ? `Live mode requested but execution is blocked by ${blockers ?? "?"} gate(s)`
            : "Live execution allowed"
        }
      >
        LIVE {blocked ? "· BLOCKED" : "· ARMED"}
      </span>
    );
  }
  const cls =
    mode === "demo" ? "border-warning text-warning" : "border-series-3 text-series-3";
  return (
    <span
      role="status"
      className={`rounded border px-2 py-0.5 text-body font-semibold ${cls}`}
      title={mode === "demo" ? "MT5 demo account, broker play money" : "Internal simulator"}
    >
      {mode.toUpperCase()}
    </span>
  );
}
