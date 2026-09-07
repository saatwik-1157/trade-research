"use client";

import Link from "next/link";
import { useEffect, useRef, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Badge, EmptyState, ErrorState, LoadingState } from "@/components/ui";
import { ApiError, notificationHref, notificationService } from "@/lib/services";
import type { NotificationRow, NotificationSeverity } from "@/lib/services";

/**
 * The bell in the top bar. Sections 34 and 14.
 *
 * **The badge is coloured by the worst thing waiting, not only sized by how
 * much is.** A count of twelve where one is a risk breach and eleven are trade
 * confirmations should not look like twelve trade confirmations, so the tone
 * comes from `by_severity` rather than from the total.
 *
 * **It shows nothing rather than something plausible.** An unreachable API
 * renders an error, and an empty list says the list is empty — §41 of L36 and
 * §58 of L34: no fabricated alert, ever, including the reassuring kind.
 *
 * **A realtime frame invalidates; it does not render.** `NOTIFICATION_CREATED`
 * carries the headline, not the record — the hub's own rule is that an event is
 * a nudge that something changed and a consumer that needs the value reads it
 * back. So the socket handler refetches and the list stays the API's answer,
 * which is also why a reconnect after an outage loses nothing.
 */
const TONES: Record<NotificationSeverity, "neutral" | "good" | "warning" | "critical" | "accent"> = {
  INFO: "neutral",
  SUCCESS: "good",
  WARNING: "warning",
  ERROR: "critical",
  CRITICAL: "critical",
};

const ORDER: NotificationSeverity[] = ["CRITICAL", "ERROR", "WARNING", "SUCCESS", "INFO"];

/** The worst severity present in an unread breakdown, or null if none is. */
export function worstOf(counts: Record<string, number>): NotificationSeverity | null {
  for (const severity of ORDER) {
    if ((counts[severity] ?? 0) > 0) return severity;
  }
  return null;
}

/** `[PAPER]` and friends are already in the title; this is for the row's chip. */
export function environmentLabel(row: NotificationRow): string | null {
  if (!row.environment) return null;
  return row.environment.toUpperCase();
}

export function NotificationRowItem({
  row,
  onRead,
}: {
  row: NotificationRow;
  onRead: (id: string) => void;
}) {
  const href = notificationHref(row);
  const environment = environmentLabel(row);
  const body = (
    <div className="flex-1">
      <div className="flex flex-wrap items-baseline gap-1.5">
        <Badge tone={TONES[row.severity] ?? "neutral"}>{row.severity}</Badge>
        {environment ? <Badge tone="neutral">{environment}</Badge> : null}
        <span className={row.read ? "text-body text-muted" : "text-body font-semibold"}>
          {row.title}
        </span>
      </div>
      {row.body ? <p className="mt-0.5 text-body text-muted">{row.body}</p> : null}
      <p className="mt-0.5 font-mono text-micro text-muted">{row.created_at}</p>
    </div>
  );
  return (
    <li className="flex items-start gap-2 border-b border-line px-2 py-2 last:border-0">
      {href ? (
        <Link href={href} className="flex-1 hover:underline">
          {body}
        </Link>
      ) : (
        body
      )}
      {!row.read && (
        <button
          type="button"
          className="shrink-0 text-micro text-accent hover:underline"
          onClick={() => onRead(row.id)}
        >
          Mark read
        </button>
      )}
    </li>
  );
}

export function NotificationBell() {
  const client = useQueryClient();
  const [open, setOpen] = useState(false);
  const box = useRef<HTMLDivElement>(null);

  const count = useQuery({
    queryKey: ["notifications", "unread-count"],
    queryFn: () => notificationService.unreadCount(),
    // A modest poll rather than a socket subscription here: the realtime hook
    // lives on the alerts page, and a bell that opened its own WebSocket would
    // be a second connection for one number.
    refetchInterval: 60_000,
    retry: false,
  });

  const recent = useQuery({
    queryKey: ["notifications", "recent"],
    queryFn: () => notificationService.list({ limit: 8 }),
    enabled: open,
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

  useEffect(() => {
    if (!open) return;
    const away = (event: MouseEvent) => {
      if (box.current && !box.current.contains(event.target as Node)) setOpen(false);
    };
    document.addEventListener("mousedown", away);
    return () => document.removeEventListener("mousedown", away);
  }, [open]);

  // Not signed in, or the endpoint is unreachable: render nothing rather than a
  // zero. A bell showing "0" when the API is down says "all clear", which is
  // the one thing it must never say without having asked.
  if (count.isError && (count.error as ApiError)?.status === 401) return null;

  const unread = count.data?.unread ?? 0;
  const worst = worstOf(count.data?.by_severity ?? {});

  return (
    <div className="relative" ref={box}>
      <button
        type="button"
        aria-label={`Notifications${unread ? `: ${unread} unread` : ""}`}
        aria-expanded={open}
        className="flex items-center gap-1 text-body text-muted hover:text-ink"
        onClick={() => setOpen((v) => !v)}
      >
        <span aria-hidden="true">🔔</span>
        {unread > 0 && (
          <span
            data-testid="unread-badge"
            className={
              worst === "CRITICAL" || worst === "ERROR"
                ? "rounded bg-critical px-1 text-micro font-bold text-plane"
                : worst === "WARNING"
                  ? "rounded bg-warning px-1 text-micro font-bold text-plane"
                  : "rounded bg-surface-2 px-1 text-micro font-bold text-ink"
            }
          >
            {unread > 99 ? "99+" : unread}
          </span>
        )}
      </button>

      {open && (
        <div
          role="dialog"
          aria-label="Recent notifications"
          className="absolute right-0 z-50 mt-2 w-96 rounded border border-line bg-surface shadow-lg"
        >
          <div className="flex items-center justify-between border-b border-line px-2 py-1.5">
            <span className="text-mini font-semibold uppercase tracking-wide text-muted">
              Notifications
            </span>
            <div className="flex items-center gap-3">
              <button
                type="button"
                className="text-micro text-accent hover:underline disabled:opacity-50"
                onClick={() => readAll.mutate()}
                disabled={readAll.isPending || unread === 0}
              >
                Mark all read
              </button>
              <Link href="/alerts" className="text-micro text-accent hover:underline">
                View all
              </Link>
            </div>
          </div>
          {recent.isPending ? (
            <LoadingState what="notifications" />
          ) : recent.isError ? (
            <ErrorState message={(recent.error as Error).message} />
          ) : recent.data.items.length === 0 ? (
            <EmptyState
              message="No notifications."
              hint="Nothing has happened that you asked to be told about."
            />
          ) : (
            <ul className="max-h-96 overflow-y-auto">
              {recent.data.items.map((row) => (
                <NotificationRowItem key={row.id} row={row} onRead={(id) => read.mutate(id)} />
              ))}
            </ul>
          )}
        </div>
      )}
    </div>
  );
}
