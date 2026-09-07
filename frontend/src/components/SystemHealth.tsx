"use client";

import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  Badge,
  Button,
  EmptyState,
  ErrorState,
  LoadingState,
  Panel,
  StatTile,
} from "@/components/ui";
import { useMe } from "@/hooks/useMe";
import { roleAtLeast } from "@/lib/roles";
import { ApiError, monitoringService } from "@/lib/services";
import type { ComponentRow, ComponentState, TradingSafety } from "@/lib/services";

/**
 * The system health dashboard. Sections 38, 39, 60, 61, 62 and 74.
 *
 * **Every state comes from the backend.** Section 40 and 65: the frontend does
 * not derive trading safety, does not decide whether a component is healthy,
 * and does not fill a gap with a reassuring default. A component the backend
 * reported as UNKNOWN renders as UNKNOWN, and an API that cannot be reached
 * renders as an error rather than as an empty green board.
 *
 * **Five states, rendered distinctly.** NOT_CONFIGURED is neutral, not red: a
 * platform with no SMTP server is correctly configured and does not send email.
 * UNKNOWN is amber, not green: not observed is not the same as fine.
 *
 * **Detail needs permission.** The summary is readable by anyone signed in;
 * the component list, the incidents and the metrics need
 * `manage_system_settings` and the backend answers 403 without it. The panels
 * below say so rather than rendering an empty box.
 */
const TONES: Record<ComponentState, "good" | "warning" | "critical" | "neutral"> = {
  HEALTHY: "good",
  DEGRADED: "warning",
  UNHEALTHY: "critical",
  UNKNOWN: "warning",
  NOT_CONFIGURED: "neutral",
};

const SAFETY_TONE: Record<TradingSafety, "good" | "warning" | "critical" | "neutral"> = {
  SAFE: "good",
  DEGRADED: "warning",
  BLOCKED: "critical",
  UNKNOWN: "warning",
};

const LEVEL_TONE: Record<string, "good" | "warning" | "critical" | "neutral"> = {
  info: "neutral",
  warning: "warning",
  error: "critical",
  critical: "critical",
};

function forbidden(error: unknown): boolean {
  return error instanceof ApiError && error.status === 403;
}

function ComponentLine({ row }: { row: ComponentRow }) {
  const [open, setOpen] = useState(false);
  return (
    <li className="border-b border-line/50 py-1">
      <button
        type="button"
        className="flex w-full flex-wrap items-baseline gap-2 text-left"
        onClick={() => setOpen((v) => !v)}
        aria-expanded={open}
      >
        <Badge tone={TONES[row.status] ?? "neutral"}>{row.status}</Badge>
        <span className="font-mono text-body">{row.name}</span>
        <span className="text-body text-muted">{row.detail}</span>
        {row.latency_ms !== null ? (
          <span className="ml-auto font-mono text-micro text-muted">{row.latency_ms} ms</span>
        ) : null}
      </button>
      {open ? (
        <dl className="mt-1 grid gap-x-4 pl-2 text-micro sm:grid-cols-2">
          <div className="flex gap-2">
            <dt className="w-24 text-muted">Criticality</dt>
            <dd>{row.criticality}</dd>
          </div>
          <div className="flex gap-2">
            <dt className="w-24 text-muted">Last checked</dt>
            <dd className="font-mono">{row.last_checked}</dd>
          </div>
          {row.error_count ? (
            <div className="flex gap-2">
              <dt className="w-24 text-muted">Errors</dt>
              <dd>{row.error_count}</dd>
            </div>
          ) : null}
          {row.last_error ? (
            <div className="flex gap-2 sm:col-span-2">
              <dt className="w-24 text-muted">Last error</dt>
              <dd className="text-critical">{row.last_error}</dd>
            </div>
          ) : null}
          {Object.entries(row.facts).map(([key, value]) => (
            <div key={key} className="flex gap-2">
              <dt className="w-24 truncate text-muted">{key}</dt>
              <dd className="truncate font-mono">{String(value)}</dd>
            </div>
          ))}
        </dl>
      ) : null}
    </li>
  );
}

