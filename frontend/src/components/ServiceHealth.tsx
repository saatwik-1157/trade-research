"use client";

import { useQuery } from "@tanstack/react-query";
import { Panel, StatusDot, type ServiceState } from "@/components/ui";
import { systemService, type ReadyReport } from "@/lib/services";

/**
 * Connection state of every service the platform will depend on.
 *
 * The rule: CONNECTED is rendered only when the backend confirmed it. A
 * service the backend does not report at all is UNKNOWN, never DISCONNECTED
 * — we have not asked it, so we cannot say it is down.
 */
const SERVICES: { key: string; label: string; level?: number; note: string }[] = [
  { key: "api", label: "Backend API", note: "the API answering this page" },
  { key: "database", label: "Database", note: "PostgreSQL" },
  { key: "redis", label: "Redis", note: "optional until the event bus carries trading events" },
  { key: "workers", label: "Workers", note: "background workers in this process" },
  { key: "market_data", label: "Market data", level: 8, note: "no provider is connected" },
  { key: "tradingview", label: "TradingView", level: 9, note: "receiver is not wired to the API" },
  { key: "mt5", label: "MetaTrader 5", level: 10, note: "broker adapter is not wired to the API" },
  { key: "websocket", label: "WebSocket", level: 7, note: "realtime endpoint does not exist" },
];

function stateOf(name: string, report: ReadyReport | undefined, apiUp: boolean): ServiceState {
  if (name === "api") return apiUp ? "connected" : "disconnected";
  if (!report) return "unknown";
  const check = report.checks[name];
  if (!check) return "unknown"; // the backend does not report it, so we do not claim
  if (check.status === "healthy") return "connected";
  if (check.status === "degraded") return "degraded";
  return "disconnected";
}

export function ServiceHealth() {
  const ready = useQuery({
    queryKey: ["ready"],
    queryFn: systemService.ready,
    refetchInterval: 15_000,
    retry: false,
  });
  const apiUp = !ready.isError;

  return (
    <Panel title="System status" level={37}>
      <ul className="grid gap-1.5 sm:grid-cols-2" aria-label="service status">
        {SERVICES.map((service) => {
          const state = stateOf(service.key, ready.data, apiUp);
          const check = ready.data?.checks[service.key];
          return (
            <li
              key={service.key}
              className="flex items-center justify-between gap-2 rounded border border-line bg-surface-2/30 px-2 py-1"
            >
              <span className="flex items-center gap-2 text-body text-ink-2">
                {service.label}
                {service.level !== undefined && (
                  <span className="font-mono text-micro text-muted">
                    L{String(service.level).padStart(2, "0")}
                  </span>
                )}
              </span>
              <StatusDot state={state} detail={check?.detail ?? service.note} />
            </li>
          );
        })}
      </ul>
      {ready.isError && (
        <p role="alert" className="mt-2 text-body text-critical">
          The API did not answer, so every dependency below it is unknown rather than healthy.
        </p>
      )}
    </Panel>
  );
}
