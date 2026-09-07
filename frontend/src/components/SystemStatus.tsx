"use client";

import { useHealth } from "@/hooks/useHealth";
import { Panel } from "./Panel";
import { StatTile } from "./StatTile";

/** Real data: the backend's mode, environment and live-execution gates. */
export function SystemStatus() {
  const h = useHealth();
  const d = h.data;

  return (
    <>
      <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
        <StatTile
          label="Trading mode"
          value={d?.trading_mode.toUpperCase()}
          note={
            d?.trading_mode === "paper"
              ? "internal simulator"
              : d?.trading_mode === "demo"
                ? "MT5 demo account"
                : d
                  ? "real account"
                  : "API unreachable"
          }
          tone={d?.trading_mode === "live" ? "critical" : d?.trading_mode === "demo" ? "warning" : "good"}
        />
        <StatTile label="Environment" value={d?.environment} note={d ? `v${d.version}` : undefined} />
        <StatTile
          label="API"
          value={h.isPending ? undefined : h.isError ? "DOWN" : "OK"}
          note={h.isError ? h.error.message : d ? d.time.replace("T", " ") : undefined}
          tone={h.isError ? "critical" : "good"}
        />
        <StatTile
          label="Live execution"
          value={d ? (d.live_execution_allowed ? "ALLOWED" : "BLOCKED") : undefined}
          note={d ? `${d.live_execution_blockers.length} blocker(s)` : undefined}
          tone={d?.live_execution_allowed ? "critical" : "good"}
        />
      </div>
      <Panel title="Live execution gates" level={2} className="mt-4">
        {d ? (
          <ul className="grid gap-1 text-body sm:grid-cols-2" aria-label="live execution blockers">
            {d.live_execution_blockers.map((b) => (
              <li key={b} className="flex items-center gap-2 text-ink-2">
                <span className="h-1.5 w-1.5 shrink-0 rounded-full bg-critical" aria-hidden />
                {b}
              </li>
            ))}
            {d.live_execution_blockers.length === 0 && (
              <li className="text-critical">No blockers: live execution is allowed.</li>
            )}
          </ul>
        ) : (
          <p className="text-body text-muted">Waiting for the API.</p>
        )}
      </Panel>
    </>
  );
}
