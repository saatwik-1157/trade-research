"use client";

import { useEffect, useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  Badge,
  Button,
  EmptyState,
  ErrorState,
  LoadingState,
  Panel,
  Field,
  Select,
  StatTile,
} from "@/components/ui";
import { NotificationRowItem } from "./NotificationBell";
import { notificationService } from "@/lib/services";
import type {
  ChannelStatus,
  NotificationCategory,
  NotificationPreferenceRow,
  NotificationSeverity,
} from "@/lib/services";
import { RealtimeClient } from "@/lib/realtime";
import { wsUrl } from "@/lib/config";

/**
 * The notification centre. Sections 12, 32 and 34.
 *
 * **Filters, pagination and read state, and nothing invented.** An empty list
 * says the list is empty; a failed request says the request failed. Neither
 * shows a sample alert, because §58 forbids a production surface that can
 * display a trading alert nobody's platform produced.
 *
 * **The preference grid shows the defaults as defaults.** A user who has saved
 * nothing sees what the platform will do, labelled `default`, rather than an
 * empty page. The in-app column is disabled with the reason attached: it can be
 * quietened, never switched off, and an unread risk breach is what that rule
 * exists to prevent.
 *
 * **A Discord row appears here from L34 and says NOT_CONFIGURED.** The seat is
 * real; the adapter is L35's. Showing the row with an honest state is more
 * useful than hiding a channel that is about to exist.
 */
const CATEGORIES: NotificationCategory[] = [
  "TRADING",
  "RISK",
  "PORTFOLIO",
  "BOTS",
  "STRATEGIES",
  "AI",
  "MONITORING",
  "BROKER",
  "SYSTEM",
  "SECURITY",
];

const SEVERITIES: NotificationSeverity[] = ["INFO", "SUCCESS", "WARNING", "ERROR", "CRITICAL"];
const ENVIRONMENTS = ["backtest", "paper", "demo", "live", "unknown"];
const PAGE = 25;

function channelTone(state: ChannelStatus["state"]): "good" | "neutral" | "warning" {
  if (state === "CONFIGURED") return "good";
  if (state === "DISABLED") return "warning";
  return "neutral";
}

function PreferenceGrid({
  rows,
  locked,
  note,
}: {
  rows: NotificationPreferenceRow[];
  locked: { severities: string[]; why: string };
  note: string;
}) {
  const client = useQueryClient();
  const [error, setError] = useState<string | null>(null);

  const save = useMutation({
    mutationFn: (row: NotificationPreferenceRow) =>
      notificationService.savePreferences([
        {
          category: row.category,
          channel: row.channel,
          enabled: row.enabled,
          min_severity: row.min_severity,
        },
      ]),
    onSuccess: () => {
      setError(null);
      client.invalidateQueries({ queryKey: ["notifications", "preferences"] });
    },
    onError: (e: Error) => setError(e.message),
  });

  const byCategory = useMemo(() => {
    const out = new Map<NotificationCategory, NotificationPreferenceRow[]>();
    for (const row of rows) {
      out.set(row.category, [...(out.get(row.category) ?? []), row]);
    }
    return out;
  }, [rows]);

  return (
    <div>
      <p className="mb-2 text-body text-muted">{note}</p>
      {error ? <ErrorState message={error} /> : null}
      <div className="overflow-x-auto">
        <table className="w-full text-body">
          <thead>
            <tr className="border-b border-line text-left text-muted">
              <th className="py-1 pr-2">Category</th>
              <th className="py-1 pr-2">Channel</th>
              <th className="py-1 pr-2">Enabled</th>
              <th className="py-1 pr-2">From severity</th>
              <th className="py-1">Source</th>
            </tr>
          </thead>
          <tbody>
            {[...byCategory.entries()].map(([category, channels]) =>
              channels.map((row) => (
                <tr key={`${row.category}-${row.channel}`} className="border-b border-line/50">
                  <td className="py-1 pr-2 font-mono text-micro text-muted">
                    {row.channel === channels[0].channel ? category : ""}
                  </td>
                  <td className="py-1 pr-2">{row.channel}</td>
                  <td className="py-1 pr-2">
                    <input
                      type="checkbox"
                      aria-label={`${row.category} ${row.channel} enabled`}
                      checked={row.enabled}
                      disabled={row.locked || save.isPending}
                      title={row.locked ? locked.why : undefined}
                      onChange={(e) => save.mutate({ ...row, enabled: e.target.checked })}
                    />
                  </td>
                  <td className="py-1 pr-2">
                    <select
                      aria-label={`${row.category} ${row.channel} minimum severity`}
                      className="rounded border border-line bg-surface px-1 py-0.5 text-body"
                      value={row.min_severity}
                      disabled={save.isPending}
                      onChange={(e) =>
                        save.mutate({
                          ...row,
                          min_severity: e.target.value as NotificationSeverity,
                        })
                      }
                    >
                      {SEVERITIES.filter(
                        (s) => !(row.locked && locked.severities.includes(s) && s !== "ERROR"),
                      ).map((s) => (
                        <option key={s} value={s}>
                          {s}
                        </option>
                      ))}
                    </select>
                  </td>
                  <td className="py-1 text-muted">{row.source}</td>
                </tr>
              )),
            )}
          </tbody>
        </table>
      </div>
    </div>
  );
}