export function SystemHealth() {
  const client = useQueryClient();
  const me = useMe();
  const canDiagnose = me.data ? roleAtLeast(me.data.role, "admin") : false;

  const summary = useQuery({
    queryKey: ["monitoring", "summary"],
    queryFn: monitoringService.summary,
    refetchInterval: 30_000,
    retry: false,
  });

  const components = useQuery({
    queryKey: ["monitoring", "components"],
    queryFn: monitoringService.components,
    enabled: canDiagnose,
    refetchInterval: 30_000,
    retry: false,
  });

  const events = useQuery({
    queryKey: ["monitoring", "events"],
    queryFn: () => monitoringService.events(15),
    enabled: canDiagnose,
    retry: false,
  });

  const collect = useMutation({
    mutationFn: monitoringService.collect,
    onSuccess: () => client.invalidateQueries({ queryKey: ["monitoring"] }),
  });

  return (
    <div className="space-y-4">
      <Panel
        title="System health"
        level={37}
        right={
          canDiagnose ? (
            <Button
              variant="secondary"
              size="sm"
              onClick={() => collect.mutate()}
              disabled={collect.isPending}
            >
              {collect.isPending ? "Collecting…" : "Collect now"}
            </Button>
          ) : undefined
        }
      >
        {summary.isPending ? (
          <LoadingState what="system health" />
        ) : summary.isError ? (
          <ErrorState
            message={(summary.error as Error).message}
            onRetry={() => summary.refetch()}
          />
        ) : (
          <>
            <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
              <StatTile
                label="Overall"
                value={summary.data.status}
                tone={
                  summary.data.status === "HEALTHY"
                    ? "good"
                    : summary.data.status === "UNHEALTHY"
                      ? "critical"
                      : "warning"
                }
                note={summary.data.collected_at ?? "never collected"}
              />
              <StatTile
                label="Trading safety"
                value={summary.data.trading_safety}
                tone={
                  summary.data.trading_safety === "SAFE"
                    ? "good"
                    : summary.data.trading_safety === "BLOCKED"
                      ? "critical"
                      : "warning"
                }
                note="derived by the backend"
              />
              <StatTile
                label="Trading mode"
                value={summary.data.environment.trading_mode.toUpperCase()}
                tone={
                  summary.data.environment.trading_mode === "live" ? "critical" : "good"
                }
              />
              <StatTile
                label="Live trading"
                value={summary.data.environment.live_trading ? "ENABLED" : "DISABLED"}
                tone={summary.data.environment.live_trading ? "critical" : "good"}
                note={`${summary.data.environment.live_execution_blockers.length} gate(s) not built`}
              />
            </div>

            <div className="mt-3 flex flex-wrap items-center gap-2">
              <Badge tone={SAFETY_TONE[summary.data.trading_safety] ?? "neutral"}>
                {summary.data.trading_safety}
              </Badge>
              <ul className="text-body text-muted">
                {summary.data.trading_safety_reasons.map((r) => (
                  <li key={r}>— {r}</li>
                ))}
              </ul>
            </div>
            {summary.data.note ? (
              <p className="mt-2 text-body text-muted">{summary.data.note}</p>
            ) : null}
            {summary.data.trading_safety_note ? (
              <p className="mt-2 text-body text-muted">{summary.data.trading_safety_note}</p>
            ) : null}
          </>
        )}
      </Panel>

      <Panel title="Components" level={37}>
        {!canDiagnose ? (
          <EmptyState
            message="Detailed component health requires the admin role."
            hint="The overall status above is what your permissions cover."
          />
        ) : components.isPending ? (
          <LoadingState what="components" />
        ) : components.isError ? (
          forbidden(components.error) ? (
            <EmptyState message="Detailed component health requires the admin role." />
          ) : (
            <ErrorState message={(components.error as Error).message} />
          )
        ) : components.data.components.length === 0 ? (
          <EmptyState
            message="No collection has run yet."
            hint="Nothing is claimed healthy until it has been observed."
          />
        ) : (
          <div className="space-y-3">
            {Object.entries(components.data.layers).map(([layer, rows]) => (
              <div key={layer}>
                <p className="mb-1 text-mini font-semibold uppercase tracking-wider text-muted">
                  {layer}
                </p>
                <ul>
                  {rows.map((row) => (
                    <ComponentLine key={row.name} row={row} />
                  ))}
                </ul>
              </div>
            ))}
          </div>
        )}
      </Panel>

      <Panel title="Recent incidents" level={37}>
        {!canDiagnose ? (
          <EmptyState message="Incident history requires the admin role." />
        ) : events.isPending ? (
          <LoadingState what="incidents" />
        ) : events.isError ? (
          forbidden(events.error) ? (
            <EmptyState message="Incident history requires the admin role." />
          ) : (
            <ErrorState message={(events.error as Error).message} />
          )
        ) : events.data.events.length === 0 ? (
          <EmptyState
            message="No incidents recorded."
            hint="A state change is recorded only after it has been observed consistently."
          />
        ) : (
          <ul className="space-y-1 text-body">
            {events.data.events.map((e) => (
              <li key={e.id} className="flex flex-wrap items-baseline gap-2">
                <span className="font-mono text-micro text-muted">{e.occurred_at}</span>
                <Badge tone={LEVEL_TONE[e.level] ?? "neutral"}>{e.level}</Badge>
                <span className="font-mono">{e.component}</span>
                <span className="text-muted">{e.event_type}</span>
                {typeof e.payload.detail === "string" ? (
                  <span className="text-muted">— {e.payload.detail}</span>
                ) : null}
              </li>
            ))}
          </ul>
        )}
      </Panel>
    </div>
  );
}
