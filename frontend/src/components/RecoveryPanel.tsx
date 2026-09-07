"use client";

import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  Badge,
  Button,
  EmptyState,
  ErrorState,
  Field,
  Input,
  LoadingState,
  Panel,
} from "@/components/ui";
import { useMe } from "@/hooks/useMe";
import { roleAtLeast } from "@/lib/roles";
import { recoveryService } from "@/lib/services";
import type { RecoveryStateName, RecoveryStep } from "@/lib/services";

/**
 * Recovery and safe mode. Step 33.
 *
 * **The frontend never claims recovery succeeded.** Every state, every step and
 * the safe-mode latch come from the backend, which derived them from the
 * reconcilers that own each piece of state. Releasing safe mode re-runs the
 * sequence server-side and refuses while a condition still holds, so this panel
 * cannot clear a latch by asking nicely — and when the backend refuses, the
 * response says which latches are still holding and this shows them.
 *
 * **It extends `/monitoring` rather than adding a dashboard.** Step 33 says not
 * to duplicate, and L37's health board is where an operator already looks.
 *
 * **Every control is a read except two.** "Reconcile now" runs the same
 * read-only sweep the startup sequence runs; it repairs nothing. Entering and
 * releasing safe mode are the only writes, both administrator-only and both
 * requiring a reason that is recorded.
 */
const STATE_TONE: Record<RecoveryStateName, "good" | "warning" | "critical" | "neutral"> = {
  NORMAL: "good",
  RECOVERED: "good",
  DEGRADED: "warning",
  RECONNECTING: "warning",
  RECONCILING: "warning",
  UNKNOWN: "warning",
  UNAVAILABLE: "critical",
  SAFE_MODE: "critical",
};

const STEP_TONE: Record<RecoveryStep["status"], "good" | "warning" | "critical" | "neutral"> = {
  OK: "good",
  ATTENTION: "warning",
  SKIPPED: "neutral",
  FAILED: "critical",
};

function Steps({ steps }: { steps: RecoveryStep[] }) {
  return (
    <ul className="space-y-1 text-body">
      {steps.map((s) => (
        <li key={s.step} className="flex flex-wrap items-baseline gap-2">
          <Badge tone={STEP_TONE[s.status] ?? "neutral"}>{s.status}</Badge>
          <span className="w-44 font-mono">{s.step}</span>
          <span className="flex-1 text-muted">{s.detail}</span>
        </li>
      ))}
    </ul>
  );
}

