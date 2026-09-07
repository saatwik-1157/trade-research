import { PlannedPage } from "@/components/PlannedPage";

export default function Page() {
  return (
    <PlannedPage
      title="Market Replay"
      level={15}
      description="Drive a strategy and the OMS through the fake broker over recorded history, bar by bar."
      sections={[
        { title: "Replay session", level: 15, reason: "Same pipeline as paper trading, clocked by historical bars instead of wall time.", today: "tools/rule_backtest.simulate() (bar walk)" },
        { title: "Fake broker", level: 10, reason: "The test double already used by the order-construction tests, promoted to a runtime adapter.", today: "tests/test_rule_backtest.py FakeMT5" },
        { title: "History source", level: 8, reason: "MT5 history and Binance daily bars through one interface.", today: "tools/rule_backtest.fetch_rates; tools/crypto_market.py" },
        { title: "Replay vs simulator parity", level: 15, reason: "A replay of the random rule with a fixed seed must match simulate() trade count and P&L on the same bars." },
      ]}
    />
  );
}
