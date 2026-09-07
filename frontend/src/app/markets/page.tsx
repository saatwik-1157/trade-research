"use client";

import { PageHeader, Panel } from "@/components/ui";
import { Watchlist } from "@/components/terminal/Watchlist";
import { useState } from "react";
import { DEFAULT_SYMBOL } from "@/lib/market";

export default function MarketsPage() {
  const [symbol, setSymbol] = useState<string>(DEFAULT_SYMBOL);
  return (
    <div>
      <PageHeader
        title="Markets"
        level={8}
        description="Instruments the platform knows. Prices appear when a market data provider is connected."
      />
      <div className="grid gap-4 lg:grid-cols-2">
        <Panel title="Watchlist" level={8}>
          <Watchlist selected={symbol} onSelect={setSymbol} />
        </Panel>
        <Panel title="Symbol mapping" level={11}>
          <p className="text-body text-ink-2">
            TradingView names are not broker names. <code className="font-mono">XETR:DAX</code>{" "}
            resolves to <code className="font-mono">DE40</code>, and an unmapped symbol is refused
            rather than passed through. Contract specs are read from a live terminal and refused
            when incomplete.
          </p>
          <p className="mt-2 text-body text-muted">
            Measured on this broker: DE40 has contract size 1 and minimum volume 0.1, where the FX
            pairs have 100,000 and 0.01.
          </p>
        </Panel>
      </div>
    </div>
  );
}