export function RecoveryPanel() {
  const client = useQueryClient();
  const me = useMe();
  const canOperate = me.data ? roleAtLeast(me.data.role, "admin") : false;
  const [reason, setReason] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);

  const status = useQuery({
    queryKey: ["recovery", "status"],
    queryFn: recoveryService.status,
    refetchInterval: 30_000,
    retry: false,
  });

  const act = useMutation({
    mutationFn: async (action: "reconcile" | "enter" | "exit") => {
      if (action === "reconcile") return recoveryService.reconcile(reason);
      if (action === "enter") return recoveryService.enterSafeMode(reason);
      return recoveryService.exitSafeMode(reason);
    },
    onSuccess: (result) => {
      setError(null);
      const still = (result as { safe_mode?: { engaged: boolean; reasons: { reason: string }[] } })
        .safe_mode;
      setNotice(
        still?.engaged
          ? `Safe mode is still engaged: ${still.reasons.map((r) => r.reason).join(", ")}`
          : "Done.",
      );
      client.invalidateQueries({ queryKey: ["recovery"] });
    },
    onError: (e: Error) => {
      setNotice(null);
      setError(e.message);
    },
  });

  if (status.isPending) return <LoadingState what="recovery state" />;
  if (status.isError)
    return (
      <Panel title="Recovery" level={38}>
        <ErrorState message={(status.error as Error).message} onRetry={() => status.refetch()} />
      </Panel>
    );

  const data = status.data;
  const safe = data.safe_mode;

  return (
    <div className="space-y-4">
      <Panel title="Recovery" level={38}>
        <div className="flex flex-wrap items-center gap-2">
          <Badge tone={STATE_TONE[data.state] ?? "neutral"}>{data.state}</Badge>
          <span className="text-body text-muted">
            {data.environment.trading_mode.toUpperCase()} ·{" "}
            {data.environment.live_trading ? "live trading ENABLED" : "live trading disabled"}
          </span>
        </div>

        {safe.engaged ? (
          <div className="mt-3 rounded border border-critical bg-critical/5 p-2">
            <p className="text-body font-semibold text-critical">
              Safe mode is engaged. New orders, automated execution and bot recovery are
              blocked.
            </p>
            <ul className="mt-1 space-y-0.5 text-body">
              {safe.reasons.map((r) => (
                <li key={r.reason}>
                  <span className="font-mono">{r.reason}</span>
                  <span className="text-muted"> — {r.detail}</span>
                  <span className="ml-1 font-mono text-micro text-muted">({r.at})</span>
                </li>
              ))}
            </ul>
            <p className="mt-2 text-body text-muted">
              Still allowed: {safe.still_allowed.join(", ")}.
            </p>
          </div>
        ) : (
          <p className="mt-3 text-body text-muted">
            Safe mode is not engaged. Nothing is blocking new work.
          </p>
        )}
        <p className="mt-2 text-body text-muted">{safe.authority}</p>
      </Panel>

      <Panel title="Last startup sequence" level={38}>
        {data.startup === null ? (
          <EmptyState
            message="No startup sequence has been recorded."
            hint="It runs in this process's lifespan; a process that has not started has not checked anything."
          />
        ) : (
          <>
            <p className="mb-2 text-body text-muted">
              {data.startup.at} · {data.startup.clean ? "clean" : "needs attention"}
            </p>
            <Steps steps={data.startup.steps} />
            <p className="mt-2 text-body text-muted">{data.startup.authority}</p>
          </>
        )}
      </Panel>

      {canOperate ? (
        <Panel title="Recovery actions" level={38}>
          <Field label="Reason (recorded in the audit trail)">
            <Input
              aria-label="Reason"
              value={reason}
              onChange={(e) => setReason(e.target.value)}
              placeholder="Why is this happening?"
            />
          </Field>
          <div className="mt-2 flex flex-wrap gap-2">
            <Button
              variant="secondary"
              disabled={act.isPending || reason.trim().length < 8}
              onClick={() => act.mutate("reconcile")}
            >
              Reconcile now
            </Button>
            <Button
              variant="secondary"
              disabled={act.isPending || reason.trim().length < 8 || safe.engaged}
              onClick={() => act.mutate("enter")}
            >
              Enter safe mode
            </Button>
            <Button
              variant="danger"
              disabled={act.isPending || reason.trim().length < 8 || !safe.engaged}
              onClick={() => act.mutate("exit")}
            >
              Release safe mode
            </Button>
          </div>
          <p className="mt-2 text-body text-muted">
            Reconciling compares and reports; it closes nothing, cancels nothing and re-sends
            nothing. Releasing safe mode re-runs the startup sequence first and only clears
            the latches whose condition has actually gone.
          </p>
          {error ? <ErrorState message={error} /> : null}
          {notice ? (
            <p role="status" className="mt-2 text-body text-muted">
              {notice}
            </p>
          ) : null}
        </Panel>
      ) : (
        <Panel title="Recovery actions" level={38}>
          <EmptyState message="Reconciling and changing safe mode are administrator actions." />
        </Panel>
      )}

      <Panel title="What recovery will not do" level={38}>
        <ul className="space-y-0.5 text-body text-muted">
          {data.rules.map((r) => (
            <li key={r}>— {r}</li>
          ))}
        </ul>
      </Panel>
    </div>
  );
}
