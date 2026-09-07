import { StatTile } from "../StatTile";
import { Unavailable } from "../Unavailable";

/** P&L, equity, margin, drawdown and exposure. All unmeasured until L10 / L30. */
export function AccountStats() {
  return (
    <div className="flex flex-col gap-3">
      <div className="grid grid-cols-2 gap-2 lg:grid-cols-5">
        <StatTile label="P&L today" note="broker, L10" />
        <StatTile label="Equity" note="broker, L10" />
        <StatTile label="Margin" note="broker, L10" />
        <StatTile label="Drawdown" note="journal, L31" />
        <StatTile label="Exposure" note="portfolio, L30" />
      </div>
      <Unavailable
        level={10}
        reason="Account figures are read from the broker adapter. Nothing is shown rather than a placeholder number."
        today="python tools/mt5_account.py · python tools/track_record.py"
      />
    </div>
  );
}
