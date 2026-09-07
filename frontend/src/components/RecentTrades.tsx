"use client";

import Link from "next/link";
import { useQuery } from "@tanstack/react-query";
import { Badge, DataTable, type Column } from "@/components/ui";
import { journalService, type TradeRow } from "@/lib/services";

/**
 * The last few closed trades, on the front page.
 *
 * It replaces a fixed notice saying the journal's trades "are not served by
 * the API yet". `GET /v1/trades` serves them, and has since L31.
 *
 * Every row carries its own `mode`, and it is shown. Pooling a demo trade and
 * a paper trade into one list without saying which is which is the omission
 * `journalService.list` is written to avoid, and a compact view is exactly
 * where it would be tempting to drop the column.
 */
const columns: Column<TradeRow>[] = [
  {
    key: "symbol",
    header: "Symbol",
    render: (t) => <span className="text-ink-2">{t.symbol ?? "—"}</span>,
  },
  { key: "side", header: "Side", render: (t) => t.side },
  {
    key: "mode",
    header: "Mode",
    render: (t) => <Badge tone={t.mode === "live" ? "warning" : "neutral"}>{t.mode}</Badge>,
  },
  {
    key: "net",
    header: "Net",
    align: "right",
    render: (t) => {
      // The sign is read from the string rather than parsed to a float: these
      // arrive as decimal strings precisely so nothing rounds them, and a
      // comparison does not need the number.
      const negative = t.net_profit.trim().startsWith("-");
      return (
        <span className={negative ? "text-critical" : "text-good"}>
          {t.net_profit}
          {t.currency ? ` ${t.currency}` : ""}
        </span>
      );
    },
  },
  {
    key: "r",
    header: "R",
    align: "right",
    render: (t) => t.r_multiple ?? <span className="text-muted">—</span>,
  },
  {
    key: "closed",
    header: "Closed",
    align: "right",
    render: (t) => (
      <span className="text-muted">{t.closed_at ? t.closed_at.slice(0, 16).replace("T", " ") : "—"}</span>
    ),
  },
];

export function RecentTrades({ limit = 6 }: { limit?: number }) {
  const query = useQuery({
    queryKey: ["trades", "recent", limit],
    queryFn: () => journalService.list({ status: "closed", limit }),
  });

  return (
    <div className="flex flex-col gap-2">
      <DataTable
        columns={columns}
        rows={query.data?.items ?? []}
        rowKey={(t) => t.id}
        loading={query.isPending}
        error={query.isError ? "Trades could not be read." : null}
        empty="no closed trades"
        emptyHint="A trade appears here once it has been closed and journalled."
        caption="The most recently closed trades"
      />
      {query.data && query.data.page.total > limit && (
        <Link href="/journal" className="self-end text-body text-accent hover:underline">
          all {query.data.page.total} trades →
        </Link>
      )}
    </div>
  );
}
