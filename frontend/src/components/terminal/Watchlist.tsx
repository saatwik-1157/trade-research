"use client";

import { useQuery } from "@tanstack/react-query";
import { DataTable, type Column, Unavailable } from "@/components/ui";
import { SYMBOLS } from "@/lib/market";
import { marketService, type Quote } from "@/lib/services";

interface Row {
  symbol: string;
  quote?: Quote;
}

const num = (v: number | undefined, dp = 5) =>
  v === undefined ? <span className="text-muted">—</span> : v.toFixed(dp);

/**
 * The watchlist shows the symbols the platform knows and no prices, because
 * no market data provider is connected. It is not labelled realtime, and it
 * will not be until a feed exists.
 */
export function Watchlist({
  selected,
  onSelect,
}: {
  selected: string;
  onSelect: (symbol: string) => void;
}) {
  const quotes = useQuery({ queryKey: ["quotes"], queryFn: marketService.quotes });
  const unavailable = quotes.data && !quotes.data.available ? quotes.data : null;

  const rows: Row[] = SYMBOLS.map((symbol) => ({ symbol }));

  const columns: Column<Row>[] = [
    {
      key: "symbol",
      header: "Symbol",
      render: (r) => (
        <button
          type="button"
          onClick={() => onSelect(r.symbol)}
          aria-pressed={r.symbol === selected}
          className={`font-mono ${r.symbol === selected ? "text-accent" : "text-ink-2 hover:text-ink"}`}
        >
          {r.symbol}
        </button>
      ),
    },
    { key: "bid", header: "Bid", align: "right", render: (r) => num(r.quote?.bid) },
    { key: "ask", header: "Ask", align: "right", render: (r) => num(r.quote?.ask) },
    { key: "last", header: "Last", align: "right", render: (r) => num(r.quote?.last) },
    {
      key: "change",
      header: "Chg%",
      align: "right",
      render: (r) => num(r.quote?.changePercent, 2),
    },
    { key: "spread", header: "Spread", align: "right", render: (r) => num(r.quote?.spread, 1) },
    {
      key: "status",
      header: "Market",
      align: "right",
      render: (r) =>
        r.quote?.marketOpen === undefined ? (
          <span className="text-muted">unknown</span>
        ) : (
          <span className={r.quote.marketOpen ? "text-good" : "text-muted"}>
            {r.quote.marketOpen ? "open" : "closed"}
          </span>
        ),
    },
  ];

  return (
    <div className="flex h-full flex-col gap-3">
      {/* The instruments are known; only the prices are not. The table
          always renders its rows, and every price cell is an em dash until a
          feed exists. */}
      <DataTable columns={columns} rows={rows} rowKey={(r) => r.symbol} caption="Watchlist" />
      {unavailable && (
        <Unavailable level={unavailable.level} reason={unavailable.reason} today={unavailable.today} />
      )}
    </div>
  );
}
