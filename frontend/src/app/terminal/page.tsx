"use client";

import { useState } from "react";
import { PageHeader, Panel, Select, StatTile, Unavailable } from "@/components/ui";
import { BotStatus } from "@/components/terminal/BotStatus";
import { OrderPanel } from "@/components/terminal/OrderPanel";
import { PriceChart } from "@/components/terminal/PriceChart";
import { OrdersTable, PositionsTable } from "@/components/terminal/Tables";
import { Watchlist } from "@/components/terminal/Watchlist";
import { useHealth } from "@/hooks/useHealth";
import {
  DEFAULT_SYMBOL,
  DEFAULT_TIMEFRAME,
  SYMBOLS,
  TIMEFRAMES,
  type Timeframe,
} from "@/lib/market";

export default function TerminalPage() {
  const [symbol, setSymbol] = useState<string>(DEFAULT_SYMBOL);
  const [timeframe, setTimeframe] = useState<Timeframe>(DEFAULT_TIMEFRAME);
  const health = useHealth();

  return (
    <div className="flex flex-col gap-4">
      <PageHeader
        title="Trading Terminal"
        level={3}
        description="Layout is final; data arrives level by level. Every panel says what feeds it."
      />

      <div className="flex flex-wrap items-end gap-3">
        <label className="text-mini uppercase tracking-wider text-muted">
          Symbol
          <Select
            aria-label="symbol"
            value={symbol}
            onChange={(e) => setSymbol(e.target.value)}
            className="mt-0.5 w-36"
          >
            {SYMBOLS.map((s) => (
              <option key={s} value={s}>
                {s}
              </option>
            ))}
          </Select>
        </label>
        <label className="text-mini uppercase tracking-wider text-muted">
          Timeframe
          <Select
            aria-label="timeframe"
            value={timeframe}
            onChange={(e) => setTimeframe(e.target.value as Timeframe)}
            className="mt-0.5 w-24"
          >
            {TIMEFRAMES.map((t) => (
              <option key={t} value={t}>
                {t}
              </option>
            ))}
          </Select>
        </label>
      </div>

      <div>
        <div className="grid grid-cols-2 gap-2 lg:grid-cols-6">
          <StatTile label="Balance" note="broker, L10" />
          <StatTile label="Equity" note="broker, L10" />
          <StatTile label="Used margin" note="broker, L10" />
          <StatTile label="Free margin" note="broker, L10" />
          <StatTile label="Unrealized P&L" note="broker, L10" />
          <StatTile label="Realized P&L" note="journal, L31" />
        </div>
        <div className="mt-3">
          <Unavailable
            level={10}
            reason="Account figures are read from the broker adapter. Nothing is shown rather than a placeholder number."
            today="python tools/mt5_account.py"
          />
        </div>
      </div>

      <div className="grid gap-4 xl:grid-cols-[260px_minmax(0,1fr)_300px]">
        <Panel title="Watchlist" level={8}>
          <Watchlist selected={symbol} onSelect={setSymbol} />
        </Panel>
        <Panel title="Chart" level={8} className="min-h-[380px]">
          <PriceChart symbol={symbol} timeframe={timeframe} />
        </Panel>
        <Panel title="Order panel" level={19}>
          <OrderPanel symbol={symbol} health={health.data} />
        </Panel>
      </div>

      <div className="grid gap-4 lg:grid-cols-2">
        <Panel title="Positions" level={10}>
          <PositionsTable />
        </Panel>
        <Panel title="Orders" level={19}>
          <OrdersTable />
        </Panel>
      </div>

      <Panel title="Bot status" level={22}>
        <BotStatus />
      </Panel>
    </div>
  );
}
