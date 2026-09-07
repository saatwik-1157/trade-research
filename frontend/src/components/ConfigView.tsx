"use client";

import { useHealth } from "@/hooks/useHealth";

/** Read-only, from GET /health. The backend never sends URLs or secrets here. */
export function ConfigView() {
  const h = useHealth();
  if (h.isPending) return <p className="text-body text-muted">Loading…</p>;
  if (h.isError) return <p className="text-body text-critical">API unreachable: {h.error.message}</p>;
  const d = h.data;
  const rows: [string, string][] = [
    ["Service", `${d.service} v${d.version}`],
    ["ENVIRONMENT", d.environment],
    ["TRADING_MODE", d.trading_mode],
    ["LIVE_TRADING", String(d.live_trading)],
    ["Live execution allowed", String(d.live_execution_allowed)],
    ["Server time", d.time],
  ];
  return (
    <dl className="grid grid-cols-[auto_1fr] gap-x-4 gap-y-1 text-body">
      {rows.map(([k, v]) => (
        <div key={k} className="contents">
          <dt className="font-mono text-muted">{k}</dt>
          <dd className="font-mono text-ink-2">{v}</dd>
        </div>
      ))}
    </dl>
  );
}