export function NotificationCenter() {
  const client = useQueryClient();
  const [category, setCategory] = useState<NotificationCategory | "">("");
  const [severity, setSeverity] = useState<NotificationSeverity | "">("");
  const [environment, setEnvironment] = useState("");
  const [unreadOnly, setUnreadOnly] = useState(false);
  const [offset, setOffset] = useState(0);

  const filters = { category, severity, environment, unread: unreadOnly || undefined };

  const list = useQuery({
    queryKey: ["notifications", "list", filters, offset],
    queryFn: () => notificationService.list({ ...filters, limit: PAGE, offset }),
    retry: false,
  });

  const counts = useQuery({
    queryKey: ["notifications", "unread-count"],
    queryFn: () => notificationService.unreadCount(),
    retry: false,
  });

  const preferences = useQuery({
    queryKey: ["notifications", "preferences"],
    queryFn: () => notificationService.preferences(),
    retry: false,
  });

  const channels = useQuery({
    queryKey: ["notifications", "channels"],
    queryFn: () => notificationService.channels(),
    retry: false,
  });

  const read = useMutation({
    mutationFn: (id: string) => notificationService.markRead(id),
    onSuccess: () => client.invalidateQueries({ queryKey: ["notifications"] }),
  });

  const readAll = useMutation({
    mutationFn: () => notificationService.markAllRead(),
    onSuccess: () => client.invalidateQueries({ queryKey: ["notifications"] }),
  });

  // A live nudge, never a live render. The frame says something changed; the
  // list is still whatever the API answers, so a browser that was disconnected
  // for an hour catches up on its next read rather than having missed rows.
  useEffect(() => {
    const socket = new RealtimeClient(wsUrl("/v1/realtime/ws"));
    const stop = socket.on("NOTIFICATION_CREATED", () => {
      client.invalidateQueries({ queryKey: ["notifications"] });
    });
    socket.connect();
    return () => {
      stop();
      socket.close();
    };
  }, [client]);

  const total = list.data?.page.total ?? 0;

  return (
    <div className="space-y-4">
      <div className="grid gap-3 sm:grid-cols-3">
        <StatTile
          label="Unread"
          value={counts.isPending ? "…" : String(counts.data?.unread ?? 0)}
        />
        <StatTile label="Matching this filter" value={list.isPending ? "…" : String(total)} />
        <StatTile
          label="Needs attention"
          value={
            counts.isPending
              ? "…"
              : String(
                  (counts.data?.by_severity?.ERROR ?? 0) +
                    (counts.data?.by_severity?.CRITICAL ?? 0),
                )
          }
        />
      </div>

      <Panel title="Notifications" level={34}>
        <div className="mb-2 flex flex-wrap items-end gap-2">
          <Field label="Category">
            <Select
              aria-label="Category"
              value={category}
              onChange={(e) => {
                setCategory(e.target.value as NotificationCategory | "");
                setOffset(0);
              }}
            >
              <option value="">All</option>
              {CATEGORIES.map((c) => (
                <option key={c} value={c}>
                  {c}
                </option>
              ))}
            </Select>
          </Field>
          <Field label="Severity">
            <Select
              aria-label="Severity"
              value={severity}
              onChange={(e) => {
                setSeverity(e.target.value as NotificationSeverity | "");
                setOffset(0);
              }}
            >
              <option value="">All</option>
              {SEVERITIES.map((s) => (
                <option key={s} value={s}>
                  {s}
                </option>
              ))}
            </Select>
          </Field>
          <Field label="Environment">
            <Select
              aria-label="Environment"
              value={environment}
              onChange={(e) => {
                setEnvironment(e.target.value);
                setOffset(0);
              }}
            >
              <option value="">All</option>
              {ENVIRONMENTS.map((name) => (
                <option key={name} value={name}>
                  {name}
                </option>
              ))}
            </Select>
          </Field>
          <label className="flex items-center gap-1 pb-1 text-body text-muted">
            <input
              type="checkbox"
              checked={unreadOnly}
              onChange={(e) => {
                setUnreadOnly(e.target.checked);
                setOffset(0);
              }}
            />
            Unread only
          </label>
          <Button
            variant="secondary"
            onClick={() => readAll.mutate()}
            disabled={readAll.isPending || (counts.data?.unread ?? 0) === 0}
          >
            Mark all read
          </Button>
        </div>

        {list.isPending ? (
          <LoadingState what="notifications" />
        ) : list.isError ? (
          <ErrorState message={(list.error as Error).message} onRetry={() => list.refetch()} />
        ) : list.data.items.length === 0 ? (
          <EmptyState
            message="No notifications match this filter."
            hint="Nothing is hidden — an empty list means nothing has been recorded."
          />
        ) : (
          <>
            <ul>
              {list.data.items.map((row) => (
                <NotificationRowItem key={row.id} row={row} onRead={(id) => read.mutate(id)} />
              ))}
            </ul>
            <div className="mt-2 flex items-center justify-between text-body text-muted">
              <span>
                {offset + 1}–{offset + list.data.items.length} of {total}
              </span>
              <div className="flex gap-2">
                <Button
                  variant="secondary"
                  disabled={offset === 0}
                  onClick={() => setOffset(Math.max(0, offset - PAGE))}
                >
                  Previous
                </Button>
                <Button
                  variant="secondary"
                  disabled={!list.data.page.has_more}
                  onClick={() => setOffset(offset + PAGE)}
                >
                  Next
                </Button>
              </div>
            </div>
          </>
        )}
      </Panel>

      <Panel title="Delivery channels" level={34}>
        {channels.isPending ? (
          <LoadingState what="channels" />
        ) : channels.isError ? (
          <ErrorState message={(channels.error as Error).message} />
        ) : (
          <ul className="space-y-1 text-body">
            {channels.data.channels.map((c) => (
              <li key={c.channel} className="flex flex-wrap items-center gap-2">
                <span className="w-20 font-mono text-micro">{c.channel}</span>
                <Badge tone={channelTone(c.state)}>{c.state}</Badge>
                {c.detail ? <span className="text-muted">{c.detail}</span> : null}
              </li>
            ))}
          </ul>
        )}
      </Panel>

      <Panel title="What you are told about" level={34}>
        {preferences.isPending ? (
          <LoadingState what="preferences" />
        ) : preferences.isError ? (
          <ErrorState message={(preferences.error as Error).message} />
        ) : (
          <PreferenceGrid
            rows={preferences.data.preferences}
            locked={preferences.data.locked}
            note={preferences.data.safety_note}
          />
        )}
      </Panel>
    </div>
  );
}
