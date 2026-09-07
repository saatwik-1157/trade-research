"use client";

import { useMutation, useQuery } from "@tanstack/react-query";
import { Badge, Button, ErrorState, LoadingState, Panel } from "@/components/ui";
import { useMe } from "@/hooks/useMe";
import { roleAtLeast } from "@/lib/roles";
import { notificationService } from "@/lib/services";
import type { DiscordStatus } from "@/lib/services";

/**
 * Discord configuration, as seen from the browser. Sections 32 and 33.
 *
 * **There is no field to type a webhook into, and that is deliberate.** The
 * webhook is a bearer credential — anyone holding it can post to the channel —
 * and it is set on the server. This page reports whether one is configured and
 * never what it is: the API does not return it, not even redacted, so there is
 * nothing here that could be rendered, copied or logged by an extension.
 *
 * **The test button is gated twice.** The backend requires
 * `manage_system_settings` and answers 403 without it; the button is hidden for
 * anyone else as a convenience. Hiding a control is not authorization, which is
 * why the first sentence is the one that matters.
 *
 * **A test says it is a test.** It creates no notification, publishes no event
 * and names no trade — the response says so and the message in the channel says
 * so, because a synthetic "trade closed" sent to prove the wiring works is a
 * trade in the channel that never happened.
 */
function stateTone(state: DiscordStatus["state"]): "good" | "warning" | "neutral" {
  if (state === "CONFIGURED") return "good";
  if (state === "DISABLED") return "warning";
  return "neutral";
}

export function DiscordSettings() {
  const me = useMe();
  const canManage = me.data ? roleAtLeast(me.data.role, "admin") : false;

  const status = useQuery({
    queryKey: ["notifications", "discord"],
    queryFn: () => notificationService.discordStatus(),
    retry: false,
  });

  const test = useMutation({
    mutationFn: () => notificationService.sendDiscordTest(),
  });

  return (
    <div className="space-y-4">
      <Panel title="Discord" level={35}>
        {status.isPending ? (
          <LoadingState what="Discord status" />
        ) : status.isError ? (
          <ErrorState
            message={(status.error as Error).message}
            onRetry={() => status.refetch()}
          />
        ) : (
          <div className="space-y-3 text-body">
            <div className="flex flex-wrap items-center gap-2">
              <Badge tone={stateTone(status.data.state)}>{status.data.state}</Badge>
              <span className="text-muted">{status.data.detail}</span>
            </div>

            <dl className="grid gap-x-4 gap-y-1 sm:grid-cols-2">
              <div className="flex gap-2">
                <dt className="w-24 text-muted">Transport</dt>
                <dd>{status.data.transport ?? "—"}</dd>
              </div>
              <div className="flex gap-2">
                <dt className="w-24 text-muted">Scope</dt>
                <dd>
                  {status.data.scope === "system"
                    ? "system — one channel for this deployment"
                    : (status.data.scope ?? "—")}
                </dd>
              </div>
              <div className="flex gap-2">
                <dt className="w-24 text-muted">Commands</dt>
                <dd>{status.data.commands ? "yes" : "none — outbound only"}</dd>
              </div>
              <div className="flex gap-2">
                <dt className="w-24 text-muted">Delivered</dt>
                <dd className="tabular font-mono">
                  {status.data.sent ?? 0} sent · {status.data.failed ?? 0} failed ·{" "}
                  {status.data.rate_limited ?? 0} rate-limited
                </dd>
              </div>
            </dl>

            <p className="text-muted">{status.data.how_to_enable}</p>

            {canManage ? (
              <div className="space-y-2 border-t border-line pt-3">
                <Button
                  variant="secondary"
                  onClick={() => test.mutate()}
                  disabled={test.isPending || !status.data.available}
                >
                  {test.isPending ? "Sending…" : "Send test notification"}
                </Button>
                <p className="text-muted">
                  Sends a message that identifies itself as a test. No trade, order or
                  alert is created, and no event is published.
                </p>
                {test.isError ? <ErrorState message={(test.error as Error).message} /> : null}
                {test.data ? (
                  <p
                    role="status"
                    className={test.data.delivered ? "text-good" : "text-warning"}
                  >
                    {test.data.status}
                    {test.data.detail ? ` — ${test.data.detail}` : ""}
                  </p>
                ) : null}
              </div>
            ) : (
              <p className="border-t border-line pt-3 text-muted">
                Configuring Discord and sending a test are administrator actions.
              </p>
            )}
          </div>
        )}
      </Panel>

      <Panel title="What is posted, and what is not" level={35}>
        <ul className="space-y-1 text-body text-muted">
          <li>
            Discord is a <strong>destination</strong>. It receives notifications the
            platform already created; it does not decide anything, and it cannot place an
            order, change a risk limit or start a bot.
          </li>
          <li>
            Every category is <strong>off by default</strong>. One webhook serves the whole
            deployment, so enabling a category means your notifications appear in a shared
            channel. Choose them in <strong>Alerts → What you are told about</strong>.
          </li>
          <li>
            Every trading message carries its environment —{" "}
            <code className="font-mono">PAPER</code>, <code className="font-mono">DEMO</code>{" "}
            or <code className="font-mono">LIVE</code> — in the title and again as a field,
            because a title is truncated in a phone notification and the environment must
            not be.
          </li>
          <li>
            A message contains only fields the event recorded. A P&amp;L the platform did
            not record does not appear as a zero; the line is left out.
          </li>
          <li>
            The webhook URL is never sent to this page, returned by the API, written to a
            delivery record or put in a log line.
          </li>
          <li>
            If Discord is unavailable the delivery is recorded as failed and retried a
            bounded number of times. Nothing about trading changes.
          </li>
        </ul>
      </Panel>
    </div>
  );
}
